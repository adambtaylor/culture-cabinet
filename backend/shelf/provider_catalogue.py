"""Small, read-only provider catalogue. No provider identity or taste inference.

Only enumerated operations reach curl. Provider text remains plain, bounded data;
no images, caches on disk, user-library operations, or model calls are supported.
"""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from datetime import datetime, timezone
from enum import StrEnum
import json
import os
import re
import subprocess
from urllib.parse import urlencode

from shelf import book_metadata, credential_store
from shelf.provider_smoke import CA, CURL

MAX_BODY = 512 * 1024
BOOK_FIELDS = ('id title slug description book_category_id canonical_id image{url width height} '
               'book_series(limit:12){book_id series_id position details compilation series{id name slug}} '
               'editions(limit:12){id book_id canonical_id edition_format edition_information}')
BOOK_NEIGHBORS = 'query Neighbors($id:Int!){books_by_pk(id:$id){cached_similar_book_ids}}'
BOOK_SEARCH = 'query Search($q:String!){search(query:$q,per_page:9,page:1){ids error}}'
BOOK_DETAILS = ('query Details($ids:[Int!]!){books(where:{id:{_in:$ids}},limit:9){'
                + BOOK_FIELDS + '}}')
BOOK_DETAILS_MINIMAL = 'query Details($ids:[Int!]!){books(where:{id:{_in:$ids}},limit:9){id title slug}}'
BOOK_STARTER_MINIMAL = 'query Deck{books(limit:6,order_by:[{users_count:desc}]){id title slug}}'
BOOK_DECK = ('query Deck{books(limit:6,order_by:[{users_count:desc}]){'
             + BOOK_FIELDS + '}}')
BOOK_CATEGORY = ('query GraphicNovels($offset:Int!){books('
                 'where:{book_category_id:{_eq:4}},'
                 'limit:9,offset:$offset,order_by:[{users_count:desc},{id:asc}]){'
                 + BOOK_FIELDS + '}}')
BOOK_PAGE_SIZE = 9
BOOK_MAX_PAGES = 10
# Public editorial starting points, not inferred preferences.
LANES = {
    'movie_science': ('movie', 878, 'Exploratory: science fiction films'),
    'movie_drama': ('movie', 18, 'Exploratory: drama films'),
    'tv_fantasy': ('tv', 10765, 'Exploratory: science fiction / fantasy series'),
    'tv_comedy': ('tv', 35, 'Exploratory: comedy series'),
}
BOOK_LANE = 'Exploratory: popular books (provider users_count)'


class Status(StrEnum):
    AUTH = 'auth'
    METADATA = 'metadata'
    RATE = 'rate'
    TIMEOUT = 'timeout'
    TLS = 'tls'
    BAD_RESPONSE = 'bad_response'
    GRAPHQL = 'graphql'
    INVALID = 'invalid'
    UNAVAILABLE = 'unavailable'


class CatalogueError(Exception):
    def __init__(self, status):
        self.status = Status(status)
        super().__init__(self.status.value)


@dataclass(frozen=True)
class Candidate:
    key: str
    provider: str
    source_id: int
    medium: str
    title: str
    description: str
    url: str
    fetched_at: str
    lane: str
    lane_label: str
    origin: str = 'editorial'
    root: str = ''
    trailer_status: str = 'Trailer unavailable: video metadata has not been verified.'
    book_metadata: dict | None = None

    def data(self):
        return asdict(self)


def _id(value):
    if type(value) is not int or not 0 < value <= 2147483647:
        raise CatalogueError(Status.BAD_RESPONSE)
    return value


def _text(value, limit, *, required=False):
    if value is None and not required:
        return ''
    if type(value) is not str or (required and not value.strip()):
        raise CatalogueError(Status.BAD_RESPONSE)
    # Do not interpret provider HTML. Templates must use normal autoescaping.
    return value[:limit]


def parse_candidates(provider, medium, rows, *, lane, lane_label,
                     origin='editorial', root='', limit=9):
    if type(rows) is not list or len(rows) > 100:
        raise CatalogueError(Status.BAD_RESPONSE)
    fetched = datetime.now(timezone.utc).isoformat()
    output = []
    seen = set()
    if (provider, medium) not in {('tmdb', 'movie'), ('tmdb', 'tv'), ('hardcover', 'book')}:
        raise CatalogueError(Status.INVALID)
    for row in rows[:limit]:
        if type(row) is not dict:
            raise CatalogueError(Status.BAD_RESPONSE)
        source_id = _id(row.get('id'))
        title = _text(row.get('name') if medium == 'tv' else row.get('title'), 300, required=True)
        if provider == 'hardcover':
            slug = row.get('slug')
            if type(slug) is not str or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug, re.ASCII) or len(slug) > 200:
                raise CatalogueError(Status.BAD_RESPONSE)
            url = 'https://hardcover.app/books/' + slug
            metadata = book_metadata.parse(row)
            description = metadata['synopsis']['text']
        else:
            url = f'https://www.themoviedb.org/{medium}/{source_id}'
            description = _text(row.get('overview'), 2000)
            metadata = None
        key = f'{provider}:{medium}:{source_id}'
        if key not in seen:
            output.append(Candidate(key, provider, source_id, medium, title, description,
                                    url, fetched, lane, lane_label, origin, root,
                                    book_metadata=metadata))
            seen.add(key)
    if provider == 'hardcover':
        book_metadata.annotate_canonical(output, rows[:limit])
    return output


def validate_query(query):
    if type(query) is not str or not 1 <= len(query.strip()) <= 100 or len(query) > 100:
        raise CatalogueError(Status.INVALID)
    if any(ord(c) < 32 for c in query):
        raise CatalogueError(Status.INVALID)
    return query.strip()


@dataclass(frozen=True, repr=False)
class MediaOperation:
    """Fixed wire operation only; no credential, permit or rights are implied."""
    source: str
    operation: str
    term: str = field(default='', repr=False)
    ids: tuple[int, ...] = field(default=(), repr=False)
    projection: str = 'legacy'

    def __post_init__(self):
        allowed = {'tmdb_movie': ('search_movie', 'starter_movie_science'),
                   'tmdb_tv': ('search_tv', 'starter_tv_fantasy'),
                   'hardcover': ('book_search', 'book_details', 'starter_books')}
        if (type(self.source) is not str or self.operation not in allowed.get(self.source, ())
                or type(self.ids) is not tuple or len(self.ids) > 9
                or type(self.projection) is not str or self.projection not in ('legacy', 'native_minimal')
                or self.projection == 'native_minimal' and self.source != 'hardcover'):
            raise CatalogueError(Status.INVALID)
        if self.operation.startswith('starter_'):
            if self.term != '' or self.ids or self.source == 'hardcover' and self.projection != 'native_minimal':
                raise CatalogueError(Status.INVALID)
        elif self.operation == 'book_details':
            if self.term != '':
                raise CatalogueError(Status.INVALID)
            for value in self.ids:
                _id(value)
            if self.projection == 'native_minimal' and (not self.ids or len(set(self.ids)) != len(self.ids)):
                raise CatalogueError(Status.INVALID)
        elif self.ids or validate_query(self.term) != self.term:
            raise CatalogueError(Status.INVALID)

    @property
    def provider(self):
        return 'hardcover' if self.source == 'hardcover' else 'tmdb'

    @property
    def method(self):
        return 'POST' if self.provider == 'hardcover' else 'GET'

    @property
    def origin(self):
        return 'https://api.hardcover.app' if self.provider == 'hardcover' else 'https://api.themoviedb.org'

    @property
    def path(self):
        if self.provider == 'hardcover':
            return '/v1/graphql'
        return ('/3/discover/' + ('movie' if self.source == 'tmdb_movie' else 'tv')
                if self.operation.startswith('starter_')
                else '/3/search/' + self.operation.removeprefix('search_'))

    @property
    def query(self):
        if self.provider == 'hardcover':
            return ''
        params = {'include_adult': 'false', 'language': 'en-US', 'page': 1}
        if self.operation.startswith('starter_'):
            params['with_genres'] = 878 if self.source == 'tmdb_movie' else 10765
        else:
            params['query'] = self.term
        return urlencode(params)

    @property
    def body(self):
        if self.provider != 'hardcover':
            return b''
        if self.operation == 'starter_books':
            return json.dumps({'query': BOOK_STARTER_MINIMAL, 'variables': {}}, ensure_ascii=True).encode('ascii')
        document = BOOK_DETAILS if self.operation == 'book_details' else BOOK_SEARCH
        if self.operation == 'book_details' and self.projection == 'native_minimal':
            document = BOOK_DETAILS_MINIMAL
        variables = {'ids': list(self.ids)} if self.operation == 'book_details' else {'q': self.term}
        # Legacy catalogue callers keep their wider document and exact bytes.
        # Native Enhance selects the closed minimal document here: its preview
        # digest and authorized send use this SAME serializer and selected bytes.
        return json.dumps({'query': document, 'variables': variables}, ensure_ascii=True).encode('ascii')

    @property
    def material(self):
        return {'method': self.method, 'origin': self.origin, 'path': self.path,
                'query': self.query, 'body_digest': sha256(self.body).hexdigest()}

    @property
    def operation_digest(self):
        from .disclosure import canonical_bytes
        return sha256(canonical_bytes(self.material)).hexdigest()


def media_operation(request):
    from .source_adapters import SourceRequest
    if type(request) is not SourceRequest:
        raise CatalogueError(Status.INVALID)
    if request.operation.startswith('starter_'):
        return MediaOperation(request.source, request.operation,
            projection='native_minimal' if request.source == 'hardcover' else 'legacy')
    if request.operation == 'book_details':
        return MediaOperation(request.source, request.operation, ids=request.ids, projection='native_minimal')
    if len(request.terms) != 1:
        raise CatalogueError(Status.INVALID)
    return MediaOperation(request.source, request.operation, validate_query(request.terms[0]),
        projection='native_minimal' if request.source == 'hardcover' else 'legacy')


def _media_arguments(operation):
    args = ['--request', operation.method]
    if operation.body:
        args += ['--header', 'Content-Type: application/json', '--data-binary', operation.body.decode('ascii')]
    return args + ['--url', operation.origin + operation.path + ('?' + operation.query if operation.query else '')]


def _command(operation, value=None):
    """Map an enum-like operation to fixed documents/paths, never a caller URL."""
    common = [CURL, '-q', '--silent', '--proto', '=https', '--proto-redir', '=https',
              '--cacert', CA, '--proxy', '', '--noproxy', '*', '--no-netrc',
              '--max-redirs', '0', '--retry', '0', '--connect-timeout', '10',
              '--max-time', '10', '--max-filesize', str(MAX_BODY),
              '--header', '@-', '--header', 'Accept: application/json',
              '--write-out', '\n%{http_code}']
    params = {'include_adult': 'false', 'language': 'en-US', 'page': 1}
    document = None
    variables = {}
    if operation in ('search_movie', 'search_tv', 'book_search', 'book_details'):
        source = {'search_movie': 'tmdb_movie', 'search_tv': 'tmdb_tv'}.get(operation, 'hardcover')
        if operation == 'book_details':
            if type(value) is not list:
                raise CatalogueError(Status.INVALID)
            wire = MediaOperation(source, operation, ids=tuple(value))
        else:
            wire = MediaOperation(source, operation, validate_query(value))
        return wire.provider, common + _media_arguments(wire)
    if operation in ('search_movie', 'search_tv'):
        medium = operation.removeprefix('search_')
        params['query'] = validate_query(value)
        path = f'search/{medium}'
    elif operation in LANES:
        medium, genre, _ = LANES[operation]
        path = f'discover/{medium}'
        params['with_genres'] = genre
    elif operation in ('neighbors_movie', 'neighbors_tv'):
        medium = operation.removeprefix('neighbors_')
        path = f'{medium}/{_id(value)}/recommendations'
    elif operation == 'book_search':
        document, variables = BOOK_SEARCH, {'q': validate_query(value)}
    elif operation == 'book_details':
        if type(value) is not list or len(value) > 9:
            raise CatalogueError(Status.INVALID)
        variables = {'ids': [_id(v) for v in value]}
        document = BOOK_DETAILS
    elif operation == 'book_neighbors':
        document, variables = BOOK_NEIGHBORS, {'id': _id(value)}
    elif operation == 'book_deck':
        document = BOOK_DECK
    elif operation == 'book_category':
        if (type(value) is not dict or set(value) != {'query', 'page'}
                or type(value['page']) is not int or not 1 <= value['page'] <= BOOK_MAX_PAGES):
            raise CatalogueError(Status.INVALID)
        if value['query'] != '':
            validate_query(value['query'])
        # Hardcover disables _ilike for API tokens. The optional title filter
        # is local to this category page, never sent as a provider operator.
        document = BOOK_CATEGORY
        variables = {'offset': (value['page'] - 1) * BOOK_PAGE_SIZE}
    else:
        raise CatalogueError(Status.INVALID)
    if document is not None:
        payload = json.dumps({'query': document, 'variables': variables}, ensure_ascii=True)
        return 'hardcover', common + ['--request', 'POST', '--header', 'Content-Type: application/json',
                                      '--data-binary', payload, '--url', 'https://api.hardcover.app/v1/graphql']
    return 'tmdb', common + ['--request', 'GET', '--url',
                            'https://api.themoviedb.org/3/' + path + '?' + urlencode(params)]


def _response(provider, returncode, output):
    if returncode:
        if returncode == 28:
            raise CatalogueError(Status.TIMEOUT)
        if returncode in (35, 51, 58, 60, 77, 80, 82, 83, 90, 91):
            raise CatalogueError(Status.TLS)
        raise CatalogueError(Status.BAD_RESPONSE)
    if len(output) > MAX_BODY + 4:
        raise CatalogueError(Status.BAD_RESPONSE)
    body, sep, code = output.rpartition(b'\n')
    if not sep or len(code) != 3 or not code.isdigit():
        raise CatalogueError(Status.BAD_RESPONSE)
    if code in (b'401', b'403'):
        raise CatalogueError(Status.AUTH)
    if code == b'429':
        raise CatalogueError(Status.RATE)
    if code != b'200' or len(body) > MAX_BODY:
        raise CatalogueError(Status.BAD_RESPONSE)
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise CatalogueError(Status.BAD_RESPONSE) from None
    if type(result) is not dict:
        raise CatalogueError(Status.BAD_RESPONSE)
    if provider == 'hardcover':
        if 'errors' in result:
            raise CatalogueError(Status.GRAPHQL)
        result = result.get('data')
        if type(result) is not dict:
            raise CatalogueError(Status.BAD_RESPONSE)
    return result


def _send(command, provider, secret):
    header = bytearray(b'Authorization: Bearer ')
    header.extend(secret)
    header.extend(b'\n')
    try:
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env={'LANG': 'C', 'LC_ALL': 'C'},
                              close_fds=True) as child:
            try:
                output, _ = child.communicate(input=header, timeout=11)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise CatalogueError(Status.TIMEOUT) from None
            return _response(provider, child.returncode, output)
    finally:
        header[:] = b'\0' * len(header)


def _request(operation, value=None, *, actor_id, profile_id):
    provider, command = _command(operation, value)
    if type(actor_id) is not int or type(profile_id) is not int or (actor_id, profile_id) != (1, 1):
        raise CatalogueError(Status.AUTH)
    store = record = None
    try:
        if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (1000, 1000, 1000, 1000):
            raise CatalogueError(Status.METADATA)
        store = credential_store.open_store(provider=provider)
        if store.context != credential_store._provider_context(provider) or (store.uid, store.gid) != (1000, 1000):
            raise CatalogueError(Status.METADATA)
        credential_store.lock_store(store)
        record = credential_store.read_record(store)
        credential_store.validate_metadata(record.metadata, now=credential_store.utc_now(), provider=provider)
        if (record.metadata.binding.actor_id, record.metadata.binding.profile_id) != ('1', '1'):
            raise CatalogueError(Status.METADATA)
        credential_store._secret_text(record.secret)
        # Release the store lock and all descriptors before bounded HTTP I/O.
        store.close()
        store = None
        return _send(command, provider, record.secret)
    except credential_store.Refused:
        raise CatalogueError(Status.METADATA) from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise CatalogueError(Status.BAD_RESPONSE) from None
    finally:
        if record is not None:
            record.close()
        if store is not None:
            try:
                store.close()
            except OSError:
                pass


def _ranked_book_candidates(ids, rows, **metadata):
    """Detail queries do not promise the source IDs' search/neighbor order."""
    if type(rows) is not list or len(rows) > 100:
        raise CatalogueError(Status.BAD_RESPONSE)
    for row in rows:
        if type(row) is not dict or _id(row.get('id')) not in ids:
            raise CatalogueError(Status.BAD_RESPONSE)
    cards = parse_candidates('hardcover', 'book', rows, limit=100, **metadata)
    by_id = {card.source_id: card for card in cards}
    return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]


def search(medium, query, *, actor_id, profile_id):
    query = validate_query(query)
    auth = {'actor_id': actor_id, 'profile_id': profile_id}
    if medium in ('movie', 'tv'):
        rows = _request('search_' + medium, query, **auth).get('results')
        provider = 'tmdb'
    elif medium == 'book':
        result = _request('book_search', query, **auth).get('search')
        if type(result) is not dict or result.get('error'):
            raise CatalogueError(Status.GRAPHQL)
        ids = result.get('ids')
        if type(ids) is not list or len(ids) > 9:
            raise CatalogueError(Status.BAD_RESPONSE)
        ids = list(dict.fromkeys(_id(v) for v in ids))
        if not ids:
            return []
        rows = _request('book_details', ids, **auth).get('books')
        return _ranked_book_candidates(ids, rows, lane='search:book',
            lane_label='Explicit human title search', origin='search')
    else:
        raise CatalogueError(Status.INVALID)
    return parse_candidates(provider, medium, rows, lane='search:' + medium,
                            lane_label='Explicit human title search', origin='search')


def browse_books(category, query='', page=1, *, actor_id, profile_id):
    """One category-filtered page; optional title matching is within that page."""
    if category != 'graphic_novels':
        raise CatalogueError(Status.INVALID)
    # Validate before the request, including under offline transport fixtures.
    value = {'query': query, 'page': page}
    _command('book_category', value)
    rows = _request('book_category', value, actor_id=actor_id, profile_id=profile_id).get('books')
    if (type(rows) is not list or len(rows) > BOOK_PAGE_SIZE
            or any(type(row) is not dict or type(row.get('book_category_id')) is not int
                   or row['book_category_id'] != 4 for row in rows)):
        raise CatalogueError(Status.BAD_RESPONSE)
    cards = parse_candidates('hardcover', 'book', rows, lane='books_graphic_novels',
                             lane_label='Graphic novels · Hardcover category 4, popular first', origin='search')
    if query:
        title = validate_query(query).casefold()
        cards = [card for card in cards if title in card.title.casefold()]
    # A locally filtered page may be empty even when another source page exists.
    return cards, len(rows) == BOOK_PAGE_SIZE and page < BOOK_MAX_PAGES


def initial_deck(*, actor_id, profile_id):
    cards, failures = [], []
    auth = {'actor_id': actor_id, 'profile_id': profile_id}
    for lane, (medium, genre, label) in LANES.items():
        try:
            rows = _request(lane, **auth).get('results')
            cards.extend(parse_candidates('tmdb', medium, rows, lane=lane, lane_label=label, limit=6))
        except CatalogueError as error:
            failures.append({'lane': lane, 'status': error.status.value})
    try:
        rows = _request('book_deck', **auth).get('books')
        cards.extend(parse_candidates('hardcover', 'book', rows, lane='books_popular',
                                      lane_label=BOOK_LANE, limit=6))
    except CatalogueError as error:
        failures.append({'lane': 'books_popular', 'status': error.status.value})
    return cards, failures


def neighbors(root, *, actor_id, profile_id):
    # Caller must authorize an existing explicit human positive root. Neighbor
    # candidates are never eligible to recursively initiate provider traversal.
    if root['provider'] == 'hardcover' and root['medium'] == 'book' and root['origin'] != 'neighbor':
        auth = {'actor_id': actor_id, 'profile_id': profile_id}
        row = _request('book_neighbors', _id(root['source_id']), **auth).get('books_by_pk')
        ids = row.get('cached_similar_book_ids') if type(row) is dict else None
        if type(ids) is not list or len(ids) > 1000 or any(type(i) is not int or not 0 < i <= 2147483647 for i in ids):
            raise CatalogueError(Status.UNAVAILABLE)
        ids = list(dict.fromkeys(i for i in ids if i != root['source_id']))[:9]
        if not ids:
            return []
        rows = _request('book_details', ids, **auth).get('books')
        return _ranked_book_candidates(ids, rows, lane='root:' + root['key'],
            lane_label='Hardcover cached similar books for ' + root['title'], origin='neighbor', root=root['key'])
    if root['provider'] != 'tmdb' or root['medium'] not in ('movie', 'tv') or root['origin'] == 'neighbor':
        raise CatalogueError(Status.INVALID)
    rows = _request('neighbors_' + root['medium'], _id(root['source_id']),
                    actor_id=actor_id, profile_id=profile_id).get('results')
    return parse_candidates('tmdb', root['medium'], rows, lane='root:' + root['key'],
                            lane_label='TMDB neighbors of ' + root['title'], origin='neighbor', root=root['key'])
