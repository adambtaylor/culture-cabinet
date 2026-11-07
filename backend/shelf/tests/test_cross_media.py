"""Offline source contracts, reconstructed facts, and wholly fictional joins."""
from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import patch

from shelf.cross_media import (
    Cache, FIXTURE, InvalidData, MAX_ALIAS_HOPS, MAX_BYTES, MAX_NODES, MAX_PATH_STATES,
    SAMPLE_IDS, SCHEMA, canonical_url, hardcover_projection, identity, isbn_join,
    node, normalize_isbn, outcome, paths_from, resolve_alias, resolve_documents,
    sample_context, tmdb_projection,
)


def fixture():
    return json.loads(FIXTURE.read_text())


def claim(prop, value, *, rank='normal', statement=None):
    return {'id': statement, 'rank': rank, 'mainsnak': {'property': prop, 'snaktype': 'value', 'datavalue': {'value': value}}}


def synthetic_entity(qid, claims):
    return {'id': qid, 'lastrevid': 900000001, 'claims': claims}


def edge(source, target, kind='based-on', rank=0):
    return {'source': source, 'target': target, 'kind': kind, 'rank': rank, 'evidence': []}


def graph(keys, edges):
    return Cache({'schema': SCHEMA, 'nodes': [node(key, 'SYNTHETIC ' + key, provenance='synthetic') for key in keys],
                  'edges': edges, 'records': []})


class IdentityTests(TestCase):
    def test_namespaces_do_not_collapse_equal_numbers(self):
        keys = {identity('hardcover', 'book', 42), identity('tmdb', 'movie', 42), identity('tmdb', 'tv', 42),
                identity('wikidata', 'entity', 'Q42'), identity('openlibrary', 'work', 'OL42W'),
                identity('openlibrary', 'edition', 'OL42M'), identity('openlibrary', 'author', 'OL42A')}
        self.assertEqual(len(keys), 7)

    def test_strict_native_ids_and_ol_suffix(self):
        for value in (True, 0, -1, 2.5, '42', None, 2147483648):
            with self.subTest(value=value), self.assertRaises(InvalidData):
                identity('tmdb', 'movie', value)
        for medium, value in (('work', 'OL42A'), ('edition', 'OL42W'), ('author', 'OL42M')):
            with self.subTest(medium=medium), self.assertRaises(InvalidData):
                identity('openlibrary', medium, value)

    def test_safe_canonical_links(self):
        self.assertEqual(canonical_url('tmdb:tv:42'), 'https://www.themoviedb.org/tv/42')
        self.assertEqual(canonical_url('openlibrary:author:OL42A'), 'https://openlibrary.org/authors/OL42A')
        for slug in ('../../private', 'https://evil.test', '<script>', 'x?q=actor'):
            self.assertIsNone(canonical_url('hardcover:book:42', slug))
        for key in ('tmdb:movie:042', 'tmdb:movie:42?x=1', 'wikidata:entity:Q1/secret'):
            with self.assertRaises(InvalidData):
                canonical_url(key)

    def test_alias_chains_and_ambiguity(self):
        first, second, third = [f'hardcover:book:{i}' for i in (9001, 9002, 9003)]
        self.assertEqual(resolve_alias(first, {first: [second], second: [third]})['items'], [third])
        self.assertEqual(resolve_alias(first, {first: [second, third]})['status'], 'ambiguous')
        self.assertEqual(resolve_alias(first, {first: []})['status'], 'ambiguous')
        self.assertEqual(resolve_alias(first, {first: [second, second]})['items'], [second])

    def test_alias_cycle_depth_and_namespace(self):
        keys = [f'hardcover:book:{9000+i}' for i in range(MAX_ALIAS_HOPS+2)]
        self.assertEqual(resolve_alias(keys[0], {keys[0]: [keys[1]], keys[1]: [keys[0]]})['status'], 'failed')
        aliases = {a: [b] for a, b in zip(keys, keys[1:])}
        self.assertEqual(resolve_alias(keys[0], aliases)['status'], 'failed')
        with self.assertRaises(InvalidData):
            resolve_alias(keys[0], {keys[0]: ['tmdb:movie:9000']})


class CacheTests(TestCase):
    def test_distinct_outcomes_survive_restart(self):
        statuses = ('items', 'empty', 'ambiguous', 'failed', 'timeout', 'rate-limited')
        data = {'schema': SCHEMA, 'nodes': [], 'edges': [], 'records': [
            {'key': status, 'outcome': outcome(status, ['synthetic'] if status in ('items', 'ambiguous') else [],
                checked_at='2026-01-01T00:00:00+00:00', retry_at='2026-01-02T00:00:00+00:00' if status == 'rate-limited' else None)}
            for status in statuses]}
        cache = Cache.from_json(Cache(data).to_json())
        self.assertEqual(cache.snapshot(), data)
        self.assertEqual([r['outcome']['status'] for r in cache.snapshot()['records']], list(statuses))
        self.assertIsNone(cache.snapshot()['records'][0]['outcome']['expires_at'])

    def test_unknown_freshness_is_not_parse_time(self):
        cache = graph(['tmdb:movie:42'], [])
        restored = Cache.from_json(cache.to_json()).snapshot()['nodes'][0]
        self.assertIsNone(restored['fetched_at'])
        self.assertEqual(restored['freshness'], 'unknown')

    def test_snapshots_are_defensive_copies(self):
        original = graph(['tmdb:movie:42'], []).snapshot()
        cache = Cache(original)
        original['nodes'][0]['title'] = 'changed'
        copy = cache.snapshot()
        copy['nodes'].clear()
        self.assertEqual(len(cache.snapshot()['nodes']), 1)
        self.assertNotEqual(cache.snapshot()['nodes'][0]['title'], 'changed')

    def test_version_size_structure_duplicate_and_nonfinite_rejected(self):
        for payload in ('{"schema":"other"}', '{"schema":"x","schema":"x"}', '{"x":NaN}', '[]', '[' * 2000):
            with self.subTest(payload=payload[:30]), self.assertRaises(InvalidData):
                Cache.from_json(payload)
        with self.assertRaises(InvalidData):
            Cache.from_json(' ' * (MAX_BYTES+1))
        data = graph(['tmdb:movie:42'], []).snapshot()
        data['nodes'] *= MAX_NODES+1
        with self.assertRaises(InvalidData):
            Cache(data)

    def test_invalid_timestamp_and_outcome_combinations(self):
        for status, items in (('items', []), ('empty', ['x']), ('timeout', ['x']), ('429', [])):
            with self.subTest(status=status), self.assertRaises(InvalidData):
                outcome(status, items)
        with self.assertRaises(InvalidData):
            outcome('empty', checked_at='2026-01-01')

    def test_invalid_edges_and_noncanonical_urls_rejected(self):
        data = graph(['tmdb:movie:42'], []).snapshot()
        data['nodes'][0]['url'] = 'https://evil.test/'
        with self.assertRaises(InvalidData):
            Cache(data)
        data = graph(['tmdb:movie:42'], []).snapshot()
        data['edges'] = [edge('tmdb:movie:42', 'tmdb:movie:43')]
        with self.assertRaises(InvalidData):
            Cache(data)


class ProviderProjectionTests(TestCase):
    def test_hardcover_native_schema_rank_shuffled_missing_duplicate(self):
        book = {'id': 9000, 'cached_similar_book_ids': [9003, 9001, 9002, 9001, 9000],
                'canonical_id': None, 'cached_similar_books_updated_at': None}
        rows = [{'id': value, 'title': f'Wholly fictional {value}', 'slug': f'fictional-{value}'} for value in (9001, 9003, 9001)]
        result = hardcover_projection(book, rows, provenance='simulated')
        self.assertEqual([item['source_id'] for item in result['items']], [9003, 9001])
        self.assertEqual([item['provider_rank'] for item in result['items']], [0, 1])
        self.assertTrue(all(item['fetched_at'] is None for item in result['items']))
        self.assertIsNone(result['checked_at'])

    def test_hardcover_original_ranks_survive_duplicate_missing_shuffled_rows(self):
        book = {'id': 9000, 'cached_similar_book_ids': [9001, 9001, 9002, 9003],
                'canonical_id': None, 'cached_similar_books_updated_at': None}
        rows = [{'id': value, 'title': f'Wholly fictional {value}', 'slug': f'fictional-{value}'}
                for value in (9003, 9001)]
        result = hardcover_projection(book, rows, provenance='simulated')
        self.assertEqual([item['source_id'] for item in result['items']], [9001, 9003])
        self.assertEqual([item['provider_rank'] for item in result['items']], [0, 3])

    def test_hardcover_retains_rank_gaps_and_supplied_timestamp(self):
        book = {'id': 9000, 'cached_similar_book_ids': [9003, 9001, 9002],
                'canonical_id': 9004, 'cached_similar_books_updated_at': '2026-01-01T00:00:00+00:00'}
        result = hardcover_projection(book, [{'id': 9002, 'title': 'SYNTHETIC', 'slug': 'synthetic'}])
        self.assertEqual(result['items'][0]['provider_rank'], 2)
        self.assertEqual(result['items'][0]['fetched_at'], book['cached_similar_books_updated_at'])

    def test_hardcover_invalid_json_ids_and_nullable_metadata(self):
        for ids in (None, '[1,2]', [True], [0], ['1'], [2147483648]):
            with self.subTest(ids=ids), self.assertRaises(InvalidData):
                hardcover_projection({'id': 9000, 'cached_similar_book_ids': ids}, [])
        self.assertEqual(hardcover_projection({'id': 9000, 'cached_similar_book_ids': []}, [])['status'], 'empty')
        with self.assertRaises(InvalidData):
            hardcover_projection({'id': 9000, 'cached_similar_book_ids': [9001]}, [{'id': 9002}])

    def test_tmdb_recommendation_vs_similar_and_tv_identity(self):
        payload = {'results': [{'id': 9001, 'name': 'SYNTHETIC TV'}, {'id': 9001, 'name': 'duplicate'}]}
        recommendation = tmdb_projection('tv', payload)
        similar = tmdb_projection('tv', payload, relation='tmdb-similar')
        self.assertEqual(recommendation['items'][0]['key'], 'tmdb:tv:9001')
        self.assertEqual(len(recommendation['items']), 1)
        self.assertNotEqual(recommendation['items'][0]['relation'], similar['items'][0]['relation'])
        self.assertIsNone(recommendation['items'][0]['fetched_at'])
        self.assertEqual(tmdb_projection('movie', {'results': []})['status'], 'empty')


class ResolverTests(TestCase):
    def research(self):
        data = fixture()
        return resolve_documents(data['entities'], redirects=data['redirects'], external_ids=data['external_ids'], provenance='reconstructed')

    def test_dune_novel_film_and_exact_relation(self):
        cache = self.research()
        titles = {item['key']: item['title'] for item in cache.snapshot()['nodes']}
        self.assertEqual(titles['wikidata:entity:Q60834962'], 'Dune (film)')
        paths = paths_from(cache, 'wikidata:entity:Q190192')
        film = next(p for p in paths if p['key'] == 'tmdb:movie:438631')
        self.assertEqual(film['fact_hops'], 1)
        self.assertEqual(film['path'][0]['kind'], 'based-on')
        self.assertEqual(film['path'][0]['traversal'], 'reverse')
        self.assertEqual(film['path'][0]['source'], 'wikidata:entity:Q60834962')
        self.assertEqual(len(film['path'][0]['evidence']), 2)

    def test_messiah_tv_children_many_to_many(self):
        paths = paths_from(self.research(), 'wikidata:entity:Q437383')
        tv = next(p for p in paths if p['key'] == 'tmdb:tv:9156')
        children = next(p for p in paths if p['key'] == 'wikidata:entity:Q437365')
        self.assertEqual(tv['fact_hops'], 1)
        self.assertEqual(children['fact_hops'], 2)
        self.assertEqual([step['traversal'] for step in children['path']], ['reverse', 'forward'])
        self.assertTrue(all(step['kind'] == 'based-on' for step in children['path']))

    def test_redirect_deprecated_movie_tv_trap(self):
        data = self.research().snapshot()
        keys = {item['key'] for item in data['nodes']}
        self.assertIn('tmdb:tv:19566', keys)
        self.assertIn('wikidata:entity:Q987305', keys)
        self.assertNotIn('wikidata:entity:Q16515173', keys)
        self.assertNotIn('tmdb:movie:911852', keys)
        self.assertNotIn('tmdb:movie:19566', keys)

    def test_reconstructed_evidence_remains_unknown_after_json_restart(self):
        cache = Cache.from_json(self.research().to_json())
        for edge_data in cache.snapshot()['edges']:
            for evidence in edge_data['evidence']:
                self.assertEqual(evidence['provenance'], 'reconstructed')
                for key in ('revision', 'statement_id', 'checked_at'):
                    self.assertIsNone(evidence[key])

    def test_synthetic_statement_revision_rank_and_dedup_preserved(self):
        docs = [synthetic_entity('Q990000001', {'P144': [claim('P144', {'id': 'Q990000002'}, statement='SYNTHETIC$one', rank='preferred')]}),
                synthetic_entity('Q990000002', {'P4969': [claim('P4969', {'id': 'Q990000001'}, statement='SYNTHETIC$two')]})]
        data = resolve_documents(docs, provenance='synthetic').snapshot()
        self.assertEqual(len(data['edges']), 1)
        evidence = data['edges'][0]['evidence']
        self.assertEqual([e['statement_id'] for e in evidence], ['SYNTHETIC$one', 'SYNTHETIC$two'])
        self.assertEqual(evidence[0]['revision'], 900000001)
        self.assertEqual(evidence[0]['rank'], 'preferred')
        with self.assertRaises(InvalidData):
            resolve_documents(docs, provenance='reconstructed')

    def test_source_revision_is_not_limited_to_provider_int32(self):
        doc = synthetic_entity('Q990000001', {'P4947': [claim('P4947', '990000001', statement='SYNTHETIC$revision')]})
        doc['lastrevid'] = 4000000000
        cache = resolve_documents([doc], provenance='synthetic')
        evidence = Cache.from_json(cache.to_json()).snapshot()['edges'][0]['evidence'][0]
        self.assertEqual(evidence['revision'], 4000000000)

    def test_multiple_ol_work_edition_author_are_distinct(self):
        doc = synthetic_entity('Q990000001', {'P648': [claim('P648', value) for value in ('OL990000001W', 'OL990000002W', 'OL990000001M', 'OL990000001A')]})
        keys = {n['key'] for n in resolve_documents([doc], provenance='synthetic').snapshot()['nodes']}
        self.assertIn('openlibrary:author:OL990000001A', keys)
        self.assertIn('openlibrary:edition:OL990000001M', keys)
        self.assertEqual(len(keys), 5)
        self.assertFalse(any(key.startswith('hardcover:') for key in keys))

    def test_deprecated_claim_dropped_before_even_invalid_value(self):
        doc = synthetic_entity('Q990000001', {'P4947': [claim('P4947', 'invalid', rank='deprecated')]})
        self.assertEqual(resolve_documents([doc], provenance='synthetic').snapshot()['edges'], [])

    def test_redirect_cycles_ambiguity_and_depth_fail_explicitly(self):
        doc = {'id': 'Q990000001', 'claims': {}}
        for redirects in (
            [{'from': 'Q990000001', 'to': 'Q990000002'}, {'from': 'Q990000002', 'to': 'Q990000001'}],
            [{'from': 'Q990000001', 'to': 'Q990000002'}, {'from': 'Q990000001', 'to': 'Q990000003'}],
            [{'from': f'Q{990000001+i}', 'to': f'Q{990000002+i}'} for i in range(MAX_ALIAS_HOPS+1)]):
            with self.assertRaises(InvalidData):
                resolve_documents([doc], redirects=redirects)

    def test_external_ids_missing_and_invalid(self):
        data = resolve_documents([], external_ids=[{'medium': 'tv', 'id': 9000, 'external_ids': {'wikidata_id': None}}]).snapshot()
        self.assertEqual(data['records'][0]['outcome']['status'], 'empty')
        with self.assertRaises(InvalidData):
            resolve_documents([], external_ids=[{'medium': 'tv', 'id': 9000, 'external_ids': {'wikidata_id': 'not-Q'}}])

    def test_unknown_snak_is_not_a_false_relation(self):
        doc = {'id': 'Q990000001', 'claims': {'P144': [{'mainsnak': {'snaktype': 'somevalue'}}]}}
        self.assertEqual(resolve_documents([doc]).snapshot()['edges'], [])


class IsbnTests(TestCase):
    def case(self):
        data = fixture()['fictional_isbn_case']
        data.pop('label')
        return data

    def test_checksum_normalization_equivalence(self):
        self.assertEqual(normalize_isbn('0-000-00000-0'), '9780000000002')
        self.assertEqual(normalize_isbn('978 0000000002'), '9780000000002')
        # Checksum arithmetic only; no real catalogue identity is asserted.
        self.assertEqual(normalize_isbn('000000006X'), '9780000000064')
        for value in ('0000000001', '9780000000001', '978000000000X', '00000000X0', None, True, '9780000000002\n'):
            self.assertIsNone(normalize_isbn(value))

    def test_wholly_fictional_edition_book_canonical_join(self):
        result = isbn_join(**self.case())
        self.assertEqual(result['items'], ['hardcover:book:990000002'])

    def test_many_to_many_ambiguity_then_canonical_convergence(self):
        data = self.case()
        data['hardcover_editions'].append({'book_id': 990000003, 'isbn_10': '0000000000', 'isbn_13': None})
        data['books'].append({'id': 990000003, 'canonical_id': None})
        self.assertEqual(isbn_join(**data)['status'], 'ambiguous')
        data['books'][-1]['canonical_id'] = 990000002
        self.assertEqual(isbn_join(**data)['items'], ['hardcover:book:990000002'])

    def test_alias_conflict_cycle_no_title_join_or_invented_ol_field(self):
        data = self.case()
        data['books'].append({'id': 990000001, 'canonical_id': 990000003})
        self.assertEqual(isbn_join(**data)['status'], 'ambiguous')
        data = self.case()
        data['books'][1]['canonical_id'] = 990000001
        self.assertEqual(isbn_join(**data)['status'], 'failed')
        data = self.case()
        data['hardcover_editions'][0].update(isbn_13=None, title='Dune', open_library='OL990000001W')
        self.assertEqual(isbn_join(**data)['status'], 'empty')

    def test_author_cannot_be_joined_as_work(self):
        data = self.case()
        data['work_id'] = 'OL990000001A'
        with self.assertRaises(InvalidData):
            isbn_join(**data)

    def test_missing_work_and_wrong_edition_work_do_not_join(self):
        data = self.case()
        data['works'] = []
        self.assertEqual(isbn_join(**data)['status'], 'empty')
        data = self.case()
        data['editions'][0]['works'] = [{'key': '/works/OL990000002W'}]
        self.assertEqual(isbn_join(**data)['status'], 'empty')


class PathTests(TestCase):
    def test_dense_identity_cycles_have_deterministic_frontier_budget(self):
        keys = [f'wikidata:entity:Q{990000001+i}' for i in range(40)]
        cache = graph(keys, [edge(source, target, 'identity')
                            for i, source in enumerate(keys) for target in keys[i+1:]])
        results = paths_from(cache, keys[0])
        self.assertEqual(len(results), MAX_PATH_STATES)
        self.assertEqual(results, paths_from(Cache.from_json(cache.to_json()), keys[0]))

    def test_shared_author_is_not_an_identity_join_between_works(self):
        keys = ['wikidata:entity:Q990000001', 'openlibrary:author:OL990000001A',
                'wikidata:entity:Q990000002', 'openlibrary:work:OL990000002W']
        cache = graph(keys, [edge(keys[0], keys[1], 'identity'), edge(keys[2], keys[1], 'identity'),
                            edge(keys[2], keys[3], 'identity')])
        self.assertEqual({p['key'] for p in paths_from(cache, keys[0])}, {keys[1]})

    def test_one_neighbor_two_fact_hops_and_cycles(self):
        keys = [f'tmdb:movie:{9000+i}' for i in range(7)]
        edges = [edge(keys[0], keys[1], 'tmdb-recommendation'), edge(keys[1], keys[2], 'tmdb-recommendation'),
                 edge(keys[1], keys[3]), edge(keys[3], keys[4]), edge(keys[4], keys[5]),
                 edge(keys[4], keys[1], 'tmdb-recommendation')]
        results = paths_from(graph(keys, edges), keys[0])
        found = {p['key'] for p in results}
        self.assertIn(keys[4], found)
        self.assertNotIn(keys[2], found)
        self.assertNotIn(keys[5], found)
        for result in results:
            path_keys = [keys[0]] + [step['to_key'] for step in result['path']]
            self.assertEqual(len(path_keys), len(set(path_keys)))
            self.assertLessEqual(result['neighbor_hops'], 1)
            self.assertLessEqual(result['fact_hops'], 2)

    def test_identity_hops_do_not_hide_budget_or_infinite_cycles(self):
        keys = [f'wikidata:entity:Q{990000000+i}' for i in range(12)]
        cache = graph(keys, [edge(a, b, 'identity') for a, b in zip(keys, keys[1:])] + [edge(keys[-1], keys[0], 'identity')])
        results = paths_from(cache, keys[0])
        self.assertTrue(results)
        self.assertTrue(all(len(p['path']) <= 8 for p in results))
        self.assertTrue(all(p['fact_hops'] == 0 for p in results))


class SampleTests(TestCase):
    def test_fixed_ids_copies_safe_links_no_io_except_fixture(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
             patch('subprocess.Popen', side_effect=AssertionError('provider execution forbidden')):
            for trail_id in SAMPLE_IDS:
                context = sample_context(trail_id)
                self.assertEqual(context['schema'], 'cross-media-sample-v1')
                self.assertIn('not the owner', context['temporary_notice'])
                self.assertIsNone(context['root']['fetched_at'] if trail_id != 'synthetic-lanes' else None)
                for card in context['cards']:
                    self.assertTrue(card['url'].startswith(('https://www.wikidata.org/wiki/', 'https://www.themoviedb.org/')))
                context['cards'].clear()
                self.assertTrue(sample_context(trail_id)['cards'])

    def test_invalid_selectors_raise_without_opening_fixture(self):
        with patch('pathlib.Path.open', side_effect=AssertionError('selector must fail before I/O')):
            for selector in (None, '', '../dune', 'Dune', ['dune'], 'dune?actor=1'):
                with self.subTest(selector=selector), self.assertRaises(ValueError):
                    sample_context(selector)

    def test_known_works_have_no_fake_hardcover_or_isbn_bindings(self):
        data = fixture()
        self.assertFalse(any('isbn' in json.dumps(entity).lower() or 'hardcover' in json.dumps(entity).lower() for entity in data['entities']))
        for selector in ('dune', 'dune-messiah'):
            context = sample_context(selector)
            self.assertEqual(context['mapping_status'], 'unresolved')
            self.assertFalse(any(card['provider'] == 'hardcover' for card in context['cards']))

    def test_synthetic_five_lanes_and_outcome_labels(self):
        context = sample_context('synthetic-lanes')
        self.assertEqual(len(context['lanes']), 5)
        self.assertEqual(len(context['selection']), 6)
        self.assertEqual({item['status'] for item in context['outcomes']}, {'items', 'empty', 'ambiguous', 'failed', 'timeout', 'rate-limited'})
        self.assertTrue(all(item['label'].startswith('SIMULATED') for item in context['outcomes']))
