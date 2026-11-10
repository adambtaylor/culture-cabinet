"""Closed nonpersonal source checks; no credential lookup, send or readiness grant.

Taste owns one reserved native intent and its current owner/session/generation
binding. A parsed PASS describes only this operation, never inference readiness.
"""
from dataclasses import dataclass
from hashlib import sha256

from .disclosure import canonical_bytes
from .discovery_sources import SourceError, strict_json


@dataclass(frozen=True)
class SourceCheckOperation:
    provider: str

    def __post_init__(self):
        if type(self.provider) is not str or self.provider not in ('tmdb', 'hardcover'):
            raise SourceError('Unsupported source check.')

    @property
    def version(self):
        return 'source-check/v1'

    @property
    def method(self):
        return 'GET' if self.provider == 'tmdb' else 'POST'

    @property
    def origin(self):
        return ('https://api.themoviedb.org' if self.provider == 'tmdb'
                else 'https://api.hardcover.app')

    @property
    def path(self):
        return '/3/authentication' if self.provider == 'tmdb' else '/v1/graphql'

    @property
    def query(self):
        return ''

    @property
    def body(self):
        # Preserve the existing minimal public smoke query, without invoking
        # that CLI's credential discovery or adding a personal search variable.
        return b'' if self.provider == 'tmdb' else b'{"query":"{ books(limit:1) { id } }"}'

    @property
    def material(self):
        return {'provider': self.provider, 'version': self.version,
                'method': self.method, 'origin': self.origin, 'path': self.path,
                'query': self.query, 'body_digest': sha256(self.body).hexdigest()}

    @property
    def operation_digest(self):
        return sha256(canonical_bytes(self.material)).hexdigest()

    @property
    def test_scope(self):
        return 'authentication_only' if self.provider == 'tmdb' else 'minimal_public_read_only'


@dataclass(frozen=True)
class SourceCheckResult:
    provider: str
    operation_digest: str
    state: str
    code: str
    test_scope: str
    http_status: int


def parse_source_check(operation, status, body):
    """Reduce hostile replies to fixed safe codes, retaining no provider text/ID.

    An empty public collection is valid; it proves the minimal read shape, not
    inventory, personal taste, account identity or recommendation usefulness.
    """
    if (type(operation) is not SourceCheckOperation or type(status) is not int
            or not 100 <= status <= 599 or type(body) is not bytes or len(body) > 262144):
        raise SourceError('Invalid source check response.')
    code = ('AUTHENTICATION_FAILED' if status in (401, 403) else
            'RATE_LIMITED' if status == 429 else 'SOURCE_UNAVAILABLE')
    if status == 200:
        code = 'BAD_RESPONSE'
        try:
            document = strict_json(body)
            if type(document) is dict and not any(k in document for k in ('errors', 'error')):
                if operation.provider == 'tmdb':
                    valid = document.get('success') is True
                else:
                    data = document.get('data')
                    rows = data.get('books') if type(data) is dict and set(data) == {'books'} else None
                    valid = (type(rows) is list and len(rows) <= 1
                             and all(type(row) is dict and set(row) == {'id'}
                                     and type(row['id']) is int and 0 < row['id'] <= 2147483647
                                     for row in rows))
                if valid:
                    code = 'OK'
        except (SourceError, ValueError, UnicodeError, RecursionError):
            pass  # Never retain an error message or echo a private bearer.
    return SourceCheckResult(operation.provider, operation.operation_digest,
        'passed' if code == 'OK' else 'failed', code, operation.test_scope, status)
