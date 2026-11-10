"""Normalize one explicitly acquired GeoNames AU archive; never fetch at runtime.

Usage: python tools/build_au_postcode_index.py /private/AU.zip --retrieved-at <UTC ISO8601>
The importer does not extract archive paths or make network calls.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import stat
from zipfile import BadZipFile, ZipFile

SOURCE_URL = 'https://download.geonames.org/export/zip/AU.zip'
NORMALIZER_VERSION = 'geonames-au/v1'
MAX_COMPRESSED = 2 * 1024 * 1024
MAX_EXPANDED = 10 * 1024 * 1024


def number(raw, lower, upper):
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError('Invalid coordinate.') from None
    if not value.is_finite() or not lower <= value <= upper:
        raise ValueError('Invalid coordinate.')
    return format(value.normalize(), 'f')


def normalize_archive(raw):
    if type(raw) is not bytes or len(raw) > MAX_COMPRESSED:
        raise ValueError('Compressed archive exceeds bound.')
    try:
        with ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (not 1 <= len(entries) <= 2 or len({e.filename for e in entries}) != len(entries)
                    or {e.filename for e in entries} != {'AU.txt', 'readme.txt'}
                    or 'AU.txt' not in archive.namelist()
                    or sum(e.file_size for e in entries) > MAX_EXPANDED
                    or any(e.flag_bits & 1 or e.is_dir() or e.file_size < 0
                           or stat.S_ISLNK(e.external_attr >> 16) for e in entries)):
                raise ValueError('Unexpected or oversized archive entries.')
            # Read a bounded stream, without trusting only the central directory.
            with archive.open('AU.txt') as stream:
                content = stream.read(MAX_EXPANDED + 1)
            with archive.open('readme.txt') as stream:
                notice = stream.read(MAX_EXPANDED + 1)
            if (len(content) + len(notice) > MAX_EXPANDED
                    or b'Creative Commons Attribution 4.0' not in notice):
                raise ValueError('Missing pinned attribution evidence.')
            if len(content) > MAX_EXPANDED:
                raise ValueError('Expanded archive exceeds bound.')
    except (BadZipFile, RuntimeError, OSError):
        raise ValueError('Invalid postal archive.') from None
    rows = set()
    for line in content.decode('utf-8-sig').splitlines():
        columns = line.split('\t')
        if len(columns) != 12 or columns[0] != 'AU' or not re.fullmatch(r'[0-9]{4}', columns[1], re.ASCII):
            raise ValueError('Invalid postal row.')
        country, postcode, locality, region, region_code, _, _, _, _, lat, lon, accuracy = columns
        for value in (locality, region, region_code):
            if not value or len(value) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError('Invalid locality metadata.')
        latitude, longitude = number(lat, Decimal('-90'), Decimal('90')), number(lon, Decimal('-180'), Decimal('180'))
        if (latitude is None) != (longitude is None) or accuracy and not re.fullmatch(r'[0-9]', accuracy):
            raise ValueError('Invalid coordinate metadata.')
        rows.add((country, postcode, locality, region, region_code, latitude, longitude,
                  int(accuracy) if accuracy else None))
        if len(rows) > 50000:
            raise ValueError('Postal row count exceeds bound.')
    if not rows:
        raise ValueError('Empty postal index.')
    # Sort explicit fields, including a stable order for absent coordinates.
    rows = sorted(rows, key=lambda row: tuple('' if v is None else str(v) for v in row))
    names = ('country', 'postcode', 'locality', 'region', 'region_code', 'latitude', 'longitude', 'accuracy')
    return {'schema': NORMALIZER_VERSION, 'rows': [dict(zip(names, row)) for row in rows]}


def build(raw, retrieved_at):
    stamp = datetime.fromisoformat(retrieved_at)
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError('Aware UTC retrieval time required.')
    document = normalize_archive(raw)
    with ZipFile(BytesIO(raw)) as archive:
        notice_hash = sha256(archive.read('readme.txt')).hexdigest()
    normalized = (json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    manifest = {'source_url': SOURCE_URL, 'retrieved_at': stamp.isoformat(),
        'upstream_sha256': sha256(raw).hexdigest(), 'archive_readme_sha256': notice_hash, 'normalized_sha256': sha256(normalized).hexdigest(),
        'license': 'CC BY 4.0', 'license_url': 'https://creativecommons.org/licenses/by/4.0/',
        'attribution': 'GeoNames Australian postal data, https://www.geonames.org/, CC BY 4.0; normalized by Culture Cabinet.',
        'documentation': 'https://download.geonames.org/export/zip/readme.txt',
        'row_count': len(document['rows']), 'normalizer_version': NORMALIZER_VERSION,
        'meaning': 'Estimated WGS84 source points; no accuracy/completeness guarantee. Derived references are not home positions, surveyed centroids or error bounds.'}
    return normalized, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--retrieved-at', required=True)
    args = parser.parse_args()
    with args.archive.open('rb') as stream:
        raw = stream.read(MAX_COMPRESSED + 1)
    normalized, manifest = build(raw, args.retrieved_at)
    target = Path(__file__).resolve().parents[1] / 'backend/shelf/data'
    target.mkdir(exist_ok=True)
    (target / 'geonames-au-postcodes.json').write_bytes(normalized)
    (target / 'geonames-au-postcodes.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    main()
