"""NoFetch and pure TMDB request/address/envelope guards.

The byte iterables are local stimuli, not an IO client. These decisions prove
neither DNS cancellation, numeric peer pinning, TLS nor socket deadlines.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from .tmdb_contract import (
    AdapterResult, Attempt, Clock, Decision, DiscoverQuery, FetchResult,
    FETCH_OPERATION, FixedRequest, PROCESS_OPERATION, SourceIdentity,
    TmdbBudget, TmdbPolicy, VideosRequest, check_policy,
)

REQUEST_HEADERS = (('Accept', 'application/json'), ('Accept-Encoding', 'identity'))
_ROUTES = {'discover/movie', 'discover/tv', 'movie/videos', 'tv/videos'}
_REPORTED_FAILURES = {'request_timeout', 'tls_failure', 'transport_failure'}
_REASONS = frozenset('''invalid_request destination_unavailable unsafe_destination
source_off paused rights_denied rights_unknown transport_unavailable request_budget
pacing pagination_limit video_request_limit job_byte_limit job_deadline request_timeout
tls_failure transport_failure header_limit malformed_headers unsupported_content_encoding
unsupported_content_type response_byte_limit orphan_304 unsafe_redirect redirect_disallowed
authentication_required access_denied terms_or_legal_block provider_rate_limit
provider_server_error unexpected_http_status malformed_utf8 malformed_json duplicate_key
nonfinite_number parse_complexity_limit field_size_limit response_row_limit schema_drift
inconsistent_counts request_mismatch identity_conflict malformed_field
required_fields_not_permitted policy_filtered no_results page_items response_ready'''.split())
_TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CRITICAL = {'content-type', 'content-encoding', 'content-length', 'location', 'retry-after'}
# Some IANA special-use services are globally routed and escape is_global's
# exclusions. They are still outside the permitted public destination set.
_SPECIAL_USE = tuple(ipaddress.ip_network(network) for network in (
    '192.0.0.0/24', '192.31.196.0/24', '192.52.193.0/24',
    '192.88.99.0/24', '192.175.48.0/24', '2001::/23', '2620:4f:8000::/48',
))


def validate_request(request: DiscoverQuery | VideosRequest, *,
                     budget: TmdbBudget) -> FixedRequest | Decision:
    invalid = Decision('blocked', 'invalid_request')
    parent = None
    cursor = None
    if type(request) is DiscoverQuery:
        if (type(request.media) is not str or request.media not in ('movie', 'tv')
                or request.language != 'en-US' or request.sort_by != 'popularity.desc'
                or request.include_adult is not False):
            return invalid
        media = request.media
        path = '/3/discover/' + media
        route = 'discover/' + media
        query = {'include_adult': 'false', 'language': 'en-US', 'sort_by': 'popularity.desc'}
        query_hash = sha256((path + '?' + urlencode(sorted(query.items()))).encode('ascii')).hexdigest()
        cursor = request.cursor
        if cursor is not None and not budget.accepts_cursor(cursor, media=media, query_sha256=query_hash):
            return invalid
        page = 1 if cursor is None else cursor.next_page
        query['page'] = str(page)
    elif type(request) is VideosRequest:
        parent = request.identity
        if (type(parent) is not SourceIdentity or request.language != 'en-US'
                or parent.fictional != budget.fictional or parent not in budget.released_identities):
            return invalid
        media = parent.media
        route = media + '/videos'
        path = '/3/' + media + '/' + str(parent.id) + '/videos'
        query = {'language': 'en-US'}
        query_hash = sha256((path + '?' + urlencode(sorted(query.items()))).encode('ascii')).hexdigest()
        page = 1
    else:
        return invalid
    return FixedRequest(budget.job_id, route, media,
                        'https://api.themoviedb.org:443' + path + '?' + urlencode(sorted(query.items())),
                        query_hash, page, 'en-US', budget.fictional, parent, cursor)


def validate_destination_addresses(addresses: tuple[str, ...]) -> tuple[str, ...] | Decision:
    """Classify every canonical numeric candidate; never resolve a hostname."""
    if not addresses:
        return Decision('blocked', 'destination_unavailable')
    validated = []
    try:
        for text in addresses:
            if type(text) is not str or '%' in text:
                raise ValueError
            address = ipaddress.ip_address(text)
            if (str(address) != text or not address.is_global or address.is_reserved
                    or address.is_private or address.is_multicast or address.is_loopback
                    or address.is_link_local or address.is_unspecified
                    or getattr(address, 'ipv4_mapped', None) is not None
                    or text == '100.100.100.200'
                    or any(address in network for network in _SPECIAL_USE
                           if network.version == address.version)):
                raise ValueError
            if text not in validated:
                validated.append(text)
    except (ValueError, TypeError):
        return Decision('quarantined', 'unsafe_destination')
    return tuple(validated)


class NoFetch:
    def send(self, request: FixedRequest) -> Decision:
        return Decision('blocked', 'transport_unavailable')


def _result(decision: Decision, *, request: FixedRequest | None, attempt: Attempt | None,
            attempted_at: datetime, clock: Clock, status: int | None = None,
            checked_at: datetime | None = None, body: bytes = b'') -> FetchResult:
    return FetchResult(
        state=decision.outcome, reason=decision.reason,
        route_id=request.route_id if request else '',
        request_sha256=sha256(request.url.encode('ascii')).hexdigest() if request else '',
        attempted_at=attempted_at, fictional=request.fictional if request else False,
        fixed_request=request, attempt=attempt, checked_at=checked_at, http_status=status,
        body_bytes=body, body_bytes_count=attempt.body_bytes_count if attempt else 0,
        elapsed_ms=max(0, int((clock.monotonic() - attempt.started) * 1000)) if attempt else 0,
        next_allowed_at=decision.next_allowed_at,
    )


def fetch_tmdb(request: DiscoverQuery | VideosRequest, *, policy: TmdbPolicy,
               clock: Clock, budget: TmdbBudget, transport=NoFetch()) -> FetchResult:
    """Only explicitly supplied fictional stimuli can pass the NoFetch boundary.

    Test supplier.send(fixed_request) returns (status, header_lines, body_chunks),
    or a fixed failed Decision reporting timeout/TLS/transport failure. There is
    no client registry, credential callback or runtime selection mechanism.
    """
    invoked = clock.utcnow()
    fixed = validate_request(request, budget=budget)
    if isinstance(fixed, Decision):
        return replace(_result(fixed, request=None, attempt=None,
                               attempted_at=invoked, clock=clock), fictional=budget.fictional)
    for operation in (FETCH_OPERATION, PROCESS_OPERATION):
        decision = check_policy(current_policy=policy, operation=operation, fictional=fixed.fictional)
        if decision is not None:
            return _result(decision, request=fixed, attempt=None, attempted_at=invoked, clock=clock)
    if isinstance(transport, NoFetch) or not budget.fictional:
        return _result(NoFetch().send(fixed), request=fixed, attempt=None,
                       attempted_at=invoked, clock=clock)
    attempt = budget.admit(fixed, clock=clock)
    if isinstance(attempt, Decision):
        return _result(attempt, request=fixed, attempt=None, attempted_at=invoked, clock=clock)
    try:
        supplied = transport.send(fixed)
        decision = budget.check_deadline(attempt=attempt, clock=clock)
        if decision is not None:
            return _result(decision, request=fixed, attempt=attempt, attempted_at=invoked, clock=clock)
        if isinstance(supplied, Decision):
            reason = supplied.reason if supplied.outcome == 'failed' and supplied.reason in _REPORTED_FAILURES else 'transport_failure'
            return _result(Decision('failed', reason), request=fixed, attempt=attempt,
                           attempted_at=invoked, clock=clock)
        status, headers, chunks = supplied
        result = guard_response(status, headers, chunks, request=fixed, attempt=attempt,
                                clock=clock, budget=budget, fictional=fixed.fictional)
        return replace(result, attempted_at=invoked)
    except Exception:
        # Supplier details may contain credentials or source text. Never stringify.
        return _result(Decision('failed', 'transport_failure'), request=fixed, attempt=attempt,
                       attempted_at=invoked, clock=clock)


def _permitted_location(location: str) -> bool:
    """No normalization, DNS or follow; only the fixed request vocabulary."""
    try:
        parts = urlsplit(location)
        if (not location or not location.isascii() or parts.scheme != 'https'
                or parts.netloc not in ('api.themoviedb.org', 'api.themoviedb.org:443')
                or parts.fragment or '%' in parts.path or '\\' in location
                or any(ord(c) <= 32 or ord(c) == 127 for c in location)):
            return False
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if len(query) != len(pairs) or '%' in parts.query:
            return False
        if parts.path in ('/3/discover/movie', '/3/discover/tv'):
            return query in ({'include_adult': 'false', 'language': 'en-US',
                              'page': str(page), 'sort_by': 'popularity.desc'} for page in (1, 2))
        return (re.fullmatch(r'/3/(movie|tv)/([1-9][0-9]{0,18})/videos', parts.path) is not None
                and int(parts.path.split('/')[3]) <= 2**63 - 1 and query == {'language': 'en-US'})
    except (ValueError, TypeError):
        return False


def guard_response(status: int, header_lines: Iterable[bytes], body_chunks: Iterable[bytes], *,
                   request: FixedRequest, attempt: Attempt, clock: Clock,
                   budget: TmdbBudget, fictional: bool) -> FetchResult:
    """Bound each supplied line/chunk before retaining; classify only at EOF.

    A blocking next() cannot be cancelled here. checked_at records a completed
    bounded envelope check, never provider success or destination reachability.
    """
    def finish(decision, *, complete=False, body=b''):
        return _result(decision, request=request, attempt=attempt,
                       attempted_at=attempt.attempted_at, clock=clock,
                       status=status if type(status) is int and 100 <= status <= 599 else None,
                       checked_at=clock.utcnow() if complete else None, body=body)

    if (request.job_id != budget.job_id or attempt.job_id != budget.job_id
            or fictional is not budget.fictional or request.fictional is not fictional
            or not any(a is attempt and r is request for r, a in budget._attempts)):
        return finish(Decision('blocked', 'invalid_request'))
    try:
        if type(status) is not int or not 100 <= status <= 599:
            return finish(Decision('failed', 'malformed_headers'))
        headers = {}
        total = count = 0
        ended = False
        for line in header_lines:
            decision = budget.check_deadline(attempt=attempt, clock=clock)
            if decision is not None:
                return finish(decision)
            if type(line) is not bytes:
                return finish(Decision('failed', 'malformed_headers'))
            total += len(line)
            if total > 16384:
                return finish(Decision('failed', 'header_limit'))
            if line != b'\r\n':
                count += 1
            if count > 64:
                return finish(Decision('failed', 'header_limit'))
            if ended:
                return finish(Decision('failed', 'malformed_headers'))
            if line == b'\r\n':
                ended = True
                continue
            if not line.endswith(b'\r\n') or b':' not in line:
                return finish(Decision('failed', 'malformed_headers'))
            name, value = line[:-2].split(b':', 1)
            if not _TOKEN.fullmatch(name) or any(c < 32 or c > 126 for c in value):
                return finish(Decision('failed', 'malformed_headers'))
            name = name.decode('ascii').lower()
            value = value.decode('ascii').strip(' ')
            if name in _CRITICAL and name in headers:
                return finish(Decision('failed', 'malformed_headers'))
            if name in _CRITICAL:
                headers[name] = value
        if not ended:
            return finish(Decision('failed', 'malformed_headers'))
        length = headers.get('content-length')
        if length is not None and re.fullmatch(r'[0-9]+', length) is None:
            return finish(Decision('failed', 'malformed_headers'))
        chunks = []
        for chunk in body_chunks:
            if type(chunk) is not bytes:
                return finish(Decision('failed', 'malformed_headers'))
            decision = budget.consume_bytes(len(chunk), attempt=attempt, clock=clock)
            if decision is not None:
                return finish(decision)
            if chunk:
                chunks.append(chunk)
        decision = budget.check_deadline(attempt=attempt, clock=clock)
        if decision is not None:
            return finish(decision)
        if headers.get('content-encoding', 'identity').lower() != 'identity':
            return finish(Decision('failed', 'unsupported_content_encoding'))
        if status == 200 and re.fullmatch(
                r'application/json(?: *; *charset *= *utf-8)?', headers.get('content-type', ''), re.I) is None:
            return finish(Decision('failed', 'unsupported_content_type'))
        # Compare decimal strings to avoid converting an unbounded integer lexeme.
        if length is not None and (length.lstrip('0') or '0') != str(attempt.body_bytes_count):
            return finish(Decision('failed', 'malformed_headers'))
        if status == 304:
            return finish(Decision('quarantined', 'orphan_304'), complete=True)
        if status in (301, 302, 303, 307, 308) or 'location' in headers:
            unsafe = 'location' in headers and not _permitted_location(headers['location'])
            return finish(Decision('quarantined', 'unsafe_redirect') if unsafe
                          else Decision('blocked', 'redirect_disallowed'), complete=True)
        decisions = {401: ('blocked', 'authentication_required'), 403: ('blocked', 'access_denied'),
                     451: ('blocked', 'terms_or_legal_block'), 429: ('limited', 'provider_rate_limit')}
        if status in decisions:
            decision = Decision(*decisions[status])
            retry = headers.get('retry-after', '')
            if status == 429 and re.fullmatch(r'[0-9]+', retry):
                delta = 3600 if len(retry.lstrip('0')) > 4 else min(int(retry.lstrip('0') or '0'), 3600)
                decision = replace(decision, next_allowed_at=clock.utcnow() + timedelta(seconds=delta))
            return finish(decision, complete=True)
        if status != 200:
            return finish(Decision('failed', 'provider_server_error' if status >= 500
                                   else 'unexpected_http_status'), complete=True)
        result = finish(Decision('failed', 'response_ready'), complete=True, body=b''.join(chunks))
        return replace(result, state='ready')
    except Exception:
        return finish(Decision('failed', 'transport_failure'))


def safe_diagnostics(result: FetchResult | AdapterResult) -> dict:
    """The only public diagnostic projection; no recursive dataclass serialization."""
    outcome = result.state if isinstance(result, FetchResult) else result.outcome
    route = result.route_id if isinstance(result, FetchResult) else result.coverage.route_id
    safe = {'route_id': route if route in _ROUTES else '',
            'outcome': outcome if outcome in {'ready', 'blocked', 'limited', 'failed', 'quarantined', 'success', 'healthy_empty'} else 'failed',
            'reason': result.reason if result.reason in _REASONS else 'transport_failure'}
    for name in ('attempted_at', 'checked_at'):
        value = getattr(result, name)
        safe[name] = value.astimezone(UTC).isoformat() if isinstance(value, datetime) and value.tzinfo is not None else None
    if isinstance(result, FetchResult):
        safe['fictional'] = result.fictional is True
        safe['body_bytes_count'] = max(0, result.body_bytes_count) if type(result.body_bytes_count) is int else 0
        safe['elapsed_ms'] = max(0, result.elapsed_ms) if type(result.elapsed_ms) is int else 0
    else:
        safe['observations_count'] = len(result.observations)
        safe['videos_count'] = len(result.videos)
    return safe
