"""All media, transfers and Kodi responses are synthetic; no live adapter calls."""
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import mac_kodi_consumer as video

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
PAYLOAD = b'SYNTHETIC video test bytes, not playable media.'


def fixture(request_id=None, movie_id=12345, payload=PAYLOAD):
    request_id = request_id or '11111111-1111-4111-8111-111111111111'
    provenance = {'request_id': request_id, 'result_id': '22222222-2222-4222-8222-222222222222',
                  'result_digest': 'a' * 64, 'nzb_sha256': 'b' * 64}
    identity = {'provider': 'tmdb', 'medium': 'movie', 'source_id': movie_id, 'scope': 'film',
                'title': 'SYNTHETIC movie & exact identity', 'edition': 'SYNTHETIC selected release',
                'basis': 'selected_catalogue_identity_not_content_verification'}
    sha = hashlib.sha256(payload).hexdigest()
    item = {'media_identity': 'sha256:' + sha, 'relative_path': 'CC-' + request_id + '/movie.mkv',
        'sha256': sha, 'size': len(payload), 'nzo_id': 'SABnzbd_nzo_synthetic', 'provenance': provenance,
        'selected_scope': 'film', 'observed_media': 'video', 'observed_media_basis': 'filename_extension_only',
        'content_identity': 'unverified', 'full_work_verified': False,
        'verified_at': '2026-10-29T09:00:00.900000+00:00', 'acquired_at': '2026-10-29T09:00:00+00:00',
        'acquired_at_basis': 'sab_history_completed_at', 'test_only': True,
        'kodi_music_catalog_eligible': False, 'indexed': False, 'playback_started': False,
        'reverify_before_playback': True}
    evidence = {'source': 'sab_history', 'nzo_id': item['nzo_id'], 'acquired_at': item['acquired_at'],
        'acquired_at_basis': item['acquired_at_basis'], 'provenance': provenance,
        'files': [{key: item[key] for key in ('relative_path', 'sha256', 'size')}], 'source_identity': identity}
    handoff = {'version': 2, 'state': 'COMPLETED_FILES_VERIFIED', 'checked_at': '2026-10-29T09:00:01+00:00',
        'authorization': {'scope': 'film', 'edition': 'SYNTHETIC selected release', 'authorized_content': True},
        'sab_history_completed': True, 'progress': None, 'files': [item], 'source_identity': identity,
        'completion_evidence': evidence, 'completion_evidence_sha256': video.digest(evidence, newline=True)}
    selection = {'source_identity': deepcopy(identity), 'provenance': deepcopy(provenance),
                 'nzo_id': item['nzo_id'], 'completion_evidence_sha256': handoff['completion_evidence_sha256']}
    return handoff, selection


class FakeTransfer:
    def __init__(self, payload=PAYLOAD):
        self.calls = []
        self.payload = payload

    def __call__(self, relative, staging, **expected):
        self.calls.append((relative, str(staging), expected))
        path = staging / relative
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_bytes(self.payload)
        sha = hashlib.sha256(self.payload).hexdigest()
        return {'local_path': str(path), 'sha256': sha, 'bytes': len(self.payload),
                'media_probe': {'validated_sha256': sha, 'streams': [{'codec_type': 'video', 'codec_name': 'h264'}],
                                'format': {'duration': '2.5'}}}


class FakeKodi:
    def __init__(self, target, movie_id=12345):
        self.calls = []
        self.movies = []
        self.result = {'movieid': 7, 'file': str(target), 'title': 'SYNTHETIC movie',
                       'uniqueid': {'tmdb': str(movie_id)}, 'playcount': 4,
                       'lastplayed': '2026-01-02 03:04:05', 'resume': {'position': 42, 'total': 99}}
        self.scan_effect = lambda: self.movies.append(deepcopy(self.result))

    def call(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if method == 'VideoLibrary.GetMovies':
            return {'movies': deepcopy(self.movies), 'limits': {'total': len(self.movies)}}
        if method == 'VideoLibrary.Scan':
            self.scan_effect()
            return 'OK'
        raise AssertionError('Unexpected Kodi method: ' + method)

    @property
    def scans(self):
        return [params for method, params in self.calls if method == 'VideoLibrary.Scan']


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.test-', dir=video.ROOT)
        self.root = Path(self.temp.name)
        self.cache, self.state = self.root / 'cache', self.root / 'state'
        self.handoff, self.selection = fixture()
        self.item = self.handoff['files'][0]
        self.target = self.cache / self.item['relative_path']
        self.transfer = FakeTransfer()
        self.kodi = FakeKodi(self.target)

    def tearDown(self):
        self.temp.cleanup()

    def consume(self, **changes):
        args = dict(cache=self.cache, state_dir=self.state, execute=True, transfer=self.transfer,
                    kodi=self.kodi, timeout=0, now=NOW)
        args.update(changes)
        return video.consume(self.handoff, self.selection, **args)

    def receipt(self):
        return video.read_json(self.state / (self.item['provenance']['request_id'] + '.json'), private=True)

    def test_delayed_export_uses_consumer_observation_clock(self):
        result = self.consume()
        self.assertEqual(result['status'], 'imported')
        self.assertLess(video.timestamp(self.handoff['checked_at']), video.timestamp(result['created_at']))
        self.assertLessEqual(video.timestamp(result['created_at']), video.timestamp(result['checked_at']))
        self.assertEqual(result['file']['verified_at'], self.item['verified_at'])

    def test_complete_movie_import_and_restart_preserve_identity_acquisition_and_history(self):
        result = self.consume()
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['file']['acquired_at'], self.item['acquired_at'])
        self.assertEqual(result['library']['playcount'], 4)
        self.assertEqual(result['library']['resume']['position'], 42)
        self.assertEqual(result['readiness']['validated_sha256'], self.item['sha256'])
        self.assertEqual(result['readiness']['video_codec'], 'h264')
        self.assertLessEqual(result['readiness']['file_verified_at'], result['readiness']['library_observed_at'])
        self.assertEqual(self.target.read_bytes(), PAYLOAD)
        nfo = self.target.with_suffix('.nfo').read_text()
        self.assertIn('type="tmdb" default="true">12345', nfo)
        self.assertIn('movie &amp; exact identity', nfo)
        for tag in ('playcount', 'lastplayed', 'resume', 'dateadded'):
            self.assertNotIn(tag, nfo)
        self.handoff['checked_at'] = '2026-10-29T10:00:00+00:00'
        again = self.consume()
        self.assertEqual(again['status'], 'imported')
        self.assertEqual(again['signature'], result['signature'])
        self.assertEqual(again['hook_sha256'], result['hook_sha256'])
        self.assertEqual(again['completion_evidence_sha256'], result['completion_evidence_sha256'])
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.kodi.scans), 1)
        self.assertEqual(again['scan_attempts'], 1)
        directory = str(self.cache / ('CC-' + result['request_id']))
        self.assertEqual(self.kodi.scans, [{'directory': directory, 'showdialogs': False}])
        for method, params in self.kodi.calls:
            if method == 'VideoLibrary.GetMovies':
                self.assertEqual(params['filter']['value'], directory + '/')
        self.assertFalse(result['playback_started'])

    def test_no_execution_or_partial_injection_cannot_touch_live_adapters(self):
        with patch('mac_kodi_consumer.live_transfer', side_effect=AssertionError('no SSH')):
            result = self.consume(execute=False, transfer=None, kodi=None)
            self.assertEqual(result['reason'], 'execution_not_authorized')
            self.assertFalse(self.cache.exists())
            self.assertFalse(self.state.exists())
            with self.assertRaises(ValueError):
                self.consume(transfer=None)
            with self.assertRaises(ValueError):
                self.consume(kodi=None)

    def test_hash_provenance_or_identity_changes_fail_before_copy(self):
        changes = [lambda h: h['files'][0].update(sha256='c' * 64),
            lambda h: h['files'][0]['provenance'].update(nzb_sha256='d' * 64),
            lambda h: h['files'][0]['provenance'].update(result_digest='d' * 64),
            lambda h: h['files'][0].update(nzo_id='SABnzbd_nzo_other'),
            lambda h: h['source_identity'].update(source_id=1),
            lambda h: h.update(completion_evidence_sha256='e' * 64),
            lambda h: h['files'][0].update(relative_path='CC-other/movie.mkv')]
        for change in changes:
            h = deepcopy(self.handoff)
            change(h)
            with self.subTest(change=change), self.assertRaises(ValueError):
                video.consume(h, self.selection, cache=self.cache, state_dir=self.state,
                              transfer=self.transfer, kodi=self.kodi, execute=True, now=NOW)
        self.assertEqual(self.transfer.calls, [])
        self.assertEqual(self.kodi.calls, [])

    def test_hook_only_future_or_non_authoritative_acquisition_is_refused(self):
        for changes in ({'acquired_at': None}, {'acquired_at': '2027-01-01T00:00:00+00:00'},
                        {'acquired_at': '2026-10-29T09:00:00'}, {'acquired_at_basis': 'file_mtime'},
                        {'verified_at': '2026-10-29T09:00:02+00:00'}):
            handoff = deepcopy(self.handoff)
            handoff['files'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                video.validate(handoff, self.selection, NOW)
        self.handoff.update(version=1, state='POSTPROCESS_FILE_VERIFIED', sab_history_completed=False)
        with self.assertRaises(ValueError):
            self.consume()

    def test_tv_series_and_multiple_files_are_refused(self):
        self.selection['source_identity'].update(medium='tv', scope='series_s01e01')
        self.handoff['source_identity'] = deepcopy(self.selection['source_identity'])
        with self.assertRaisesRegex(ValueError, 'movie/film only'):
            self.consume()
        self.handoff, self.selection = fixture()
        self.handoff['files'].append(deepcopy(self.item))
        with self.assertRaisesRegex(ValueError, 'Exactly one'):
            self.consume()

    def test_changed_transfer_bytes_or_audio_only_probe_blocks_without_scan(self):
        self.transfer.payload = b'Unexpected changed bytes'
        result = self.consume()
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(self.target.exists())
        self.assertEqual(self.kodi.scans, [])
        self.transfer.payload = PAYLOAD
        def audio(relative, staging, **kwargs):
            result = self.transfer(relative, staging, **kwargs)
            result['media_probe']['streams'] = [{'codec_type': 'audio', 'codec_name': 'aac'}]
            return result
        result = self.consume(transfer=audio)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.kodi.scans, [])

    def test_scan_timeout_and_restart_reconcile_without_second_scan_or_copy(self):
        def response_lost():
            self.kodi.movies.append(deepcopy(self.kodi.result))
            raise RuntimeError('PRIVATE adapter details')
        self.kodi.scan_effect = response_lost
        result = self.consume()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.receipt()['scan_attempts'], 1)
        self.assertNotIn('PRIVATE', json.dumps(result))
        original_key = result['scan_key']
        again = self.consume()
        self.assertEqual(again['status'], 'imported')
        self.assertEqual(again['scan_key'], original_key)
        self.assertEqual(len(self.kodi.scans), 1)
        self.assertEqual(len(self.transfer.calls), 1)

    def test_crash_after_scan_checkpoint_never_retries_scan(self):
        self.kodi.scan_effect = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.consume()
        self.assertEqual(self.receipt()['status'], 'scanning')
        self.assertEqual(self.receipt()['scan_attempts'], 1)
        result = self.consume()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(len(self.kodi.scans), 1)
        self.kodi.movies = [deepcopy(self.kodi.result)]
        self.assertEqual(self.consume()['status'], 'imported')

    def test_unknown_empty_and_wrong_identity_cannot_rescan_or_claim_import(self):
        self.kodi.scan_effect = lambda: None
        result = self.consume()
        self.assertEqual(result['status'], 'unknown')
        for _ in range(3):
            self.assertEqual(self.consume()['status'], 'unknown')
        self.kodi.movies = [dict(self.kodi.result, uniqueid={'tmdb': '999'})]
        result = self.consume()
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['library'])
        self.assertEqual(len(self.kodi.scans), 1)

    def test_existing_exact_library_item_is_read_back_without_scan_or_history_edits(self):
        self.kodi.movies = [deepcopy(self.kodi.result)]
        result = self.consume()
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['scan_attempts'], 0)
        self.assertEqual(self.kodi.scans, [])
        self.assertEqual(result['library'], self.kodi.result)
        self.kodi.movies = []
        result = self.consume()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['library'], self.kodi.result)
        self.assertEqual(self.kodi.scans, [])
        self.assertEqual(len(self.transfer.calls), 1)
        self.target.unlink()
        self.assertEqual(self.consume()['status'], 'unknown')
        self.assertFalse(self.target.exists())
        self.assertEqual(len(self.transfer.calls), 1)

    def test_queue_and_scan_checkpoints_are_durable_before_external_calls(self):
        def transfer(relative, staging, **kwargs):
            self.assertEqual(self.receipt()['status'], 'queued')
            self.assertEqual(self.receipt()['scan_attempts'], 0)
            return self.transfer(relative, staging, **kwargs)
        def scan():
            saved = self.receipt()
            self.assertEqual(saved['status'], 'scanning')
            self.assertEqual(saved['scan_attempts'], 1)
            self.assertEqual(saved['file']['sha256'], self.item['sha256'])
            self.kodi.movies.append(deepcopy(self.kodi.result))
        self.kodi.scan_effect = scan
        self.assertEqual(self.consume(transfer=transfer)['status'], 'imported')

    def test_private_receipt_binding_change_is_refused_without_new_io(self):
        result = self.consume()
        path = self.state / (result['request_id'] + '.json')
        result['file']['sha256'] = '0' * 64
        video.save_receipt(path, result)
        calls = len(self.kodi.calls)
        with self.assertRaisesRegex(ValueError, 'Conflicting existing'):
            self.consume()
        self.assertEqual(len(self.kodi.calls), calls)
        self.assertEqual(len(self.transfer.calls), 1)

    def test_different_existing_video_or_nfo_is_preserved(self):
        self.target.parent.mkdir(parents=True, mode=0o700)
        self.target.write_bytes(b'Keep this unrelated user file')
        self.assertEqual(self.consume()['status'], 'blocked')
        self.assertEqual(self.target.read_bytes(), b'Keep this unrelated user file')
        self.target.write_bytes(PAYLOAD)
        nfo = self.target.with_suffix('.nfo')
        nfo.write_bytes(b'<movie>Existing corrections</movie>')
        self.assertEqual(self.consume()['status'], 'blocked')
        self.assertEqual(nfo.read_bytes(), b'<movie>Existing corrections</movie>')
        self.assertEqual(self.kodi.calls, [])

    def test_symlink_cache_and_handoff_and_receipt_are_refused(self):
        alternate = self.root / 'other'
        alternate.mkdir(mode=0o700)
        self.cache.symlink_to(alternate, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.consume()
        self.cache.unlink()
        document = self.root / 'handoff.json'
        document.write_text(json.dumps(self.handoff))
        alias = self.root / 'alias.json'
        alias.symlink_to(document)
        with self.assertRaises(OSError):
            video.read_json(alias)
        self.state.mkdir(mode=0o700, exist_ok=True)
        receipt = self.state / (self.item['provenance']['request_id'] + '.json')
        receipt.symlink_to(document)
        with self.assertRaises(OSError):
            self.consume()

    def test_duplicate_movie_or_bytes_across_requests_and_changed_binding_are_refused(self):
        original = self.consume()
        self.handoff, self.selection = fixture(str(uuid.uuid4()), payload=b'Other bytes')
        with self.assertRaisesRegex(ValueError, 'already belong'):
            self.consume()
        self.handoff, self.selection = fixture(str(uuid.uuid4()), movie_id=54321)
        with self.assertRaisesRegex(ValueError, 'already belong'):
            self.consume()
        self.handoff, self.selection = fixture()
        self.selection['source_identity']['title'] = 'Changed selected title'
        self.handoff['source_identity'] = deepcopy(self.selection['source_identity'])
        self.handoff['completion_evidence']['source_identity'] = deepcopy(self.selection['source_identity'])
        new_hash = video.digest(self.handoff['completion_evidence'], newline=True)
        self.handoff['completion_evidence_sha256'] = self.selection['completion_evidence_sha256'] = new_hash
        with self.assertRaisesRegex(ValueError, 'Conflicting existing'):
            self.consume()
        self.assertEqual(self.receipt(), original)

    def test_changed_local_media_after_import_keeps_history_and_cannot_recopy_or_rescan(self):
        original = self.consume()
        self.target.write_bytes(b'User changed bytes')
        result = self.consume()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['library'], original['library'])
        self.assertNotIn('readiness', result)
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.kodi.scans), 1)

    def test_private_receipts_locking_duplicate_json_keys_and_timeout_bounds(self):
        self.consume()
        path = self.state / (self.item['provenance']['request_id'] + '.json')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        with open(self.state / 'lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.consume()
        bad = self.root / 'bad.json'
        bad.write_text('{"version":2,"version":1}')
        with self.assertRaises(ValueError):
            video.read_json(bad)
        for timeout in (-1, 61, True):
            with self.assertRaises(ValueError):
                self.consume(timeout=timeout)

    def test_live_transfer_reuses_owner_helper_with_a_bounded_runner(self):
        calls = []
        def fetch(relative, staging, **kwargs):
            self.assertEqual(kwargs['expected_sha256'], self.item['sha256'])
            self.assertEqual(kwargs['max_bytes'], video.MAX_BYTES)
            kwargs['runner'](['synthetic-no-command'], timeout=3600)
            return {'synthetic': True}
        with patch.object(video.helpers()[1], 'fetch', side_effect=fetch):
            with patch('mac_kodi_consumer.subprocess.run', side_effect=lambda command, **kwargs: calls.append(kwargs)):
                video.live_transfer('synthetic.mkv', self.root, expected_sha256=self.item['sha256'],
                                    expected_bytes=self.item['size'])
        self.assertEqual(calls[0]['timeout'], 300)

    def test_original_selection_snapshot_cannot_change_during_transfer(self):
        def transfer(relative, staging, **kwargs):
            self.selection['source_identity']['source_id'] = 999
            return self.transfer(relative, staging, **kwargs)
        result = self.consume(transfer=transfer)
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['selection']['source_identity']['source_id'], 12345)
        self.assertIn('>12345</uniqueid>', self.target.with_suffix('.nfo').read_text())

    def test_receipt_capacity_stops_new_requests_but_existing_import_reconciles(self):
        self.consume()
        with patch('mac_kodi_consumer.MAX_RECEIPTS', 1):
            self.assertEqual(self.consume()['status'], 'imported')
            handoff, selection = fixture(str(uuid.uuid4()), movie_id=54321, payload=b'Different movie')
            with self.assertRaisesRegex(ValueError, 'capacity'):
                video.consume(handoff, selection, cache=self.cache, state_dir=self.state,
                              execute=True, transfer=self.transfer, kodi=self.kodi, timeout=0, now=NOW)
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.kodi.scans), 1)

    def test_unselected_media_sidecar_subtree_and_symlink_are_preserved_without_scan(self):
        for name, kind in (('unselected.mkv', 'file'), ('unselected.nfo', 'file'),
                           ('subtitles.srt', 'file'), ('unselected-tree', 'directory'),
                           ('linked-movie.mkv', 'symlink')):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.root) as case:
                cache, state = Path(case) / 'cache', Path(case) / 'state'
                target = cache / self.item['relative_path']
                target.parent.mkdir(parents=True, mode=0o700)
                extra = target.parent / name
                kept = b'Unselected content must remain untouched'
                if kind == 'directory':
                    extra.mkdir(mode=0o700)
                    (extra / 'nested.mkv').write_bytes(kept)
                elif kind == 'symlink':
                    outside = Path(case) / 'outside.mkv'
                    outside.write_bytes(kept)
                    extra.symlink_to(outside)
                else:
                    extra.write_bytes(kept)
                kodi = FakeKodi(target)
                result = video.consume(self.handoff, self.selection, cache=cache, state_dir=state,
                    execute=True, transfer=FakeTransfer(), kodi=kodi, timeout=0, now=NOW)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['scan_attempts'], 0)
                self.assertEqual(kodi.calls, [])
                if kind == 'directory':
                    self.assertEqual((extra / 'nested.mkv').read_bytes(), kept)
                elif kind == 'symlink':
                    self.assertTrue(extra.is_symlink())
                    self.assertEqual(outside.read_bytes(), kept)
                else:
                    self.assertEqual(extra.read_bytes(), kept)

    def test_extra_added_during_readback_is_refused_before_scan_and_stays_unknown(self):
        original = self.kodi.call
        extra = self.target.parent / 'late-unselected.mkv'
        def inject(method, params):
            if method == 'VideoLibrary.GetMovies':
                extra.write_bytes(b'Keep the late extra')
            return original(method, params)
        self.kodi.call = inject
        result = self.consume()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['scan_attempts'], 1)  # Reservation preceded the final inventory.
        self.assertEqual(self.kodi.scans, [])
        self.assertEqual(extra.read_bytes(), b'Keep the late extra')
        again = self.consume()
        self.assertEqual(again['status'], 'unknown')
        self.assertEqual(again['scan_key'], result['scan_key'])
        self.assertEqual(again['scan_attempts'], 1)
        self.assertEqual(self.kodi.scans, [])
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(extra.read_bytes(), b'Keep the late extra')

    def test_selected_file_changed_during_readback_is_not_scanned_or_replaced(self):
        original = self.kodi.call
        def change(method, params):
            if method == 'VideoLibrary.GetMovies':
                self.target.write_bytes(b'Preserve this later edit')
            return original(method, params)
        self.kodi.call = change
        result = self.consume()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.kodi.scans, [])
        self.assertEqual(self.target.read_bytes(), b'Preserve this later edit')

    def nested_fixture(self, parent='nested'):
        self.item['relative_path'] = 'CC-' + self.item['provenance']['request_id'] + '/' + parent + '/movie.mkv'
        self.handoff['completion_evidence']['files'][0]['relative_path'] = self.item['relative_path']
        checksum = video.digest(self.handoff['completion_evidence'], newline=True)
        self.handoff['completion_evidence_sha256'] = self.selection['completion_evidence_sha256'] = checksum
        self.target = self.cache / self.item['relative_path']
        self.kodi = FakeKodi(self.target)

    def test_only_required_nested_parent_directories_are_allowed(self):
        self.nested_fixture('nested/part')
        self.assertEqual(self.consume()['status'], 'imported')
        self.assertEqual(len(self.kodi.scans), 1)
        request = self.cache / ('CC-' + self.item['provenance']['request_id'])
        video.verify_import_tree(request, self.target, self.item, self.selection)

    def test_expected_parent_and_selected_nfo_symlinks_are_refused(self):
        self.nested_fixture()
        request = self.cache / ('CC-' + self.item['provenance']['request_id'])
        request.mkdir(parents=True, mode=0o700)
        outside = self.root / 'outside'
        outside.mkdir(mode=0o700)
        (outside / 'movie.mkv').write_bytes(PAYLOAD)
        (request / 'nested').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.consume()['status'], 'blocked')
        self.assertTrue((request / 'nested').is_symlink())
        self.assertEqual((outside / 'movie.mkv').read_bytes(), PAYLOAD)
        self.assertEqual(self.kodi.scans, [])
        with self.assertRaises(OSError):
            video.verify_import_tree(request, self.target, self.item, self.selection)
        # A matching-name NFO symlink is also refused without replacing it.
        (request / 'nested').unlink()
        self.target.parent.mkdir(mode=0o700)
        self.target.write_bytes(PAYLOAD)
        external_nfo = outside / 'movie.nfo'
        external_nfo.write_bytes(video.nfo_bytes(self.selection))
        self.target.with_suffix('.nfo').symlink_to(external_nfo)
        self.assertEqual(self.consume()['status'], 'blocked')
        self.assertTrue(self.target.with_suffix('.nfo').is_symlink())
        self.assertEqual(external_nfo.read_bytes(), video.nfo_bytes(self.selection))
        self.assertEqual(self.kodi.scans, [])

    def test_import_tree_depth_is_bounded_before_copy(self):
        self.nested_fixture('/'.join(['nested'] * 16))
        with self.assertRaises(ValueError):
            self.consume()
        self.assertEqual(self.transfer.calls, [])
        self.assertEqual(self.kodi.calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
