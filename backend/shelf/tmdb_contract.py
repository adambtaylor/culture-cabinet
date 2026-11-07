"""Immutable TMDB facts and current-use policy; one trusted, serial job budget.

No persistence, household authority, provider IO, or source-to-human conversion.
Synthetic policy grants describe invented data, not permission to use a provider.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from time import monotonic
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

Media = Literal['movie', 'tv']
Permission = Literal['allow', 'deny', 'unknown']
FETCH_OPERATION = 'fetch'
PROCESS_OPERATION = 'transient_processing'
DISPLAY_OPERATION = 'display_catalog_fields'
PARSER_VERSION = 'tmdb-json-v1'


@dataclass(frozen=True)
class Clock:
    monotonic: Callable[[], float] = field(default=monotonic, repr=False)
    utcnow: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)


@dataclass(frozen=True)
class Decision:
    outcome: Literal['blocked', 'limited', 'failed', 'quarantined']
    reason: str
    next_allowed_at: datetime | None = None


@dataclass(frozen=True)
class SourceIdentity:
    media: Media
    id: int
    fictional: bool
    provider: str = 'tmdb'

    def __post_init__(self):
        if (self.provider != 'tmdb' or self.media not in ('movie', 'tv')
                or type(self.id) is not int or not 1 <= self.id <= 2**63 - 1
                or type(self.fictional) is not bool):
            raise ValueError('Invalid TMDB identity')


@dataclass(frozen=True, eq=False)
class Cursor:
    """Only a handle issued and retained by this job is admissible."""
    job_id: str
    query_sha256: str
    media: Media
    next_page: int


@dataclass(frozen=True)
class DiscoverQuery:
    media: Media
    cursor: Cursor | None = field(default=None, repr=False)
    language: str = 'en-US'
    sort_by: str = 'popularity.desc'
    include_adult: bool = False


@dataclass(frozen=True)
class VideosRequest:
    identity: SourceIdentity
    language: str = 'en-US'


@dataclass(frozen=True, eq=False)
class FixedRequest:
    job_id: str
    route_id: str
    media: Media
    url: str = field(repr=False)
    query_sha256: str
    page: int
    language: str
    fictional: bool
    parent: SourceIdentity | None = None
    cursor: Cursor | None = field(default=None, repr=False)


@dataclass(frozen=True, eq=False)
class Attempt:
    job_id: str
    sequence: int
    started: float
    deadline: float
    attempted_at: datetime
    _budget: TmdbBudget = field(repr=False, compare=False)

    @property
    def body_bytes_count(self) -> int:
        return self._budget._body_counts.get(self, 0)


@dataclass(frozen=True)
class PolicyEvidence:
    decision: Permission = 'unknown'
    reviewed_at: datetime | None = None
    evidence_digest: str = ''
    attribution: str = ''
    terms_url: str | None = None
    terms_date: str | None = None

    def __post_init__(self):
        if self.decision not in ('allow', 'deny', 'unknown'):
            raise ValueError('Invalid permission')


@dataclass(frozen=True)
class TmdbPolicy:
    version: str
    source_off: bool = False
    paused: bool = False
    operations: Mapping[str, PolicyEvidence] = field(default_factory=dict, repr=False)
    field_permissions: Mapping[str, Mapping[str, PolicyEvidence]] = field(default_factory=dict, repr=False)
    fictional_only: bool = True
    attribution: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'operations', MappingProxyType(dict(self.operations)))
        object.__setattr__(self, 'field_permissions', MappingProxyType({
            op: MappingProxyType(dict(records)) for op, records in self.field_permissions.items()
        }))


@dataclass(frozen=True)
class DateFact:
    value: str
    precision: str = 'day'


@dataclass(frozen=True)
class Overview:
    state: Literal['missing', 'empty', 'text'] = 'missing'
    value: str | None = None


@dataclass(frozen=True)
class FieldProvenance:
    route_id: str
    query_sha256: str
    payload_sha256: str
    json_pointer: str
    checked_at: datetime
    fictional: bool


@dataclass(frozen=True)
class Coverage:
    route_id: str
    media: Media
    mode: str = 'fixture'
    pages_attempted: int = 0
    pages_completed: int = 0
    route_items: int = 0
    provider_total: int | None = None
    page_valid: bool = False
    incomplete_coverage: bool = True
    policy_filtered_count: int = 0
    state: str = 'blocked'


@dataclass(frozen=True, kw_only=True)
class Observation:
    source_identity: SourceIdentity
    domain_kind: Literal['film', 'series']
    title: str
    checked_at: datetime
    payload_sha256: str
    field_provenance: Mapping[str, FieldProvenance]
    rights_snapshot: TmdbPolicy
    coverage: Coverage
    overview: Overview = field(default_factory=Overview)
    release: DateFact | None = None
    genre_ids: tuple[int, ...] | None = None
    source_url: str | None = None
    source_url_reason: str = 'fixture'
    parser_version: str = PARSER_VERSION
    visibility: str = 'source_catalog'

    def __post_init__(self):
        object.__setattr__(self, 'field_provenance', MappingProxyType(dict(self.field_provenance)))
        if self.genre_ids is not None:
            object.__setattr__(self, 'genre_ids', tuple(self.genre_ids))


@dataclass(frozen=True, kw_only=True)
class VideoFact:
    key: str
    site: str
    type: str
    official: bool
    source_identity: SourceIdentity
    checked_at: datetime
    provenance: Mapping[str, FieldProvenance]
    payload_sha256: str
    rights_snapshot: TmdbPolicy
    coverage: Coverage
    iso_639_1: str | None = None
    iso_3166_1: str | None = None
    published_at: str | None = None
    published_precision: str | None = None
    parser_version: str = PARSER_VERSION

    def __post_init__(self):
        object.__setattr__(self, 'provenance', MappingProxyType(dict(self.provenance)))


@dataclass(frozen=True)
class Projection:
    fields: Mapping[str, object]
    provenance: Mapping[str, FieldProvenance]
    current_policy_version: str

    def __post_init__(self):
        object.__setattr__(self, 'fields', MappingProxyType(dict(self.fields)))
        object.__setattr__(self, 'provenance', MappingProxyType(dict(self.provenance)))


@dataclass(frozen=True, kw_only=True)
class ContextAction:
    reason: str
    href: str | None = None
    state: str = 'unavailable'
    canonical_href: str | None = None
    source_identity: SourceIdentity | None = None
    provider_reported_official: bool | None = None
    type: str | None = None
    metadata_checked_at: datetime | None = None
    template_policy_version: str | None = None
    destination_checked_at: datetime | None = None
    ownership_verified: bool = False


@dataclass(frozen=True, kw_only=True)
class FetchResult:
    state: str
    reason: str
    route_id: str
    request_sha256: str
    attempted_at: datetime
    fictional: bool
    fixed_request: FixedRequest | None = field(default=None, repr=False)
    attempt: Attempt | None = field(default=None, repr=False)
    checked_at: datetime | None = None
    http_status: int | None = None
    body_bytes: bytes = field(default=b'', repr=False)
    body_bytes_count: int = 0
    elapsed_ms: int = 0
    next_allowed_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class AdapterResult:
    outcome: str
    reason: str
    attempted_at: datetime
    coverage: Coverage
    rights_snapshot: TmdbPolicy
    observations: tuple[Observation, ...] = ()
    videos: tuple[VideoFact, ...] = ()
    checked_at: datetime | None = None
    attribution: str = ''
    paused: bool = False
    source_off: bool = False
    next_allowed_at: datetime | None = None

    def __post_init__(self):
        object.__setattr__(self, 'observations', tuple(self.observations))
        object.__setattr__(self, 'videos', tuple(self.videos))


def check_policy(*, current_policy: TmdbPolicy, operation: str,
                 fictional: bool) -> Decision | None:
    if current_policy.source_off:
        return Decision('blocked', 'source_off')
    if current_policy.paused:
        return Decision('blocked', 'paused')
    record = current_policy.operations.get(operation, PolicyEvidence())
    if record.decision == 'deny':
        return Decision('blocked', 'rights_denied')
    if record.decision != 'allow' or (current_policy.fictional_only and not fictional):
        return Decision('blocked', 'rights_unknown')
    return None


def project_for_use(observation: Observation | VideoFact, *, operation: str,
                    current_policy: TmdbPolicy) -> Projection | Decision:
    """Recheck the named operation and each field; snapshots confer no authority."""
    decision = check_policy(current_policy=current_policy, operation=operation,
                            fictional=observation.source_identity.fictional)
    if decision is not None:
        return decision
    is_video = isinstance(observation, VideoFact)
    provenance = observation.provenance if is_video else observation.field_provenance
    required = ({'source_identity', 'key', 'site', 'type', 'official', 'provenance'}
                if is_video else {'source_identity', 'title', 'field_provenance'})
    permissions = current_policy.field_permissions.get(operation, {})
    allowed = {name for name, record in permissions.items() if record.decision == 'allow'}
    values = {f.name: getattr(observation, f.name) for f in fields(observation)}
    provenance_name = 'provenance' if is_video else 'field_provenance'
    # Provenance is checked as a required field, then retained only for released fields.
    data_required = required - {provenance_name}
    if not required <= allowed or not data_required <= provenance.keys():
        return Decision('blocked', 'required_fields_not_permitted')
    retained = {name: value for name, value in values.items()
                if name in allowed and name in provenance and name != provenance_name}
    kept_provenance = {name: provenance[name] for name in retained}
    return Projection(retained, kept_provenance, current_policy.version)


class TmdbBudget:
    """Trusted in-memory state for one serial job, including failed attempts.

    A cursor is consumed only on admission. Validation, policy and NoFetch cannot
    reserve quota. UTC labels never participate in expiry or pacing decisions.
    """
    def __init__(self, *, clock: Clock = Clock(), fictional: bool = False):
        if type(fictional) is not bool:
            raise ValueError('Invalid fictional context')
        self.job_id = uuid4().hex
        self.fictional = fictional
        self.clock = clock
        self.started = clock.monotonic()
        self.deadline = self.started + 30
        self._attempts: list[tuple[FixedRequest, Attempt]] = []
        self._body_counts: dict[Attempt, int] = {}
        self._bytes = 0
        self._completed: set[tuple[str, str, int]] = set()
        self._released: set[SourceIdentity] = set()
        self._valid_pages: set[FixedRequest] = set()
        self._cursors: dict[Cursor, bool] = {}
        self._issued: dict[tuple[str, str], Cursor] = {}

    @property
    def body_bytes_count(self) -> int:
        return self._bytes

    @property
    def attempts(self) -> tuple[Attempt, ...]:
        return tuple(attempt for _, attempt in self._attempts)

    @property
    def released_identities(self) -> tuple[SourceIdentity, ...]:
        return tuple(sorted(self._released, key=lambda x: (x.media, x.id)))

    def pages_attempted(self, request: FixedRequest) -> int:
        return sum(r.route_id == request.route_id and r.media == request.media
                   for r, _ in self._attempts)

    def pages_completed(self, request: FixedRequest) -> int:
        return sum(r.route_id == request.route_id and r.media == request.media
                   for r in self._valid_pages)

    def accepts_cursor(self, cursor: Cursor, *, media: str, query_sha256: str) -> bool:
        return (type(cursor) is Cursor and cursor in self._cursors
                and not self._cursors[cursor] and cursor.job_id == self.job_id
                and cursor.media == media and cursor.query_sha256 == query_sha256
                and cursor.next_page == 2 and self.clock.monotonic() < self.deadline)

    def issue_cursor(self, request: FixedRequest) -> Cursor | None:
        """Parser calls only when valid page1 advertises page2; never auto-fetch."""
        if (request.job_id != self.job_id or request.page != 1 or request.parent is not None
                or (request.route_id, request.media, 1) not in self._completed
                or self.clock.monotonic() >= self.deadline):
            return None
        key = (request.media, request.query_sha256)
        if key not in self._issued:
            cursor = Cursor(self.job_id, request.query_sha256, request.media, 2)
            self._issued[key] = cursor
            self._cursors[cursor] = False
        return self._issued[key]

    def admit(self, request: FixedRequest, *, clock: Clock) -> Attempt | Decision:
        now = clock.monotonic()
        if now >= self.deadline:
            return Decision('limited', 'job_deadline')
        if request.job_id != self.job_id or request.fictional != self.fictional:
            return Decision('blocked', 'invalid_request')
        if request.page > 2:
            return Decision('limited', 'pagination_limit')
        key = (request.route_id, request.media, request.page)
        if request.parent is None and key in self._completed:
            return Decision('blocked', 'invalid_request')
        if request.cursor is not None and not self.accepts_cursor(
                request.cursor, media=request.media, query_sha256=request.query_sha256):
            return Decision('blocked', 'invalid_request')
        if request.parent is not None:
            if request.parent not in self._released:
                return Decision('blocked', 'invalid_request')
            if sum(r.parent is not None for r, _ in self._attempts) >= 2:
                return Decision('limited', 'video_request_limit')
        if sum(now - a.started < 60 for _, a in self._attempts) >= 6:
            return Decision('limited', 'request_budget')
        if self._attempts and now - self._attempts[-1][1].started < 1:
            return Decision('limited', 'pacing')
        attempt = Attempt(self.job_id, len(self._attempts) + 1, now, now + 10,
                          clock.utcnow(), self)
        self._attempts.append((request, attempt))
        self._body_counts[attempt] = 0
        if request.cursor is not None:
            self._cursors[request.cursor] = True
        return attempt

    def check_deadline(self, *, attempt: Attempt, clock: Clock) -> Decision | None:
        if attempt not in self._body_counts or attempt._budget is not self:
            return Decision('blocked', 'invalid_request')
        now = clock.monotonic()
        if now >= self.deadline:
            return Decision('limited', 'job_deadline')
        if now >= attempt.deadline:
            return Decision('failed', 'request_timeout')
        return None

    def consume_bytes(self, count: int, *, attempt: Attempt, clock: Clock) -> Decision | None:
        decision = self.check_deadline(attempt=attempt, clock=clock)
        if decision is not None:
            return decision
        if type(count) is not int or count < 0:
            return Decision('failed', 'malformed_headers')
        # Observed overflow counts even though that chunk must never be retained.
        self._bytes += count
        self._body_counts[attempt] += count
        if self._bytes > 1048576:
            return Decision('limited', 'job_byte_limit')
        if self._body_counts[attempt] > 262144:
            return Decision('limited', 'response_byte_limit')
        return None

    def record_page(self, request: FixedRequest, identities: tuple[SourceIdentity, ...],
                    *, page_valid: bool) -> None:
        if not any(r is request for r, _ in self._attempts):
            raise ValueError('Page not admitted by this job')
        if not page_valid or request in self._valid_pages:
            return
        if request.parent is not None:
            if identities:
                raise ValueError('Videos cannot release discovery identities')
        else:
            if (len(identities) > 20 or any(i.media != request.media
                    or i.fictional != self.fictional for i in identities)
                    or len(self._released | set(identities)) > 80):
                raise ValueError('Invalid released discovery identities')
            self._released.update(identities)
        self._completed.add((request.route_id, request.media, request.page))
        self._valid_pages.add(request)
