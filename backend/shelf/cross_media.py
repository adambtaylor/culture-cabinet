"""Bounded offline source snapshots and factual bridges. No provider clients."""
from collections import deque
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re

SCHEMA = 'cross-media-cache-v1'
MAX_BYTES = 2 * 1024 * 1024
MAX_NODES = 512
MAX_EDGES = 2048
MAX_RECORDS = 128
MAX_ROWS = 1000
MAX_ALIAS_HOPS = 8
MAX_PATH_STATES = 512
MAX_EVIDENCE = 16
OUTCOMES = {'items', 'empty', 'ambiguous', 'failed', 'timeout', 'rate-limited'}
PROVENANCE = {'reconstructed', 'simulated', 'synthetic', 'supplied'}
NEIGHBORS = {'hardcover-similar', 'tmdb-recommendation', 'tmdb-similar'}
RELATIONS = NEIGHBORS | {'based-on', 'identity', 'edition-of'}
SAMPLE_IDS = ('dune', 'dune-messiah', 'synthetic-lanes')
FIXTURE = Path(__file__).with_name('fixtures') / 'cross_media_sample.json'


class InvalidData(ValueError):
    """Malformed or out-of-budget offline input."""


def _list(value, limit=MAX_ROWS):
    if type(value) is not list or len(value) > limit:
        raise InvalidData('Expected a bounded native JSON list.')
    return value


def _text(value, limit=300):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise InvalidData('Expected bounded nonempty text.')
    return value


def positive_id(value):
    if type(value) is not int or not 0 < value <= 2147483647:
        raise InvalidData('Expected a positive source ID.')
    return value


def stamp(value):
    if value is not None:
        try:
            parsed = datetime.fromisoformat(value) if type(value) is str else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.utcoffset() is None:
            raise InvalidData('Expected a timezone-aware timestamp or null.')
    return value


def identity(provider, medium, source_id):
    if (provider, medium) in {('hardcover', 'book'), ('tmdb', 'movie'), ('tmdb', 'tv')}:
        positive_id(source_id)
    elif (provider, medium) == ('wikidata', 'entity'):
        if type(source_id) is not str or not re.fullmatch(r'Q[1-9][0-9]{0,11}', source_id):
            raise InvalidData('Invalid Wikidata identity.')
    elif provider == 'openlibrary' and medium in ('work', 'edition', 'author'):
        suffix = {'work': 'W', 'edition': 'M', 'author': 'A'}[medium]
        if type(source_id) is not str or not re.fullmatch(r'OL[1-9][0-9]{0,11}' + suffix, source_id):
            raise InvalidData('Invalid Open Library namespace.')
    else:
        raise InvalidData('Unsupported identity namespace.')
    return f'{provider}:{medium}:{source_id}'


def split_identity(key):
    if type(key) is not str or len(key) > 100:
        raise InvalidData('Invalid identity.')
    parts = key.split(':')
    if len(parts) != 3:
        raise InvalidData('Invalid identity.')
    provider, medium, source_id = parts
    if provider in ('hardcover', 'tmdb'):
        if not re.fullmatch(r'[1-9][0-9]{0,9}', source_id):
            raise InvalidData('Invalid numeric identity.')
        source_id = int(source_id)
    if identity(provider, medium, source_id) != key:
        raise InvalidData('Noncanonical identity.')
    return provider, medium, source_id


def canonical_url(key, slug=None):
    provider, medium, source_id = split_identity(key)
    if provider == 'wikidata':
        return f'https://www.wikidata.org/wiki/{source_id}'
    if provider == 'tmdb':
        return f'https://www.themoviedb.org/{medium}/{source_id}'
    if provider == 'openlibrary':
        section = {'work': 'works', 'edition': 'books', 'author': 'authors'}[medium]
        return f'https://openlibrary.org/{section}/{source_id}'
    if type(slug) is str and len(slug) <= 200 and re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
        return 'https://hardcover.app/books/' + slug
    return None


def node(key, title, *, fetched_at=None, provenance='supplied', slug=None):
    provider, medium, source_id = split_identity(key)
    if provenance not in PROVENANCE:
        raise InvalidData('Unknown provenance.')
    if slug is not None and (provider != 'hardcover' or canonical_url(key, slug) is None):
        raise InvalidData('Invalid source slug.')
    if provenance == 'reconstructed' and fetched_at is not None:
        raise InvalidData('Reconstructed source freshness is unknown.')
    return {'key': key, 'provider': provider, 'medium': medium, 'source_id': source_id,
            'title': _text(title), 'slug': slug, 'url': canonical_url(key, slug),
            'fetched_at': stamp(fetched_at),
            'freshness': 'unknown' if fetched_at is None else 'supplied snapshot',
            'provenance': provenance}


def outcome(status, items=None, *, checked_at=None, retry_at=None, expires_at=None, reason=None):
    items = [] if items is None else deepcopy(_list(items))
    if (status not in OUTCOMES or (status == 'items' and not items)
            or (status not in ('items', 'ambiguous') and items)):
        raise InvalidData('Outcome and items disagree.')
    if reason is not None:
        _text(reason, 300)
    return {'status': status, 'items': items, 'checked_at': stamp(checked_at),
            'retry_at': stamp(retry_at), 'expires_at': stamp(expires_at), 'reason': reason}


def _json(value):
    try:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise InvalidData('Expected finite JSON data.') from None
    if len(encoded.encode()) > MAX_BYTES:
        raise InvalidData('Snapshot byte budget exceeded.')
    return encoded


class Cache:
    """Validated, copied JSON snapshot; persistence is explicitly caller-owned."""

    def __init__(self, data=None):
        data = {'schema': SCHEMA, 'nodes': [], 'edges': [], 'records': []} if data is None else data
        _json(data)
        if type(data) is not dict or set(data) != {'schema', 'nodes', 'edges', 'records'} or data['schema'] != SCHEMA:
            raise InvalidData('Unsupported cache schema.')
        keys = set()
        for item in _list(data['nodes'], MAX_NODES):
            if type(item) is not dict:
                raise InvalidData('Invalid node.')
            try:
                rebuilt = node(item['key'], item['title'], fetched_at=item['fetched_at'],
                               provenance=item['provenance'], slug=item['slug'])
            except KeyError:
                raise InvalidData('Incomplete node.') from None
            if rebuilt != item or item['key'] in keys:
                raise InvalidData('Noncanonical or duplicate node.')
            keys.add(item['key'])
        edge_keys = set()
        for edge in _list(data['edges'], MAX_EDGES):
            if type(edge) is not dict or set(edge) != {'source', 'target', 'kind', 'rank', 'evidence'}:
                raise InvalidData('Invalid edge.')
            if (edge['source'] not in keys or edge['target'] not in keys or edge['kind'] not in RELATIONS
                    or type(edge['rank']) is not int or not 0 <= edge['rank'] <= MAX_ROWS):
                raise InvalidData('Invalid edge endpoints or rank.')
            signature = edge['source'], edge['target'], edge['kind']
            if signature in edge_keys:
                raise InvalidData('Duplicate edge; combine evidence first.')
            edge_keys.add(signature)
            for evidence in _list(edge['evidence'], MAX_EVIDENCE):
                if type(evidence) is not dict or set(evidence) != {'entity', 'property', 'statement_id', 'revision', 'rank', 'source_url', 'provenance', 'checked_at'}:
                    raise InvalidData('Invalid evidence.')
                entity = evidence['entity']
                if entity is not None:
                    if canonical_url(identity('wikidata', 'entity', entity)) != evidence['source_url']:
                        raise InvalidData('Noncanonical evidence URL.')
                elif evidence['source_url'] is not None:
                    raise InvalidData('Unsupported evidence URL.')
                if evidence['provenance'] not in PROVENANCE or evidence['rank'] not in ('normal', 'preferred'):
                    raise InvalidData('Invalid evidence metadata.')
                for field in ('statement_id', 'property'):
                    if evidence[field] is not None:
                        _text(evidence[field], 160)
                revision = evidence['revision']
                if revision is not None and (type(revision) is not int or not 0 < revision < 2**63):
                    raise InvalidData('Invalid source revision.')
                if evidence['provenance'] == 'reconstructed' and (revision is not None or evidence['statement_id'] is not None or evidence['checked_at'] is not None):
                    raise InvalidData('Reconstructed facts have unknown source metadata.')
                stamp(evidence['checked_at'])
        record_keys = set()
        for record in _list(data['records'], MAX_RECORDS):
            if type(record) is not dict or set(record) != {'key', 'outcome'}:
                raise InvalidData('Invalid record.')
            key = _text(record['key'], 160)
            if key in record_keys:
                raise InvalidData('Duplicate cache record.')
            record_keys.add(key)
            result = record['outcome']
            if type(result) is not dict:
                raise InvalidData('Invalid outcome.')
            try:
                valid = outcome(**result)
            except TypeError:
                raise InvalidData('Invalid outcome fields.') from None
            if valid != result:
                raise InvalidData('Invalid outcome.')
        self._data = deepcopy(data)

    def snapshot(self):
        return deepcopy(self._data)

    def to_json(self):
        return _json(self._data)

    @classmethod
    def from_json(cls, payload):
        if type(payload) is not str or len(payload.encode()) > MAX_BYTES:
            raise InvalidData('Snapshot byte budget exceeded.')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise InvalidData('Duplicate JSON field.')
                result[key] = value
            return result
        try:
            data = json.loads(payload, object_pairs_hook=unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(InvalidData('Nonfinite JSON.')))
        except (ValueError, RecursionError):
            raise InvalidData('Invalid snapshot JSON.') from None
        return cls(data)


def resolve_alias(key, aliases):
    """Only explicit singleton verified mappings merge; no guesses on conflict."""
    split_identity(key)
    if type(aliases) is not dict or len(aliases) > MAX_ROWS:
        raise InvalidData('Invalid alias table.')
    visited = set()
    for hop in range(MAX_ALIAS_HOPS + 1):
        if key in visited:
            return outcome('failed', reason='Alias cycle.')
        visited.add(key)
        if key not in aliases:
            return outcome('items', [key])
        targets = _list(aliases[key], 100)
        for target in targets:
            split_identity(target)
            if split_identity(target)[:2] != split_identity(key)[:2]:
                raise InvalidData('Aliases cannot cross namespaces.')
        targets = sorted(set(targets))
        if len(targets) != 1:
            return outcome('ambiguous', targets, reason='Alias has no unique verified target.')
        if hop == MAX_ALIAS_HOPS:
            return outcome('failed', reason='Alias depth budget exceeded.')
        key = targets[0]
    raise AssertionError('Unreachable')


def hardcover_projection(book, rows, *, provenance='supplied'):
    """Validate the real cached JSON field and hydrate in source rank order."""
    if type(book) is not dict:
        raise InvalidData('Invalid Hardcover book.')
    root = positive_id(book.get('id'))
    ids = _list(book.get('cached_similar_book_ids'))
    ranks = {}
    for rank, value in enumerate(ids):
        ranks.setdefault(positive_id(value), rank)
    checked = stamp(book.get('cached_similar_books_updated_at'))
    if book.get('canonical_id') is not None:
        positive_id(book['canonical_id'])
    details = {}
    for row in _list(rows, 100):
        if type(row) is not dict or positive_id(row.get('id')) not in ranks:
            raise InvalidData('Unexpected Hardcover detail identity.')
        key = identity('hardcover', 'book', row['id'])
        item = node(key, row.get('title'), slug=row.get('slug'), fetched_at=checked, provenance=provenance)
        if item['url'] is None:
            raise InvalidData('Invalid Hardcover slug.')
        details.setdefault(row['id'], item)
    items = [dict(details[value], provider_rank=rank) for value, rank in ranks.items()
             if value != root and value in details]
    return outcome('items' if items else 'empty', items, checked_at=checked)


def tmdb_projection(medium, payload, *, relation='tmdb-recommendation', checked_at=None, provenance='supplied'):
    if medium not in ('movie', 'tv') or relation not in ('tmdb-recommendation', 'tmdb-similar') or type(payload) is not dict:
        raise InvalidData('Invalid TMDB projection.')
    items, seen = [], set()
    for rank, row in enumerate(_list(payload.get('results'), 100)):
        if type(row) is not dict:
            raise InvalidData('Invalid TMDB row.')
        key = identity('tmdb', medium, row.get('id'))
        item = node(key, row.get('name' if medium == 'tv' else 'title'), fetched_at=checked_at, provenance=provenance)
        if key not in seen:
            items.append(dict(item, provider_rank=rank, relation=relation))
            seen.add(key)
    return outcome('items' if items else 'empty', items, checked_at=checked_at)


def normalize_isbn(value):
    if type(value) is not str or len(value) > 32:
        return None
    value = value.replace('-', '').replace(' ', '').upper()
    if re.fullmatch(r'[0-9]{9}[0-9X]', value):
        digits = [int(c) if c != 'X' else 10 for c in value]
        if sum((10-i)*digit for i, digit in enumerate(digits)) % 11:
            return None
        prefix = '978' + value[:9]
        return prefix + str((-sum(int(c)*(1 if i % 2 == 0 else 3) for i, c in enumerate(prefix))) % 10)
    if re.fullmatch(r'97[89][0-9]{10}', value):
        if sum(int(c)*(1 if i % 2 == 0 else 3) for i, c in enumerate(value)) % 10 == 0:
            return value
    return None


def isbn_join(work_id, works, editions, hardcover_editions, books):
    """OL work -> editions -> checksum ISBN -> Hardcover book -> canonical ID."""
    identity('openlibrary', 'work', work_id)
    isbn_values = set()
    work_key = '/works/' + work_id
    supplied_work = False
    for work in _list(works):
        if type(work) is not dict:
            raise InvalidData('Invalid OL work.')
        key = work.get('key')
        if type(key) is not str or not key.startswith('/works/'):
            raise InvalidData('Expected an OL work key.')
        identity('openlibrary', 'work', key.removeprefix('/works/'))
        supplied_work |= key == work_key
    for edition in _list(editions):
        if type(edition) is not dict:
            raise InvalidData('Invalid OL edition.')
        edition_key = edition.get('key', '')
        if type(edition_key) is not str or not edition_key.startswith('/books/'):
            raise InvalidData('Expected an OL edition key.')
        identity('openlibrary', 'edition', edition_key.removeprefix('/books/'))
        refs = _list(edition.get('works', []))
        if not supplied_work or not any(type(ref) is dict and ref.get('key') == work_key for ref in refs):
            continue
        for field in ('isbn_10', 'isbn_13'):
            for value in _list(edition.get(field, []), 100):
                if normalized := normalize_isbn(value):
                    isbn_values.add(normalized)
    aliases, known = {}, set()
    for book in _list(books):
        if type(book) is not dict:
            raise InvalidData('Invalid Hardcover canonical row.')
        key = identity('hardcover', 'book', book.get('id'))
        known.add(key)
        if book.get('canonical_id') is not None:
            target = identity('hardcover', 'book', book['canonical_id'])
            aliases.setdefault(key, []).append(target)
    matches = set()
    for edition in _list(hardcover_editions):
        if type(edition) is not dict:
            raise InvalidData('Invalid Hardcover edition.')
        key = identity('hardcover', 'book', edition.get('book_id'))
        if not any(normalize_isbn(edition.get(field)) in isbn_values for field in ('isbn_10', 'isbn_13')):
            continue
        result = resolve_alias(key, aliases)
        if result['status'] != 'items':
            return result
        if key in known:
            matches.update(result['items'])
    return outcome('ambiguous' if len(matches) > 1 else 'items' if matches else 'empty', sorted(matches),
                   reason='No supplied binding.' if not matches else None)


def resolve_documents(entities, *, redirects=None, external_ids=None, provenance='supplied'):
    """Project Wikidata claim envelopes and supplied TMDB external_ids documents."""
    aliases = {}
    for redirect in _list([] if redirects is None else redirects):
        if type(redirect) is not dict:
            raise InvalidData('Invalid redirect.')
        source = identity('wikidata', 'entity', redirect.get('from'))
        target = identity('wikidata', 'entity', redirect.get('to'))
        aliases.setdefault(source, []).append(target)
    nodes, edges, records = {}, {}, []

    def canonical(qid):
        result = resolve_alias(identity('wikidata', 'entity', qid), aliases)
        if result['status'] != 'items':
            raise InvalidData(result['reason'])
        return result['items'][0]

    def add_node(key, title=None):
        if key not in nodes and len(nodes) >= MAX_NODES:
            raise InvalidData('Node budget exceeded.')
        if key not in nodes or title is not None:
            nodes[key] = node(key, title or key, provenance=provenance)

    def add_edge(source, target, kind, evidence):
        add_node(source)
        add_node(target)
        signature = source, target, kind
        if signature not in edges and len(edges) >= MAX_EDGES:
            raise InvalidData('Edge budget exceeded.')
        edge = edges.setdefault(signature, {'source': source, 'target': target, 'kind': kind, 'rank': 0, 'evidence': []})
        if evidence not in edge['evidence']:
            if len(edge['evidence']) >= MAX_EVIDENCE:
                raise InvalidData('Evidence budget exceeded.')
            edge['evidence'].append(evidence)

    for entity in _list(entities, MAX_NODES):
        if type(entity) is not dict:
            raise InvalidData('Invalid entity document.')
        original = entity.get('id')
        key = canonical(original)
        label = entity.get('labels', {}).get('en', {}).get('value', original)
        add_node(key, label)
        claims = entity.get('claims', {})
        if type(claims) is not dict or len(claims) > 100:
            raise InvalidData('Invalid claims.')
        for prop in ('P144', 'P4969', 'P4947', 'P4983', 'P648'):
            for claim in _list(claims.get(prop, []), 100):
                if type(claim) is not dict:
                    raise InvalidData('Invalid statement.')
                rank = claim.get('rank', 'normal')
                if rank == 'deprecated':
                    continue
                if rank not in ('normal', 'preferred'):
                    raise InvalidData('Invalid statement rank.')
                snak = claim.get('mainsnak')
                if type(snak) is not dict:
                    raise InvalidData('Invalid mainsnak.')
                if snak.get('snaktype') in ('novalue', 'somevalue'):
                    continue
                if snak.get('snaktype') != 'value' or snak.get('property') != prop or type(snak.get('datavalue')) is not dict:
                    raise InvalidData('Invalid claim value.')
                value = snak['datavalue'].get('value')
                evidence = {'entity': original, 'property': prop, 'statement_id': claim.get('id'),
                            'revision': entity.get('lastrevid'), 'rank': rank,
                            'source_url': canonical_url(identity('wikidata', 'entity', original)),
                            'provenance': provenance, 'checked_at': entity.get('checked_at')}
                if prop in ('P144', 'P4969'):
                    if type(value) is not dict:
                        raise InvalidData('Expected Wikidata entity reference.')
                    other = canonical(value.get('id'))
                    source, target = (key, other) if prop == 'P144' else (other, key)
                    add_edge(source, target, 'based-on', evidence)
                elif prop in ('P4947', 'P4983'):
                    if type(value) is not str or not re.fullmatch(r'[1-9][0-9]{0,9}', value):
                        raise InvalidData('Invalid TMDB external ID.')
                    other = identity('tmdb', 'movie' if prop == 'P4947' else 'tv', int(value))
                    add_edge(key, other, 'identity', evidence)
                else:
                    if type(value) is not str:
                        raise InvalidData('Invalid OL external ID.')
                    medium = {'W': 'work', 'M': 'edition', 'A': 'author'}.get(value[-1:])
                    other = identity('openlibrary', medium, value)
                    add_edge(key, other, 'identity', evidence)
    for document in _list([] if external_ids is None else external_ids, MAX_NODES):
        if type(document) is not dict or type(document.get('external_ids')) is not dict:
            raise InvalidData('Invalid TMDB external_ids document.')
        source = identity('tmdb', document.get('medium'), document.get('id'))
        qid = document['external_ids'].get('wikidata_id')
        add_node(source)
        if qid is None:
            records.append({'key': 'external:' + source, 'outcome': outcome('empty')})
            continue
        target = canonical(qid)
        evidence = {'entity': qid, 'property': 'external_ids.wikidata_id', 'statement_id': None,
                    'revision': None, 'rank': 'normal', 'source_url': canonical_url(identity('wikidata', 'entity', qid)),
                    'provenance': provenance, 'checked_at': document.get('checked_at')}
        add_edge(target, source, 'identity', evidence)
    return Cache({'schema': SCHEMA, 'nodes': list(nodes.values()), 'edges': list(edges.values()), 'records': records})


def paths_from(cache, root):
    """Exact simple paths: <=1 neighbor and <=2 facts; identity costs no fact hop."""
    data = cache.snapshot()
    keys = {item['key'] for item in data['nodes']}
    if root not in keys:
        return []
    adjacent = {key: [] for key in keys}
    for edge in data['edges']:
        adjacent[edge['source']].append((edge['target'], edge, 'forward'))
        if edge['kind'] not in NEIGHBORS:
            adjacent[edge['target']].append((edge['source'], edge, 'reverse'))
    for values in adjacent.values():
        values.sort(key=lambda item: (item[1]['kind'] not in NEIGHBORS, item[1]['rank'], item[0], item[1]['kind']))
    queue = deque([(root, [], {root}, 0, 0)])
    results, states = [], 0
    while queue and states < MAX_PATH_STATES:
        current, path, visited, neighbors, facts = queue.popleft()
        states += 1
        for target, edge, direction in adjacent[current]:
            n = neighbors + (edge['kind'] in NEIGHBORS)
            f = facts + (edge['kind'] not in NEIGHBORS | {'identity'})
            if target in visited or n > 1 or f > 2 or len(path) >= 8:
                continue
            step = dict(deepcopy(edge), traversal=direction, from_key=current, to_key=target)
            next_path = path + [step]
            results.append({'key': target, 'path': next_path, 'neighbor_hops': n, 'fact_hops': f})
            # Explicit cap also bounds a high-fanout frontier before allocation.
            if len(results) >= MAX_PATH_STATES:
                return results
            # An OL author reference is metadata, never a bridge between books.
            if split_identity(target)[1] != 'author':
                queue.append((target, next_path, visited | {target}, n, f))
    return results


def sample_context(trail_id):
    """Return a fresh plain template context for one fixed offline sample only."""
    if type(trail_id) is not str or trail_id not in SAMPLE_IDS:
        raise ValueError('Unknown cross-media sample selector.')
    with FIXTURE.open(encoding='utf-8') as handle:
        payload = handle.read(MAX_BYTES + 1)
    if len(payload.encode()) > MAX_BYTES:
        raise InvalidData('Fixture byte budget exceeded.')
    fixture = json.loads(payload)
    trail = fixture['trails'][trail_id]
    projection = None
    synthetic_trail = trail_id == 'synthetic-lanes'
    if synthetic_trail:
        from shelf.cross_media_ranking import rank
        synthetic = fixture['synthetic']
        cache = Cache(synthetic['cache'])
        projection = rank(synthetic['run'], cache, actor_id=7001, profile_id=7002,
                          run_id='synthetic-run', expected_revision=5)
    else:
        cache = resolve_documents(fixture['entities'], redirects=fixture['redirects'],
                                  external_ids=fixture['external_ids'], provenance='reconstructed')
    data = cache.snapshot()
    by_key = {item['key']: item for item in data['nodes']}
    reachable = paths_from(cache, trail['root'])
    cards = []
    seen = set()
    for result in reachable:
        if result['key'] not in seen:
            cards.append(dict(by_key[result['key']], **{k: v for k, v in result.items() if k != 'key'}))
            seen.add(result['key'])
    return deepcopy({'schema': 'cross-media-sample-v1', 'trail_id': trail_id,
                     'sample_title': trail['title'], 'sample_ids': list(SAMPLE_IDS),
                     'temporary_notice': 'SAMPLE: reconstructed research facts and simulated rankings; not the owner’s likes. No live fetching.',
                     'provenance_label': ('SYNTHETIC human examples and SIMULATED provider rankings.' if synthetic_trail
                                          else 'RECONSTRUCTED research fixtures; not captured API responses.'),
                     'freshness_label': ('Synthetic timestamps are fictional; provider freshness unknown.' if synthetic_trail
                                         else 'Source revision, statement IDs and freshness unknown.'),
                     'ranking_label': 'SIMULATED provider rankings and synthetic human history.',
                     'mapping_status': 'not-applicable' if synthetic_trail else 'unresolved',
                     'mapping_label': ('Wholly fictional lane example; no real catalogue bindings.' if synthetic_trail
                                       else 'No supplied Hardcover/ISBN binding for these known works.'),
                     'root': by_key[trail['root']], 'cards': cards, 'lanes': projection['lanes'] if projection else [],
                     'selection': projection['selection'] if projection else [],
                     'status': projection['status'] if projection else 'factual-trail',
                     'outcomes': fixture['outcome_examples'],
                     'limits': {'lanes': 5, 'neighbor_hops': 1, 'fact_hops': 2}})
