"""Synthetic S01E01 imports with injected transfers/Kodi; no real media or network."""
from copy import deepcopy
from datetime import datetime
import hashlib
import shutil
from pathlib import Path, PurePosixPath
import tempfile
import types
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import mac_episode_consumer as episode
import mac_kodi_consumer as video
from test_mac_kodi_consumer import fixture as movie_fixture, FakeTransfer, NOW, PAYLOAD


def fixture(scope='series_s01e01', **kwargs):
    handoff, selection = movie_fixture(**kwargs)
    identity = handoff['source_identity']
    identity.update(medium='tv', scope=scope, title='SYNTHETIC show & <identity>')
    handoff['authorization']['scope'] = scope
    item = handoff['files'][0]
    item['selected_scope'] = scope
    item['relative_path'] = 'CC-' + item['provenance']['request_id'] + '/obfuscated/n3ver-a-guessed-title.MKV'
    handoff['completion_evidence']['source_identity'] = deepcopy(identity)
    handoff['completion_evidence']['files'] = [{k: item[k] for k in ('relative_path', 'sha256', 'size')}]
    handoff['completion_evidence_sha256'] = video.digest(handoff['completion_evidence'], newline=True)
    selection.update(source_identity=deepcopy(identity), completion_evidence_sha256=handoff['completion_evidence_sha256'])
    return handoff, selection


class FakeKodi:
    def __init__(self, target, selection):
        self.calls, self.episodes = [], []
        season, number = episode.parse_scope(selection['source_identity']['scope'])
        code = episode.episode_code(selection['source_identity']['scope'])
        self.row = {'episodeid': 9, 'tvshowid': 7, 'file': str(target), 'season': season, 'episode': number,
                    'title': code, 'uniqueid': {'cabinet': episode.episode_id(selection)},
                    'playcount': 4, 'lastplayed': '2026-01-02 03:04:05', 'resume': {'position': 42, 'total': 99}}
        self.show = {'tvshowid': 7, 'uniqueid': {'tmdb': str(selection['source_identity']['source_id'])}}
        self.effect = lambda: self.episodes.append(deepcopy(self.row))

    def call(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if method == 'VideoLibrary.GetEpisodes':
            return {'episodes': deepcopy(self.episodes), 'limits': {'total': len(self.episodes)}}
        if method == 'VideoLibrary.GetTVShowDetails':
            return {'tvshowdetails': deepcopy(self.show)}
        if method == 'VideoLibrary.Scan':
            self.effect()
            return 'OK'
        raise AssertionError('Unexpected Kodi call: ' + method)

    @property
    def scans(self):
        return [params for method, params in self.calls if method == 'VideoLibrary.Scan']


class EpisodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=episode.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache, self.state = self.root / 'TV Shows', self.root / 'state'
        self.handoff, self.selection = fixture()
        self.item = self.handoff['files'][0]
        self.directory = self.cache / ('CC-' + self.item['provenance']['request_id'])
        self.target = self.directory / 'S01E01.mkv'
        self.transfer = FakeTransfer()
        self.kodi = FakeKodi(self.target, self.selection)
        # Only date/JSON/path helpers are needed. Never load an owner's Kodi
        # credentials or launch even an accidental real adapter in these tests.
        helper = types.SimpleNamespace(date=datetime.fromisoformat, unique_object=lambda pairs: dict(pairs),
                                       Kodi=lambda: self.fail('No live Kodi'))
        sync = types.SimpleNamespace(validate_relative=PurePosixPath)
        for target in (patch.object(video, 'helpers', return_value=(helper, sync)),
                       patch.object(episode, 'live_transfer', side_effect=AssertionError('No live transfer')),
                       patch.object(episode, 'helpers', return_value=(helper, sync))):
            target.start(); self.addCleanup(target.stop)

    def consume(self, **kwargs):
        args = dict(cache=self.cache, state_dir=self.state, execute=True,
                    transfer=self.transfer, kodi=self.kodi, timeout=0, now=NOW)
        args.update(kwargs)
        return episode.consume(self.handoff, self.selection, **args)

    def saved(self):
        return video.read_json(self.state / (self.item['provenance']['request_id'] + '.json'), private=True)

    def select_episode(self, scope='episode_s02e02', **kwargs):
        self.handoff, self.selection = fixture(scope, **kwargs)
        self.item = self.handoff['files'][0]
        self.directory = self.cache / ('CC-' + self.item['provenance']['request_id'])
        self.target = self.directory / (episode.episode_code(scope) + '.mkv')
        self.kodi = FakeKodi(self.target, self.selection)

    def test_shared_scope_wrapper_handles_specials_and_three_digit_numbers(self):
        for scope, code, selected in (('episode_s00e01', 'S00E01', (0, 1)),
                                       ('episode_s999e999', 'S999E999', (999, 999))):
            handoff, selection = fixture(scope)
            self.assertEqual(episode.parse_scope(scope), selected)
            self.assertEqual(episode.episode_code(scope), code)
            episode.validate(handoff, selection, NOW)
            self.assertIn(code + '.nfo', episode.nfo_files(selection))
        for scope in ('episode_s000e01', 'episode_s01e001', 'episode_s1000e01', 'episode_s01e00'):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                episode.parse_scope(scope)

    def test_explicit_s02e02_import_readback_restart_and_receipt(self):
        self.select_episode()
        receipt = self.consume()
        self.assertEqual(receipt['status'], 'imported')
        self.assertEqual(receipt['library']['uniqueid']['cabinet'], 'tmdb-show-12345:s02e02')
        self.assertEqual((receipt['library']['season'], receipt['library']['episode']), (2, 2))
        self.assertEqual(set(p.name for p in self.directory.iterdir()), {'tvshow.nfo', 'S02E02.nfo', 'S02E02.mkv'})
        nfo = ET.fromstring((self.directory / 'S02E02.nfo').read_bytes())
        self.assertEqual((nfo.findtext('season'), nfo.findtext('episode')), ('2', '2'))
        self.assertEqual(self.consume(), receipt)
        self.assertEqual(len(self.kodi.scans), 1)
        self.assertEqual(episode.validate_receipt(receipt, self.handoff, self.selection, cache=self.cache, now=NOW), receipt)
        for field, value in (('season', 1), ('episode', 1), ('show_uniqueid', {'tmdb': '99'})):
            wrong = deepcopy(receipt)
            wrong['library'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                episode.validate_receipt(wrong, self.handoff, self.selection, cache=self.cache, now=NOW)

    def test_explicit_scope_tamper_and_wrong_readback_never_import(self):
        self.select_episode()
        for field in ('authorization', 'source_identity'):
            bad = deepcopy(self.handoff)
            bad[field]['scope'] = 'series_s01e01'
            with self.subTest(field=field), self.assertRaises(ValueError):
                episode.validate(bad, self.selection, NOW)
        self.kodi.row['episode'] = 1
        self.assertEqual(self.consume()['status'], 'unknown')
        self.assertEqual(len(self.kodi.scans), 1)
        self.assertEqual(self.consume()['status'], 'unknown')
        self.assertEqual(len(self.kodi.scans), 1)

    def test_distinct_selected_episode_of_same_show_is_admitted_but_equivalent_scope_is_duplicate(self):
        self.consume()
        self.select_episode(request_id='33333333-3333-4333-8333-333333333333', payload=b'Other synthetic episode')
        self.transfer = FakeTransfer(b'Other synthetic episode')
        self.assertEqual(self.consume()['status'], 'imported')
        self.select_episode('episode_s01e01', request_id='44444444-4444-4444-8444-444444444444', payload=b'Changed synthetic pilot')
        self.transfer = FakeTransfer(b'Changed synthetic pilot')
        with self.assertRaises(ValueError):
            self.consume()
        self.assertEqual(self.transfer.calls, [])

    def test_obfuscated_video_uses_canonical_pilot_path_and_show_episode_identity(self):
        receipt = self.consume()
        self.assertEqual(receipt['status'], 'imported')
        self.assertEqual(self.target.read_bytes(), PAYLOAD)
        self.assertEqual(set(p.name for p in self.directory.iterdir()), {'S01E01.mkv', 'S01E01.nfo', 'tvshow.nfo'})
        self.assertEqual(self.transfer.calls[0][0], self.item['relative_path'])
        self.assertEqual(receipt['file']['relative_path'], self.item['relative_path'])
        self.assertEqual(receipt['local_path'], str(self.target))
        self.assertEqual(receipt['library']['show_uniqueid'], {'tmdb': '12345'})
        self.assertEqual(receipt['library']['uniqueid'], {'cabinet': 'tmdb-show-12345:s01e01'})
        self.assertEqual(receipt['scan_key'], 'episode-' + receipt['request_id'])
        self.assertEqual(receipt['readiness']['validated_sha256'], self.item['sha256'])
        self.assertEqual(episode.validate_receipt(receipt, self.handoff, self.selection, cache=self.cache, now=NOW), receipt)
        self.assertEqual(self.kodi.scans, [{'directory': str(self.directory), 'showdialogs': False}])
        self.assertFalse(receipt['playback_started'])

    def test_nfos_escape_show_title_and_never_invent_episode_scraper_id_or_history(self):
        self.consume()
        show = ET.fromstring((self.directory / 'tvshow.nfo').read_bytes())
        ep = ET.fromstring((self.directory / 'S01E01.nfo').read_bytes())
        self.assertEqual(show.findtext('title'), 'SYNTHETIC show & <identity>')
        self.assertEqual(show.find('uniqueid').attrib, {'type': 'tmdb', 'default': 'true'})
        self.assertEqual(ep.findtext('title'), 'S01E01')
        self.assertEqual(ep.find('uniqueid').attrib, {'type': 'cabinet', 'default': 'true'})
        self.assertIn('Actual episode title unknown', ep.findtext('plot'))
        for root in (ep, show):
            for field in ('playcount', 'lastplayed', 'resume', 'dateadded', 'aired', 'episodeguide'):
                self.assertIsNone(root.find(field))
        self.assertFalse(any(node.attrib.get('type') == 'tmdb' for node in ep.findall('uniqueid')))

    def test_restart_readback_preserves_history_and_never_recopies_or_rescans(self):
        first = self.consume()
        self.handoff['checked_at'] = '2026-10-30T10:00:00+00:00'
        second = self.consume()
        self.assertEqual((first['signature'], first['hook_sha256']), (second['signature'], second['hook_sha256']))
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.kodi.scans), 1)
        self.assertEqual(second['library']['playcount'], 4)
        self.assertEqual(second['library']['resume'], {'position': 42, 'total': 99})
        self.assertEqual({m for m, _ in self.kodi.calls},
                         {'VideoLibrary.GetEpisodes', 'VideoLibrary.GetTVShowDetails', 'VideoLibrary.Scan'})

    def test_imported_receipt_is_terminal_without_later_file_or_kodi_operations(self):
        original = self.consume()
        shutil.rmtree(self.cache)
        calls = deepcopy(self.kodi.calls)
        self.kodi.call = lambda *args: self.fail('Terminal import must not query Kodi')
        self.assertEqual(self.consume(), original)
        self.assertEqual(self.kodi.calls, calls)
        self.assertFalse(self.cache.exists())
        self.assertEqual(len(self.transfer.calls), 1)

    def test_delayed_consumer_dates_receipt_after_producer_export_and_keeps_terminal_time(self):
        producer_checked = self.handoff['checked_at']
        observed = datetime.fromisoformat('2026-10-30T12:00:00+00:00')
        with patch.object(episode, 'datetime', types.SimpleNamespace(now=lambda tz: observed)):
            receipt = self.consume()
        self.assertEqual(receipt['status'], 'imported')
        created = datetime.fromisoformat(receipt['created_at'])
        checked = datetime.fromisoformat(receipt['checked_at'])
        self.assertLess(datetime.fromisoformat(producer_checked), created)
        self.assertLessEqual(created, checked)
        self.assertEqual(checked, observed)
        self.assertLessEqual(datetime.fromisoformat(receipt['readiness']['library_observed_at']), checked)
        self.assertEqual(self.handoff['checked_at'], producer_checked)
        self.handoff['checked_at'] = '2026-10-31T12:00:00+00:00'
        self.assertEqual(self.consume(), receipt)


    def test_terminal_receipt_still_requires_its_original_selection_and_probe(self):
        self.consume()
        receipt = self.saved()
        receipt['probe']['validated_sha256'] = 'd' * 64
        video.save_receipt(self.state / (receipt['request_id'] + '.json'), receipt)
        with self.assertRaises(ValueError):
            self.consume()
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.kodi.scans), 1)

    def test_malformed_identity_authorization_and_library_are_explicitly_rejected(self):
        for key in ('authorization', 'source_identity'):
            handoff = deepcopy(self.handoff)
            handoff[key] = []
            with self.subTest(key=key), self.assertRaises(ValueError):
                episode.validate(handoff, self.selection, NOW)
        self.handoff['authorization']['edition'] = 'other release'
        with self.assertRaises(ValueError):
            self.consume()
        self.assertEqual(self.transfer.calls, [])


    def test_only_s01e01_scope_and_one_video_are_accepted(self):
        for scope in ('film', 'series_s02e01', 'series_s01', 'series_full'):
            handoff, selection = deepcopy(self.handoff), deepcopy(self.selection)
            selection['source_identity']['scope'] = scope
            handoff['source_identity'] = deepcopy(selection['source_identity'])
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                episode.validate(handoff, selection, NOW)
        handoff = deepcopy(self.handoff)
        handoff['files'].append(deepcopy(handoff['files'][0]))
        with self.assertRaises(ValueError):
            episode.validate(handoff, self.selection, NOW)
        with self.assertRaises(ValueError):
            episode.validate(*movie_fixture(), NOW)
        self.assertEqual(self.transfer.calls, [])

    def test_selection_provenance_evidence_or_bytes_mismatch_rejected_before_transfer(self):
        changes = [lambda h: h['files'][0].update(sha256='c'*64),
                   lambda h: h['files'][0]['provenance'].update(result_digest='d'*64),
                   lambda h: h.update(completion_evidence_sha256='e'*64),
                   lambda h: h['source_identity'].update(source_id=2),
                   lambda h: h['files'][0].update(acquired_at_basis='file_mtime')]
        for change in changes:
            handoff = deepcopy(self.handoff); change(handoff)
            with self.subTest(change=change), self.assertRaises(ValueError):
                episode.consume(handoff, self.selection, cache=self.cache, state_dir=self.state,
                                transfer=self.transfer, kodi=self.kodi, execute=True, now=NOW)
        self.assertEqual(self.transfer.calls, [])
        self.assertEqual(self.kodi.calls, [])

    def test_live_adapters_are_refused_for_synthetic_or_unapproved_execution(self):
        self.assertEqual(self.consume(execute=False)['status'], 'blocked')
        self.assertFalse(self.cache.exists())
        with self.assertRaises(ValueError):
            self.consume(transfer=None)
        with self.assertRaises(ValueError):
            self.consume(kodi=None)

    def test_wrong_show_or_episode_readback_blocks_without_overwriting_metadata(self):
        variants = [('show', {'uniqueid': {'tmdb': '999'}}), ('row', {'season': 2}),
                    ('row', {'episode': 2}), ('row', {'uniqueid': {'tmdb': '12345'}}),
                    ('row', {'file': str(self.target) + '-other'}), ('row', {'tvshowid': True})]
        for field, changes in variants:
            with self.subTest(changes=changes):
                kodi = FakeKodi(self.target, self.selection)
                getattr(kodi, field).update(changes)
                kodi.episodes = [deepcopy(kodi.row)]
                result = self.consume(kodi=kodi)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(kodi.scans, [])
                self.assertNotIn('readiness', result)

    def test_multiple_episodes_in_directory_are_ambiguous(self):
        self.kodi.episodes = [deepcopy(self.kodi.row)] * 2
        self.assertEqual(self.consume()['status'], 'blocked')
        self.assertEqual(self.kodi.scans, [])

    def test_unknown_scan_is_reconciled_read_only_even_after_restart(self):
        self.kodi.effect = lambda: None
        first = self.consume()
        self.assertEqual(first['status'], 'unknown')
        self.assertEqual(first['scan_attempts'], 1)
        self.assertNotIn('readiness', first)
        self.assertEqual(self.consume()['status'], 'unknown')
        self.kodi.episodes = [deepcopy(self.kodi.row)]
        self.assertEqual(self.consume()['status'], 'imported')
        self.assertEqual(len(self.kodi.scans), 1)
        self.assertEqual(len(self.transfer.calls), 1)

    def test_interrupt_during_scan_keeps_marker_and_prevents_second_scan(self):
        def interrupt():
            raise KeyboardInterrupt
        self.kodi.effect = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.consume()
        self.assertEqual(self.saved()['status'], 'scanning')
        self.assertEqual(self.saved()['scan_attempts'], 1)
        self.kodi.effect = lambda: None
        self.assertEqual(self.consume()['status'], 'unknown')
        self.assertEqual(len(self.kodi.scans), 1)

    def test_changed_or_missing_video_after_unknown_does_not_repair_or_rescan(self):
        self.kodi.effect = lambda: None
        self.consume()
        self.target.unlink()
        result = self.consume()
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(self.target.exists())
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.kodi.scans), 1)

    def test_unexpected_tree_entry_blocks_before_first_scan_and_is_preserved(self):
        original = self.transfer
        def extra_file(*args, **kwargs):
            result = original(*args, **kwargs)
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / 'S01E02.mkv').write_bytes(b'UNRELATED')
            return result
        result = self.consume(transfer=extra_file)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual((self.directory / 'S01E02.mkv').read_bytes(), b'UNRELATED')
        self.assertEqual(self.kodi.scans, [])

    def test_symlink_or_changed_nfo_blocks_and_never_overwrites(self):
        self.directory.mkdir(parents=True)
        foreign = self.root / 'foreign'
        foreign.write_bytes(b'UNRELATED')
        nfo = self.directory / 'tvshow.nfo'
        nfo.symlink_to(foreign)
        self.assertEqual(self.consume()['status'], 'blocked')
        self.assertEqual(foreign.read_bytes(), b'UNRELATED')
        self.assertTrue(nfo.is_symlink())
        self.assertEqual(self.kodi.scans, [])

    def test_new_request_for_same_show_cannot_duplicate_pilot(self):
        self.consume()
        handoff, selection = fixture(request_id='99999999-1111-4111-8111-111111111111')
        with self.assertRaises(ValueError):
            episode.consume(handoff, selection, cache=self.cache, state_dir=self.state, execute=True,
                            transfer=self.transfer, kodi=self.kodi, now=NOW)
        self.assertEqual(len(self.transfer.calls), 1)

    def test_receipt_validator_rejects_wrong_show_episode_path_and_scan_key(self):
        original = self.consume()
        changes = [lambda r: r.update(scan_key='video-' + r['request_id']),
                   lambda r: r['library']['show_uniqueid'].update(tmdb='9'),
                   lambda r: r['library'].update(episode=2),
                   lambda r: r.update(local_path='/foreign/S01E01.mkv'),
                   lambda r: r['library']['uniqueid'].update(cabinet='tmdb-show-12345:s02e01')]
        for change in changes:
            receipt = deepcopy(original); change(receipt)
            with self.subTest(change=change), self.assertRaises(ValueError):
                episode.validate_receipt(receipt, self.handoff, self.selection, cache=self.cache, now=NOW)


if __name__ == '__main__':
    unittest.main()
