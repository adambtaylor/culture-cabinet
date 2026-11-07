"""Production recording backend with explicitly synthetic issuer/HTTP ports."""
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from unittest import TestCase
from unittest.mock import patch

from shelf.disclosure import SCALARS, canonical_bytes, read_json
from shelf.discovery_sources import SourceError
from shelf.discovery_transport import MediaHostLimiter
from shelf.enhance_contract import CollectionBudget, EnhanceFault, digest
from shelf.musicbrainz_source import (SOURCE, OPERATION, MusicBrainzBackend,
                                     MusicBrainzOperation, musicbrainz_operation)
from shelf.source_adapters import (SourceAuthorityIssuer, SourcePermit, SourceReply,
                                   SourceRequest, normalize, registered_source)
from shelf.source_collection import SourceCollection
from shelf.source_evidence import FieldPolicy
from .test_source_collection import Clock, plan
from .test_source_evidence import NOW

IDENTITY = 'CultureCabinet/fixture (https://example.invalid/contact)'
FIRST = '00000000-0000-4000-8000-000000000001'
SECOND = '00000000-0000-4000-8000-000000000002'


def recording_plan(categories=('recording',)):
    registry, query = plan(categories)
    body = read_json(query.body)
    for row in body['queries']:
        if row['category'] == 'recording':
            row['terms'] = ['Synthetic artist']
    encoded = canonical_bytes(body)
    return registry, replace(query, body=encoded, body_digest=digest(encoded))


class Issuer(SourceAuthorityIssuer):
    """Synthetic port, not evidence of native grant/owner authorization."""
    def __init__(self):
        self.rights = FieldPolicy('synthetic-cc0-core', 1, NOW - timedelta(seconds=1),
            NOW + timedelta(seconds=300), (('*', ('fetch',)),) + tuple(
                (key, ('fetch', 'display', 'retain', 'model_context')) for key in sorted(SCALARS)))
        self.current, self.processor, self.generation = True, True, 0
        self.permits = {}

    def operation_for(self, request, query, *, now):
        return musicbrainz_operation(request, IDENTITY)

    def authorize(self, request, query, operation, *, now):
        key = (query.plan_ref, request.request_digest)
        if key not in self.permits:
            permit = SourcePermit(SOURCE, 'https://musicbrainz.org', 'catalogue_lookup',
                query.plan_ref, query.bindings, request.request_digest, 1, 1,
                self.rights.ref, self.rights.revision, self.rights.expires_at,
                ('synthetic-disclosed-artist',), self.generation)
            self.permits[key] = (permit, operation, self.rights)
        return self.permits[key][0]

    def revalidate(self, permit, request, query, operation, *, now):
        value = self.permits.get((query.plan_ref, request.request_digest))
        return bool(self.current and value and value[0] is permit
                    and value[1:] == (operation, self.rights) and self.rights.current(now))

    def policy(self, permit, operation, *, now):
        return self.rights

    def model_context_current(self, permit, operation, *, now):
        return self.processor


class FixtureBackend(MusicBrainzBackend):
    isolated_fixture = True  # A mocked reply must never become live evidence.


class MusicBrainzSourceTests(TestCase):
    def test_fixed_literal_query_prevents_parameter_and_lucene_injection(self):
        op = MusicBrainzOperation('A" OR artist:* + AC/DC & X', IDENTITY)
        parsed = urlsplit(op.url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ('https', 'musicbrainz.org', '/ws/2/recording/'))
        self.assertEqual(parse_qs(parsed.query), {'query': ['artistname:"A\\" OR artist\\:\\* \\+ AC\\/DC \\& X"'],
            'fmt': ['json'], 'limit': ['9'], 'offset': ['0']})
        self.assertEqual(op.body, b'')
        self.assertEqual(op.operation_digest, digest(canonical_bytes(op.material)))
        self.assertNotIn('A" OR', repr(op))
        self.assertIn('Synthet%C3%AFc', MusicBrainzOperation('Synthetïc', IDENTITY).url)

    def test_missing_input_contact_override_and_wrong_operation_are_denied(self):
        for title in ('', 'x' * 201, 'raw\nnotes', '\ud800', None):
            with self.subTest(title=repr(title)), self.assertRaises(SourceError):
                MusicBrainzOperation(title, IDENTITY)
        for identity in ('CultureCabinet/1.0', 'CultureCabinet/1.0 (not-a-contact)',
                         'CultureCabinet/1.0 (https://u:p@example.invalid)',
                         'CultureCabinet/1.0 (https://example.invalid/?secret=bad)',
                         'CultureCabinet/1.0 (a@example.invalid)\r\nAuthorization: bad'):
            with self.subTest(identity=identity), self.assertRaises(SourceError):
                MusicBrainzOperation('Synthetic artist', identity)
        with self.assertRaises(EnhanceFault):
            SourceRequest(SOURCE, 'recording', OPERATION)
        with self.assertRaises(EnhanceFault):
            musicbrainz_operation(SourceRequest('tmdb_movie', 'movie', 'search_movie', ('Synthetic',)), IDENTITY)

    def test_core_projection_discards_rank_and_supplementary_fields_under_permutation(self):
        request = SourceRequest(SOURCE, 'recording', OPERATION, ('Synthetic artist',))
        rows = [{'id': SECOND, 'title': 'Synthetic second', 'score': 'MUST_NOT_PROJECT',
                 'tags': [{'name': 'MUST_NOT_PROJECT'}]},
                {'id': FIRST, 'title': 'Synthetic first'}, {'id': FIRST, 'title': 'Synthetic alternate'}]
        def cards(items):
            reply = SourceReply(request.request_digest, 200, canonical_bytes({'recordings': items}), NOW)
            return normalize(registered_source(SOURCE), request, reply, now=NOW).cards
        result = cards(rows)
        self.assertEqual(result, cards(list(reversed(rows))))
        self.assertEqual([row.source_id for row in result], [FIRST, SECOND])
        self.assertEqual([row.title for row in result], ['Synthetic alternate', 'Synthetic second'])
        self.assertEqual([row.locator for row in result],
                         ['normalized-core-recording:0', 'normalized-core-recording:1'])
        self.assertTrue(all(row.facts == () and row.provider == 'musicbrainz'
                            and row.medium == 'recording' for row in result))
        with self.assertRaises(EnhanceFault):
            cards(rows * 4)

    def test_real_backend_collects_one_mocked_reply_with_complete_honest_coverage(self):
        from shelf.category_registry import KEYS
        registry, query = recording_plan(KEYS)
        clock, issuer = Clock(), Issuer()
        backend = FixtureBackend(issuer, MediaHostLimiter('musicbrainz'), clock=clock)
        body = canonical_bytes({'recordings': [{'id': FIRST, 'title': 'Synthetic recording',
                                               'score': 'MUST_NOT_PROJECT'}]})
        def wire(operation, **bounds):
            self.assertEqual(operation.artist_name, 'Synthetic artist')
            self.assertTrue(bounds['revalidate']())
            return 200, body, (('content-type', 'application/json'),)
        with patch('shelf.discovery_transport.musicbrainz_authorized', side_effect=wire) as send, \
             patch('shelf.credential_store.open_store', side_effect=AssertionError('No credential access')):
            collector = SourceCollection((('recording', (SOURCE,)),), {SOURCE: backend},
                clock=clock, isolated_fixture=True)
            seal = collector.collect(query, registry, CollectionBudget())
            self.assertEqual(send.call_count, 1)
            self.assertEqual(collector.model_eligible_keys(seal, query, registry),
                             frozenset({'musicbrainz:recording:' + FIRST}))
        outcomes = {row.category: row for row in seal.outcomes}
        self.assertEqual(set(outcomes), set(KEYS))
        self.assertEqual((outcomes['recording'].state, outcomes['recording'].candidate_count), ('fresh', 1))
        self.assertTrue(all(row.state == 'unavailable' for key, row in outcomes.items() if key != 'recording'))
        self.assertEqual(seal.records[0]['candidate']['evidence_type'], 'fixture_not_live')
        self.assertNotIn('MUST_NOT_PROJECT', seal._material.decode())
        self.assertEqual(collector.state.cache, {})

    def test_permit_identity_and_one_shot_state_survive_issuer_replay_and_failure(self):
        _, query = recording_plan()
        clock, issuer = Clock(), Issuer()
        backend = FixtureBackend(issuer, MediaHostLimiter('musicbrainz'), clock=clock)
        request = SourceRequest(SOURCE, 'recording', OPERATION, ('Synthetic artist',))
        permit = backend.authorize(request, query, now=NOW)
        with self.assertRaises(EnhanceFault):
            backend.policy(replace(permit), now=NOW)
        def fail(operation, **bounds):
            self.assertTrue(bounds['revalidate']())
            raise TimeoutError
        with patch('shelf.discovery_transport.musicbrainz_authorized', side_effect=fail) as send:
            with self.assertRaises(TimeoutError):
                backend.request(request, permit, response_bytes=131072, request_seconds=5, parent_seconds=6)
            self.assertIs(backend.authorize(request, query, now=NOW), permit)
            with self.assertRaises(EnhanceFault):
                backend.request(request, permit, response_bytes=131072, request_seconds=5, parent_seconds=6)
            self.assertEqual(send.call_count, 1)

    def test_current_source_processor_and_anonymous_generation_cannot_be_borrowed(self):
        _, query = recording_plan()
        clock, issuer = Clock(), Issuer()
        backend = FixtureBackend(issuer, MediaHostLimiter('musicbrainz'), clock=clock)
        request = SourceRequest(SOURCE, 'recording', OPERATION, ('Synthetic artist',))
        permit = backend.authorize(request, query, now=NOW)
        issuer.processor = False
        with self.assertRaises(EnhanceFault):
            backend.policy(permit, now=NOW)
        issuer.processor, issuer.current = True, False
        self.assertFalse(backend.revalidate(permit, request, query, now=NOW))
        issuer = Issuer(); issuer.generation = 1
        backend = FixtureBackend(issuer, MediaHostLimiter('musicbrainz'), clock=clock)
        with self.assertRaises(EnhanceFault):
            backend.authorize(request, query, now=NOW)
