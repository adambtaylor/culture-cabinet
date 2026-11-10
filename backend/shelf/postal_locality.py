"""Pinned public postal references, pure after load. No owner-location I/O.

A postcode reference is a spherical mean of distinct source points, not the
person's address or a surveyed centroid. Source spread is not an error bound.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from math import asin, atan2, cos, degrees, hypot, isfinite, radians, sin, sqrt
from pathlib import Path
import re
from types import MappingProxyType
import unicodedata

EARTH_KM = 6371.0088
MAX_BYTES = 10 * 1024 * 1024
ATTRIBUTION = 'GeoNames, https://www.geonames.org/, CC BY 4.0'
APPROXIMATION_NOTICE = 'Approximate postcode reference from estimated source points; not your home position or an error bound.'


def _name(value):
    return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()


def _coordinate(value, lower, upper):
    if type(value) is not str or len(value) > 40:
        raise ValueError('Invalid coordinate.')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError('Invalid coordinate.') from None
    if not number.is_finite() or not lower <= number <= upper:
        raise ValueError('Invalid coordinate.')
    return float(number)


def distance_km(first, second):
    """Great-circle distance only; no routing/travel-time claim."""
    if type(first) is not LocalReference or type(second) is not LocalReference:
        raise ValueError('Validated local references required.')
    lat1, lon1 = radians(first.latitude), radians(first.longitude)
    lat2, lon2 = radians(second.latitude), radians(second.longitude)
    value = sin((lat2-lat1)/2)**2 + cos(lat1)*cos(lat2)*sin((lon2-lon1)/2)**2
    return 2 * EARTH_KM * asin(sqrt(min(1.0, max(0.0, value))))


@dataclass(frozen=True)
class LocalReference:
    latitude: float
    longitude: float
    basis: str
    source_refs: tuple[str, ...]
    point_count: int
    spread_km: float | None = None

    def __post_init__(self):
        if (type(self.latitude) not in (int, float) or not isfinite(self.latitude)
                or not -90 <= self.latitude <= 90 or type(self.longitude) not in (int, float)
                or not isfinite(self.longitude) or not -180 <= self.longitude <= 180):
            raise ValueError('Invalid reference.')
        if (type(self.source_refs) is not tuple or len(self.source_refs) > 50000
                or any(type(ref) is not str or not 1 <= len(ref) <= 300 for ref in self.source_refs)
                or type(self.point_count) is not int or self.point_count < 1
                or self.spread_km is not None and (type(self.spread_km) not in (int, float)
                    or not isfinite(self.spread_km) or self.spread_km < 0)):
            raise ValueError('Invalid source reference.')


@dataclass(frozen=True)
class PostalLookup:
    state: str
    dataset_revision: str | None
    country: str
    postcode: str
    reference: LocalReference | None
    attribution: str = ATTRIBUTION
    approximation_notice: str = APPROXIMATION_NOTICE


@dataclass(frozen=True)
class VenueReference:
    basis: str
    reference: LocalReference | None
    dataset_revision: str | None
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, init=False, repr=False)
class PostalIndex:
    dataset_revision: str | None
    _postcodes: MappingProxyType
    _localities: MappingProxyType

    def __init__(self, rows=(), *, dataset_revision=None):
        """Rows are bounded public data, not owner settings or remote lookups."""
        if type(rows) not in (list, tuple) or len(rows) > 50000:
            raise ValueError('Invalid index size.')
        if (dataset_revision is not None and (type(dataset_revision) is not str
                or not 1 <= len(dataset_revision) <= 100) or rows and dataset_revision is None):
            raise ValueError('Dataset revision required.')
        by_postcode, by_locality = {}, {}
        for i, row in enumerate(rows):
            if type(row) is not dict or set(row) != {'country', 'postcode', 'locality', 'region',
                    'region_code', 'latitude', 'longitude', 'accuracy'}:
                raise ValueError('Invalid index row.')
            if (row['country'] != 'AU' or type(row['postcode']) is not str
                    or not re.fullmatch(r'[0-9]{4}', row['postcode'], re.ASCII)):
                raise ValueError('Invalid postal identity.')
            for key in ('locality', 'region', 'region_code'):
                if (type(row[key]) is not str or not 1 <= len(row[key]) <= 200
                        or any(ord(c) < 32 or ord(c) == 127 for c in row[key])):
                    raise ValueError('Invalid locality.')
            if row['accuracy'] is not None and (type(row['accuracy']) is not int or not 0 <= row['accuracy'] <= 9):
                raise ValueError('Invalid accuracy.')
            point = None
            if row['latitude'] is not None or row['longitude'] is not None:
                point = (_coordinate(row['latitude'], -90, 90), _coordinate(row['longitude'], -180, 180))
            ref = 'geonames-au:row:' + str(i)
            by_postcode.setdefault(row['postcode'], []).append((point, ref))
            # Both documented state name and code are valid exact source-listed
            # forms; no alias/fuzzy venue-name or default-city inference.
            for region in {row['region'], row['region_code']}:
                by_locality.setdefault((_name(region), _name(row['locality'])), set()).add(row['postcode'])
        object.__setattr__(self, 'dataset_revision', dataset_revision)
        object.__setattr__(self, '_postcodes', MappingProxyType({key: self._mean(points) for key, points in by_postcode.items()}))
        object.__setattr__(self, '_localities', MappingProxyType({key: tuple(sorted(values)) for key, values in by_locality.items()}))

    @staticmethod
    def _mean(points):
        unique = sorted({point for point, _ in points if point is not None})
        if not unique:
            return None
        vectors = [(cos(radians(lat))*cos(radians(lon)), cos(radians(lat))*sin(radians(lon)),
                    sin(radians(lat))) for lat, lon in unique]
        x, y, z = (sum(v[i] for v in vectors) / len(vectors) for i in range(3))
        if hypot(hypot(x, y), z) < 1e-12:
            return None  # Antipodal/degenerate source points have no guessed mean.
        result = LocalReference(degrees(atan2(z, hypot(x, y))), degrees(atan2(y, x)),
            'postcode_reference', tuple(sorted(ref for point, ref in points if point is not None)), len(unique))
        spread = max(distance_km(result, LocalReference(lat, lon, 'source_point', (), 1)) for lat, lon in unique)
        return LocalReference(result.latitude, result.longitude, result.basis, result.source_refs, len(unique), spread)

    @classmethod
    def load(cls, directory=None):
        """Validate the pinned bundled bytes once; failure stays explicit/nonblocking."""
        directory = Path(directory) if directory is not None else Path(__file__).with_name('data')
        try:
            with (directory / 'geonames-au-postcodes.json').open('rb') as f:
                raw = f.read(MAX_BYTES + 1)
            with (directory / 'geonames-au-postcodes.manifest.json').open('rb') as f:
                manifest_raw = f.read(16385)
            if len(raw) > MAX_BYTES or len(manifest_raw) > 16384:
                raise ValueError('Oversized data.')
            manifest, data = json.loads(manifest_raw), json.loads(raw)
            revision = sha256(raw).hexdigest()
            if (manifest['normalized_sha256'] != revision or manifest['license'] != 'CC BY 4.0'
                    or manifest['source_url'] != 'https://download.geonames.org/export/zip/AU.zip'
                    or manifest['normalizer_version'] != 'geonames-au/v1'
                    or data['schema'] != 'geonames-au/v1' or len(data['rows']) != manifest['row_count']):
                raise ValueError('Unpinned data.')
            return cls(data['rows'], dataset_revision='sha256:' + revision)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return cls()  # No0,0 fallback and no runtime refresh.

    def lookup(self, country, postcode):
        if (country != 'AU' or type(country) is not str or type(postcode) is not str
                or not re.fullmatch(r'[0-9]{4}', postcode.strip(), re.ASCII)):
            raise ValueError('AU and four ASCII postcode digits required.')
        postcode = postcode.strip()
        point = self._postcodes.get(postcode)
        state = ('unavailable' if self.dataset_revision is None else
                 'mapped' if point is not None else 'unmapped' if postcode not in self._postcodes else 'unavailable')
        return PostalLookup(state, self.dataset_revision, country, postcode, point)

    def venue_reference(self, facts, *, evidence_refs=()):
        """Consume ONLY already source-attributed, permitted facts supplied by caller.

        The native evidence reader must check current rights/freshness first.
        Bare dictionaries are not source authority or permission to infer venue
        location. Prefer coordinates, then listed AU postcode, then unique locality.
        """
        if type(facts) not in (dict, tuple) or type(evidence_refs) is not tuple:
            raise ValueError('Bounded public facts required.')
        items = tuple(facts.items()) if type(facts) is dict else facts
        if (len(items) > 40 or len(dict(items)) != len(items) or len(evidence_refs) > 40
                or any(type(ref) is not str or not 1 <= len(ref) <= 300 for ref in evidence_refs)):
            raise ValueError('Ambiguous public facts.')
        facts = dict(items)
        if 'venue.latitude' in facts and 'venue.longitude' in facts:
            try:
                point = LocalReference(_coordinate(facts['venue.latitude'], -90, 90),
                    _coordinate(facts['venue.longitude'], -180, 180), 'source_coordinate', evidence_refs, 1)
                return VenueReference('coordinate', point, None, evidence_refs)
            except ValueError:
                pass
        if facts.get('venue.address.country') == 'AU' and 'venue.address.postcode' in facts:
            try:
                value = self.lookup('AU', facts['venue.address.postcode'])
                if value.reference is not None:
                    return VenueReference('postcode', value.reference, self.dataset_revision, evidence_refs)
            except ValueError:
                pass
        region, locality = facts.get('venue.address.region'), facts.get('venue.address.locality')
        if (facts.get('venue.address.country') in (None, 'AU') and type(region) is str and type(locality) is str
                and len(region) <= 200 and len(locality) <= 200):
            postcodes = self._localities.get((_name(region), _name(locality)), ())
            if len(postcodes) == 1:
                point = self._postcodes[postcodes[0]]
                if point is not None:
                    return VenueReference('locality', point, self.dataset_revision, evidence_refs)
        return VenueReference('unknown', None, self.dataset_revision, evidence_refs)
