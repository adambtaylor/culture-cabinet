"""Pure, atomic TMDB page mapping. No IO, persistence or household authority."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from hashlib import sha256
import json
import math
import re

from .tmdb_contract import (
    AdapterResult, Clock, ContextAction, Coverage, DateFact, Decision,
    DISPLAY_OPERATION, FetchResult, FieldProvenance, FixedRequest, Observation,
    Overview, PROCESS_OPERATION, SourceIdentity, TmdbBudget, TmdbPolicy,
    VideoFact, check_policy, project_for_use,
)

_KEY = re.compile(r'[A-Za-z0-9_-]{11}', re.ASCII)
_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', re.ASCII)
_PUBLISHED = re.compile(
    r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-5][0-9]'
    r'(?:\.([0-9]{1,9}))?(Z|[+-](?:0[0-9]|1[0-4]):[0-5][0-9])', re.ASCII)


class _Reject(Exception):
    def __init__(self, reason, outcome='failed'):
        self.decision = Decision(outcome, reason)


def _require(condition, reason='malformed_field', outcome='failed'):
    if not condition:
        raise _Reject(reason, outcome)


def _integer(value, minimum=0):
    _require(type(value) is int and minimum <= value <= 2**63 - 1)
    return value


def _text(value):
    _require(type(value) is str and bool(value.strip()))
    return value


def _decode(body, deadline):
    try:
        text = body.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        raise _Reject('malformed_utf8') from None
    _require(not text.startswith('\ufeff') and not any(0xD800 <= ord(c) <= 0xDFFF for c in text),
             'malformed_utf8')
    # Check escapes for surrogate validity before lexical depth/decoder callbacks.
    # json's string scanner resolves paired escapes and leaves unpaired ones intact.
    i = 0
    while i < len(text):
        if i % 256 == 0:
            deadline()
        if text[i] == '"':
            try:
                value, i = json.decoder.scanstring(text, i + 1, True)
            except (ValueError, json.JSONDecodeError):
                # Leave grammar errors to the decoder after nesting validation.
                i += 1
                continue
            _require(not any(0xD800 <= ord(c) <= 0xDFFF for c in value), 'malformed_utf8')
        else:
            i += 1
    depth = 0
    quoted = escaped = False
    for i, char in enumerate(text):
        if i % 256 == 0:
            deadline()
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            depth += 1
            _require(depth <= 12, 'parse_complexity_limit')
        elif char in ']}':
            depth -= 1

    def number(token, convert):
        _require(len(token) <= 128, 'malformed_json')
        value = convert(token)
        _require(not isinstance(value, float) or math.isfinite(value), 'nonfinite_number')
        return value

    def constant(token):
        raise _Reject('nonfinite_number')

    decoder = json.JSONDecoder(parse_constant=constant,
        parse_int=lambda token: number(token, int),
        parse_float=lambda token: number(token, float))

    def whitespace(position):
        while position < len(text) and text[position] in ' \t\r\n':
            position += 1
        return position

    def value_at(position):
        # Decode containers in document order so duplicate keys are checked
        # before their values. stdlib callbacks alone visit objects postorder.
        # All scalar grammar remains with the stdlib decoder; a final stdlib
        # document decode also validates the complete container grammar.
        deadline()
        position = whitespace(position)
        _require(position < len(text), 'malformed_json')
        opening = text[position]
        if opening not in '[{':
            return decoder.raw_decode(text, position)
        is_object = opening == '{'
        closing = '}' if is_object else ']'
        result = {} if is_object else []
        position = whitespace(position + 1)
        if position < len(text) and text[position] == closing:
            return result, position + 1
        while True:
            if is_object:
                _require(position < len(text) and text[position] == '"', 'malformed_json')
                key, position = decoder.raw_decode(text, position)
                _require(key not in result, 'duplicate_key')
                position = whitespace(position)
                _require(position < len(text) and text[position] == ':', 'malformed_json')
                child, position = value_at(position + 1)
                result[key] = child
            else:
                child, position = value_at(position)
                result.append(child)
            position = whitespace(position)
            _require(position < len(text), 'malformed_json')
            if text[position] == closing:
                return result, position + 1
            _require(text[position] == ',', 'malformed_json')
            position = whitespace(position + 1)

    try:
        root, end = value_at(0)
        _require(whitespace(end) == len(text), 'malformed_json')
        # Do not use this second decode for values or error precedence.
        json.loads(text)
    except (ValueError, RecursionError):
        raise _Reject('malformed_json') from None
    nodes = 0

    def walk(value):
        nonlocal nodes
        nodes += 1
        if nodes % 128 == 0:
            deadline()
        _require(nodes <= 5000, 'parse_complexity_limit')
        if isinstance(value, str):
            _require(len(value) <= 4096, 'field_size_limit')
        elif isinstance(value, dict):
            for key, child in value.items():
                walk(key)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(root)
    deadline()
    return root


def _release(value):
    if value == '':
        return None
    _require(type(value) is str and _DATE.fullmatch(value) is not None)
    try:
        date.fromisoformat(value)
    except ValueError:
        raise _Reject('malformed_field') from None
    return DateFact(value)


def _published(value):
    if value is None:
        return None, None
    _require(type(value) is str)
    match = _PUBLISHED.fullmatch(value)
    _require(match is not None)
    zone = match.group(2)
    _require(zone == 'Z' or zone[1:3] != '14' or zone[4:] == '00')
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise _Reject('malformed_field') from None
    return value, 'fraction:' + str(len(match.group(1))) if match.group(1) else 'second'


def _page(root, request):
    _require(type(root) is dict, 'schema_drift', 'quarantined')
    if type(root.get('results')) is list:
        _require(len(root['results']) <= (100 if request.parent else 20),
                 'response_row_limit', 'limited')
    markers = ('id', 'results') if request.parent else ('page', 'total_pages', 'total_results', 'results')
    _require(all(k in root for k in markers) and type(root['results']) is list,
             'schema_drift', 'quarantined')
    rows = root['results']
    title_key = 'title' if request.media == 'movie' else 'name'
    required = ('key', 'site', 'type', 'official') if request.parent else ('id', title_key)
    for row in rows:
        _require(type(row) is dict and all(k in row for k in required),
                 'schema_drift', 'quarantined')
    if request.parent:
        _integer(root['id'], 1)
        _require(root['id'] == request.parent.id, 'request_mismatch', 'quarantined')
        return rows, len(rows), 1
    page = _integer(root['page'], 1)
    pages = _integer(root['total_pages'])
    total = _integer(root['total_results'])
    valid = (page == 1 and not rows and pages in (0, 1)) if total == 0 else (
        pages == (total + 19) // 20 and 1 <= page <= pages
        and len(rows) == min(20, total - 20 * (page - 1)))
    _require(valid, 'inconsistent_counts', 'quarantined')
    _require(page == request.page, 'request_mismatch', 'quarantined')
    return rows, total, pages


def _map_rows(rows, *, request, fetch, policy, coverage, deadline):
    digest = sha256(fetch.body_bytes).hexdigest()
    result = []
    seen = set()
    for index, row in enumerate(rows):
        deadline()
        title_key = 'title' if request.media == 'movie' else 'name'
        required = ('key', 'site', 'type', 'official') if request.parent else ('id', title_key)
        if not request.parent:
            for name, limit in ((title_key, 200), ('overview', 2000)):
                if type(row.get(name)) is str:
                    _require(len(row[name]) <= limit, 'field_size_limit')
        pointers = {}
        def provenance(name, key):
            pointers[name] = FieldProvenance(request.route_id, request.query_sha256, digest,
                '/results/' + str(index) + '/' + key, fetch.checked_at, fetch.fictional)
        if request.parent:
            key, site, kind = (_text(row[k]) for k in ('key', 'site', 'type'))
            _require(type(row['official']) is bool)
            if site == 'YouTube':
                _require(_KEY.fullmatch(key) is not None)
            identity = (site, key)
            optional = {}
            for name, pattern in (('iso_639_1', r'[a-z]{2}'), ('iso_3166_1', r'[A-Z]{2}')):
                value = row.get(name)
                _require(value is None or (type(value) is str and re.fullmatch(pattern, value, re.ASCII) is not None))
                optional[name] = value
            published, precision = _published(row.get('published_at'))
            for name in (*required, *optional):
                if name in row:
                    provenance(name, name)
            if 'published_at' in row:
                provenance('published_at', 'published_at')
                provenance('published_precision', 'published_at')
            pointers['source_identity'] = FieldProvenance(request.route_id, request.query_sha256,
                digest, '/id', fetch.checked_at, fetch.fictional)
            fact = VideoFact(key=key, site=site, type=kind, official=row['official'],
                source_identity=request.parent, checked_at=fetch.checked_at, provenance=pointers,
                payload_sha256=digest, rights_snapshot=policy, coverage=coverage,
                published_at=published, published_precision=precision, **optional)
        else:
            identity = SourceIdentity(request.media, _integer(row['id'], 1), fetch.fictional)
            title = _text(row[title_key])
            overview = Overview()
            if 'overview' in row:
                _require(type(row['overview']) is str)
                overview = Overview('text' if row['overview'] else 'empty', row['overview'])
                provenance('overview', 'overview')
            date_key = 'release_date' if request.media == 'movie' else 'first_air_date'
            release = _release(row[date_key]) if date_key in row else None
            if date_key in row:
                provenance('release', date_key)
            genres = None
            if 'genre_ids' in row:
                _require(type(row['genre_ids']) is list)
                genres = tuple(_integer(g, 1) for g in row['genre_ids'])
                _require(len(set(genres)) == len(genres))
                provenance('genre_ids', 'genre_ids')
            provenance('source_identity', 'id')
            provenance('title', title_key)
            fact = Observation(source_identity=identity, domain_kind='film' if request.media == 'movie' else 'series',
                title=title, checked_at=fetch.checked_at, payload_sha256=digest,
                field_provenance=pointers, rights_snapshot=policy, coverage=coverage,
                overview=overview, release=release, genre_ids=genres,
                source_url=None, source_url_reason='fixture' if fetch.fictional else 'rights_unknown')
        _require(identity not in seen, 'identity_conflict', 'quarantined')
        seen.add(identity)
        result.append(fact)
    return result


def _project(fact, policy):
    projection = project_for_use(fact, operation=DISPLAY_OPERATION, current_policy=policy)
    if isinstance(projection, Decision):
        return projection
    values = projection.fields
    if isinstance(fact, Observation):
        return replace(fact, overview=values.get('overview', Overview()),
            release=values.get('release'), genre_ids=values.get('genre_ids'),
            source_url=values.get('source_url'), field_provenance=projection.provenance)
    return replace(fact, iso_639_1=values.get('iso_639_1'), iso_3166_1=values.get('iso_3166_1'),
        published_at=values.get('published_at'), published_precision=values.get('published_precision'),
        provenance=projection.provenance)


def parse_tmdb(fetch: FetchResult, *, current_policy: TmdbPolicy,
               clock: Clock, budget: TmdbBudget) -> AdapterResult:
    """Validate an entire bounded page before releasing any authorized facts.

    Results contain this page only; callers may keep earlier complete pages only
    for limited aggregation and must reproject them under current policy.
    """
    request = fetch.fixed_request
    coverage = Coverage(fetch.route_id, request.media if request else 'movie')
    def finish(outcome, reason, facts=(), next_allowed_at=None):
        current = replace(coverage, state=outcome)
        facts = tuple(replace(f, coverage=current) for f in facts) if outcome in ('success', 'healthy_empty', 'limited') else ()
        return AdapterResult(outcome=outcome, reason=reason, attempted_at=fetch.attempted_at,
            checked_at=fetch.checked_at, coverage=current, rights_snapshot=current_policy,
            observations=tuple(f for f in facts if isinstance(f, Observation)),
            videos=tuple(f for f in facts if isinstance(f, VideoFact)),
            attribution=current_policy.attribution, paused=current_policy.paused,
            source_off=current_policy.source_off, next_allowed_at=next_allowed_at)
    if request:
        coverage = replace(coverage, pages_attempted=budget.pages_attempted(request),
                           pages_completed=budget.pages_completed(request))
    if fetch.state != 'ready':
        return finish(fetch.state, fetch.reason, next_allowed_at=fetch.next_allowed_at)
    if (request is None or fetch.attempt is None or request.job_id != budget.job_id
            or fetch.attempt.job_id != budget.job_id or budget.clock is not clock
            or fetch.fictional != budget.fictional or request.fictional != budget.fictional
            or not any(r is request and a is fetch.attempt for r, a in budget._attempts)
            or fetch.route_id != request.route_id or fetch.http_status != 200
            or fetch.checked_at is None or fetch.request_sha256 != sha256(request.url.encode('ascii')).hexdigest()
            or type(fetch.body_bytes) is not bytes or len(fetch.body_bytes) > 262144
            or len(fetch.body_bytes) != fetch.body_bytes_count
            or fetch.body_bytes_count != fetch.attempt.body_bytes_count):
        return finish('blocked', 'invalid_request')
    def deadline():
        decision = budget.check_deadline(attempt=fetch.attempt, clock=clock)
        if decision:
            raise _Reject(decision.reason, decision.outcome)
    try:
        decision = check_policy(current_policy=current_policy, operation=PROCESS_OPERATION, fictional=fetch.fictional)
        if decision:
            return finish(decision.outcome, decision.reason)
        deadline()
        root = _decode(fetch.body_bytes, deadline)
        rows, total, pages = _page(root, request)
        facts = _map_rows(rows, request=request, fetch=fetch, policy=current_policy,
                          coverage=coverage, deadline=deadline)
        deadline()
        retained = []
        decisions = []
        for fact in facts:
            deadline()
            projected = _project(fact, current_policy)
            if isinstance(projected, Decision):
                decisions.append(projected)
            else:
                retained.append(projected)
        # Empty pages still require the current display operation.
        display = check_policy(current_policy=current_policy, operation=DISPLAY_OPERATION, fictional=fetch.fictional)
        deadline()
        if display:
            retained = []
        budget.record_page(request, tuple(f.source_identity for f in retained if isinstance(f, Observation)), page_valid=True)
        incomplete = bool(decisions or display or (not request.parent and pages > budget.pages_completed(request)))
        coverage = replace(coverage, provider_total=total, route_items=len(retained), page_valid=True,
            pages_completed=budget.pages_completed(request), policy_filtered_count=len(decisions),
            incomplete_coverage=incomplete)
        if display:
            return finish(display.outcome, display.reason)
        if facts and not retained:
            return finish('blocked', 'required_fields_not_permitted')
        if decisions:
            return finish('limited', 'policy_filtered', retained)
        if not request.parent and pages > request.page:
            if request.page == 2:
                return finish('limited', 'pagination_limit', retained)
            budget.issue_cursor(request)
        return finish('success', 'page_items', retained) if facts else finish('healthy_empty', 'no_results')
    except _Reject as rejection:
        return finish(rejection.decision.outcome, rejection.decision.reason)


def context_action(fact: VideoFact | Observation, *, request: FixedRequest,
                   current_policy: TmdbPolicy) -> ContextAction:
    """Construct fixed context templates only; never check a destination."""
    if fact.source_identity.fictional:
        return ContextAction(reason='fixture')
    required = (('source_identity', 'key', 'site', 'type', 'official', 'iso_639_1', 'provenance')
                if isinstance(fact, VideoFact) else ('source_identity', 'title', 'field_provenance'))
    records = current_policy.field_permissions.get(DISPLAY_OPERATION, {})
    projection = project_for_use(fact, operation=DISPLAY_OPERATION, current_policy=current_policy)
    if isinstance(projection, Decision):
        reason = projection.reason
        if reason == 'required_fields_not_permitted':
            reason = 'rights_denied' if any(records.get(k) and records[k].decision == 'deny'
                                           for k in required) else 'rights_unknown'
        return ContextAction(reason=reason)
    if any(records.get(k) and records[k].decision == 'deny' for k in required):
        return ContextAction(reason='rights_denied')
    if any(k not in records or records[k].decision != 'allow' for k in required):
        return ContextAction(reason='rights_unknown')
    identity = fact.source_identity
    if (identity.media != request.media or request.fictional or request.language != 'en-US'
            or (isinstance(fact, VideoFact) and request.parent != identity)):
        return ContextAction(reason='request_mismatch')
    evidence = current_policy.operations.get(DISPLAY_OPERATION)
    def dated(evidence):
        return (evidence is not None and evidence.reviewed_at is not None
                and evidence.reviewed_at.tzinfo is not None and bool(evidence.evidence_digest))
    if not dated(evidence) or not all(dated(records.get(k)) for k in required):
        return ContextAction(reason='rights_unknown')
    provenance = fact.provenance if isinstance(fact, VideoFact) else fact.field_provenance
    if fact.checked_at.tzinfo is None or not provenance:
        return ContextAction(reason='rights_unknown')
    for name in required:
        if name in ('provenance', 'field_provenance'):
            continue
        assertion = provenance.get(name)
        if (assertion is None or assertion.fictional or assertion.checked_at != fact.checked_at
                or assertion.route_id != request.route_id or assertion.query_sha256 != request.query_sha256
                or assertion.payload_sha256 != fact.payload_sha256):
            return ContextAction(reason='rights_unknown')
    if isinstance(fact, VideoFact):
        if fact.site != 'YouTube':
            return ContextAction(reason='unsupported_site')
        if type(fact.key) is not str or not _KEY.fullmatch(fact.key):
            return ContextAction(reason='malformed_key')
        if fact.official is not True:
            return ContextAction(reason='not_official')
        if fact.type != 'Trailer':
            return ContextAction(reason='not_trailer')
        if projection.fields.get('iso_639_1') != 'en':
            return ContextAction(reason='language_mismatch')
        href = 'https://www.youtube.com/watch?v=' + fact.key
    else:
        href = 'https://www.themoviedb.org/' + identity.media + '/' + str(identity.id)
    return ContextAction(reason='validated_template', state='available', href=href, canonical_href=href,
        source_identity=identity, provider_reported_official=fact.official if isinstance(fact, VideoFact) else None,
        type=fact.type if isinstance(fact, VideoFact) else None, metadata_checked_at=fact.checked_at,
        template_policy_version=current_policy.version)
