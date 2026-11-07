"""Synthetic album transfers and Kodi replies; no network, downloads or playback."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path, PurePosixPath
import tempfile
import types
import unittest
from unittest.mock import patch

import mac_album_consumer as album
import mac_kodi_consumer as core
from test_mac_kodi_consumer import fixture as movie_fixture, NOW


def fixture(request_id=None, *, artwork=True, cue=True):
    handoff, selection = movie_fixture(request_id=request_id)
    identity = handoff['source_identity']
    identity.update(provider='album_request', medium='music', scope='album_flac', title='Synthetic album')
    release = {'namespace': 'musicbrainz', 'artist_id': '33333333-3333-4333-8333-333333333333',
               'release_id': '44444444-4444-4444-8444-444444444444', 'artist': 'Synthetic artist',
               'album': 'Synthetic album', 'track_count': 2, 'source': 'Synthetic authorized catalogue'}
    handoff['authorization'].update(scope='album_flac', album_identity=deepcopy(release))
    handoff['album_identity'] = deepcopy(release)
    original = handoff['files'][0]
    files = []
    names = ['01.flac', '02.flac'] + (['folder.jpg'] if artwork else []) + (['disc.cue'] if cue else [])
    for name in names:
        item = deepcopy(original)
        payload = ('SYNTHETIC ' + name).encode()
        item.update(relative_path='CC-' + item['provenance']['request_id'] + '/' + name,
                    sha256=hashlib.sha256(payload).hexdigest(), size=len(payload),
                    selected_scope='album_flac', observed_media='audio' if name.endswith('.flac') else 'unknown')
        item['media_identity'] = 'sha256:' + item['sha256']
        files.append(item)
    handoff['files'] = files
    evidence = handoff['completion_evidence']
    evidence.update(source_identity=deepcopy(identity), album_identity=deepcopy(release),
                    files=[{k: item[k] for k in ('relative_path', 'sha256', 'size')} for item in files])
    handoff['completion_evidence_sha256'] = core.digest(evidence, newline=True)
    selection.update(source_identity=deepcopy(identity), album_identity=deepcopy(release),
                     completion_evidence_sha256=handoff['completion_evidence_sha256'])
    return handoff, selection


class FakeTransfer:
    def __init__(self, *, embedded=True):
        self.calls, self.embedded = [], embedded
        self.change_probe = lambda probe: None

    def __call__(self, relative, staging, **expected):
        self.calls.append(relative)
        name = PurePosixPath(relative).name
        payload = ('SYNTHETIC ' + name).encode()
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        result = {'local_path': str(target), 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}
        if name.endswith('.flac'):
            probe = {'validated_sha256': result['sha256'], 'streams': [{'codec_type': 'audio', 'codec_name': 'flac'}],
                     'format': {'duration': '123.5', 'tags': {'TITLE': 'Track ' + name[:2],
                        'ARTIST': 'Synthetic artist', 'ALBUM': 'Synthetic album', 'TRACKNUMBER': str(int(name[:2])) + '/2'}}}
            if self.embedded:
                probe['streams'].append({'codec_type': 'video', 'codec_name': 'mjpeg', 'disposition': {'attached_pic': 1}})
            self.change_probe(probe)
            result['media_probe'] = probe
        return result


class FakeKodi:
    def __init__(self, directory, *, artwork=True):
        self.calls, self.songs = [], []
        self.rows = [{'songid': i, 'file': str(directory / ('%02d.flac' % i)), 'title': 'Track %02d' % i,
                      'album': 'Synthetic album', 'artist': ['Synthetic artist'], 'albumartist': ['Synthetic artist'],
                      'track': i, 'disc': 1, 'duration': 124, 'albumid': 7, 'playcount': 8,
                      'lastplayed': '2026-01-01 01:02:03'} for i in (1, 2)]
        self.details = {'albumid': 7, 'title': 'Synthetic album', 'artist': ['Synthetic artist'], 'description': ''}
        if artwork:
            self.details['art'] = {'thumb': str(directory / 'folder.jpg')}
        self.effect = lambda: self.songs.extend(deepcopy(self.rows))

    def call(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if method == 'AudioLibrary.GetSongs':
            return {'songs': deepcopy(self.songs), 'limits': {'total': len(self.songs)}}
        if method == 'AudioLibrary.GetAlbumDetails':
            return {'albumdetails': deepcopy(self.details)}
        if method == 'Settings.GetSettingValue':
            return {'value': False}
        if method == 'AudioLibrary.Scan':
            self.effect()
            return 'OK'
        raise AssertionError('Unexpected Kodi action: ' + method)

    @property
    def scans(self):
        return [params for method, params in self.calls if method == 'AudioLibrary.Scan']


class AlbumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=album.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache, self.state = self.root / 'albums', self.root / 'state'
        self.handoff, self.selection = fixture()
        self.request = self.selection['provenance']['request_id']
        self.directory = self.cache / ('CC-' + self.request)
        self.transfer, self.art = FakeTransfer(), FakeTransfer()
        self.sidecar_calls = []
        self.kodi = FakeKodi(self.directory)
        helper = types.SimpleNamespace(date=datetime.fromisoformat, unique_object=lambda pairs: dict(pairs),
                                       Kodi=lambda: self.fail('Live Kodi forbidden'))
        sync = types.SimpleNamespace(validate_relative=PurePosixPath)
        patches = [patch.object(core, 'helpers', return_value=(helper, sync)),
                   patch.object(core, 'live_transfer', side_effect=AssertionError('Live transfer forbidden')),
                   patch.object(album, 'live_art', side_effect=AssertionError('Live artwork forbidden')),
                   patch.object(album, 'live_sidecar', side_effect=AssertionError('Live sidecar forbidden'))]
        for item in patches:
            item.start(); self.addCleanup(item.stop)

    def sidecar(self, relative, **expected):
        self.sidecar_calls.append(relative)
        return {'sha256': expected['expected_sha256'], 'bytes': expected['expected_bytes']}

    def consume(self, **kwargs):
        options = dict(cache=self.cache, state_dir=self.state, execute=True, transfer=self.transfer,
                       art_transfer=self.art, sidecar_verify=self.sidecar, kodi=self.kodi, timeout=0, now=NOW)
        options.update(kwargs)
        return album.consume(self.handoff, self.selection, **options)

    def saved(self):
        return core.read_json(self.state / (self.request + '.json'), private=True)

    def test_complete_album_preserves_tracks_cover_history_and_opaque_cue(self):
        result = self.consume()
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['files'], self.handoff['files'])
        self.assertEqual({p.name for p in self.directory.iterdir()}, {'01.flac', '02.flac', 'folder.jpg'})
        self.assertEqual(self.sidecar_calls, ['CC-' + self.request + '/disc.cue'])
        self.assertEqual(len(self.transfer.calls), 2)
        self.assertTrue(all(t['embedded_artwork'] for t in result['tracks']))
        self.assertTrue(result['library']['artwork_present'])
        self.assertEqual(result['library']['songs'][0]['playcount'], 8)
        self.assertEqual(result['library']['albums'][0]['description'], '')
        self.assertEqual(self.kodi.scans, [{'directory': str(self.directory), 'showdialogs': False}])
        self.assertEqual(album.validate_receipt(result, self.handoff, self.selection, cache=self.cache, now=NOW), result)
        self.assertFalse(result['playback_started'])

    def test_delayed_export_uses_consumer_observation_time_for_receipt(self):
        observed = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
        with patch.object(album, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = observed
            result = self.consume()
        self.assertEqual(result['status'], 'imported')
        self.assertLess(datetime.fromisoformat(self.handoff['checked_at']),
                        datetime.fromisoformat(result['created_at']))
        self.assertEqual(result['created_at'], observed.isoformat())
        self.assertEqual(result['checked_at'], observed.isoformat())
        self.assertEqual(album.validate_receipt(result, self.handoff, self.selection, cache=self.cache, now=NOW), result)
        stale = deepcopy(result)
        stale['checked_at'] = self.handoff['checked_at']
        with self.assertRaises(ValueError):
            album.validate_receipt(stale, self.handoff, self.selection, cache=self.cache, now=NOW)

    def test_imported_receipt_is_terminal_without_transfer_query_or_rescan(self):
        first = self.consume()
        self.kodi.songs = []
        count = len(self.kodi.calls)
        self.assertEqual(self.consume(), first)
        self.assertEqual(len(self.kodi.calls), count)
        self.assertEqual(len(self.transfer.calls), 2)

    def test_unknown_scan_and_interrupt_are_durable_and_never_rescan(self):
        self.kodi.effect = lambda: None
        self.assertEqual(self.consume()['status'], 'unknown')
        self.assertEqual(self.consume()['status'], 'unknown')
        self.kodi.songs = deepcopy(self.kodi.rows)
        self.assertEqual(self.consume()['status'], 'imported')
        self.assertEqual(len(self.kodi.scans), 1)
        self.assertEqual(len(self.transfer.calls), 2)

    def test_interrupt_reserves_scan_before_call(self):
        def interrupt():
            raise KeyboardInterrupt
        self.kodi.effect = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.consume()
        self.assertEqual(self.saved()['scan_attempts'], 1)
        self.assertEqual(self.saved()['status'], 'scanning')
        self.kodi.effect = lambda: None
        self.assertEqual(self.consume()['status'], 'unknown')
        self.assertEqual(len(self.kodi.scans), 1)

    def test_partial_or_conflicting_library_is_read_only_across_restart(self):
        self.kodi.songs = [deepcopy(self.kodi.rows[0])]
        self.assertEqual(self.consume()['status'], 'unknown')
        self.kodi.songs = []
        self.assertEqual(self.consume()['status'], 'unknown')
        self.assertEqual(self.kodi.scans, [])
        self.assertEqual(len(self.transfer.calls), 2)

    def test_unsafe_tree_preserved_before_scan_and_after_uncertainty(self):
        self.kodi.effect = lambda: None
        self.consume()
        track = self.directory / '01.flac'
        track.unlink()
        foreign = self.root / 'foreign'
        foreign.write_bytes(b'preserve me')
        track.symlink_to(foreign)
        self.assertEqual(self.consume()['status'], 'unknown')
        self.assertEqual(foreign.read_bytes(), b'preserve me')
        self.assertEqual(len(self.kodi.scans), 1)
        self.assertEqual(len(self.transfer.calls), 2)

    def test_full_hashed_identity_and_manifest_binding_reject_before_adapters(self):
        changes = [lambda h: h['album_identity'].update(album='wrong'),
                   lambda h: h['completion_evidence']['album_identity'].update(release_id='wrong'),
                   lambda h: h['authorization']['album_identity'].update(track_count=3),
                   lambda h: h['files'][0].update(relative_path='CC-' + self.request + '/../01.flac'),
                   lambda h: h['files'][1].update(sha256=h['files'][0]['sha256']),
                   lambda h: h['files'][-1].update(size=1024**2 + 1)]
        for change in changes:
            handoff = deepcopy(self.handoff); change(handoff)
            with self.subTest(change=change), self.assertRaises(ValueError):
                album.consume(handoff, self.selection, execute=True, now=NOW)
        self.assertEqual(self.transfer.calls, [])
        self.assertEqual(self.kodi.calls, [])

    def test_probe_hash_tags_codec_numbering_and_duration_reject_before_scan(self):
        changes = [lambda p: p.update(validated_sha256='f'*64),
                   lambda p: p['format']['tags'].update(ALBUM='wrong'),
                   lambda p: p['format']['tags'].update(ARTIST='wrong'),
                   lambda p: p['format']['tags'].update(TRACKNUMBER='1/2'),
                   lambda p: p['format'].update(duration='nan'),
                   lambda p: p['streams'][0].update(codec_name='mp3')]
        for change in changes:
            with self.subTest(change=change):
                self.transfer.change_probe = change
                self.assertEqual(self.consume()['status'], 'blocked')
        self.assertEqual(self.kodi.scans, [])

    def test_kodi_wrong_metadata_mbid_art_duration_or_album_cannot_import(self):
        variants = [('rows', 'duration', 4), ('rows', 'albumid', 8), ('rows', 'disc', 2),
                    ('rows', 'musicbrainzalbumid', 'wrong'), ('rows', 'albumartist', ['foreign']), ('details', 'art', {'thumb': 'https://foreign/cover.jpg'})]
        for target, key, value in variants:
            with self.subTest(key=key):
                kodi = FakeKodi(self.directory)
                if target == 'rows':
                    kodi.rows[0][key] = value
                else:
                    kodi.details[key] = value
                kodi.songs = deepcopy(kodi.rows)
                self.assertEqual(self.consume(kodi=kodi)['status'], 'unknown')
                self.assertEqual(kodi.scans, [])

    def test_embedded_image_url_proves_art_without_conventional_cover(self):
        self.handoff, self.selection = fixture(artwork=False, cue=False)
        self.kodi.details['art'] = {'thumb': 'image://music@' + str(self.directory / '01.flac').replace('/', '%2f') + '/'}
        self.assertEqual(self.consume()['status'], 'imported')
        self.assertEqual(self.art.calls, [])

    def test_missing_art_is_honest(self):
        self.handoff, self.selection = fixture(artwork=False, cue=False)
        self.transfer.embedded = False
        self.kodi.details.pop('art')
        result = self.consume()
        self.assertEqual(result['status'], 'imported')
        self.assertFalse(result['library']['artwork_present'])

    def test_expected_embedded_or_local_art_must_be_observed(self):
        self.kodi.details.pop('art')
        self.assertEqual(self.consume()['status'], 'unknown')
        self.assertNotIn('readiness', self.saved())
        self.assertEqual(len(self.kodi.scans), 1)

    def test_all_conventional_covers_are_preserved_but_unrelated_images_are_hash_only(self):
        for name in ('cover.png', 'liner.jpg'):
            item = deepcopy(self.handoff['files'][2])
            payload = ('SYNTHETIC ' + name).encode()
            item.update(relative_path='CC-' + self.request + '/' + name,
                        sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))
            item['media_identity'] = 'sha256:' + item['sha256']
            self.handoff['files'].append(item)
        self.handoff['completion_evidence']['files'] = [
            {k: item[k] for k in ('relative_path', 'sha256', 'size')} for item in self.handoff['files']]
        sha = core.digest(self.handoff['completion_evidence'], newline=True)
        self.handoff['completion_evidence_sha256'] = sha
        self.selection['completion_evidence_sha256'] = sha
        self.assertEqual(self.consume()['status'], 'imported')
        self.assertEqual({p.name for p in self.directory.iterdir()}, {'01.flac', '02.flac', 'folder.jpg', 'cover.png'})
        self.assertEqual({PurePosixPath(p).name for p in self.sidecar_calls}, {'liner.jpg', 'disc.cue'})

    def test_unexpected_extra_file_blocks_scan_and_is_preserved(self):
        self.directory.mkdir(parents=True)
        extra = self.directory / 'unselected.flac'
        extra.write_bytes(b'preserved unrelated file')
        self.assertEqual(self.consume()['status'], 'blocked')
        self.assertEqual(extra.read_bytes(), b'preserved unrelated file')
        self.assertEqual(self.kodi.scans, [])

    def test_no_execute_or_missing_synthetic_adapter_has_no_effect(self):
        self.assertEqual(self.consume(execute=False)['status'], 'blocked')
        self.assertFalse(self.cache.exists())
        for option in ('transfer', 'kodi', 'art_transfer', 'sidecar_verify'):
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.consume(**{option: None})

    def test_cue_mismatch_never_copies_cue_or_scans(self):
        self.assertEqual(self.consume(sidecar_verify=lambda *a, **k: {'sha256': 'f'*64, 'bytes': 1})['status'], 'blocked')
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.kodi.scans, [])

    def test_conflicting_receipt_or_same_release_other_request_cannot_rescan(self):
        receipt = self.consume()
        for change in (lambda r: r['tracks'][0].update(embedded_artwork='yes'),
                       lambda r: r.update(scan_key='other'),
                       lambda r: r['library']['songs'][0].update(duration=1)):
            modified = deepcopy(receipt); change(modified)
            with self.subTest(change=change), self.assertRaises(ValueError):
                album.validate_receipt(modified, self.handoff, self.selection, cache=self.cache, now=NOW)
        handoff, selection = fixture(request_id='99999999-1111-4111-8111-111111111111')
        with self.assertRaises(ValueError):
            album.consume(handoff, selection, cache=self.cache, state_dir=self.state, execute=True,
                          transfer=self.transfer, art_transfer=self.art, sidecar_verify=self.sidecar, kodi=self.kodi, now=NOW)
        self.assertEqual(len(self.kodi.scans), 1)


if __name__ == '__main__':
    unittest.main()
