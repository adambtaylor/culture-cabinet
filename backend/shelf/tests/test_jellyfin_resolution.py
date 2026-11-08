"""Closed library/series/episode resolution through the real injected transport."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from shelf import jellyfin_native as jf
from shelf.obtain_tv_flow import tv_content_identity_digest
from .test_jellyfin_native import Clock, Connection, Response


class EpisodeResolutionTests(TestCase):
    def setUp(self):
        self.clock = Clock()
        self.binding = jf.ServiceBinding(jf.ORIGIN, '1' * 32, 'fixture-version', '2' * 32,
            '3' * 32, '4' * 32, 11, 22, '5' * 32, '6' * 32, jf.TV_ROOTS[0],
            'selected_tv_read', self.clock.now() + timedelta(seconds=60))
        self.target = jf.EpisodeResolutionTarget('fixture-request', 'a' * 64,
            tv_content_identity_digest(tmdb_show_id=1399, season=1, episode=2), 'b' * 64,
            1399, 'episode_s01e02', 'Fictional Fixture Show', None,
            self.clock.now() + timedelta(seconds=30))
        self.series = {'Id': '7' * 32, 'ServerId': '1' * 32, 'Type': 'Series',
            'Name': self.target.show_name, 'ProviderIds': {'Tmdb': '1399'},
            'Path': jf.TV_ROOTS[0] + '/FixtureShow'}
        self.episode = {'Id': '8' * 32, 'ServerId': '1' * 32, 'Type': 'Episode',
            'SeriesId': '7' * 32, 'ParentId': '9' * 32, 'IndexNumber': 2,
            'ParentIndexNumber': 1, 'Path': jf.TV_ROOTS[0] + '/FixtureShow/S01E02.mkv',
            'MediaSources': [{'Id': 'c' * 32, 'Path': jf.TV_ROOTS[0] + '/FixtureShow/S01E02.mkv',
                              'Size': 4096, 'Protocol': 'File', 'IsRemote': False}],
            'UserData': {'Played': True}, 'Overview': 'PRIVATE_UNNEEDED_TEXT'}

    @contextmanager
    def credential(self, binding):
        yield jf.ScopedCredential(jf._digest(binding.material()), 'SYNTHETIC_NOT_A_KEY')

    def run_resolve(self, values=None, **overrides):
        if values is None:
            values = [{'Items': [self.series]}, {'Items': [self.episode]}, {'Items': [self.episode]}]
        self.connections = [Connection(Response(json.dumps(value).encode())) for value in values]
        args = dict(credential_scope=self.credential, revalidate=lambda b, t: (b, t), clock=self.clock)
        args.update(overrides)
        with patch.object(jf, '_Connection', side_effect=self.connections) as factory:
            result = jf.resolve_episode(self.binding, self.target, **args)
            self.factory_count = factory.call_count
        return result

    def test_bound_library_series_exact_episode_then_original_single_id_reader(self):
        result = self.run_resolve()
        self.assertEqual(result.status, 'resolved')
        self.assertEqual(result.content_identity_digest, self.target.content_identity_digest)
        self.assertEqual(result.observation.status, 'matched')
        self.assertEqual(len(result.operation_digests), 3)
        self.assertEqual(len(result.response_digests), 3)
        first, second, third = [parse_qs(urlsplit(c.calls[1][1]).query) for c in self.connections]
        self.assertEqual(first['parentId'], [self.binding.library_id])
        self.assertEqual(first['nameStartsWith'], [self.target.show_name])
        self.assertEqual(first['limit'], ['2'])
        self.assertEqual(second['parentId'], [self.series['Id']])
        self.assertEqual(second['indexNumber'], ['2'])
        self.assertEqual(second['parentIndexNumber'], ['1'])
        self.assertEqual(third['ids'], [self.episode['Id']])
        self.assertEqual(third['limit'], ['1'])
        for query in (first, second, third):
            self.assertNotIn('searchTerm', query)
            self.assertEqual(query['enableUserData'], ['false'])
            self.assertEqual(query['enableImages'], ['false'])
        self.assertNotIn('PRIVATE_', repr(result))
        self.assertNotIn('Fictional Fixture Show', repr(result))
        self.assertTrue(all(c.closed for c in self.connections))

    def test_existing_series_binding_avoids_title_export_but_rechecks_library_scope(self):
        self.target = replace(self.target, series_id=self.series['Id'], show_name=None)
        result = self.run_resolve()
        self.assertEqual(result.status, 'resolved')
        query = parse_qs(urlsplit(self.connections[0].calls[1][1]).query)
        self.assertEqual(query['ids'], [self.series['Id']])
        self.assertEqual(query['parentId'], [self.binding.library_id])
        self.assertNotIn('nameStartsWith', query)

    def test_missing_and_ambiguous_series_or_episode_never_resolve(self):
        cases = [([{'Items': []}], 'not_indexed'),
                 ([{'Items': [self.series, self.series]}], 'ambiguous'),
                 ([{'Items': [self.series]}, {'Items': []}], 'not_indexed'),
                 ([{'Items': [self.series]}, {'Items': [self.episode, self.episode]}], 'ambiguous')]
        for values, expected in cases:
            with self.subTest(expected=expected, values=values):
                result = self.run_resolve(values)
                self.assertEqual(result.status, expected)
                self.assertIsNone(result.observation)
                self.assertEqual(self.factory_count, len(values))

    def test_wrong_show_server_episode_scope_or_virtual_readback_refuses(self):
        for changes in ({'ProviderIds': {'Tmdb': '123'}}, {'Name': 'Different show'},
                        {'ServerId': 'f' * 32}, {'Path': jf.TV_ROOTS[0] + '-other/Show'}):
            values = [{'Items': [{**self.series, **changes}]}]
            self.assertEqual(self.run_resolve(values).status, 'unavailable')
        for changes in ({'SeriesId': 'f' * 32}, {'IndexNumber': 3}, {'IndexNumber': True},
                        {'IndexNumberEnd': 3}, {'ParentIndexNumber': 2}):
            values = [{'Items': [self.series]}, {'Items': [{**self.episode, **changes}]}]
            self.assertEqual(self.run_resolve(values).status, 'unavailable')
        for changes in ({'IsVirtualItem': True}, {'LocationType': 'Remote'}, {'MediaType': 'Audio'}):
            values = [{'Items': [self.series]}, {'Items': [self.episode]},
                      {'Items': [{**self.episode, **changes}]}]
            self.assertEqual(self.run_resolve(values).status, 'unavailable')

    def test_expired_or_foreign_target_and_revocation_have_no_followup_reads(self):
        self.assertEqual(self.run_resolve(credential_scope=None).status, 'unavailable')
        self.assertEqual(self.factory_count, 0)
        original = self.target
        self.target = replace(original, content_identity_digest='0' * 64)
        self.assertEqual(self.run_resolve().status, 'unavailable')
        self.assertEqual(self.factory_count, 0)
        self.target = replace(original, facts_valid_until=self.clock.now())
        self.assertEqual(self.run_resolve().status, 'unavailable')
        self.assertEqual(self.factory_count, 0)
        self.target = original
        reads = [0]
        def revalidate(binding, target):
            reads[0] += 1
            return (binding, target) if reads[0] < 6 else None
        result = self.run_resolve(revalidate=revalidate)
        self.assertEqual(result.status, 'unavailable')
        self.assertLess(self.factory_count, 3)
