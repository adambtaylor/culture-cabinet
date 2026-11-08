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
                                     MusicBrainzOperation, musicbrainz_operation,
                                     ARTIST_SOURCE, ARTIST_OPERATION, musicbrainz_template)
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
    def __init__(self, source=SOURCE):
        self.source = source
        self.rights = FieldPolicy('synthetic-cc0-core:' + source, 1, NOW - timedelta(seconds=1),
            NOW + timedelta(seconds=300), (('*', ('fetch',)),) + tuple(
                (key, ('fetch', 'display', 'retain', 'model_context')) for key in sorted(SCALARS)))
        self.current, self.processor, self.generation = True, True, 0
        self.permits = {}

    def operation_for(self, request, query, *, now):
        return musicbrainz_operation(request, IDENTITY)

    def authorize(self, request, query, operation, *, now):
        key = (query.plan_ref, request.request_digest)
        if key not in self.permits:
            permit = SourcePermit(self.source, 'https://musicbrainz.org', 'catalogue_lookup',
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
    def test_artist_wire_identity_literal_and_template_cannot_be_recording(self):
        title = 'A" OR artist:* + AC/DC & X'
        request = SourceRequest(ARTIST_SOURCE, 'artist', ARTIST_OPERATION, (title,))
        operation = musicbrainz_operation(request, IDENTITY)
        self.assertEqual(urlsplit(operation.url).path, '/ws/2/artist/')
        self.assertEqual(parse_qs(urlsplit(operation.url).query), {
            'query': ['artist:"A\\" OR artist\\:\\* \\+ AC\\/DC \\& X"'],
            'fmt': ['json'], 'limit': ['9'], 'offset': ['0']})
        self.assertEqual(operation.material['path'], '/ws/2/artist/')
        self.assertNotEqual(operation.operation_digest,
                            MusicBrainzOperation(title, IDENTITY).operation_digest)
        self.assertEqual(musicbrainz_template(ARTIST_SOURCE), {
            'source': ARTIST_SOURCE, 'method': 'GET', 'origin': 'https://musicbrainz.org',
            'path': '/ws/2/artist/', 'operation': ARTIST_OPERATION, 'query_field': 'artist',
            'fixed_parameters': {'fmt': 'json', 'limit': 9, 'offset': 0},
            'projection': ['artist_mbid', 'name', 'source_url']})
        self.assertEqual(musicbrainz_template()['path'], '/ws/2/recording/')
        for source in ('musicbrainz', 'artist', 'https://example.invalid', None):
            with self.subTest(source=source), self.assertRaises(EnhanceFault):
                MusicBrainzOperation(title, IDENTITY, source)
        with self.assertRaises(EnhanceFault):
            SourceRequest(ARTIST_SOURCE, 'recording', ARTIST_OPERATION, (title,))
        with self.assertRaises(EnhanceFault):
            SourceRequest(ARTIST_SOURCE, 'artist', OPERATION, (title,))

    def test_artist_projection_is_core_only_stable_and_never_relabels_recordings(self):
        request = SourceRequest(ARTIST_SOURCE, 'artist', ARTIST_OPERATION, ('Synthetic artist',))
        rows = [{'id': SECOND, 'name': 'Synthetic second', 'score': 'MUST_NOT_PROJECT',
                 'sort-name': 'MUST_NOT_PROJECT', 'relations': [{'target': 'MUST_NOT_PROJECT'}]},
                {'id': FIRST, 'name': 'Synthetic first', 'tags': [{'name': 'MUST_NOT_PROJECT'}]},
                {'id': FIRST, 'name': 'Synthetic alternate'}]
        def parse(body):
            return normalize(registered_source(ARTIST_SOURCE), request,
                SourceReply(request.request_digest, 200, canonical_bytes(body), NOW), now=NOW).cards
        cards = parse({'artists': rows})
        self.assertEqual(cards, parse({'artists': list(reversed(rows))}))
        self.assertEqual([(row.key, row.title, row.source_url, row.locator) for row in cards], [
            ('musicbrainz:artist:' + FIRST, 'Synthetic alternate',
             'https://musicbrainz.org/artist/' + FIRST, 'normalized-core-artist:0'),
            ('musicbrainz:artist:' + SECOND, 'Synthetic second',
             'https://musicbrainz.org/artist/' + SECOND, 'normalized-core-artist:1')])
        self.assertTrue(all(row.medium == 'artist' and row.facts == () for row in cards))
        for body in ({'recordings': [{'id': FIRST, 'title': 'Not an artist'}]},
                     {'artists': rows * 4}, {'artists': [{'id': 'bad-id', 'name': 'Bad'}]},
                     {'artists': [{'id': FIRST, 'title': 'No actual name'}]}):
            with self.subTest(body=body), self.assertRaises((EnhanceFault, SourceError, KeyError)):
                parse(body)

    def test_artist_and_recording_collect_distinct_identities_with_same_host_limiter(self):
        from shelf.category_registry import KEYS
        registry, query = recording_plan(KEYS)
        clock, limiter = Clock(), MediaHostLimiter('musicbrainz')
        artist = FixtureBackend(Issuer(ARTIST_SOURCE), limiter, clock=clock, source=ARTIST_SOURCE)
        recording = FixtureBackend(Issuer(), limiter, clock=clock)
        self.assertIs(artist.limiter, recording.limiter)
        seen = []
        def wire(operation, **bounds):
            seen.append(operation.source)
            self.assertIs(bounds['limiter'], limiter)
            self.assertTrue(bounds['revalidate']())
            body = ({'artists': [{'id': FIRST, 'name': 'Synthetic artist', 'score': 'MUST_NOT_PROJECT'}]}
                if operation.source == ARTIST_SOURCE else
                {'recordings': [{'id': FIRST, 'title': 'Synthetic recording'}]})
            return 200, canonical_bytes(body), ()
        with patch('shelf.discovery_transport.musicbrainz_authorized', side_effect=wire), \
             patch('shelf.credential_store.open_store', side_effect=AssertionError('No credential access')):
            collector = SourceCollection((('artist', (ARTIST_SOURCE,)), ('recording', (SOURCE,))),
                {ARTIST_SOURCE: artist, SOURCE: recording}, clock=clock, isolated_fixture=True)
            result = collector.collect(query, registry, CollectionBudget())
            self.assertEqual(collector.model_eligible_keys(result, query, registry),
                frozenset({'musicbrainz:artist:' + FIRST, 'musicbrainz:recording:' + FIRST}))
        self.assertEqual(seen, [ARTIST_SOURCE, SOURCE])
        outcomes = {row.category: row for row in result.outcomes}
        self.assertEqual(tuple(row.category for row in result.outcomes), KEYS)
        self.assertTrue(all(outcomes[key].state == 'fresh' for key in ('artist', 'recording')))
        self.assertTrue(all(row.state == 'unavailable' for key, row in outcomes.items()
                            if key not in ('artist', 'recording')))
        self.assertTrue(all(row['candidate']['evidence_type'] == 'fixture_not_live' for row in result.records))
        self.assertNotIn('MUST_NOT_PROJECT', result._material.decode())
        self.assertEqual(collector.state.cache, {})

    def test_artist_local_only_rights_collect_without_model_eligibility(self):
        registry, query = plan(('artist',))
        clock, issuer = Clock(), Issuer(ARTIST_SOURCE)
        issuer.rights = replace(issuer.rights, permissions=tuple(
            (target, tuple(use for use in uses if use != 'model_context'))
            for target, uses in issuer.rights.permissions))
        issuer.processor = False
        backend = FixtureBackend(issuer, MediaHostLimiter('musicbrainz'), clock=clock, source=ARTIST_SOURCE)
        with patch('shelf.discovery_transport.musicbrainz_authorized',
            return_value=(200, canonical_bytes({'artists': [{'id': FIRST, 'name': 'Synthetic artist'}]}), ())):
            collector = SourceCollection((('artist', (ARTIST_SOURCE,)),), {ARTIST_SOURCE: backend},
                clock=clock, isolated_fixture=True)
            result = collector.collect(query, registry, CollectionBudget())
            self.assertEqual(result.outcomes[0].state, 'fresh')
            self.assertEqual(collector.model_eligible_keys(result, query, registry), frozenset())

    def test_artist_wrong_issuer_operation_and_foreign_or_replayed_permit_never_send(self):
        _, query = plan(('artist', 'recording'))
        clock, limiter = Clock(), MediaHostLimiter('musicbrainz')
        request = SourceRequest(ARTIST_SOURCE, 'artist', ARTIST_OPERATION, ('Synthetic artist',))
        with patch('shelf.discovery_transport.musicbrainz_authorized') as wire:
            recording = FixtureBackend(Issuer(), limiter, clock=clock)
            with self.assertRaises(EnhanceFault):
                recording.authorize(request, query, now=NOW)
            artist = FixtureBackend(Issuer(), limiter, clock=clock, source=ARTIST_SOURCE)
            with self.assertRaises(EnhanceFault):
                artist.authorize(request, query, now=NOW)  # Recording source permit from wrong issuer.
            issuer = Issuer(ARTIST_SOURCE)
            artist = FixtureBackend(issuer, limiter, clock=clock, source=ARTIST_SOURCE)
            with patch.object(issuer, 'operation_for', return_value=MusicBrainzOperation('Synthetic artist', IDENTITY)):
                with self.assertRaises(EnhanceFault):
                    artist.authorize(request, query, now=NOW)
            permit = artist.authorize(request, query, now=NOW)
            with self.assertRaises(EnhanceFault):
                artist.request(request, replace(permit), response_bytes=131072, request_seconds=5, parent_seconds=6)
            wire.assert_not_called()
        def fail(operation, **bounds):
            self.assertEqual(operation.source, ARTIST_SOURCE)
            self.assertTrue(bounds['revalidate']())
            raise TimeoutError
        with patch('shelf.discovery_transport.musicbrainz_authorized', side_effect=fail) as wire:
            with self.assertRaises(TimeoutError):
                artist.request(request, permit, response_bytes=131072, request_seconds=5, parent_seconds=6)
            self.assertIs(artist.authorize(request, query, now=NOW), permit)
            with self.assertRaises(EnhanceFault):
                artist.request(request, permit, response_bytes=131072, request_seconds=5, parent_seconds=6)
            wire.assert_called_once()

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
