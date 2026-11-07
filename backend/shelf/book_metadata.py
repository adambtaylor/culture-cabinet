"""Bounded Hardcover book metadata; never identities or human preferences.

Missing fields (older cache/schema), explicit null/empty values and malformed
values stay distinct. References are descriptive: no alias/title-only merges.
"""
import math
import re
from urllib.parse import urlsplit

RELATED_LIMIT = 12
CATEGORIES = {1: 'Book', 4: 'Graphic novel', 10: 'Light novel'}


def source_id(value):
    return type(value) is int and 0 < value <= 2147483647


def field_status(row, field):
    if field not in row:
        return 'unknown'
    return 'absent' if row[field] is None else 'present'


def optional_text(row, field, limit=500):
    status = field_status(row, field)
    value = row.get(field)
    if status == 'present':
        if type(value) is not str:
            status = 'invalid'
        elif not value.strip():
            status = 'absent'
    return {'status': status, 'text': value[:limit] if status == 'present' else ''}


def reference(row, field):
    status = field_status(row, field)
    value = row.get(field)
    if status == 'present' and not source_id(value):
        status = 'invalid'
    return {'status': status, 'id': value if status == 'present' else None}


def cover_url(value):
    """Explicit outbound HTTPS link only; no fetching, host trust or inline image."""
    if (type(value) is not str or not 1 <= len(value) <= 2048
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
            or '\\' in value):
        return ''
    try:
        parts = urlsplit(value)
        host = parts.hostname or ''
        if (parts.scheme != 'https' or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443) or parts.fragment
                or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', host, re.ASCII)
                or host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid'))):
            return ''
        return value
    except ValueError:
        pass
    return ''


def _cover(row):
    status = field_status(row, 'image')
    image = row.get('image')
    url = ''
    if status == 'present':
        if type(image) is not dict:
            status = 'invalid'
        else:
            status = field_status(image, 'url')
            if status == 'present':
                url = cover_url(image['url'])
                status = 'present' if url else 'invalid'
    return {'status': status, 'url': url}


def _series(row):
    status = field_status(row, 'book_series')
    rows = row.get('book_series')
    if status != 'present':
        return {'status': status, 'items': []}
    if type(rows) is not list or len(rows) > RELATED_LIMIT:
        return {'status': 'invalid', 'items': []}
    items = {}
    invalid = False
    for member in rows:
        if type(member) is not dict:
            invalid = True
            continue
        series = member.get('series')
        sid = member.get('series_id')
        if (not source_id(sid) or type(series) is not dict or type(series.get('id')) is not int
                or series['id'] != sid or type(member.get('book_id', row['id'])) is not int
                or member.get('book_id', row['id']) != row['id']):
            invalid = True
            continue
        position = member.get('position')
        position_status = field_status(member, 'position')
        if position_status == 'present' and (type(position) not in (int, float)
                or abs(position) > 1.7976931348623157e308 or not math.isfinite(position)):
            position_status = 'invalid'
        compilation = member.get('compilation')
        compilation_status = field_status(member, 'compilation')
        if compilation_status == 'present' and type(compilation) is not bool:
            compilation_status = 'invalid'
        item = {'id': sid, 'name': optional_text(series, 'name', 300),
                'slug': optional_text(series, 'slug', 200),
                'position': position if position_status == 'present' else None,
                'position_status': position_status, 'details': optional_text(member, 'details'),
                'compilation': compilation if compilation_status == 'present' else None,
                'compilation_status': compilation_status}
        if sid in items:
            # Merge each fact independently. Cleared conflicts stay ambiguous
            # even if a later row repeats one of the original values.
            existing = items[sid]
            for field in ('name', 'slug', 'details'):
                if existing[field] != item[field]:
                    existing[field] = {'status': 'ambiguous', 'text': ''}
                    invalid = True
            for field in ('position', 'compilation'):
                status_field = field + '_status'
                if (existing[field], existing[status_field]) != (item[field], item[status_field]):
                    existing[field], existing[status_field] = None, 'ambiguous'
                    invalid = True
        else:
            items[sid] = item
    return {'status': 'partial' if invalid else 'present' if items else 'absent',
            'items': list(items.values()), 'bounded': len(rows) == RELATED_LIMIT}


def _editions(row):
    status = field_status(row, 'editions')
    rows = row.get('editions')
    if status != 'present':
        return {'status': status, 'items': []}
    if type(rows) is not list or len(rows) > RELATED_LIMIT:
        return {'status': 'invalid', 'items': []}
    items, invalid = {}, False
    for edition in rows:
        if (type(edition) is not dict or not source_id(edition.get('id'))
                or type(edition.get('book_id')) is not int or edition['book_id'] != row['id']):
            invalid = True
            continue
        item = {'id': edition['id'], 'canonical': reference(edition, 'canonical_id'),
                'format': optional_text(edition, 'edition_format', 200),
                'information': optional_text(edition, 'edition_information', 500)}
        if edition['id'] in items:
            existing = items[edition['id']]
            for field in ('format', 'information'):
                if existing[field] != item[field]:
                    existing[field] = {'status': 'ambiguous', 'text': ''}
                    invalid = True
            if existing['canonical'] != item['canonical']:
                existing['canonical'] = {'status': 'ambiguous', 'id': None}
                invalid = True
        else:
            items[edition['id']] = item
    references = {eid: {(item['canonical']['status'], item['canonical']['id'])}
                  for eid, item in items.items()}
    for eid, item in items.items():
        if problem := _canonical_problem(eid, references):
            item['canonical']['status'] = problem
    return {'status': 'partial' if invalid else 'present' if items else 'absent',
            'items': list(items.values()), 'bounded': len(rows) == RELATED_LIMIT}


def parse(row):
    category = reference(row, 'book_category_id')
    category['label'] = CATEGORIES.get(category['id'], '')
    if category['status'] == 'present' and not category['label']:
        category['status'] = 'unsupported'
    return {'category': category, 'canonical': reference(row, 'canonical_id'),
            'synopsis': optional_text(row, 'description', 2000), 'cover': _cover(row),
            'series': _series(row), 'editions': _editions(row)}


def annotate_canonical(cards, rows):
    """Inspect only the supplied bounded page. Retain every original work key."""
    references = {}
    for row in rows:
        ref = reference(row, 'canonical_id')
        references.setdefault(row['id'], set()).add((ref['status'], ref['id']))
    for card in cards:
        if problem := _canonical_problem(card.source_id, references):
            card.book_metadata['canonical']['status'] = problem
        categories = {tuple(reference(row, 'book_category_id').values())
                      for row in rows if row['id'] == card.source_id}
        if len(categories) > 1:
            card.book_metadata['category'] = {'status': 'invalid', 'id': None, 'label': ''}


def _canonical_problem(current, references):
    seen = set()
    while current in references:
        if current in seen:
            return 'cycle'
        seen.add(current)
        values = references[current]
        if len(values) != 1:
            return 'ambiguous'
        status, target = next(iter(values))
        if status in ('invalid', 'ambiguous'):
            return status
        if status != 'present':
            break
        current = target
    return None


def series_groups(cards):
    """Group the displayed works by source series ID without reordering cards."""
    groups = {}
    for card in cards:
        metadata = card.get('book_metadata') or {}
        for member in metadata.get('series', {}).get('items', []):
            group = groups.setdefault(member['id'], {'id': member['id'],
                'name': member['name']['text'], 'works': []})
            group['works'].append({'key': card['key'], 'title': card['title']})
    return list(groups.values())
