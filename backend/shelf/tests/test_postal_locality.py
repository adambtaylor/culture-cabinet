"""Public data and synthetic locations only; no owner settings or network I/O."""
from dataclasses import FrozenInstanceError
from hashlib import sha256
from io import BytesIO
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from shelf.postal_locality import LocalReference, PostalIndex, distance_km

_BUILDER = Path(__file__).resolve().parents[3] / 'tools/build_au_postcode_index.py'
spec = importlib.util.spec_from_file_location('au_postcode_builder', _BUILDER)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def row(code='0800', locality='Test City', lat='-12', lon='130', region='Northern Territory', state='NT'):
    return dict(country='AU', postcode=code, locality=locality, region=region,
                region_code=state, latitude=lat, longitude=lon, accuracy=3)


def archive(entries):
    raw = BytesIO()
    with ZipFile(raw, 'w') as z:
        for name, content in entries:
            z.writestr(name, content)
    return raw.getvalue()


class PostalLocalityTests(TestCase):
    def test_pinned_real_index_is_licensed_and_preserves_leading_zero(self):
        index = PostalIndex.load()
        value = index.lookup('AU', ' 0800 ')
        self.assertEqual(value.state, 'mapped')
        self.assertEqual(value.postcode, '0800')
        self.assertEqual(index.dataset_revision,
            'sha256:6aef84e344a0827b77f9a3bb5f52933f00871c8a1e5ce988dd33741ea3e348dd')
        self.assertIn('GeoNames', value.attribution)
        self.assertIn('not your home', value.approximation_notice)
        with self.assertRaises(FrozenInstanceError):
            index.dataset_revision = 'changed'
        # Lookups and venue resolution never re-open files after load.
        with patch('builtins.open', side_effect=AssertionError('No lookup I/O')):
            self.assertIsNotNone(index.lookup('AU', '0800').reference)
            self.assertEqual(index.venue_reference({'venue.address.country': 'AU',
                'venue.address.postcode': '0800'}).basis, 'postcode')

    def test_invalid_ascii_country_missing_and_unmapped_are_distinct(self):
        index = PostalIndex([row()], dataset_revision='synthetic')
        for country, postcode in [('NZ', '0800'), ('AU', '８０００'), ('AU', '800'), ('AU', '08000'), ('AU', '08 0')]:
            with self.assertRaises(ValueError):
                index.lookup(country, postcode)
        self.assertEqual(index.lookup('AU', '9999').state, 'unmapped')
        self.assertEqual(PostalIndex().lookup('AU', '0800').state, 'unavailable')
        self.assertIsNone(PostalIndex().lookup('AU', '0800').reference)

    def test_distinct_point_spherical_mean_is_deterministic_not_a_home_or_error_bound(self):
        rows = [row(lat='-12', lon='130'), row(lat='-12', lon='132'), row(lat='-12', lon='130')]
        first = PostalIndex(rows, dataset_revision='synthetic').lookup('AU', '0800').reference
        second = PostalIndex(rows[::-1], dataset_revision='synthetic').lookup('AU', '0800').reference
        self.assertAlmostEqual(first.latitude, second.latitude)
        self.assertAlmostEqual(first.longitude, 131)
        self.assertEqual(first.point_count, 2)
        self.assertGreater(first.spread_km, 100)
        self.assertEqual(first.basis, 'postcode_reference')
        zero = LocalReference(0, 0, 'source_coordinate', (), 1)
        east = LocalReference(0, 1, 'source_coordinate', (), 1)
        self.assertAlmostEqual(distance_km(zero, east), 111.1950802335, places=6)

    def test_missing_and_antipodal_coordinates_have_no_guessed_zero_reference(self):
        for rows in [[row(lat=None, lon=None)], [row(lat='0', lon='0'), row(lat='0', lon='180')]]:
            value = PostalIndex(rows, dataset_revision='synthetic').lookup('AU', '0800')
            self.assertEqual(value.state, 'unavailable')
            self.assertIsNone(value.reference)
        for lat in ['NaN', 'Infinity', '91', 'garbage']:
            with self.assertRaises(ValueError):
                PostalIndex([row(lat=lat)], dataset_revision='synthetic')

    def test_venue_priority_exact_locality_and_ambiguity_never_guess_from_name(self):
        index = PostalIndex([row(), row('0801', 'Other City')], dataset_revision='synthetic')
        facts = {'venue.latitude': '-13', 'venue.longitude': '131', 'venue.address.country': 'AU',
                 'venue.address.postcode': '0800', 'venue.address.region': 'NT',
                 'venue.address.locality': 'Test City'}
        self.assertEqual(index.venue_reference(facts, evidence_refs=('permitted-observation:1',)).basis, 'coordinate')
        del facts['venue.latitude']; del facts['venue.longitude']
        self.assertEqual(index.venue_reference(facts).basis, 'postcode')
        del facts['venue.address.postcode']
        self.assertEqual(index.venue_reference(facts).basis, 'locality')
        self.assertEqual(index.venue_reference({'venue.name': 'Test City'}).basis, 'unknown')
        ambiguous = PostalIndex([row(), row('0801')], dataset_revision='synthetic')
        self.assertEqual(ambiguous.venue_reference(facts).basis, 'unknown')
        facts['venue.address.country'] = 'NZ'
        self.assertEqual(index.venue_reference(facts).basis, 'unknown')

    def test_builder_checks_archive_paths_sizes_coordinates_and_deterministic_bytes(self):
        line = b'AU\t0800\tTest City\tNorthern Territory\tNT\t\t\t\t\t-12\t130\t3\n'
        raw = archive([('AU.txt', line), ('readme.txt', b'Creative Commons Attribution 4.0')])
        data, manifest = builder.build(raw, '2026-11-11T00:00:00+00:00')
        again, _ = builder.build(raw, '2026-11-11T01:00:00+00:00')
        self.assertEqual(data, again)
        self.assertEqual(manifest['upstream_sha256'], sha256(raw).hexdigest())
        self.assertEqual(manifest['normalized_sha256'], sha256(data).hexdigest())
        for entries in [[('../AU.txt', line)], [('AU.txt', line), ('extra.txt', b'x')],
                        [('AU.txt', line), ('AU.txt', line)], [('AU.txt', b'bad')],
                        [('AU.txt', line.replace(b'-12', b'NaN'))]]:
            with self.assertRaises(ValueError):
                builder.normalize_archive(archive(entries))
        with patch.object(builder, 'MAX_EXPANDED', 10), self.assertRaises(ValueError):
            builder.normalize_archive(raw)
        with self.assertRaises(ValueError):
            builder.normalize_archive(b'x' * (builder.MAX_COMPRESSED + 1))

    def test_missing_or_corrupted_bundled_data_is_nonblocking_unavailable(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(PostalIndex.load(directory).lookup('AU', '0800').state, 'unavailable')
            base = Path(directory)
            (base/'geonames-au-postcodes.json').write_text('{}')
            (base/'geonames-au-postcodes.manifest.json').write_text('{"normalized_sha256":"wrong"}')
            self.assertEqual(PostalIndex.load(base).lookup('AU', '0800').state, 'unavailable')
