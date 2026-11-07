"""Selected scope and release binding regressions; no provider or filesystem IO."""
from dataclasses import replace
from unittest import TestCase

from shelf.acquisition_contract import ConfirmedSelection, ContractError
from shelf.acquisition_pipeline import Snapshot, scope_family
from shelf.episode_scope import ScopeError, episode_code, make_scope, parse_scope
from shelf.obtain_parser import listing_reason


class EpisodeScopeTests(TestCase):
    def test_canonical_bounds_and_legacy_identity(self):
        for season, episode in ((1, 1), (2, 2), (99, 99), (999, 999), (0, 1)):
            scope = make_scope(season, episode)
            self.assertEqual(parse_scope(scope), (season, episode))
            self.assertEqual(scope_family('tmdb', 'tv', scope),
                             ('episode', 'S%02dE%02d' % (season, episode)))
        self.assertEqual(parse_scope('series_s01e01'), (1, 1))
        self.assertEqual(episode_code('series_s01e01'), 'S01E01')
        self.assertIsNone(parse_scope('film'))
        for scope in ('episode_s000e01', 'episode_s01e00', 'episode_s01e001',
                      'episode_s1e01', 'episode_s01e1000', 'series_s02e02'):
            with self.subTest(scope=scope), self.assertRaises(ScopeError):
                parse_scope(scope)
        for values in ((True, 1), (1, False), (1000, 1), (1, 1000)):
            with self.assertRaises(ScopeError):
                make_scope(*values)

    def test_exact_snapshot_selection_refuses_foreign_episode_and_series(self):
        snapshot = Snapshot(1, 1, '00000000-0000-0000-0000-000000000001',
                            'tmdb', 'tv', 42, 'Selected release', 'episode_s02e02',
                            'local_library', True, True)
        selection = ConfirmedSelection('tmdb:tv:42', 'episode', 'Selected release',
            'local_library', authorized_content=True, user_confirmed=True,
            storage_checked=True, duplicate_checked=True, episode='S02E02')
        snapshot.check_selection(selection)
        for wrong in (replace(selection, episode='S01E01'),
                      replace(selection, item_id='tmdb:tv:43')):
            with self.assertRaises(ContractError):
                snapshot.check_selection(wrong)
        with self.assertRaises(ContractError):
            replace(snapshot, medium='movie')

    def test_listing_requires_exact_selected_token_attributes_and_series(self):
        metadata = {'title': 'Fiction Show S02E02 1080p', 'categories': [5000],
                    'season': '02', 'episode': '2'}
        self.assertEqual(listing_reason(metadata, 'Fiction Show', 'episode_s02e02'), '')
        for title in ('Foreign Show S02E02', 'Fiction Show S01E01',
                      'Fiction Show S02E02E03', 'Fiction Show S02E02-03',
                      'Fiction Show S02E02 to 03', 'Fiction Show S02E02 pack',
                      'Fiction Show S02E02 S02E03', 'Fiction Show S02E02 + 03'):
            with self.subTest(title=title):
                self.assertTrue(listing_reason(dict(metadata, title=title),
                                               'Fiction Show', 'episode_s02e02'))
        for field in ('season', 'episode'):
            self.assertEqual(listing_reason(dict(metadata, **{field: '1'}),
                'Fiction Show', 'episode_s02e02'), 'ambiguous_episode')
        self.assertEqual(listing_reason(metadata, 'Fiction Show', 'series_s01e01'),
                         'ambiguous_episode')

    def test_completion_keeps_authoritative_scope_despite_misleading_filename(self):
        from shelf.completion_files import completion_receipt
        evidence = {'request_id': 'selected', 'result_id': 'result',
            'result_digest': 'a' * 64, 'nzb_sha256': 'b' * 64,
            'scope': 'episode_s02e02', 'root': '/complete/selected',
            'complete_root': '/complete', 'nzo_id': 'SAB-selected', 'test_only': True,
            'files': [{'path': 'S01E01.mkv', 'sha256': 'c' * 64, 'bytes': 123}]}
        handoff = completion_receipt(evidence, '2026-10-30T00:00:00Z')['handoff'][0]
        self.assertEqual(handoff['selected_scope'], 'episode_s02e02')
        self.assertEqual(handoff['observed_media'], 'video')
        self.assertEqual(handoff['observed_media_basis'], 'filename_extension_only')
        self.assertFalse(handoff['full_work_verified'])
