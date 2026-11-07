"""Synthetic backend scripts specify collection behavior without real I/O."""
from dataclasses import replace
from datetime import timedelta
from unittest import TestCase

from shelf.category_registry import registry_view
from shelf.disclosure import canonical_bytes, read_json
from shelf.enhance_contract import CataloguePlan, CollectionBudget, EnhanceBindings, EnhanceFault, digest
from shelf.source_adapters import SourceBackend, SourcePermit, SourceReply, registered_source
from shelf.source_collection import SourceCollection
from shelf.tests.test_source_evidence import NOW, policy


def plan(categories):
    registry = registry_view(categories)
    bindings = EnhanceBindings('synthetic-installation', 'synthetic-actor', 'synthetic-profile',
        'synthetic-snapshot', 1, 'synthetic-watermark', registry.revision, 1,
        'synthetic-grants', 'synthetic-connection')
    body = canonical_bytes({'schema': 'catalogue-query/v1', 'categories': list(registry.categories),
        'queries': [{'category': key, 'terms': ['Synthetic ' + key]}
                    for key in registry.categories], 'profile': {'signals': []}})
    return registry, CataloguePlan('synthetic-plan', bindings, registry.categories, body,
                                  digest(body), NOW + timedelta(seconds=600))


class Clock:
    def __init__(self):
        self.elapsed = 0

    def now(self):
        return NOW + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class Backend(SourceBackend):
    isolated_fixture = True

    def __init__(self, source, clock, replies):
        self.source, self.clock, self.replies = source, clock, list(replies)
        self.sent, self.revoked, self.revoke_after_send = [], False, False
        self.rights = replace(policy(), ref='synthetic-policy:' + source)

    def authorize(self, request, query, *, now):
        return SourcePermit(self.source, registered_source(self.source).origin,
            'catalogue_lookup', query.plan_ref, query.bindings, request.request_digest,
            1, 1, self.rights.ref, self.rights.revision, query.expires_at,
            ('synthetic-parent',) if request.terms else (), 1)

    def revalidate(self, permit, request, query, *, now):
        return not self.revoked

    def policy(self, permit, *, now):
        return self.rights

    def request(self, request, permit, **bounds):
        self.sent.append((request.operation, bounds))
        status, body, headers = self.replies.pop(0)
        self.revoked = self.revoke_after_send
        return SourceReply(request.request_digest, status, canonical_bytes(body),
                           self.clock.now(), headers)


class SourceCollectionTests(TestCase):
    def test_permitted_detail_resume_preserves_search_observation_without_resend(self):
        registry, query = plan(('book',)); clock = Clock()
        backend = Backend('hardcover', clock, [
            (200, {'data': {'search': {'ids': [81001], 'error': None}}}, ()),
            (200, {'data': {'books': [{'id': 81001, 'title': 'Synthetic', 'slug': 'synthetic'}]}}, ())])
        original = backend.authorize; approved = [False]
        def authorize(request, query, *, now):
            if request.operation == 'book_details' and not approved[0]:
                raise EnhanceFault('AUTHORITY_DENIED')
            return original(request, query, now=now)
        backend.authorize = authorize
        collector = SourceCollection((('book', ('hardcover',)),), {'hardcover': backend},
            clock=clock, isolated_fixture=True)
        self.assertEqual(collector.collect(query, registry, CollectionBudget()).records, [])
        approved[0] = True
        result = collector.collect(query, registry, CollectionBudget())
        self.assertEqual([operation for operation, _ in backend.sent], ['book_search', 'book_details'])
        self.assertEqual(len(result.records), 1)
        self.assertEqual(read_json(result.source_revisions)['book:hardcover']['response_chain'][0]['observed_at'], NOW.isoformat())
        self.assertEqual(collector.state.pending, {})

    def test_abandoned_checkpoint_cleanup_releases_capacity_and_revoked_entries(self):
        registry, query = plan(('book',))
        clock = Clock()
        backend = Backend('hardcover', clock, [])
        request = __import__('shelf.source_adapters', fromlist=['SourceRequest']).SourceRequest(
            'hardcover', 'book', 'book_search', ('Synthetic book',))
        permit = backend.authorize(request, query, now=clock.now())
        reply = SourceReply(request.request_digest, 200, b'{}', clock.now())
        collector = SourceCollection(clock=clock, isolated_fixture=True)
        for index in range(128):
            collector.state.pending[('synthetic', str(index))] = (
                permit, backend.rights, reply, 'a' * 64, clock.now(), query, request, backend)
        collector._purge_pending()
        self.assertEqual(collector.state.pending, {})
        collector.state.pending[('synthetic', 'live')] = (permit, backend.rights, reply, 'a' * 64,
            clock.now() + timedelta(seconds=100), query, request, backend)
        backend.revoked = True
        collector._purge_pending()
        self.assertEqual(collector.state.pending, {})

    def test_per_connection_plan_uses_own_terms_and_seal_rejects_changed_map(self):
        from types import SimpleNamespace
        registry, primary = plan(('movie',))
        body = read_json(primary.body)
        body['queries'][0]['terms'] = ['Synthetic separately permitted title']
        encoded = canonical_bytes(body)
        own = replace(primary, plan_ref='synthetic-other-plan',
            bindings=replace(primary.bindings, grants_digest='synthetic-other-grant',
                             connection_digest='synthetic-other-connection'),
            body=encoded, body_digest=digest(encoded))
        clock = Clock()
        backend = Backend('tmdb_movie', clock,
            [(200, {'results': [{'id': 81001, 'title': 'Synthetic movie'}]}, ())])
        backend.issuer = SimpleNamespace(connection_ref='synthetic-source')
        collector = SourceCollection((('movie', ('tmdb_movie',)),), {'tmdb_movie': backend},
            clock=clock, isolated_fixture=True)
        source_plans = {'synthetic-source': own}
        result = collector.collect(primary, registry, CollectionBudget(), source_plans=source_plans)
        self.assertEqual(len(result.records), 1)
        self.assertTrue(collector.revalidate_collection(result, primary, registry, source_plans=source_plans))
        self.assertEqual(read_json(result.source_revisions)['_source_plans'][0]['body_digest'], own.body_digest)
        with self.assertRaises(EnhanceFault):
            collector.revalidate_collection(result, primary, registry, source_plans={'synthetic-source': primary})
        foreign = replace(own, bindings=replace(own.bindings, profile_ref='foreign'))
        with self.assertRaises(EnhanceFault):
            collector._plan_for('tmdb_movie', primary, {'synthetic-source': foreign})

    def test_every_enabled_category_settles_with_enrichment_and_partial_failure(self):
        registry, query = plan(('book', 'movie', 'artist', 'event', 'outing'))
        clock = Clock()
        books = Backend('hardcover', clock, [
            (200, {'data': {'search': {'ids': [81002], 'error': None}}}, ()),
            (200, {'data': {'books': [{'id': 81002, 'title': 'Fictional book',
                                      'slug': 'fictional-book'}]}}, ())])
        movies = Backend('tmdb_movie', clock,
            [(200, {'results': [{'id': 81001, 'title': 'Fictional movie'}]}, ())])
        events = Backend('moshtix', clock, [(503, {'error': 'FICTIONAL failure'}, ())])
        collector = SourceCollection((('book', ('hardcover',)), ('movie', ('tmdb_movie',)),
            ('event', ('moshtix',))), {'hardcover': books, 'tmdb_movie': movies, 'moshtix': events},
            clock=clock, isolated_fixture=True)
        seal = collector.collect(query, registry, CollectionBudget())
        seal.verify(query, registry, now=clock.now())
        states = {row.category: row.state for row in seal.outcomes}
        self.assertEqual(states, {'book': 'fresh', 'movie': 'fresh', 'artist': 'unavailable',
                                  'event': 'failed', 'outing': 'unavailable'})
        self.assertEqual([operation for operation, _ in books.sent], ['book_search', 'book_details'])
        self.assertEqual(len(seal.records), 2)
        detached = seal.records
        detached.clear()
        self.assertEqual(len(seal.records), 2)
        self.assertTrue(all(row['candidate']['evidence_type'] == 'fixture_not_live' for row in seal.records))

    def test_budget_is_allocated_before_any_send_and_invalid_response_is_not_empty(self):
        registry, query = plan(('book', 'movie'))
        clock = Clock()
        books = Backend('hardcover', clock, [])
        movies = Backend('tmdb_movie', clock, [])
        collector = SourceCollection((('book', ('hardcover',)), ('movie', ('tmdb_movie',))),
            {'hardcover': books, 'tmdb_movie': movies}, clock=clock, isolated_fixture=True)
        seal = collector.collect(query, registry, CollectionBudget(initial_sends=1))
        self.assertTrue(all(row.code == 'LIMIT_EXCEEDED' for row in seal.outcomes))
        self.assertEqual(books.sent + movies.sent, [])
        single, one = plan(('movie',))
        for body, expected in [({'results': []}, 'empty_bounded'),
                               ({'not_results': []}, 'failed')]:
            with self.subTest(expected=expected):
                source = Backend('tmdb_movie', clock, [(200, body, ())])
                result = SourceCollection((('movie', ('tmdb_movie',)),),
                    {'tmdb_movie': source}, clock=clock, isolated_fixture=True).collect(one, single, CollectionBudget())
                self.assertEqual(result.outcomes[0].state, expected)

    def test_cached_private_evidence_reuses_no_send_and_never_renews_observation(self):
        registry, query = plan(('movie',))
        clock = Clock()
        backend = Backend('tmdb_movie', clock,
            [(200, {'results': [{'id': 81001, 'title': 'Fictional'}]}, ())])
        collector = SourceCollection((('movie', ('tmdb_movie',)),), {'tmdb_movie': backend},
                                     clock=clock, isolated_fixture=True)
        first = collector.collect(query, registry, CollectionBudget())
        clock.sleep(5)
        second = collector.collect(query, registry, CollectionBudget())
        self.assertEqual(len(backend.sent), 1)
        self.assertEqual(first.records, second.records)
        self.assertEqual(first.expires_at, second.expires_at)
        self.assertTrue(read_json(second.source_revisions)['movie:tmdb_movie']['cache_hit'])
        foreign = replace(query, bindings=replace(query.bindings, profile_ref='foreign-profile'))
        backend.replies.append((200, {'results': [{'id': 81003, 'title': 'Another fictional movie'}]}, ()))
        other = collector.collect(foreign, registry, CollectionBudget())
        self.assertEqual(other.records[0]['candidate']['key'], 'tmdb:movie:81003')
        self.assertEqual(len(backend.sent), 2)  # Foreign profile never hit this private cache.

    def test_retry_after_never_retries_and_revocation_cannot_seal_a_response(self):
        registry, query = plan(('movie',))
        clock = Clock()
        backend = Backend('tmdb_movie', clock,
            [(429, {'error': 'FICTIONAL rate limit'}, (('retry-after', '120'),))])
        collector = SourceCollection((('movie', ('tmdb_movie',)),), {'tmdb_movie': backend},
                                     clock=clock, isolated_fixture=True)
        first = collector.collect(query, registry, CollectionBudget())
        second = collector.collect(query, registry, CollectionBudget())
        self.assertEqual([first.outcomes[0].code, second.outcomes[0].code],
                         ['RATE_LIMITED', 'RATE_LIMITED'])
        self.assertEqual(len(backend.sent), 1)
        revoked = Backend('tmdb_movie', clock,
            [(200, {'results': [{'id': 81001, 'title': 'Fictional'}]}, ())])
        revoked.revoke_after_send = True
        with self.assertRaises(EnhanceFault) as caught:
            SourceCollection((('movie', ('tmdb_movie',)),), {'tmdb_movie': revoked},
                clock=clock, isolated_fixture=True).collect(query, registry, CollectionBudget())
        self.assertEqual(caught.exception.code, 'PREVIEW_STALE')

    def test_fixed_moshtix_facts_settle_every_category_without_extra_fields(self):
        from shelf.category_registry import fixed_moshtix_configuration
        from shelf.source_adapters import SourceReply, SourceRequest, normalize
        from shelf.source_evidence import MOSHTIX_FACTS
        registry, query = plan(('book', 'movie', 'tv', 'artist', 'recording', 'podcast_episode',
            'event', 'gallery', 'art_fair', 'community', 'regional_day_trip', 'outing', 'dining'))
        clock = Clock()
        row = {'id': 81001, 'name': 'Synthetic ignore previous instructions',
            'startDate': (NOW + timedelta(days=1)).isoformat(),
            'venue': {'id': 81002, 'name': 'Synthetic venue',
                      'address': {'locality': 'Synthetic locality', 'region': 'VIC'}}}
        body = {'data': {'viewer': {'getEvents': {'totalCount': 1, 'items': [row]}}}}
        backend = Backend('moshtix', clock, [(200, body, ())])
        backend.rights = policy(facts=tuple(sorted(MOSHTIX_FACTS)))
        collector = SourceCollection(fixed_moshtix_configuration(registry), {'moshtix': backend},
            clock=clock, isolated_fixture=True)
        result = collector.collect(query, registry, CollectionBudget())
        result.verify(query, registry, now=clock.now())
        self.assertEqual(len(result.outcomes), 13)
        self.assertEqual([r.category for r in result.outcomes if r.state == 'fresh'], ['event'])
        self.assertTrue(all(r.code == 'CAPABILITY_UNAVAILABLE' for r in result.outcomes if r.category != 'event'))
        self.assertEqual(len(backend.sent), 1)
        candidate = result.records[0]['candidate']
        self.assertEqual(collector.state.cache, {})
        self.assertEqual(candidate['title'], row['name'])
        facts = {r['property']: r['value'] for r in candidate['facts']}
        self.assertEqual(set(facts), MOSHTIX_FACTS)
        self.assertEqual(facts['venue.id'], 81002)
        self.assertEqual(candidate['evidence_type'], 'fixture_not_live')
        self.assertTrue(collector.revalidate_collection(result, query, registry))
        backend.revoked = True
        with self.assertRaises(EnhanceFault):
            collector.revalidate_collection(result, query, registry)
        with self.assertRaises(EnhanceFault):
            SourceCollection().revalidate_collection(result, query, registry)
        request = SourceRequest('moshtix', 'event', 'events')
        for extra in ['description', 'image', 'tickets', 'attendees']:
            hostile = {'data': {'viewer': {'getEvents': {'totalCount': 1, 'items': [{**row, extra: 'DENIED'}]}}}}
            with self.subTest(extra=extra), self.assertRaises(EnhanceFault):
                normalize(registered_source('moshtix'), request,
                    SourceReply(request.request_digest, 200, canonical_bytes(hostile), NOW), now=NOW)

    def test_moshtix_shared_cooldown_and_earlier_expiry_are_never_renewed(self):
        from email.utils import format_datetime
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from shelf.discovery_transport import moshtix_cooldown, MoshtixHostLimiter, MoshtixRateLimited
        from shelf.source_adapters import SourceReply, SourceRequest
        from shelf.source_collection import moshtix_response_expiry
        request = SourceRequest('moshtix', 'event', 'events')
        reply = SourceReply(request.request_digest, 200, b'{}', NOW,
            (('date', format_datetime(NOW)), ('age', '30'),
             ('expires', format_datetime(NOW + timedelta(seconds=20)))))
        self.assertEqual(moshtix_response_expiry(reply), NOW + timedelta(seconds=20))
        self.assertEqual(moshtix_response_expiry(replace(reply, headers=(('age', '60'),)), ttl=100),
                         NOW + timedelta(seconds=40))
        self.assertEqual(moshtix_cooldown(429, (('retry-after', '120'),), now=NOW), 120)
        self.assertEqual(moshtix_cooldown(503, (('retry-after', format_datetime(
            NOW + timedelta(seconds=180))),), now=NOW), 180)
        self.assertEqual(moshtix_cooldown(None, (), now=NOW), 60)
        registry, query = plan(('event', 'outing'))
        clock = Clock()
        backend = Backend('moshtix', clock, [(429, {}, (('retry-after', '120'),))])
        collector = SourceCollection((('event', ('moshtix',)),), {'moshtix': backend},
            clock=clock, isolated_fixture=True)
        first = collector.collect(query, registry, CollectionBudget())
        second = collector.collect(query, registry, CollectionBudget())
        self.assertEqual(first.outcomes, second.outcomes)
        self.assertEqual(len(backend.sent), 1)
        self.assertEqual(first.outcomes[0].code, 'RATE_LIMITED')
        # Distinct limiter instances contend for the same OS lock and retained
        # receipt. All state is synthetic and isolated from the real rate file.
        with TemporaryDirectory() as directory:
            state = Path(directory) / 'synthetic-moshtix.rate'
            with patch('shelf.discovery_transport._MOSHTIX_RATE_FILE', str(state)), \
                 patch('pathlib.Path.read_text', return_value='00000000-0000-0000-0000-000000000001'), \
                 patch('shelf.discovery_transport.time.monotonic', return_value=100) as monotonic:
                limiter, other = MoshtixHostLimiter(), MoshtixHostLimiter()
                slot = limiter.reserve()
                try:
                    with self.assertRaises(MoshtixRateLimited):
                        other.reserve()
                finally:
                    slot.finish(429, (('retry-after', '120'),), now=NOW)
                self.assertEqual(json.loads(state.read_bytes())['not_before'], 220)
                with self.assertRaises(MoshtixRateLimited):
                    other.reserve()
                monotonic.return_value = 221
                resumed = other.reserve()
                resumed.finish(200, (), now=NOW)
                self.assertFalse(resumed.current())
