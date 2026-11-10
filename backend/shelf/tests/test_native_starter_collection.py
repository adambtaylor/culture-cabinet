"""Declared editorial starts under real collector/permit/rights, synthetic replies.

These tests establish no native session, live provider result or inference PASS.
"""
from dataclasses import replace
from hashlib import sha256
import json
from unittest import TestCase

from shelf.category_registry import KEYS
from shelf.disclosure import canonical_bytes, read_json
from shelf.enhance_contract import CollectionBudget, EnhanceFault, digest
from shelf.provider_catalogue import CatalogueError, MediaOperation, media_operation
from shelf.source_adapters import SourceRequest, initial_source_request, normalize, registered_source
from shelf.source_collection import SourceCollection
from shelf.tests.test_source_collection import Backend, Clock, plan
from shelf.tests.test_source_evidence import NOW
from shelf.tests import test_media_authorized_transport as fixtures


def empty_plan(categories):
    registry, query = plan(categories)
    material = read_json(query.body)
    for row in material['queries']:
        row['terms'] = []
    body = canonical_bytes(material)
    return registry, replace(query, body=body, body_digest=digest(body))


def rows(source, count=6):
    if source == 'hardcover':
        return {'data': {'books': [{'id': 81001 + i, 'title': 'Fictional book ' + str(i),
            'slug': 'fictional-book-' + str(i)} for i in range(count)]}}
    return {'results': [{'id': 81001 + i,
        'name' if source == 'tmdb_tv' else 'title': 'Fictional item ' + str(i),
        'overview': 'UNSELECTED_BODY', 'vote_average': 10} for i in range(count)]}


class NativeStarterCollectionTests(TestCase):
    def test_fixed_editorial_wire_matches_transport_and_never_has_personal_terms(self):
        fixture = fixtures.MediaAuthorizedTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        document = 'query Deck{books(limit:6,order_by:[{users_count:desc}]){id title slug}}'
        for source, category, operation, path, query, body in [
            ('tmdb_movie', 'movie', 'starter_movie_science', '/3/discover/movie',
             'include_adult=false&language=en-US&page=1&with_genres=878', b''),
            ('tmdb_tv', 'tv', 'starter_tv_fantasy', '/3/discover/tv',
             'include_adult=false&language=en-US&page=1&with_genres=10765', b''),
            ('hardcover', 'book', 'starter_books', '/v1/graphql', '',
             json.dumps({'query': document, 'variables': {}}, ensure_ascii=True).encode())]:
            request = initial_source_request(source, category, ())
            self.assertEqual(request.operation, operation)
            self.assertEqual((request.terms, request.ids), ((), ()))
            wire = media_operation(request)
            self.assertEqual((wire.path, wire.query, wire.body), (path, query, body))
            expected = {'method': 'POST' if source == 'hardcover' else 'GET',
                'origin': registered_source(source).origin, 'path': path,
                'query': query, 'body_digest': sha256(body).hexdigest()}
            self.assertEqual(wire.operation_digest, sha256(json.dumps(expected,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest())
            _, spawn, _ = fixture.fake_send(operation=wire)
            args = spawn.args[0]
            self.assertEqual(args[args.index('--url') + 1], wire.origin + path + ('?' + query if query else ''))
            if body:
                self.assertEqual(args[args.index('--data-binary') + 1].encode(), body)
            self.assertFalse(registered_source(source).requires_terms(operation))
            with self.assertRaises(EnhanceFault):
                replace(request, terms=('Hidden personal title',))
            with self.assertRaises(CatalogueError):
                replace(wire, term='Hidden personal title')
        with self.assertRaises(CatalogueError):
            MediaOperation('hardcover', 'starter_books')  # wider legacy projection forbidden

    def test_all_skipped_reserves_three_single_sends_and_seals_all_thirteen_outcomes(self):
        registry, query = empty_plan(KEYS)
        clock = Clock()
        sources = [('book', 'hardcover'), ('movie', 'tmdb_movie'), ('tv', 'tmdb_tv')]
        backends = {source: Backend(source, clock, [(200, rows(source, 9 if source != 'hardcover' else 6), ())])
                    for _, source in sources}
        collector = SourceCollection(tuple((category, (source,)) for category, source in sources),
            backends, clock=clock, isolated_fixture=True)
        seal = collector.collect(query, registry, CollectionBudget(initial_sends=3))
        self.assertEqual(len(seal.records), 18)
        self.assertEqual(tuple(outcome.category for outcome in seal.outcomes), KEYS)
        self.assertEqual(len(seal.outcomes), 13)
        self.assertNotIn('UNSELECTED_BODY', seal.material.decode() if hasattr(seal, 'material') else repr(seal.records))
        revisions = read_json(seal.source_revisions)
        self.assertEqual(revisions['_collection']['initial_sends_reserved'], 3)
        self.assertEqual(revisions['_collection']['sends'], 3)
        self.assertEqual(revisions['_collection']['retry_sends'], 0)
        for category, source in sources:
            spec = registered_source(source)
            self.assertEqual([operation for operation, _ in backends[source].sent], [spec.starter_operation])
            self.assertEqual(revisions[category + ':' + source]['selection_kind'], 'editorial_starter')
            self.assertEqual(revisions[category + ':' + source]['selection_label'], spec.starter_label)
            records = [r for r in seal.records if r['candidate']['category'] == category]
            self.assertEqual(len(records), 6)
            self.assertTrue(all(not r['candidate']['facts'] for r in records))
        self.assertEqual(collector.state.pending, {})
        self.assertTrue(collector.revalidate_collection(seal, query, registry))
        clock.sleep(600)
        with self.assertRaises(EnhanceFault):
            collector.revalidate_collection(seal, query, registry)

    def test_nonempty_terms_keep_search_and_never_fallback_after_failure(self):
        registry, query = plan(('movie',))
        clock = Clock()
        backend = Backend('tmdb_movie', clock, [(503, {}, ())])
        collector = SourceCollection((('movie', ('tmdb_movie',)),), {'tmdb_movie': backend},
            clock=clock, isolated_fixture=True)
        result = collector.collect(query, registry, CollectionBudget())
        self.assertEqual([name for name, _ in backend.sent], ['search_movie'])
        self.assertEqual(result.records, [])
        self.assertEqual(read_json(result.source_revisions)['_collection']['retry_sends'], 0)
        with self.assertRaises(EnhanceFault):
            SourceRequest('tmdb_movie', 'movie', 'search_movie')

    def test_starter_failure_is_terminal_and_no_extra_hardcover_request_is_planned(self):
        registry, query = empty_plan(('book',))
        clock = Clock()
        backend = Backend('hardcover', clock, [(200, {'errors': [{'message': 'UNTRUSTED'}]}, ())])
        collector = SourceCollection((('book', ('hardcover',)),), {'hardcover': backend},
            clock=clock, isolated_fixture=True)
        result = collector.collect(query, registry, CollectionBudget(initial_sends=1))
        self.assertEqual(result.records, [])
        self.assertEqual([name for name, _ in backend.sent], ['starter_books'])
        self.assertEqual(collector.state.pending, {})
        self.assertEqual(collector.state.cache, {})

    def test_permit_refuses_switch_to_editorial_with_a_nonempty_permitted_plan(self):
        _, nonempty = plan(('movie',)); _, empty = empty_plan(('movie',))
        backend = Backend('tmdb_movie', Clock(), [])
        starter = initial_source_request('tmdb_movie', 'movie', ())
        permit = backend.authorize(starter, nonempty, now=NOW)
        with self.assertRaises(EnhanceFault):
            permit.verify(starter, nonempty, NOW)
        permit = backend.authorize(starter, empty, now=NOW)
        permit.verify(starter, empty, NOW)
        search = initial_source_request('tmdb_movie', 'movie', ('Synthetic movie',))
        with self.assertRaises(EnhanceFault):
            backend.authorize(search, empty, now=NOW).verify(search, empty, NOW)

    def test_missing_rights_deny_before_send(self):
        registry, query = empty_plan(('movie',)); clock = Clock()
        backend = Backend('tmdb_movie', clock, [(200, rows('tmdb_movie'), ())])
        backend.rights = replace(backend.rights, permissions=tuple(
            (name, tuple(use for use in uses if use != 'fetch')) for name, uses in backend.rights.permissions))
        collector = SourceCollection((('movie', ('tmdb_movie',)),), {'tmdb_movie': backend},
            clock=clock, isolated_fixture=True)
        result = collector.collect(query, registry, CollectionBudget())
        self.assertEqual(backend.sent, [])
        self.assertEqual(result.records, [])
        self.assertEqual(result.outcomes[0].code, 'AUTHORITY_DENIED')

    def test_missing_starter_and_insufficient_all_category_capacity_send_nothing(self):
        registry, query = empty_plan(('artist',)); clock = Clock()
        backend = Backend('musicbrainz_artist', clock, [])
        result = SourceCollection((('artist', ('musicbrainz_artist',)),),
            {'musicbrainz_artist': backend}, clock=clock, isolated_fixture=True).collect(query, registry, CollectionBudget())
        self.assertEqual(result.outcomes[0].code, 'NO_GROUNDED_CANDIDATES')
        self.assertEqual(backend.sent, [])
        registry, query = empty_plan(('movie', 'tv'))
        backends = {s: Backend(s, clock, []) for s in ('tmdb_movie', 'tmdb_tv')}
        result = SourceCollection((('movie', ('tmdb_movie',)), ('tv', ('tmdb_tv',))),
            backends, clock=clock, isolated_fixture=True).collect(query, registry, CollectionBudget(initial_sends=1))
        self.assertEqual(result.records, [])
        self.assertTrue(all(not b.sent for b in backends.values()))

    def test_hardcover_starter_rejects_extra_fields_duplicate_ids_and_oversized_rows(self):
        from shelf.source_adapters import SourceReply
        request = initial_source_request('hardcover', 'book', ())
        for document in [rows('hardcover', 7), {'data': {'books': [{'id': 1, 'title': 'X',
            'slug': 'x', 'description': 'UNSELECTED'}]}}, {'data': {'books': [
            {'id': 1, 'title': 'X', 'slug': 'x'}, {'id': 1, 'title': 'Y', 'slug': 'y'}]}}]:
            reply = SourceReply(request.request_digest, 200, canonical_bytes(document), NOW)
            with self.assertRaises(EnhanceFault):
                normalize(registered_source('hardcover'), request, reply, now=NOW)
