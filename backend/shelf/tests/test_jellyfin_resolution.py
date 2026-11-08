"""Closed library/series/episode resolution through the real injected transport."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import json
import hashlib
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
        self.target = replace(self.target, series_id=self.series['Id'], show_name=None, show_id=None)
        result = self.run_resolve()
        self.assertEqual(result.status, 'resolved')
        query = parse_qs(urlsplit(self.connections[0].calls[1][1]).query)
        self.assertEqual(query['ids'], [self.series['Id']])
        self.assertEqual(query['parentId'], [self.binding.library_id])
        self.assertNotIn('nameStartsWith', query)
        wrong = {**self.series, 'ProviderIds': {'Tmdb': '123'}}
        self.assertEqual(self.run_resolve([{'Items': [wrong]}]).status, 'unavailable')

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

    def test_validated_series_survives_explicit_missing_episode_without_arrival(self):
        result = self.run_resolve([{'Items': [self.series]}, {'Items': []}])
        self.assertEqual(result.status, 'not_indexed')
        self.assertIsNone(result.series_id)
        self.assertIsNone(result.selection)
        self.assertIsNone(result.observation)
        self.assertIsNone(result.binding)
        evidence = result.series_evidence
        self.assertIs(type(evidence), jf.ValidatedSeriesBinding)
        self.assertEqual(evidence.series_id, self.series['Id'])
        self.assertEqual(evidence.binding, self.binding)
        self.assertEqual(evidence.content_identity_digest, self.target.content_identity_digest)
        self.assertEqual(evidence.request_id, self.target.request_id)
        self.assertEqual(evidence.job_binding_digest, self.target.binding_digest)
        self.assertEqual(evidence.destination_binding_digest, self.target.destination_binding_digest)
        self.assertEqual(evidence.scope, self.target.scope)
        self.assertEqual(evidence.target_digest, jf._digest(self.target.material()))
        self.assertEqual(evidence.service_binding_digest, jf._digest(self.binding.material()))
        self.assertEqual(evidence.operation_digest, result.operation_digests[0])
        self.assertEqual(evidence.response_digest, result.response_digests[0])
        self.assertEqual(evidence.response_digest,
                         hashlib.sha256(self.connections[0].response.payload).hexdigest())
        self.assertEqual(evidence.observed_at, self.clock.now())
        self.assertEqual(evidence.valid_until, self.target.facts_valid_until)
        self.assertEqual(self.factory_count, 2)

    def test_series_evidence_closed_roundtrip_preserves_original_bounds(self):
        result = self.run_resolve()
        evidence = result.series_evidence
        self.assertEqual(result.status, 'resolved')
        self.assertIs(type(evidence), jf.ValidatedSeriesBinding)
        self.assertEqual(len(result.operation_digests), 3)
        self.assertEqual(len(result.response_digests), 3)
        material = jf.series_evidence_material(evidence)
        self.clock.elapsed = 600
        self.assertEqual(jf.read_series_evidence(material), evidence)
        self.assertEqual(jf.series_evidence_material(jf.read_series_evidence(material)), material)
        self.assertEqual(evidence.valid_until, self.target.facts_valid_until)
        self.assertEqual(set(material), {'schema', *jf.ValidatedSeriesBinding.__dataclass_fields__})
        for forbidden in ('ProviderIds', '"show_id"', '"Tmdb"', 'Fictional Fixture Show', 'SYNTHETIC_NOT_A_KEY'):
            self.assertNotIn(forbidden, json.dumps(material))
        with self.assertRaises(FrozenInstanceError):
            evidence.series_id = 'f' * 32

    def test_final_selected_item_not_observed_keeps_only_validated_series(self):
        result = self.run_resolve([{'Items': [self.series]}, {'Items': [self.episode]}, {'Items': []}])
        self.assertEqual(result.status, 'not_indexed')
        self.assertIs(type(result.series_evidence), jf.ValidatedSeriesBinding)
        self.assertIsNone(result.selection)
        self.assertIsNone(result.observation)
        self.assertIsNone(result.series_id)
        self.assertIsNone(result.binding)
        self.assertEqual(self.factory_count, 3)
        self.assertTrue(all(connection.closed for connection in self.connections))

    def test_unvalidated_ambiguous_and_generic_failures_never_salvage_series_locals(self):
        cases = [([{'Items': []}], 'not_indexed'),
                 ([{'Items': [self.series, self.series]}], 'ambiguous'),
                 ([{'Items': [{'Id': self.series['Id']}]}], 'unavailable'),
                 ([{'Items': [{**self.series, 'ProviderIds': {'Tmdb': '123'}}]}], 'unavailable'),
                 ([{'Items': [{**self.series, 'ProviderIds': {'Tmdb': '01399'}}]}], 'unavailable'),
                 ([{'Items': [{**self.series, 'ServerId': 'f' * 32}]}], 'unavailable'),
                 ([{'Items': [{**self.series, 'Type': 'Episode'}]}], 'unavailable'),
                 ([{'Items': [{**self.series, 'Name': 'Different show'}]}], 'unavailable'),
                 ([{'Items': [{**self.series, 'Path': jf.TV_ROOTS[0] + '-other/Show'}]}], 'unavailable'),
                 ([{'Items': [self.series]}, {'Items': [self.episode, self.episode]}], 'ambiguous'),
                 ([{'Items': [self.series]}, {'Items': [{'Id': self.episode['Id']}]}], 'unavailable'),
                 ([{'Items': [self.series]}, {'Items': [self.episode]},
                   {'Items': [{**self.episode, 'ServerId': 'f' * 32}]}], 'unavailable')]
        for values, expected in cases:
            with self.subTest(expected=expected, values=values):
                result = self.run_resolve(values)
                self.assertEqual(result.status, expected)
                self.assertIsNone(result.series_evidence)
                self.assertIsNone(result.series_id)
                self.assertIsNone(result.binding)
                self.assertIsNone(result.selection)
                self.assertIsNone(result.observation)
                self.assertEqual(self.factory_count, len(values))
        entries = [0]
        @contextmanager
        def credential_failure(binding):
            entries[0] += 1
            if entries[0] == 2:
                raise RuntimeError('SYNTHETIC_PRIVATE_CREDENTIAL_FAILURE')
            with self.credential(binding) as credential:
                yield credential
        result = self.run_resolve(credential_scope=credential_failure)
        self.assertEqual(result.status, 'unavailable')
        self.assertIsNone(result.series_evidence)
        self.assertEqual(self.factory_count, 1)
        self.assertNotIn('SYNTHETIC_PRIVATE_CREDENTIAL_FAILURE', repr(result))

    def test_partial_return_revalidates_after_episode_empty_before_releasing_evidence(self):
        original_init = jf._ResolutionStop.__init__
        for refusal in ('revoked', 'source_expired', 'service_expired', 'generation_changed'):
            allowed = [True]
            self.clock.elapsed = 0
            def stopped(error, status, reason):
                original_init(error, status, reason)
                if refusal == 'source_expired':
                    self.clock.elapsed = 30
                elif refusal == 'service_expired':
                    self.clock.elapsed = 60
                else:
                    allowed[0] = False
            def current(binding, target):
                if allowed[0]:
                    return binding, target
                if refusal == 'generation_changed':
                    return replace(binding, credential_generation='f' * 32), target
                return None
            with self.subTest(refusal=refusal), patch.object(jf._ResolutionStop, '__init__', stopped):
                result = self.run_resolve([{'Items': [self.series]}, {'Items': []}], revalidate=current)
                self.assertEqual(result.status, 'unavailable')
                self.assertIsNone(result.series_evidence)
                self.assertEqual(self.factory_count, 2)

    def test_series_codec_rejects_mixed_keys_types_digests_and_invalid_time_bounds(self):
        evidence = self.run_resolve([{'Items': [self.series]}, {'Items': []}]).series_evidence
        material = jf.series_evidence_material(evidence)
        malformed = [{**material, 'unknown': 'extra'},
                     {**material, 'schema': 'other-schema'},
                     {key: value for key, value in material.items() if key != 'scope'}]
        for field in ('job_binding_digest', 'content_identity_digest', 'destination_binding_digest',
                      'target_digest', 'service_binding_digest', 'operation_digest', 'response_digest'):
            for value in (True, 'a' * 63, 'G' * 64):
                malformed.append({**material, field: value})
        for field, values in {
                'request_id': (None, '', 'x' * 65, 'bad\nrequest'),
                'scope': (None, True, 'series', 'episode_s1e02'),
                'series_id': ('0' * 32, '7' * 31, '7' * 32 + ',other'),
                'observed_at': (None, True, 'not-a-date', '2026-11-08T00:00:00', material['valid_until']),
                'valid_until': (material['observed_at'], (self.binding.expires_at + timedelta(seconds=1)).isoformat())
        }.items():
            malformed.extend({**material, field: value} for value in values)
        for binding in ({**material['binding'], 'token': 'SYNTHETIC_FORBIDDEN'},
                        {key: value for key, value in material['binding'].items() if key != 'request_seconds'},
                        {**material['binding'], 'profile_id': True},
                        {**material['binding'], 'credential_generation': 'f' * 32},
                        {**material['binding'], 'expires_at': '2026-11-08T00:00:00'}):
            malformed.append({**material, 'binding': binding})
        with patch.object(jf, '_Connection', side_effect=AssertionError('Codec must perform no I/O')):
            for value in malformed:
                with self.subTest(value=value), self.assertRaises(jf.Unavailable):
                    jf.read_series_evidence(value)
