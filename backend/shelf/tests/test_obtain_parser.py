"""Offline focused parser tests using supplied public DTO shape."""
import json
from copy import deepcopy
from unittest import TestCase
from shelf import obtain_parser as parser
from shelf.acquisition_contract import ContractError

def item(title='Fiction Film 2024 1080p', guid='-9223372036854775808', category='2000', **changes):
    return {'title': title, 'guid': guid, 'id': guid,
        'link': 'http://private.invalid/getnzb?apikey=SYNTHETIC_SECRET',
        'description': '<b>SYNTHETIC_SECRET</b>',
        'enclosure': {'@attributes': {'length': '12345', 'type': 'application/x-nzb',
            'url': 'http://private.invalid/getnzb?apikey=SYNTHETIC_SECRET'}},
        'attr': [{'@attributes': {'name': 'category', 'value': category}},
                 {'@attributes': {'name': 'hydraIndexerName', 'value': 'Fiction Index'}},
                 {'@attributes': {'name': 'hydraIndexerHost', 'value': 'http://private.invalid'}}], **changes}


def payload(*rows):
    return json.dumps({'channel': {'item': list(rows)}}).encode()


class ParserTests(TestCase):
    def test_one_unsafe_title_does_not_hide_nineteen_distinct_results(self):
        safe = [item(guid=str(i), title=f'Fiction Film 2024 edition {i}') for i in range(19)]
        for title in (None, 1, [], {}, '', ' ', '<script>secret</script>',
                      'Film\x00secret', 'Film\ud800', 'Film\u202esecret',
                      'Film https://secret', 'Fiction Film ' + 'x' * 201):
            for position in (0, 10, 19):
                rows = safe.copy()
                rows.insert(position, item(guid='20', title=title))
                with self.subTest(title=title, position=position):
                    results, omitted = parser.parse(payload(*rows), title='Fiction Film', scope='film')
                    self.assertEqual(omitted, 1)
                    self.assertEqual([row['guid'] for row in results], [row['guid'] for row in safe])
                    self.assertEqual([row['title'] for row in results], [row['title'] for row in safe])
                    self.assertTrue(all(row['selectable'] for row in results))

    def parse(self, *rows, title='Fiction Film', scope='film'):
        return parser.parse(payload(*rows), title=title, scope=scope)[0]

    def test_unsafe_indexer_labels_omit_only_their_rows(self):
        for label in (None, 1, [], {}, '', '<b>secret</b>', 'https://secret',
                      'index\x00secret', 'index\udfff', 'x' * 101, 'x' * 4097):
            unsafe = item(guid='2')
            unsafe['attr'][1]['@attributes']['value'] = label
            with self.subTest(label=label):
                rows, omitted = parser.parse(payload(item(), unsafe), title='Fiction Film', scope='film')
                self.assertEqual(omitted, 1)
                self.assertEqual(rows, self.parse(item()))
        # Absence is distinct from a present label that cannot be displayed.
        row = item(attr=[])
        rows, omitted = parser.parse(payload(row), title='Fiction Film', scope='film')
        self.assertEqual(omitted, 0)
        self.assertEqual(rows[0]['indexer'], '')
        self.assertFalse(rows[0]['selectable'])

    def test_omission_count_empty_response_duplicates_and_exact_title(self):
        unsafe = item(title='<b>secret</b>')
        self.assertEqual(parser.parse(payload(unsafe, unsafe), title='Fiction Film', scope='film'), ([], 2))
        self.assertEqual(parser.parse(payload(), title='Fiction Film', scope='film'), ([], 0))
        title = '  Fiction Film 2024 1080p  '
        rows, omitted = parser.parse(payload(item(title=title), item(title=title)),
                                     title='Fiction Film', scope='film')
        self.assertEqual(omitted, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], title)
        self.assertTrue(rows[0]['selectable'])

    def test_display_omissions_cannot_hide_conflicting_duplicate_identities(self):
        safe = item(guid='1')
        unsafe = item(guid='1', title='<b>secret</b>')
        bad_indexer = deepcopy(safe)
        bad_indexer['attr'][1]['@attributes']['value'] = '<b>secret</b>'
        conflicts = [(safe, unsafe), (safe, bad_indexer),
                     (unsafe, item(guid='1', title='<i>different</i>'))]
        absent_indexer = item(guid='1', attr=[])
        empty_indexer = item(guid='1', attr=[{'@attributes': {'name': 'hydraIndexerName', 'value': ''}}])
        conflicts.append((absent_indexer, empty_indexer))
        for change in ('size', 'category', 'indexer', 'type', 'season', 'protection'):
            altered = deepcopy(unsafe)
            if change in ('size', 'type'):
                altered['enclosure']['@attributes'][{'size': 'length', 'type': 'type'}[change]] = '9'
            elif change == 'category':
                altered['attr'][0]['@attributes']['value'] = '3040'
            elif change == 'indexer':
                altered['attr'][1]['@attributes']['value'] = 'Different Index'
            else:
                altered['attr'].append({'@attributes': {
                    'name': 'password' if change == 'protection' else 'season', 'value': '1'}})
            conflicts.append((unsafe, altered))
        for first, second in conflicts:
            for pair in ((first, second), (second, first)):
                with self.subTest(pair=pair), self.assertRaises(ContractError):
                    self.parse(*pair, scope='album_flac')

    def test_unsafe_display_does_not_relax_structural_or_episode_validation(self):
        unsafe = item(title='<b>secret</b>', guid='2')
        malformed = [item(title='<b>secret</b>', guid='01'),
                     item(title='<b>secret</b>', id='other'),
                     item(title='<b>secret</b>', enclosure={}),
                     item(title='<b>secret</b>', category='bad'),
                     item(title='<b>secret</b>', attr=None),
                     item(title='<b>secret</b>', attr=[{}]),
                     item(title='<b>secret</b>', attr=unsafe['attr'] * 43),
                     item(title='<b>secret</b>', enclosure={'@attributes': {'length': '-1'}})]
        for name, value in (('season', '<bad>'), ('episode', 'x' * 11), ('guid', 'other'),
                            ('category', 3040), ('unknown', 'x' * 4097)):
            row = deepcopy(unsafe)
            row['attr'].append({'@attributes': {'name': name, 'value': value}})
            malformed.append(row)
        row = deepcopy(unsafe)
        row['attr'].append({'@attributes': {'name': 'hydraIndexerName', 'value': '<different>'}})
        malformed.append(row)
        for row in malformed:
            for rows in ((item(guid='3'), row), (row, item(guid='3'))):
                with self.subTest(row=row), self.assertRaises(ContractError):
                    self.parse(*rows)
        with self.assertRaises(ContractError):
            self.parse(*[item(title='<bad>', guid=str(i)) for i in range(21)])
        with self.assertRaises(ContractError):
            self.parse(item(title='x' * 65537))

    def test_public_dto_sanitizes_and_deduplicates_exact_signed_ids(self):
        row = item()
        results = self.parse(row, row)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['guid'], '-9223372036854775808')
        self.assertTrue(results[0]['selectable'])
        for secret in ('SYNTHETIC_SECRET', 'http', 'description', 'link', 'url', 'api', 'Host'):
            self.assertNotIn(secret, json.dumps(results))
        self.assertIn('Unknown', results[0]['edition'])
        self.assertIn('Unknown', results[0]['full_work'])

    def test_duplicate_json_keys_malformed_errors_types_bounds(self):
        invalid = [b'<error code="100"/>', b'{"error":"secret"}', b'{}',
                   b'{"channel":{"item":[],"item":[]}}', b'{', b'[]',
                   payload(*[item(guid=str(i)) for i in range(21)]), b' ' * 65537,
                   b'{"channel":{"item":null}}', b'{"channel":{"item":NaN}}']
        for raw in invalid:
            with self.subTest(raw=raw[:60]), self.assertRaises(ContractError):
                parser.parse(raw, title='Fiction Film', scope='film')
        self.assertEqual(self.parse(), [])

    def test_guid_types_mismatch_range_conflict(self):
        for row in (item(id='2'), item(guid=2), item(guid='9223372036854775808'),
                    item(guid='01'), item(guid='-0'), item(guid=True), item(guid='http://secret')):
            with self.subTest(row=row), self.assertRaises(ContractError):
                self.parse(row)
        with self.assertRaises(ContractError):
            self.parse(item(), item(title='Different title'))
        with self.assertRaises(ContractError):
            self.parse(item(attr=[{'@attributes': {'name': 'guid', 'value': '2'}}]))

    def test_html_urls_and_private_text_never_exposed(self):
        for title in ('<script>alert(1)</script>', 'Film &lt;script&gt;', 'Film https://secret',
                      'Film apikey=secret', 'Film\x00secret'):
            with self.subTest(title=title):
                self.assertEqual(parser.parse(payload(item(title=title)), title='Fiction Film',
                                              scope='film'), ([], 1))
        for enclosure in ({}, {'@attributes': {'length': 1}},
                          {'@attributes': {'length': '-2'}}, {'@attributes': {'length': '9'*20}}):
            with self.assertRaises(ContractError):
                self.parse(item(enclosure=enclosure))

    def test_title_year_ebook_torrent_and_category_are_not_verified_matches(self):
        self.assertEqual(self.parse(item(title='Other Film'))[0]['reason'], 'title_mismatch')
        self.assertEqual(self.parse(item(title='Fiction Film 2023'), title='Fiction Film 2024')[0]['reason'], 'title_mismatch')
        self.assertFalse(self.parse(item(category='5000'))[0]['selectable'])
        self.assertFalse(self.parse(item(title='Fiction Book ebook', category='3030'),
                                    title='Fiction Book', scope='audiobook')[0]['selectable'])
        self.assertFalse(self.parse(item(title='Fiction Book', category='7020'),
                                    title='Fiction Book', scope='audiobook')[0]['selectable'])
        row=item(); row['enclosure']['@attributes']['type']='application/x-bittorrent'
        self.assertEqual(self.parse(row)[0]['reason'], 'unsupported_type')

    def test_tv_requires_isolated_s01e01_evidence(self):
        good = self.parse(item(title='Fiction Series S01E01 1080p', category='5000'),
                          title='Fiction Series', scope='series_s01e01')[0]
        self.assertTrue(good['selectable'])
        for suffix in ('S01E01E02', 'S01E010', '1x01', '1x01-02', 'S01E01-E02',
                       'S01E01 02', 'S01E01 to 03', 'Season 1', 'S01E01 pack',
                       'S01E01 pilot', 'S01E02', 'S01E01 S01E02', 'S01E01 complete'):
            with self.subTest(suffix=suffix):
                self.assertFalse(self.parse(item(title='Fiction Series '+suffix, category='5000'),
                    title='Fiction Series', scope='series_s01e01')[0]['selectable'])
        row=item(title='Fiction Series S01E01', category='5000')
        row['attr'].append({'@attributes': {'name': 'episode', 'value': '2'}})
        self.assertFalse(self.parse(row, title='Fiction Series', scope='series_s01e01')[0]['selectable'])

    def test_tv_rejects_extra_episode_markers_and_numeric_continuations(self):
        for suffix in ('S01E01+E02', 'S01E01 + E02', 'S01E01 and E02', 'S01E01/02',
                       'S01E01/E02', 'S01E01, E02', 'S01E01 E02', 'S01E01 extra E02',
                       'S01E01+02', 'S01E01 + 02', 'S01E01 / 02', 'S01E01 and 02',
                       'S01E01,02', 'S01E01, 02', 'S01E01;02', 'S01E01; E02',
                       's01e01 AND e02', 's01e01 AND 02', 'S01E01 1080p E02'):
            with self.subTest(suffix=suffix):
                result = self.parse(item(title='Fiction Series '+suffix, category='5000'),
                    title='Fiction Series', scope='series_s01e01')[0]
                self.assertFalse(result['selectable'])
                self.assertEqual(result['reason'], 'ambiguous_episode')

    def test_tv_isolated_episode_resolution_controls_and_ampersand_rejection(self):
        for suffix in ('S01E01', 'S01E01 1080p', 'S01E01 720p', 'S01E01.1080p',
                       'S01E01-720p', 'S01E01/1080p', 'S01E01 + 720p'):
            with self.subTest(suffix=suffix):
                result = self.parse(item(title='Fiction Series '+suffix, category='5000'),
                    title='Fiction Series', scope='series_s01e01')[0]
                self.assertTrue(result['selectable'])
                self.assertEqual(result['reason'], '')
        # Ampersands fail the display boundary; those rows remain unselectable.
        for suffix in ('S01E01&E02', 'S01E01 & E02', 'S01E01&02', 'S01E01 & 02'):
            with self.subTest(suffix=suffix):
                self.assertEqual(parser.parse(payload(item(title='Fiction Series '+suffix, category='5000')),
                    title='Fiction Series', scope='series_s01e01'), ([], 1))


