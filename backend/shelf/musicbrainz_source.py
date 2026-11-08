"""Fixed anonymous core-recording/artist operations through existing ports.

An artist name is a disclosed query input, not a verified favourite/artist ID.
This module never discovers credentials, grants consent or enables a policy.
"""
from dataclasses import dataclass
from hashlib import sha256
import re
from threading import RLock
from urllib.parse import urlencode, urlsplit

from .disclosure import SCALARS, canonical_bytes
from .enhance_contract import require, utc
from .discovery_sources import SourceError
from .source_adapters import (SourceAuthorityIssuer, SourceBackend, SourcePermit,
                              SourceReply, SourceRequest)
from .source_evidence import FieldPolicy

SOURCE = 'musicbrainz_recording'
ORIGIN = 'https://musicbrainz.org'
PATH = '/ws/2/recording/'
OPERATION = 'recordings_by_artist_name'
ARTIST_SOURCE = 'musicbrainz_artist'
ARTIST_PATH = '/ws/2/artist/'
ARTIST_OPERATION = 'artists_by_name'


def _operation_spec(source):
    # Two explicit wire identities, never an arbitrary endpoint/query builder.
    require(type(source) is str and source in (SOURCE, ARTIST_SOURCE), 'AUTHORITY_DENIED')
    return (('recording', OPERATION, PATH, 'artistname') if source == SOURCE else
            ('artist', ARTIST_OPERATION, ARTIST_PATH, 'artist'))


def _identity(value):
    if (type(value) is not str or not 20 <= len(value) <= 200
            or any(not 32 <= ord(char) < 127 for char in value)):
        raise SourceError('Invalid source application identity.')
    match = re.fullmatch(r'CultureCabinet/[A-Za-z0-9._-]{1,32} \(([^()]+)\)', value)
    if not match:
        raise SourceError('Invalid source application identity.')
    contact = match.group(1)
    if re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", contact):
        return
    try:
        parsed = urlsplit(contact)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and parsed.port in (None, 80, 443) and not parsed.query and not parsed.fragment
                 and not any(char.isspace() for char in contact))
    except ValueError:
        valid = False
    if not valid:
        raise SourceError('Invalid source application contact.')


@dataclass(frozen=True, repr=False)
class MusicBrainzOperation:
    artist_name: str
    application_identity: str
    source: str = SOURCE

    def __post_init__(self):
        _operation_spec(self.source)
        if (type(self.artist_name) is not str or not 1 <= len(self.artist_name) <= 200
                or not self.artist_name.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in self.artist_name)):
            raise SourceError('Invalid source artist name.')
        try:
            self.artist_name.encode('utf-8')
        except UnicodeError:
            raise SourceError('Invalid source artist encoding.') from None
        _identity(self.application_identity)

    @property
    def query(self):
        # Quoting alone is insufficient: escape Lucene metacharacters BEFORE
        # URL encoding so a supplied name cannot add fields/operators/parameters.
        literal = ''.join('\\' + char if char in '+-&|!(){}[]^"~*?:\\/' else char
                          for char in self.artist_name)
        return urlencode((('query', _operation_spec(self.source)[3] + ':"' + literal + '"'),
                          ('fmt', 'json'), ('limit', '9'), ('offset', '0')))

    @property
    def url(self):
        return ORIGIN + _operation_spec(self.source)[2] + '?' + self.query

    @property
    def body(self):
        return b''

    @property
    def material(self):
        return {'method': 'GET', 'origin': ORIGIN, 'path': _operation_spec(self.source)[2], 'query': self.query,
            'headers': [['accept', 'application/json'], ['accept-encoding', 'identity'],
                        ['user-agent', self.application_identity]],
            'body_digest': sha256(self.body).hexdigest()}

    @property
    def operation_digest(self):
        return sha256(canonical_bytes(self.material)).hexdigest()


def musicbrainz_operation(request, application_identity):
    require(type(request) is SourceRequest, 'AUTHORITY_DENIED')
    category, operation, _, _ = _operation_spec(request.source)
    require(request.category == category and request.operation == operation
            and len(request.terms) == 1 and not request.ids, 'AUTHORITY_DENIED')
    return MusicBrainzOperation(request.terms[0], application_identity, request.source)


def musicbrainz_template(source=SOURCE):
    """Canonical fixed template for the source issuer's current-profile binding."""
    category, operation, path, query_field = _operation_spec(source)
    return {'source': source, 'method': 'GET', 'origin': ORIGIN, 'path': path,
            'operation': operation, 'query_field': query_field,
            'fixed_parameters': {'fmt': 'json', 'limit': 9, 'offset': 0},
            'projection': (['recording_mbid', 'title', 'source_url'] if category == 'recording'
                           else ['artist_mbid', 'name', 'source_url'])}


@dataclass(repr=False)
class _Context:
    permit: SourcePermit
    request: SourceRequest
    plan: object
    operation: MusicBrainzOperation
    sent: bool = False


class MusicBrainzBackend(SourceBackend):
    """Retained issuer capabilities plus one bounded keyless HTTP operation."""
    def __init__(self, issuer, limiter, *, clock, source=SOURCE):
        from .discovery_transport import MediaHostLimiter
        require(isinstance(issuer, SourceAuthorityIssuer), 'CAPABILITY_UNAVAILABLE')
        require(type(limiter) is MediaHostLimiter and limiter.provider == 'musicbrainz',
                'CAPABILITY_UNAVAILABLE')
        require(callable(getattr(clock, 'now', None)), 'CAPABILITY_UNAVAILABLE')
        _operation_spec(source)
        self.issuer, self.limiter, self.clock = issuer, limiter, clock
        self.source, self._contexts, self._lock = source, {}, RLock()

    def _context(self, permit):
        with self._lock:
            value = self._contexts.get(id(permit))
        require(value is not None and value.permit is permit, 'AUTHORITY_DENIED')
        return value

    def authorize(self, request, plan, *, now):
        # A recording issuer/grant/backend cannot be borrowed for artist use.
        require(type(request) is SourceRequest and request.source == self.source,
                'AUTHORITY_DENIED')
        operation = self.issuer.operation_for(request, plan, now=now)
        require(type(operation) is MusicBrainzOperation
                and operation == musicbrainz_operation(request, operation.application_identity),
                'PREVIEW_STALE')
        permit = self.issuer.authorize(request, plan, operation, now=now)
        require(type(permit) is SourcePermit and type(permit.credential_generation) is int
                and permit.credential_generation == 0, 'AUTHORITY_DENIED')
        permit.verify(request, plan, now)
        with self._lock:
            self._contexts = {key: value for key, value in self._contexts.items()
                              if utc(now) < value.permit.expires_at}
            require(len(self._contexts) < 128, 'CAPABILITY_UNAVAILABLE')
            existing = self._contexts.get(id(permit))
            require(existing is None or existing.permit is permit
                    and existing.request == request and existing.plan == plan
                    and existing.operation == operation, 'AUTHORITY_DENIED')
            if existing is None:
                self._contexts[id(permit)] = _Context(permit, request, plan, operation)
        self.policy(permit, now=now)
        return permit

    def revalidate(self, permit, request, plan, *, now):
        context = self._context(permit)
        require(context.request == request and context.plan == plan, 'AUTHORITY_DENIED')
        permit.verify(request, plan, now)
        return self.issuer.revalidate(permit, request, plan, context.operation, now=now) is True

    def policy(self, permit, *, now):
        context = self._context(permit)
        require(self.revalidate(permit, context.request, context.plan, now=now), 'PREVIEW_STALE')
        policy, model_current = self.issuer.policy_context(permit, context.operation, now=now)
        require(type(policy) is FieldPolicy and policy.current(now)
                and policy.ref == permit.policy_ref and policy.revision == permit.policy_revision
                and policy.allows('*', 'fetch'), 'AUTHORITY_DENIED')
        require(all(target in SCALARS | {'*'} and set(uses) <=
                    {'fetch', 'display', 'retain', 'model_context'}
                    and (target != '*' or uses == ('fetch',))
                    for target, uses in policy.permissions), 'AUTHORITY_DENIED')
        require(not any('model_context' in uses for _, uses in policy.permissions)
                or model_current is True, 'AUTHORITY_DENIED')
        return policy

    def request(self, request, permit, *, response_bytes, request_seconds, parent_seconds):
        from .discovery_transport import musicbrainz_authorized, MoshtixRateLimited
        from .enhance_contract import EnhanceFault
        context = self._context(permit)
        require(request == context.request, 'AUTHORITY_DENIED')
        with self._lock:
            require(not context.sent, 'AUTHORITY_DENIED')
            context.sent = True  # A failed/denied attempt cannot reuse its slot.

        def current():
            self.policy(permit, now=utc(self.clock.now()))
            return True

        try:
            status, body, headers = musicbrainz_authorized(context.operation,
                revalidate=current,
                limiter=self.limiter, response_bytes=min(response_bytes, 131072),
                request_seconds=min(request_seconds, 5), parent_seconds=min(parent_seconds, 6))
        except MoshtixRateLimited:
            raise EnhanceFault('RATE_LIMITED') from None
        except SourceError:
            raise EnhanceFault('SOURCE_UNAVAILABLE') from None
        return SourceReply(request.request_digest, status, body, self.clock.now(), headers)
