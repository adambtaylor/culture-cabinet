"""One authorized anonymous episode-metadata read through existing source ports.

The legacy podcast fetcher/cache is deliberately not used. Native authority,
source consent and processor eligibility belong to the supplied issuer.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import os
import selectors
import subprocess
from threading import RLock
import time
from urllib.parse import urlencode

from .disclosure import SCALARS, canonical_bytes
from .discovery_sources import SourceError
from .discovery_transport import MediaHostLimiter, MoshtixRateLimited, _base, _moshtix_envelope
from .enhance_contract import EnhanceFault, require, utc
from .source_adapters import SourceAuthorityIssuer, SourceBackend, SourcePermit, SourceReply, SourceRequest
from .source_evidence import FieldPolicy

SOURCE = 'apple_podcast'
CATEGORY = 'podcast_episode'
OPERATION = 'podcast_search'
ORIGIN = 'https://itunes.apple.com'
PATH = '/search'
MAX_RESPONSE_BYTES = 131072
MAX_REQUEST_SECONDS = 5
MAX_PARENT_SECONDS = 6


@dataclass(frozen=True, repr=False)
class ApplePodcastOperation:
    term: str

    def __post_init__(self):
        if (type(self.term) is not str or not 1 <= len(self.term) <= 150
                or not self.term.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in self.term)):
            raise SourceError('Invalid episode search term.')
        try:
            self.term.encode('utf-8')
        except UnicodeError:
            raise SourceError('Invalid episode search encoding.') from None

    @property
    def query(self):
        # Encode one literal term, without trimming or joining extra profile text.
        return urlencode((('term', self.term), ('media', 'podcast'),
                          ('entity', 'podcastEpisode'), ('country', 'AU'), ('limit', '10')))

    @property
    def url(self):
        return ORIGIN + PATH + '?' + self.query

    @property
    def body(self):
        return b''

    @property
    def material(self):
        return {'method': 'GET', 'origin': ORIGIN, 'path': PATH, 'query': self.query,
                'headers': [['accept', 'application/json'], ['accept-encoding', 'identity']],
                'body_digest': sha256(self.body).hexdigest()}

    @property
    def operation_digest(self):
        return sha256(canonical_bytes(self.material)).hexdigest()


def apple_podcast_operation(request):
    require(type(request) is SourceRequest and request.source == SOURCE
            and request.category == CATEGORY and request.operation == OPERATION
            and len(request.terms) == 1 and not request.ids, 'AUTHORITY_DENIED')
    return ApplePodcastOperation(request.terms[0])


def apple_podcast_template():
    """Fixed issuer template; it grants neither source nor processor permission."""
    return {'source': SOURCE, 'method': 'GET', 'origin': ORIGIN, 'path': PATH,
            'operation': OPERATION, 'query_field': 'term',
            'fixed_parameters': {'media': 'podcast', 'entity': 'podcastEpisode',
                                 'country': 'AU', 'limit': 10},
            'projection': ['episode_id', 'title', 'source_url']}


def _seconds(value, ceiling):
    return type(value) in (int, float) and 0 < value <= ceiling


def apple_podcast_authorized(operation, *, revalidate, limiter,
                             response_bytes=MAX_RESPONSE_BYTES,
                             request_seconds=MAX_REQUEST_SECONDS,
                             parent_seconds=MAX_PARENT_SECONDS):
    """One fixed TLS GET; reserve before spawn and recheck authority after I/O.

    Curl and its parent both enforce bounds. Reserve cleanup time inside the
    caller's deadline, and retain the host reservation even on a failed send.
    HTTP failures are returned intact for the existing collector's distinctions.
    """
    if (type(operation) is not ApplePodcastOperation or not callable(revalidate)
            or type(limiter) is not MediaHostLimiter or limiter.provider != SOURCE
            or type(response_bytes) is not int or not 0 < response_bytes <= MAX_RESPONSE_BYTES
            or not _seconds(request_seconds, MAX_REQUEST_SECONDS)
            or not _seconds(parent_seconds, MAX_PARENT_SECONDS)):
        raise SourceError('Episode transport unavailable.')
    child, slot, status, headers = None, None, None, ()
    end = time.monotonic() + parent_seconds
    try:
        def current():
            try:
                if revalidate() is not True or slot is not None and slot.current() is not True:
                    raise SourceError('Episode authority unavailable.')
            except Exception:
                raise SourceError('Episode authority unavailable.') from None

        current()
        slot = limiter.reserve()
        current()
        io_end = end - min(0.2, parent_seconds / 4)
        if time.monotonic() >= io_end:
            raise TimeoutError
        command = _base(response_bytes)
        command[command.index('--connect-timeout') + 1] = str(min(request_seconds, parent_seconds))
        command[command.index('--max-time') + 1] = str(min(request_seconds, parent_seconds))
        command += ['--include', '--request', 'GET', '--url', operation.url]
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, env={'LANG': 'C', 'LC_ALL': 'C'})
        output, cap = bytearray(), response_bytes + 8192 + 8
        with selectors.DefaultSelector() as ready:
            os.set_blocking(child.stdout.fileno(), False)
            ready.register(child.stdout, selectors.EVENT_READ)
            while ready.get_map():
                remaining = io_end - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise TimeoutError
                try:
                    block = os.read(child.stdout.fileno(), min(8192, cap + 1 - len(output)))
                except BlockingIOError:
                    continue
                if not block:
                    ready.unregister(child.stdout)
                else:
                    output.extend(block)
                    if len(output) > cap:
                        raise SourceError('Episode response budget exceeded.')
            child.wait(timeout=max(0.001, io_end - time.monotonic()))
        if child.returncode == 28:
            raise TimeoutError
        if child.returncode:
            raise SourceError('Episode transport failed.')
        status, body, headers = _moshtix_envelope(bytes(output), response_bytes=response_bytes)
        if 300 <= status < 400:
            raise SourceError('Episode redirect refused.')
        current()
        if time.monotonic() >= io_end:
            raise TimeoutError
        return status, body, headers
    except (OSError, subprocess.SubprocessError) as error:
        if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
            raise TimeoutError from None
        raise SourceError('Episode transport failed.') from None
    finally:
        cleanup_failed = False
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=max(0.001, end - time.monotonic()))
            except (OSError, subprocess.SubprocessError):
                cleanup_failed = True
            finally:
                try:
                    child.stdout.close()
                except Exception:
                    cleanup_failed = True
        if slot is not None:
            try:
                slot.finish(status, headers, now=datetime.now(timezone.utc))
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise SourceError('Episode child cleanup failed.') from None


@dataclass(repr=False)
class _Context:
    permit: SourcePermit
    request: SourceRequest
    plan: object
    operation: ApplePodcastOperation
    sent: bool = False


class ApplePodcastBackend(SourceBackend):
    """Consume existing issuer authority; never create grants, credentials or cache."""
    def __init__(self, issuer, limiter, *, clock):
        require(isinstance(issuer, SourceAuthorityIssuer), 'CAPABILITY_UNAVAILABLE')
        require(type(limiter) is MediaHostLimiter and limiter.provider == SOURCE,
                'CAPABILITY_UNAVAILABLE')
        require(callable(getattr(clock, 'now', None)), 'CAPABILITY_UNAVAILABLE')
        self.issuer, self.limiter, self.clock = issuer, limiter, clock
        self._contexts, self._lock = {}, RLock()

    def _context(self, permit):
        with self._lock:
            context = self._contexts.get(id(permit))
        require(context is not None and context.permit is permit, 'AUTHORITY_DENIED')
        return context

    def authorize(self, request, plan, *, now):
        expected = apple_podcast_operation(request)
        operation = self.issuer.operation_for(request, plan, now=now)
        # Equal request bytes are not the issuer's retained preview capability.
        # Pass that exact object back through authorize and every revalidation.
        require(type(operation) is ApplePodcastOperation and operation == expected, 'PREVIEW_STALE')
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
        context = self._context(permit)
        require(request == context.request, 'AUTHORITY_DENIED')
        with self._lock:
            require(not context.sent, 'AUTHORITY_DENIED')
            # Even a timeout/denial consumes this permit's sole send attempt.
            context.sent = True

        def current():
            self.policy(permit, now=utc(self.clock.now()))
            return True

        try:
            status, body, headers = apple_podcast_authorized(context.operation,
                revalidate=current, limiter=self.limiter,
                response_bytes=min(response_bytes, MAX_RESPONSE_BYTES),
                request_seconds=min(request_seconds, MAX_REQUEST_SECONDS),
                parent_seconds=min(parent_seconds, MAX_PARENT_SECONDS))
        except MoshtixRateLimited:
            raise EnhanceFault('RATE_LIMITED') from None
        except SourceError:
            raise EnhanceFault('SOURCE_UNAVAILABLE') from None
        return SourceReply(request.request_digest, status, body, self.clock.now(), headers)
