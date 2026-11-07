"""Album selection constraints use only fabricated listings and NZB text."""
import json
from unittest import TestCase

from shelf.acquisition_contract import Blocked, ContractError, parse_caps, search_request
from shelf.obtain_parser import parse
from shelf.nzb_parser import validate_nzb
from shelf.tests.test_acquisition_contract import CAPS
from shelf.tests.test_obtain_retrieval import NZB
from shelf.tests.test_obtain_ui import listing


class AlbumContractTests(TestCase):
    def test_album_nzb_refuses_known_segment_gaps_before_submission(self):
        import re
        original = re.search(rb'<segment[^>]*>.*?</segment>', NZB).group(0)
        for numbers in ((1, 3), (2,), (1, 10000)):
            segments = b''.join(re.sub(rb'number="[0-9]+"',
                ('number="%s"' % number).encode(), original) for number in numbers)
            payload = NZB.replace(original, segments)
            with self.subTest(numbers=numbers), self.assertRaises(ContractError):
                validate_nzb(payload, album=True)
            validate_nzb(payload)  # Other existing acquisition scopes are unchanged.
        contiguous = b''.join(re.sub(rb'number="[0-9]+"',
            ('number="%s"' % number).encode(), original) for number in (3, 1, 2))
        validate_nzb(NZB.replace(original, contiguous), album=True)

    def test_missing_categories_stay_unselectable_among_omitted_display_rows(self):
        row = listing(title='Fiction Artist - Fiction Album FLAC')
        row['attr'] = [attr for attr in row['attr'] if attr['@attributes']['name'] != 'category']
        unsafe = listing(title='<unsafe>', guid='2')
        rows, omitted = parse(json.dumps({'channel': {'item': [unsafe, row]}}).encode(),
                              title='Fiction Artist - Fiction Album', scope='album_flac')
        self.assertEqual(omitted, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['categories'], [])
        self.assertEqual(rows[0]['reason'], 'category_mismatch')
        self.assertFalse(rows[0]['selectable'])

    def test_music_search_needs_actual_lossless_category_and_text_capability(self):
        lossless = CAPS.replace(b'</categories>', b'<category id="3040" name="Lossless"/></categories>')
        for payload in (CAPS, lossless.replace(b'<search available="yes"', b'<search available="no"'),
                        lossless.replace(b'supportedParams="q"', b'supportedParams="artist,album"')):
            with self.assertRaises(Blocked):
                search_request(parse_caps(payload), 'Fiction Artist - Fiction Album', 'music')
        request = search_request(parse_caps(lossless), 'Fiction Artist - Fiction Album', 'music')
        self.assertEqual(request.params[-1], ('cat', '3040'))
        self.assertEqual(request.operation, 'search')
        music = parse_caps(lossless.replace(b'</searching>',
            b'<music-search available="yes" supportedParams="artist,album"/></searching>'))
        self.assertIn('music', music.query_types)

    def test_protection_attributes_and_ambiguous_album_listings_are_refused(self):
        for suffix in ('discography', 'box-set', 'albums', 'encrypted', 'passworded',
                       'single', 'partial', 'exe', 'cmd', 'MP3'):
            row = listing(title='Fiction Artist - Fiction Album FLAC ' + suffix, category='3040')
            if suffix == 'passworded':
                self.assertEqual(parse(json.dumps({'channel': {'item': [row]}}).encode(),
                          title='Fiction Artist - Fiction Album', scope='album_flac'), ([], 1))
                continue
            rows, omitted = parse(json.dumps({'channel': {'item': [row]}}).encode(),
                         title='Fiction Artist - Fiction Album', scope='album_flac')
            self.assertFalse(rows[0]['selectable'])
        for name in ('password', 'passworded', 'protected', 'encrypted'):
            row = listing(title='Fiction Artist - Fiction Album FLAC', category='3040')
            row['attr'].append({'@attributes': {'name': name, 'value': '1'}})
            rows, omitted = parse(json.dumps({'channel': {'item': [row]}}).encode(),
                         title='Fiction Artist - Fiction Album', scope='album_flac')
            self.assertFalse(rows[0]['selectable'])

    def test_nzb_protection_metadata_and_executable_subjects_are_rejected_for_album(self):
        validate_nzb(NZB, album=True)
        protected = NZB.replace(b'<file subject="fiction">',
                               b'<head><meta type="password">fixture</meta></head><file subject="fiction">')
        for payload in (protected, NZB.replace(b'subject="fiction"', b'subject="setup.exe"'),
                        NZB.replace(b'subject="fiction"', b'subject="encrypted album"')):
            with self.assertRaises(ContractError):
                validate_nzb(payload, album=True)
            validate_nzb(payload)  # Existing film/book NZB grammar is preserved.

    def test_album_exclusions_use_release_separator_boundaries(self):
        for separator in (' ', '_', '.', '-', '/', '\u2014'):
            for marker in ('pack', 'discography', 'collection', 'anthology', 'boxset',
                           'box' + separator + 'set', 'albums', 'protected', 'encrypted',
                           'drm', 'exe', 'single', 'sampler', 'partial', 'incomplete',
                           'mp3', 'aac', 'audiobook'):
                with self.subTest(separator=separator, marker=marker):
                    row = listing(title='Fiction Artist - Fiction Album FLAC '
                                  + separator + marker.upper() + separator, category='3040')
                    rows, omitted = parse(json.dumps({'channel': {'item': [row]}}).encode(),
                                 title='Fiction Artist - Fiction Album', scope='album_flac')
                    self.assertFalse(rows[0]['selectable'])
                    self.assertEqual(rows[0]['reason'], 'ambiguous_album')

    def test_benign_flac_listing_tokens_use_release_separator_boundaries(self):
        for separator in (' ', '_', '.', '-', '/', '\u2014'):
            for codec, selectable in (('fLaC', True), ('FLACish', False), ('notFLAC', False)):
                with self.subTest(separator=separator, codec=codec):
                    title = separator.join(('Fiction', 'Artist', 'Fiction', 'Album',
                                            codec, 'Packard', 'Shellac', '24bit'))
                    row = listing(title=title, category='3040')
                    rows, omitted = parse(json.dumps({'channel': {'item': [row]}}).encode(),
                                 title='Fiction Artist - Fiction Album', scope='album_flac')
                    self.assertEqual(rows[0]['selectable'], selectable)
                    self.assertEqual(rows[0]['title'], title)

    def test_nzb_subject_markers_and_separators_are_refused_only_for_albums(self):
        for separator in (' ', '_', '.', '-', '/', '\u2014'):
            for marker in ('password', 'passworded', 'password-protected', 'encrypted',
                           'protected', 'discography', 'pack'):
                with self.subTest(separator=separator, marker=marker):
                    subject = '&quot;album.flac&quot;' + separator + marker.upper() + separator
                    payload = NZB.replace(b'subject="fiction"',
                                          ('subject="' + subject + '"').encode())
                    validate_nzb(payload)
                    with self.assertRaises(ContractError):
                        validate_nzb(payload, album=True)

    def test_benign_nzb_flac_names_are_allowed(self):
        for subject in ('01_Packard-Shellac_Unprotected.FLAC', 'backpack.flac',
                        'passwordedness.flac', 'Disc_1/01.flac', '&quot;album.flac&quot; yEnc'):
            with self.subTest(subject=subject):
                payload = NZB.replace(b'subject="fiction"', ('subject="' + subject + '"').encode())
                validate_nzb(payload, album=True)
                validate_nzb(payload)
