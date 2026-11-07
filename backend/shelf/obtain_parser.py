"""Hydra 9.1.1 JSON listing metadata, never a content-verification attestation.

Contract source: 8ef3e6489aac8c8d776dca538127be38b2aff962, supplied
NewznabJsonTransformer and JSON DTOs. URLs/descriptions are never copied.
"""
import hashlib
import json
import re
import unicodedata

from shelf.acquisition_contract import ContractError, MAX_RESPONSE_BYTES
from shelf.episode_scope import ScopeError, parse_scope, episode_code


def fail():
    raise ContractError('Invalid search response')


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            fail()
        result[key] = value
    return result


def plain(value, maximum=200):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(unicodedata.category(c).startswith('C') for c in value)
            or re.search(r'[<>&]|://|www\.|apikey|api_key|password|token\s*=', value, re.I)):
        fail()
    return value.strip()


def digest(metadata):
    return hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


def words(value):
    return ' '.join(re.findall(r'[^\W_]+', value.casefold()))


def listing_reason(metadata, title, scope):
    text = metadata['title']
    normalized, wanted = words(text), words(title)
    # Release suffixes may follow the complete saved title, never title-only joining.
    if not (normalized == wanted or normalized.startswith(wanted + ' ')):
        return 'title_mismatch'
    expected_years = set(re.findall(r'\b(?:19|20)\d{2}\b', title))
    years = set(re.findall(r'\b(?:19|20)\d{2}\b', text))
    if expected_years and years != expected_years:
        return 'year_mismatch'
    categories = metadata['categories']
    if scope == 'album_flac':
        if any(value.casefold() not in ('0', 'false', 'no')
               for value in metadata.get('protection', {}).values()):
            return 'protected_album'
        if 3040 not in categories or any(c not in (3000, 3040) for c in categories):
            return 'category_mismatch'
        if (not re.search(r'\bflac\b', normalized)
                or re.search(r'\b(discography|collection|anthology|box ?set|pack|albums|'
                             r'password|passworded|protected|encrypted|drm|exe|com|msi|apk|bat|cmd|ps1|sh|scr|'
                             r'single|sampler|partial|incomplete|mp3|aac|audiobook)\b', normalized)):
            return 'ambiguous_album'
        return ''
    try:
        selected = parse_scope(scope)
    except ScopeError as error:
        raise ContractError(str(error)) from None
    family = 5000 if selected else {'film': 2000, 'audiobook': 3030}[scope]
    if not categories or any(not (c == family or family != 3030 and family <= c < family + 1000)
                             for c in categories):
        return 'category_mismatch'
    if selected:
        season, episode = selected
        code = episode_code(scope)
        # Require an isolated exact token. Ranges, extra episode tokens, packs and
        # contradictory season/episode attributes make the candidate ambiguous.
        tokens = re.findall(r'(?i)s\d+e\d+(?:e\d+)*|\d+x\d+|(?<![a-z0-9])e\d+', text)
        if (len(tokens) != 1 or tokens[0].upper() != code
                or not re.search(r'(?i)(?<![a-z0-9])' + code + r'(?![a-z0-9]|[ ._-]*(?:E\d|\d+(?![\dp])|to\b))', text)
                or re.search(r'(?i)' + code + r'\s*(?:[+,/;&]|\band\b)\s*\d+\b', text)
                or re.search(r'(?i)\b(pack|complete|season|episodes|pilot)\b', text)
                or metadata['season'] not in (None, str(season), '%02d' % season)
                or metadata['episode'] not in (None, str(episode), '%02d' % episode)):
            return 'ambiguous_episode'
    if scope == 'audiobook' and re.search(r'(?i)\b(ebook|epub|pdf|mobi)\b', text):
        return 'ebook_not_audiobook'
    return ''


def parse(payload, *, title, scope):
    """Return (distinct safe rows, omitted display-row count).

    Structural or conflicting identity errors still reject the entire response.
    Omitted display text stays private; accepted titles retain their exact text.
    """
    if type(payload) not in (bytes, bytearray) or not 0 < len(payload) <= MAX_RESPONSE_BYTES:
        fail()
    try:
        root = json.loads(payload, object_pairs_hook=pairs,
                          parse_constant=lambda value: fail())
    except (ValueError, UnicodeError, RecursionError):
        fail()
    if (type(root) is not dict or 'error' in root or type(root.get('channel')) is not dict
            or 'error' in root['channel']):
        fail()
    rows = root['channel'].get('item')
    if type(rows) is not list or len(rows) > 20:
        fail()
    results, identities = {}, {}
    omitted_rows = 0
    for row in rows:
        if type(row) is not dict:
            fail()
        guid = row.get('guid')
        if (type(guid) is not str or not re.fullmatch(r'-?(?:0|[1-9][0-9]{0,18})', guid)
                or not -(2**63) <= int(guid) < 2**63 or str(int(guid)) != guid
                or type(row.get('id')) is not str or row['id'] != guid):
            fail()
        enclosure = row.get('enclosure')
        if type(enclosure) is not dict or type(enclosure.get('@attributes')) is not dict:
            fail()
        enclosure = enclosure['@attributes']
        size = enclosure.get('length')
        if (type(size) is not str or not re.fullmatch(r'[0-9]{1,19}', size)
                or not 0 <= int(size) < 2**63):
            fail()
        attrs = row.get('attr')
        if type(attrs) is not list or len(attrs) > 128:
            fail()
        values = {}
        categories = []
        for attr in attrs:
            if type(attr) is not dict or type(attr.get('@attributes')) is not dict:
                fail()
            attr = attr['@attributes']
            name, value = attr.get('name'), attr.get('value')
            if type(name) is not str or len(name) > 100:
                fail()
            if name != 'hydraIndexerName' and (type(value) is not str or len(value) > 4096):
                fail()
            if name == 'category':
                if not re.fullmatch(r'[0-9]{4}', value):
                    fail()
                categories.append(int(value))
            elif name in ('season', 'episode', 'hydraIndexerName', 'guid') or (
                    scope == 'album_flac' and name in ('password', 'passworded', 'protected', 'encrypted')):
                if name in values and digest(values[name]) != digest(value):
                    fail()
                values[name] = value
        if 'guid' in values and values['guid'] != guid:
            fail()
        metadata = {'guid': guid, 'title': row.get('title'), 'size': int(size),
                    'categories': sorted(set(categories)),
                    'indexer': values.get('hydraIndexerName', ''),
                    'season': plain(values['season'], 10) if 'season' in values else None,
                    'episode': plain(values['episode'], 10) if 'episode' in values else None,
                    'edition': 'Unknown; listing title only', 'full_work': 'Unknown; listing claim only'}
        if scope == 'album_flac':
            metadata['protection'] = {k: values[k] for k in
                ('password', 'passworded', 'protected', 'encrypted') if k in values}
        # Check every identity before omitting unsafe display text. A hidden row
        # must not mask a conflict with a selectable row, in either row order.
        identity = digest(metadata | {'type': enclosure.get('type'),
                                      'indexer_present': 'hydraIndexerName' in values})
        if guid in identities and identities[guid] != identity:
            fail()
        identities[guid] = identity
        try:
            plain(metadata['title'])
            if 'hydraIndexerName' in values:
                plain(metadata['indexer'], 100)
        except ContractError:
            omitted_rows += 1
            continue
        reason = listing_reason(metadata, title, scope)
        if scope == 'album_flac' and any(value.casefold() not in ('0', 'false', 'no')
                                       for value in metadata['protection'].values()):
            reason = 'protected_album'
        if enclosure.get('type') != 'application/x-nzb':
            reason = 'unsupported_type'
        metadata['reason'] = reason
        metadata['selectable'] = not reason
        results[guid] = metadata
    return list(results.values()), omitted_rows
