"""Frozen REQ033 server-owned ports shared by Taste, Catalogue and Experience.

These immutable records bind work; constructing one never creates authority,
source rights, a spend allowance, a durable attempt or a publication. The host
must supply current backend authority and capability-checked implementations.
Catalogue owns collection/evidence semantics; this module owns shared shapes.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
from typing import Protocol

from .disclosure import canonical_bytes, read_json


VERSION = 'enhance/v1'
STAGES = ('authorized', 'collecting', 'sealed', 'awaiting_preview', 'ready',
          'reserved', 'in_flight', 'validated', 'staged', 'published')
TERMINALS = ('blocked', 'no_candidates', 'failed', 'cancelled', 'stale', 'unknown')
CATEGORY_STATES = ('fresh', 'empty_bounded', 'unavailable', 'failed', 'cancelled', 'retained_previous_stale')
SAFE_CODES = ('OK', 'UNAUTHENTICATED', 'AUTHORITY_DENIED', 'REVISION_CONFLICT',
    'IDEMPOTENCY_CONFLICT', 'PREVIEW_STALE', 'VALIDATION_STALE', 'RUN_STALE',
    'UNKNOWN_RECONCILIATION_REQUIRED', 'CONTRACT_INVALID', 'LIMIT_EXCEEDED',
    'NO_GROUNDED_CANDIDATES', 'RATE_LIMITED', 'SOURCE_UNAVAILABLE',
    'CREDENTIAL_REQUIRED', 'CAPABILITY_UNAVAILABLE')


class EnhanceFault(ValueError):
    def __init__(self, code='CONTRACT_INVALID'):
        self.code = code if code in SAFE_CODES else 'CONTRACT_INVALID'
        super().__init__(self.code)


def require(condition, code='CONTRACT_INVALID'):
    if not condition:
        raise EnhanceFault(code)


def text(value, limit=300):
    require(type(value) is str and 0 < len(value) <= limit)
    return value


def utc(value):
    require(type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0))
    return value


def digest(raw):
    require(type(raw) is bytes)
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class RegistryView:
    revision: str
    categories: tuple[str, ...]

    def __post_init__(self):
        text(self.revision)
        require(type(self.categories) is tuple and 0 < len(self.categories) <= 100)
        for category in self.categories:
            text(category, 80)
        require('all' not in self.categories and len(set(self.categories)) == len(self.categories))


@dataclass(frozen=True)
class EnhanceBindings:
    installation_id: str
    actor_ref: str
    profile_ref: str
    snapshot_revision: str
    policy_epoch: int
    restriction_watermark: str
    registry_revision: str
    config_revision: int
    grants_digest: str
    connection_digest: str

    def __post_init__(self):
        for name in ('installation_id', 'actor_ref', 'profile_ref', 'snapshot_revision',
                     'restriction_watermark', 'registry_revision', 'grants_digest', 'connection_digest'):
            text(getattr(self, name))
        require(type(self.policy_epoch) is int and self.policy_epoch >= 0)
        require(type(self.config_revision) is int and self.config_revision >= 0)


@dataclass(frozen=True)
class EnhanceIntent:
    run_ref: str
    client_key: str
    refresh_nonce: str
    bindings: EnhanceBindings

    def __post_init__(self):
        text(self.run_ref)
        text(self.client_key)
        text(self.refresh_nonce)
        require(type(self.bindings) is EnhanceBindings)


@dataclass(frozen=True, repr=False)
class CataloguePlan:
    """Exact authorized query JSON; backend source authorization stays separate.

    Body shape: {schema:'catalogue-query/v1', categories:[enabled keys],
    queries:[{category,terms:[exact permitted title strings]}], profile: D3
    permitted profile}. No URL, header, credential or browser-selected endpoint.
    Collectors must authorize any actual registered-source request independently;
    this plan does not license a legacy transport or undisclosed derived query.
    """
    plan_ref: str
    bindings: EnhanceBindings
    categories: tuple[str, ...]
    body: bytes = field(repr=False)
    body_digest: str
    expires_at: datetime

    def __post_init__(self):
        text(self.plan_ref)
        require(type(self.bindings) is EnhanceBindings)
        RegistryView(self.bindings.registry_revision, self.categories)
        require(type(self.body) is bytes and 0 < len(self.body) <= 262144, 'LIMIT_EXCEEDED')
        require(digest(self.body) == self.body_digest)
        utc(self.expires_at)
        value = read_json(self.body)
        require(type(value) is dict and set(value) == {'schema', 'categories', 'queries', 'profile'})
        require(value['schema'] == 'catalogue-query/v1' and value['categories'] == list(self.categories))
        require(type(value['queries']) is list and len(value['queries']) == len(self.categories))
        for category, query in zip(self.categories, value['queries']):
            require(type(query) is dict and set(query) == {'category', 'terms'} and query['category'] == category)
            require(type(query['terms']) is list and len(query['terms']) <= 500)
            for term in query['terms']:
                text(term)
            require(len(set(query['terms'])) == len(query['terms']))


@dataclass(frozen=True)
class CollectionBudget:
    initial_sends: int = 12
    sources_per_category: int = 2
    candidates_per_category: int = 20
    candidates_total: int = 100
    aggregate_bytes: int = 2097152
    response_bytes: int = 262144
    request_seconds: int = 8
    parent_seconds: int = 9
    collection_seconds: int = 90

    def __post_init__(self):
        ceilings = (12, 2, 20, 100, 2097152, 262144, 8, 9, 90)
        for name, maximum in zip(self.__dataclass_fields__, ceilings):
            value = getattr(self, name)
            require(type(value) is int and 0 < value <= maximum, 'LIMIT_EXCEEDED')


@dataclass(frozen=True)
class CategoryOutcome:
    category: str
    state: str
    candidate_count: int = 0
    code: str = 'OK'

    def __post_init__(self):
        text(self.category, 80)
        require(self.state in CATEGORY_STATES and self.code in SAFE_CODES)
        require(type(self.candidate_count) is int and 0 <= self.candidate_count <= 20)
        require(self.state == 'fresh' or self.candidate_count == 0)
        require(self.state != 'fresh' or self.candidate_count > 0)


@dataclass(frozen=True, repr=False)
class CollectionSeal:
    """Catalogue-owned evidence, immutable and complete for the enabled registry.

    _material is canonical {records,observations,policies} using accepted REQ-010
    source bindings. source_revisions is canonical current source revision JSON.
    The host revalidates authenticity/freshness; a digest alone grants nothing.
    """
    collection_ref: str
    plan_ref: str
    bindings: EnhanceBindings
    outcomes: tuple[CategoryOutcome, ...]
    _material: bytes = field(repr=False)
    source_revisions: bytes = field(repr=False)
    expires_at: datetime

    def __post_init__(self):
        text(self.collection_ref)
        text(self.plan_ref)
        require(type(self.bindings) is EnhanceBindings and type(self.outcomes) is tuple)
        require(all(type(row) is CategoryOutcome for row in self.outcomes))
        require(len({row.category for row in self.outcomes}) == len(self.outcomes))
        utc(self.expires_at)
        value = read_json(self._material)
        require(type(value) is dict and set(value) == {'records', 'observations', 'policies'})
        require(type(value['records']) is list and len(value['records']) <= 100, 'LIMIT_EXCEEDED')
        require(sum(row.candidate_count for row in self.outcomes) == len(value['records']))
        require(type(read_json(self.source_revisions)) is dict)

    @property
    def records(self):
        return read_json(self._material)['records']

    @property
    def observations(self):
        return read_json(self._material)['observations']

    @property
    def policies(self):
        return read_json(self._material)['policies']

    @property
    def collection_digest(self):
        return digest(canonical_bytes({'material': read_json(self._material),
            'source_revisions': read_json(self.source_revisions),
            'outcomes': [vars(row) for row in self.outcomes], 'plan_ref': self.plan_ref,
            'bindings': vars(self.bindings), 'expires_at': self.expires_at.isoformat()}))

    def verify(self, plan, registry, *, now):
        require(type(plan) is CataloguePlan and type(registry) is RegistryView)
        require(self.plan_ref == plan.plan_ref and self.bindings == plan.bindings, 'PREVIEW_STALE')
        require(self.bindings.registry_revision == registry.revision
            and plan.categories == registry.categories, 'PREVIEW_STALE')
        require({row.category for row in self.outcomes} == set(registry.categories))
        require(utc(now) < min(self.expires_at, plan.expires_at), 'PREVIEW_STALE')
        counts = {key: 0 for key in registry.categories}
        seen = set()
        for record in self.records:
            require(type(record) is dict and type(record.get('candidate')) is dict)
            candidate = record['candidate']
            key, category = candidate.get('key'), candidate.get('category')
            text(key)
            text(category, 80)
            require(category in counts and key not in seen)
            seen.add(key)
            counts[category] += 1
        require(all(counts[row.category] == row.candidate_count for row in self.outcomes))


@dataclass(frozen=True)
class StageEvent:
    run_ref: str
    stage: str
    code: str = 'OK'
    outcomes: tuple[CategoryOutcome, ...] = ()
    publication_revision: int | None = None

    def __post_init__(self):
        text(self.run_ref)
        require(self.stage in STAGES + TERMINALS and self.code in SAFE_CODES)
        require(type(self.outcomes) is tuple and all(type(row) is CategoryOutcome for row in self.outcomes))
        require(self.publication_revision is None or type(self.publication_revision) is int and self.publication_revision > 0)
        require(self.stage != 'published' or self.publication_revision is not None)

    def public(self):
        # No prompt, profile IDs, provider errors or source/model prose enter UI status.
        return {'schema': VERSION, 'run_ref': self.run_ref, 'state': self.stage,
                'stage': self.stage, 'code': self.code, 'terminal': self.stage in TERMINALS,
                'category_outcomes': [vars(row).copy() for row in self.outcomes],
                'run_ready': False, 'published': self.stage == 'published',
                'publication_revision': self.publication_revision}


class CataloguePort(Protocol):
    def collect(self, authorized_query_plan: CataloguePlan, registry_view: RegistryView,
                budget: CollectionBudget) -> CollectionSeal: ...


class EnhancePorts(Protocol):
    """Future backend composition; never select these ports from browser/source JSON.

    revalidate covers current authority, full snapshot, grants, source seal and
    config/proof/credential/floor/expiry bindings. reserve_attempt must be durable;
    dispatch accepts only its server-issued reservation and original sealed body.
    This tranche supplies neither implementation nor a production send route.
    """
    def revalidate(self, intent, plan, seal, body): ...
    def reserve_attempt(self, intent, seal, body): ...
    def dispatch(self, reservation, body) -> bytes: ...
