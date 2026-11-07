"""Source-contract normalization and bounded reads; no network or credentials."""
import json
from copy import deepcopy
from itertools import permutations
from unittest import TestCase
from unittest.mock import patch

from shelf import book_metadata as books, intake, provider_catalogue as p
from shelf.tests.graphic_novel_fixtures import rows


def cards(source=None):
    return p.parse_candidates('hardcover', 'book', rows() if source is None else source,
                              lane='synthetic-books', lane_label='SYNTHETIC source')


class BookMetadataTests(TestCase):
    def test_only_documented_integer_category_classifies_no_title_or_adaptation_guess(self):
        for value, status, label in ((4, 'present', 'Graphic novel'), (1, 'present', 'Book'),
                (10, 'present', 'Light novel'), (99, 'unsupported', ''),
                (None, 'absent', ''), ('4', 'invalid', ''), (True, 'invalid', ''), ({}, 'invalid', '')):
            row = dict(rows()[0], book_category_id=value, title='Graphic novel Superman film manga')
            category = cards([row])[0].book_metadata['category']
            self.assertEqual((category['status'], category['label']), (status, label))
        row.pop('book_category_id')
        self.assertEqual(cards([row])[0].book_metadata['category']['status'], 'unknown')
        conflicting = dict(row, book_category_id=1)
        self.assertEqual(cards([dict(row, book_category_id=4), conflicting])[0].book_metadata['category']['status'], 'invalid')

    def test_unknown_absent_and_invalid_optional_metadata_preserve_useful_book(self):
        base = {'id': 1, 'title': 'SYNTHETIC old book', 'slug': 'synthetic-old'}
        for changes, expected in (({}, 'unknown'),
                ({'image': None, 'description': None, 'book_series': None, 'editions': None}, 'absent'),
                ({'image': [], 'description': {}, 'book_series': 'bad', 'editions': {}}, 'invalid')):
            card = cards([dict(base, **changes)])[0]
            self.assertEqual(card.key, 'hardcover:book:1')
            for field in ('cover', 'synopsis', 'series', 'editions'):
                self.assertEqual(card.book_metadata[field]['status'], expected)
        self.assertEqual(cards([dict(base, image={}, book_series=[], editions=[])])[0].book_metadata['cover']['status'], 'unknown')

    def test_series_identity_fractional_null_compilation_and_subordinate_edition_dedup(self):
        parsed = cards()
        groups = books.series_groups([card.data() for card in parsed])
        self.assertEqual([g['id'] for g in groups], [73001, 73002])
        self.assertEqual([w['key'] for w in groups[0]['works']], ['hardcover:book:94002', 'hardcover:book:94003', 'hardcover:book:94004'])
        self.assertEqual(parsed[2].book_metadata['series']['items'][0]['position'], 2.5)
        omnibus = parsed[3].book_metadata
        self.assertEqual(omnibus['series']['items'][0]['position_status'], 'absent')
        self.assertTrue(omnibus['series']['items'][0]['compilation'])
        self.assertEqual(omnibus['editions']['items'], [])
        self.assertEqual(len(parsed[0].book_metadata['editions']['items']), 2)
        self.assertEqual(len(parsed), 6)
        self.assertEqual(len({c.key for c in parsed}), 6)

    def test_bad_membership_position_and_edition_parent_not_reinterpreted(self):
        row = rows()[1]
        row['book_series'][0].update(position=float('nan'), compilation='false')
        row['book_series'].append(dict(row['book_series'][0], series_id=999))
        row['editions'] = rows()[0]['editions']
        book = cards([row])[0].book_metadata
        self.assertEqual(book['series']['status'], 'partial')
        self.assertEqual(book['series']['items'][0]['position_status'], 'invalid')
        self.assertIsNone(book['series']['items'][0]['compilation'])
        self.assertEqual(book['editions']['items'], [])
        json.dumps(book, allow_nan=False)

    def test_duplicate_series_conflicts_are_field_wise_sticky_and_order_independent(self):
        row = rows()[1]
        original = row['book_series'][0]
        agreed = books.parse(row)['series']['items'][0]
        for field, alternatives in (
                ('name', ('SYNTHETIC other name', 'SYNTHETIC third name')),
                ('slug', ('synthetic-other', 'synthetic-third')),
                ('details', ('SYNTHETIC other details', 'SYNTHETIC third details')),
                ('position', (2.5, None)), ('compilation', (True, None))):
            variants = [deepcopy(original) for _ in range(3)]
            for member, value in zip(variants[1:], alternatives):
                target = member['series'] if field in ('name', 'slug') else member
                target[field] = value
            expected = deepcopy(agreed)
            if field in ('position', 'compilation'):
                expected[field], expected[field + '_status'] = None, 'ambiguous'
            else:
                expected[field] = {'status': 'ambiguous', 'text': ''}
            # Both orders, three distinct rows, and repetitions of either side.
            for members in (variants[:2], variants, [variants[0], variants[1], variants[0]],
                            [variants[0], variants[1], variants[1]]):
                for ordered in permutations(members):
                    with self.subTest(field=field, rows=ordered):
                        metadata = books.parse(dict(row, book_series=list(ordered)))['series']
                        self.assertEqual(metadata['status'], 'partial')
                        self.assertEqual(metadata['items'], [expected])

    def test_duplicate_series_agreement_retains_true_false_and_nullable_position(self):
        row = rows()[1]
        for compilation in (True, False):
            for position in (1, 2.5, None):
                member = dict(row['book_series'][0], compilation=compilation, position=position)
                for count in (2, 3):
                    with self.subTest(compilation=compilation, position=position, count=count):
                        single = books.parse(dict(row, book_series=[member]))['series']
                        repeated = books.parse(dict(row, book_series=[member] * count))['series']
                        self.assertEqual(repeated, single)
                        for ordered in permutations([member, dict(member, details='SYNTHETIC conflict'), member]):
                            merged = books.parse(dict(row, book_series=list(ordered)))['series']['items'][0]
                            self.assertEqual(merged['compilation'], compilation)
                            self.assertEqual(merged['compilation_status'], 'present')
                            self.assertEqual(merged['position'], position)
                            self.assertEqual(merged['position_status'], 'absent' if position is None else 'present')

    def test_duplicate_editions_merge_description_and_canonical_independently(self):
        row = rows()[0]
        original = dict(row['editions'][0], canonical_id=85000)
        agreed = books.parse(dict(row, editions=[original]))['editions']['items'][0]
        for source_field, field, alternatives, empty in (
                ('edition_format', 'format', ('SYNTHETIC hardcover', 'SYNTHETIC digital'), {'text': ''}),
                ('edition_information', 'information', ('SYNTHETIC revised', 'SYNTHETIC expanded'), {'text': ''}),
                ('canonical_id', 'canonical', (85001, None), {'id': None})):
            variants = [original] + [dict(original, **{source_field: value}) for value in alternatives]
            expected = dict(agreed, **{field: {'status': 'ambiguous', **empty}})
            for editions in (variants[:2], variants, [variants[0], variants[1], variants[0]],
                             [variants[0], variants[1], variants[1]]):
                for ordered in permutations(editions):
                    with self.subTest(field=field, rows=ordered):
                        metadata = books.parse(dict(row, editions=list(ordered)))['editions']
                        self.assertEqual(metadata['status'], 'partial')
                        self.assertEqual(metadata['items'], [expected])

    def test_duplicate_missing_absent_invalid_values_cannot_restore_a_conflicting_fact(self):
        row = rows()[1]
        for field, invalid in (('position', 'one'), ('compilation', 'false'), ('details', 12)):
            present = row['book_series'][0]
            missing = {k: v for k, v in present.items() if k != field}
            for uncertain in (missing, dict(present, **{field: None}), dict(present, **{field: invalid})):
                for ordered in permutations([present, uncertain, present]):
                    merged = books.parse(dict(row, book_series=list(ordered)))['series']['items'][0]
                    status = merged[field]['status'] if field == 'details' else merged[field + '_status']
                    self.assertEqual(status, 'ambiguous')
        row = rows()[0]
        for field, normalized, invalid in (('edition_format', 'format', 12),
                ('edition_information', 'information', []), ('canonical_id', 'canonical', '85000')):
            present = dict(row['editions'][0], canonical_id=85000)
            missing = {k: v for k, v in present.items() if k != field}
            for uncertain in (missing, dict(present, **{field: None}), dict(present, **{field: invalid})):
                for ordered in permutations([present, uncertain, present]):
                    merged = books.parse(dict(row, editions=list(ordered)))['editions']['items'][0]
                    self.assertEqual(merged[normalized]['status'], 'ambiguous')

    def test_conflicting_duplicates_preserve_work_identity_grouping_and_provider_rank(self):
        graphic = rows()[1]
        member = graphic['book_series'][0]
        prose = dict(deepcopy(graphic), id=96001, book_category_id=1, canonical_id=graphic['id'])
        prose['book_series'][0]['book_id'] = prose['id']
        other = rows()[2]
        for ordered in permutations([member, dict(member, compilation=True), member]):
            source = [prose, other, dict(graphic, book_series=list(ordered))]
            parsed = cards(source)
            self.assertEqual([card.key for card in parsed],
                             ['hardcover:book:96001', 'hardcover:book:94003', 'hardcover:book:94002'])
            self.assertEqual([card.book_metadata['category']['id'] for card in parsed], [1, 4, 4])
            groups = books.series_groups([card.data() for card in parsed])
            self.assertEqual([group['id'] for group in groups], [73001])
            self.assertEqual([work['key'] for work in groups[0]['works']], [card.key for card in parsed])

    def test_canonical_references_conflicts_cycles_do_not_rekey_or_merge_works(self):
        source = rows()[:3]
        source[0]['canonical_id'], source[1]['canonical_id'] = 94002, 94001
        source[2]['canonical_id'] = 99999
        parsed = cards(source)
        self.assertEqual([c.key for c in parsed], ['hardcover:book:94001', 'hardcover:book:94002', 'hardcover:book:94003'])
        self.assertEqual([c.book_metadata['canonical']['status'] for c in parsed], ['cycle', 'cycle', 'present'])
        self.assertEqual(parsed[2].book_metadata['canonical']['id'], 99999)
        parsed = cards([source[2], dict(source[2], canonical_id=88888)])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].book_metadata['canonical']['status'], 'ambiguous')
        row = rows()[0]
        row['editions'] = [dict(row['editions'][0], canonical_id=84002),
                           dict(row['editions'][2], canonical_id=84001)]
        self.assertEqual([e['canonical']['status'] for e in cards([row])[0].book_metadata['editions']['items']], ['cycle', 'cycle'])

    def test_external_cover_links_validate_without_network_or_official_link_guess(self):
        for url in ('javascript:alert(1)', '//evil.org/a', 'http://example.org/a',
                    'https://user:pass@example.org/a', 'https://example.org:999/a',
                    'https://127.0.0.1/a', 'https://[::1]/a', 'https://localhost/a',
                    'https://host.local/a', 'https://example.org/\\evil', 'https://example.org/\nx',
                    'https://example.org/a#fragment', 'https://example.org:<bad>/x'):
            self.assertEqual(books.cover_url(url), '', url)
        url = 'https://covers.example.org/book.jpg?width=300&height=400'
        self.assertEqual(books.cover_url(url), url)
        row = dict(rows()[0], links={'publisher': 'https://invented.example.org', 'preview': 'javascript:x'})
        self.assertNotIn('links', cards([row])[0].book_metadata)

    def test_source_filtered_browse_single_call_page_budget_and_no_disabled_operators(self):
        _, command = p._command('book_category', {'query': '50%_\\ title', 'page': 2})
        body = json.loads(command[command.index('--data-binary') + 1])
        self.assertIn('book_category_id:{_eq:4}', body['query'])
        self.assertEqual(body['variables'], {'offset': 9})
        for disabled in ('_like', '_ilike', '_regex', '_similar', '$title', '50%'):
            self.assertNotIn(disabled, body['query'])
        self.assertIn('limit:9', body['query'])
        self.assertIn('book_series(limit:12)', body['query'])
        with patch.object(p, '_request', return_value={'books': rows()}) as source:
            result, more = p.browse_books('graphic_novels', actor_id=1, profile_id=1)
        source.assert_called_once_with('book_category', {'query': '', 'page': 1}, actor_id=1, profile_id=1)
        self.assertEqual(len(result), 6)
        self.assertFalse(more)
        for category, page in (('unknown', 1), ('graphic_novels', 0), ('graphic_novels', 11), ('graphic_novels', True)):
            with patch.object(p, '_request') as source, self.assertRaises(p.CatalogueError):
                p.browse_books(category, page=page, actor_id=1, profile_id=1)
            source.assert_not_called()
        for category in (1, None, '4'):
            with patch.object(p, '_request', return_value={'books': [dict(rows()[0], book_category_id=category)]}), self.assertRaises(p.CatalogueError):
                p.browse_books('graphic_novels', actor_id=1, profile_id=1)

    def test_title_filter_is_literal_case_insensitive_and_only_filters_current_category_page(self):
        source_rows = [dict(rows()[0], id=97000+i, slug=f'synthetic-page-{i}',
                            title='SYNTHETIC ordinary title', editions=[]) for i in range(9)]
        source_rows[2]['title'] = 'SYNTHETIC 50%_\\ Title'
        with patch.object(p, '_request', return_value={'books': source_rows}) as source:
            result, more = p.browse_books('graphic_novels', '  50%_\\ TITLE  ', 2,
                                          actor_id=1, profile_id=1)
        self.assertEqual([card.source_id for card in result], [97002])
        self.assertTrue(more)
        self.assertEqual(source.call_count, 1)
        with patch.object(p, '_request', return_value={'books': source_rows}):
            result, more = p.browse_books('graphic_novels', 'not present', 2,
                                          actor_id=1, profile_id=1)
        self.assertEqual(result, [])
        self.assertTrue(more)
        with patch.object(p, '_request', return_value={'books': source_rows}):
            _, more = p.browse_books('graphic_novels', 'not present', 10,
                                      actor_id=1, profile_id=1)
        self.assertFalse(more)

    def test_title_filter_validates_before_io_and_cannot_hide_wrong_category(self):
        for invalid in (None, True, '   ', 'x'*101, 'a\nb'):
            with patch.object(p, '_request') as source, self.assertRaises(p.CatalogueError):
                p.browse_books('graphic_novels', invalid, actor_id=1, profile_id=1)
            source.assert_not_called()
        invalid_rows = [dict(rows()[0], book_category_id=1, title='does not match')]
        with patch.object(p, '_request', return_value={'books': invalid_rows}), self.assertRaises(p.CatalogueError):
            p.browse_books('graphic_novels', 'unmatched', actor_id=1, profile_id=1)

    def test_hydration_preserves_search_neighbor_rank_and_old_cached_identity(self):
        source = rows()[:3]
        ids = [94003, 94001, 94002, 94001]
        with patch.object(p, '_request', side_effect=[{'search': {'ids': ids}}, {'books': source}]):
            result = p.search('book', 'SYNTHETIC', actor_id=1, profile_id=1)
        self.assertEqual([c.source_id for c in result], [94003, 94001, 94002])
        root = cards()[3].data()
        with patch.object(p, '_request', side_effect=[{'books_by_pk': {'cached_similar_book_ids': ids}}, {'books': source}]):
            neighbors = p.neighbors(root, actor_id=1, profile_id=1)
        self.assertEqual([c.source_id for c in neighbors], [94003, 94001, 94002])
        state = intake.new_state(1, 1)
        old = result[0].data()
        old.pop('book_metadata')
        old['lane'] = 'legacy-root-lane'
        state['candidates'][old['key']] = old
        before_expiry = state['expires_at']
        intake.merge(state, result)
        self.assertEqual(state['candidates'][old['key']]['lane'], 'legacy-root-lane')
        self.assertEqual(state['expires_at'], before_expiry)
        self.assertEqual(state['candidates'][old['key']]['book_metadata']['category']['id'], 4)
