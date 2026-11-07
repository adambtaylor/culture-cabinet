"""Initial configured collection/enrichment, complete coverage and immutable seal.

This service contains no inference, publication, network client or credential
lookup. Explicit backend ports own real source authorization and bounded I/O.
Legacy incompatible transports remain unavailable rather than borrowing keys.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from threading import RLock
import time
from types import MappingProxyType
from uuid import uuid4

from .category_registry import registry_view as current_registry
from .disclosure import DisclosureError, canonical_bytes, read_json
from .enhance_contract import (CataloguePlan, CategoryOutcome, CollectionBudget,
    CollectionSeal, EnhanceFault, RegistryView, digest, require, utc)
from .source_adapters import (SourceBackend, SourcePermit, SourceReply, SourceRequest,
    normalize, registered_source)
from .source_evidence import EvidenceBatch, FieldPolicy, build_evidence, cached_evidence


class CollectionClock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)


class CollectionState:
    """Private process state, explicitly shared by cooperating collector instances.

    Production integration additionally needs host-wide provider reservations;
    this lock/cache is not a cross-process limiter or durable job service.
    """
    def __init__(self):
        self.lock = RLock()
        self.not_before = {}
        self.cache = {}


@dataclass(frozen=True)
class _Result:
    batch: EvidenceBatch
    revision_material: bytes


class SourceCollection:
    def __init__(self, configured=(), backends=None, *, clock=None, state=None,
                 isolated_fixture=False):
        require(type(configured) is tuple and type(isolated_fixture) is bool)
        require(backends is None or type(backends) is dict)
        selected = {}
        for category, names in configured:
            require(category not in selected and type(names) is tuple
                    and len(names) <= 2 and len(set(names)) == len(names))
            for name in names:
                require(category in registered_source(name).categories)
            selected[category] = names
        supplied = dict(backends or {})
        for name, backend in supplied.items():
            registered_source(name)
            require(isinstance(backend, SourceBackend))
            require(not backend.isolated_fixture or isolated_fixture, 'AUTHORITY_DENIED')
        self.configured = MappingProxyType(selected)
        self.backends = MappingProxyType(supplied)
        self.clock = clock or CollectionClock()
        self.state = state or CollectionState()
        require(type(self.state) is CollectionState)
        self.isolated_fixture = isolated_fixture
        self._sealed = {}

    def collect(self, authorized_query_plan, registry_view, budget):
        require(type(authorized_query_plan) is CataloguePlan
                and type(registry_view) is RegistryView and type(budget) is CollectionBudget)
        plan, registry = authorized_query_plan, registry_view
        require(registry == current_registry(registry.categories), 'PREVIEW_STALE')
        require(plan.categories == registry.categories
                and plan.bindings.registry_revision == registry.revision, 'PREVIEW_STALE')
        require(utc(self.clock.now()) < plan.expires_at, 'PREVIEW_STALE')
        queries = read_json(plan.body)['queries']
        require(set(self.configured) <= set(registry.categories))
        allocations = {}
        for query in queries:
            category = query['category']
            names = self.configured.get(category, ())
            require(len(names) <= budget.sources_per_category, 'LIMIT_EXCEEDED')
            requests = []
            for name in names:
                spec = registered_source(name)
                if name not in self.backends or spec.search and not query['terms']:
                    continue
                # Search coverage is explicitly bounded to the first permitted
                # term per selected source. The full profile/history remains in
                # the shared plan and later inference; nothing is silently cut.
                terms = tuple(query['terms'][:1]) if spec.search else ()
                requests.append(SourceRequest(name, category, spec.operations[0], terms))
            allocations[category] = tuple(requests)
        # Reserve worst-case enrichment capacity for ALL categories before any
        # read. A useful early category must not consume a hidden category's slot.
        cost = sum(2 if req.source == 'hardcover' else 1
                   for requests in allocations.values() for req in requests)
        if cost > budget.initial_sends:
            outcomes = tuple(CategoryOutcome(category, 'unavailable', 0,
                'LIMIT_EXCEEDED' if allocations[category] else 'CAPABILITY_UNAVAILABLE')
                for category in registry.categories)
            return self._seal(plan, registry, outcomes, (), {}, plan.expires_at, budget)
        with self.state.lock:
            start = self.clock.monotonic()
            usage = {'sends': 0, 'bytes': 0}
            batches, revisions, outcomes, retained, attempts = [], {}, [], [], []
            seen, total = set(), 0
            for query in queries:
                category = query['category']
                records, category_batches, failures = [], [], []
                for request in allocations[category]:
                    try:
                        result = self._read(request, plan, budget, usage, start, retained)
                        material = read_json(result.batch.material)
                        # Exact IDs dedupe; names/titles/URLs never merge works,
                        # adaptations or distinct occurrences across sources.
                        fresh = [record for record in material['records']
                                 if record['candidate']['key'] not in seen]
                        require(len(records) + len(fresh) <= budget.candidates_per_category
                                and total + len(fresh) <= budget.candidates_total, 'LIMIT_EXCEEDED')
                        seen.update(row['candidate']['key'] for row in fresh)
                        total += len(fresh)
                        records.extend(fresh)
                        category_batches.append(EvidenceBatch(canonical_bytes({**material,
                            'records': fresh}), result.batch.expires_at, False))
                        key = category + ':' + request.source
                        revisions[key] = {**read_json(result.revision_material),
                            'planned_term_count': len(query['terms']),
                            'requested_term_count': len(request.terms),
                            'source_admitted_count': len(material['records']),
                            'duplicate_count': len(material['records']) - len(fresh)}
                        attempts.append({'category': category, 'source': request.source, 'code': 'OK'})
                    except EnhanceFault as error:
                        if error.code == 'PREVIEW_STALE':
                            raise
                        failures.append(error.code)
                        attempts.append({'category': category, 'source': request.source, 'code': error.code})
                    except TimeoutError:
                        failures.append('SOURCE_UNAVAILABLE')
                        attempts.append({'category': category, 'source': request.source, 'code': 'SOURCE_UNAVAILABLE'})
                    except DisclosureError as error:
                        code = EnhanceFault(error.code).code
                        failures.append(code)
                        attempts.append({'category': category, 'source': request.source, 'code': code})
                    except (ValueError, TypeError, KeyError, AttributeError, OSError):
                        # Provider text and exception strings never become UI
                        # status, instructions, policy or authority evidence.
                        failures.append('SOURCE_UNAVAILABLE')
                        attempts.append({'category': category, 'source': request.source, 'code': 'SOURCE_UNAVAILABLE'})
                if records:
                    outcome = CategoryOutcome(category, 'fresh', len(records),
                        failures[0] if failures else 'OK')
                elif not allocations[category]:
                    outcome = CategoryOutcome(category, 'unavailable', 0, 'CAPABILITY_UNAVAILABLE')
                elif failures:
                    code = failures[0]
                    state = 'unavailable' if code in ('CAPABILITY_UNAVAILABLE', 'AUTHORITY_DENIED',
                        'CREDENTIAL_REQUIRED', 'RATE_LIMITED') else 'failed'
                    outcome = CategoryOutcome(category, state, 0, code)
                else:
                    outcome = CategoryOutcome(category, 'empty_bounded')
                batches.extend(category_batches)
                outcomes.append(outcome)
            # Revalidate every used source, including cached/empty reads. One
            # revoked source cannot remain in a sealed supposedly current pool.
            for backend, permit, request, policy in retained:
                self._current(backend, permit, request, plan, policy)
            revisions['_collection'] = {'sends': usage['sends'], 'response_bytes': usage['bytes'],
                'initial_sends_reserved': cost, 'extension_sends': 0, 'retry_sends': 0,
                'source_outcomes': attempts,
                'configured_sources': {key: list(value) for key, value in self.configured.items()}}
            end = min((batch.expires_at for batch in batches), default=plan.expires_at)
            return self._seal(plan, registry, tuple(outcomes), tuple(batches), revisions, end, budget,
                              retained=tuple(retained))

    def _current(self, backend, permit, request, plan, policy):
        now = utc(self.clock.now())
        permit.verify(request, plan, now)
        require(backend.revalidate(permit, request, plan, now=now) is True, 'PREVIEW_STALE')
        require(type(policy) is FieldPolicy and policy.current(now)
                and policy.ref == permit.policy_ref and policy.revision == permit.policy_revision,
                'AUTHORITY_DENIED')
        require(backend.policy(permit, now=now) == policy, 'PREVIEW_STALE')
        require(policy.allows('*', 'fetch'), 'AUTHORITY_DENIED')

    def _read(self, request, plan, budget, usage, start, retained):
        spec, backend = registered_source(request.source), self.backends[request.source]
        now = utc(self.clock.now())
        permit = backend.authorize(request, plan, now=now)
        require(type(permit) is SourcePermit, 'AUTHORITY_DENIED')
        policy = backend.policy(permit, now=now)
        self._current(backend, permit, request, plan, policy)
        key = digest(canonical_bytes({'bindings': vars(plan.bindings), 'request': request.material,
            'source_config': permit.source_config_revision, 'source_revision': permit.source_revision,
            'policy': {'ref': policy.ref, 'revision': policy.revision,
                'permissions': [[name, list(uses)] for name, uses in policy.permissions],
                'checked_at': policy.checked_at.isoformat(), 'expires_at': policy.expires_at.isoformat()},
            'credential_generation': permit.credential_generation,
            'parents': list(permit.parent_ids), 'fixture': self.isolated_fixture}))
        cached = self.state.cache.get(key)
        if cached and now < cached.batch.expires_at:
            retained.append((backend, permit, request, policy))
            return _Result(cached_evidence(cached.batch), canonical_bytes({
                **read_json(cached.revision_material), 'cache_hit': True}))
        reply = self._send(backend, spec, request, permit, plan, policy, budget, usage, start)
        self._current(backend, permit, request, plan, policy)
        response_chain = [{'request_digest': request.request_digest,
            'body_digest': digest(reply.body), 'observed_at': reply.observed_at.isoformat()}]
        parsed = normalize(spec, request, reply, now=utc(self.clock.now()))
        if parsed.enrichment_ids:
            details = SourceRequest(request.source, request.category, 'book_details',
                                    request.terms, parsed.enrichment_ids)
            second = backend.authorize(details, plan, now=utc(self.clock.now()))
            require(type(second) is SourcePermit, 'AUTHORITY_DENIED')
            self._current(backend, second, details, plan, policy)
            require(second.parent_ids == permit.parent_ids
                    and second.source_revision == permit.source_revision
                    and second.source_config_revision == permit.source_config_revision
                    and second.credential_generation == permit.credential_generation, 'PREVIEW_STALE')
            reply = self._send(backend, spec, details, second, plan, policy, budget, usage, start)
            self._current(backend, second, details, plan, policy)
            response_chain.append({'request_digest': details.request_digest,
                'body_digest': digest(reply.body), 'observed_at': reply.observed_at.isoformat()})
            retained.append((backend, second, details, policy))
            parsed = normalize(spec, details, reply, now=utc(self.clock.now()))
        now = utc(self.clock.now())
        require(reply.observed_at <= now, 'CONTRACT_INVALID')
        ttl = min(spec.ttl_seconds, 900 if request.category in
            ('event', 'gallery', 'art_fair', 'community', 'regional_day_trip', 'outing', 'dining') else 86400)
        cache_control = dict(reply.headers).get('cache-control', '').lower()
        for directive in cache_control.split(','):
            if directive.strip().startswith('max-age='):
                value = directive.strip()[8:].strip('"')
                require(value.isdigit() and len(value) <= 10)
                ttl = min(ttl, int(value))
        source_end = reply.observed_at + timedelta(seconds=ttl)
        if request.source == 'moshtix':
            source_end = moshtix_response_expiry(reply, ttl=ttl)
            require(now < source_end, 'SOURCE_UNAVAILABLE')
        end = min(source_end, permit.expires_at,
                  policy.expires_at, plan.expires_at)
        # Search membership and detail identity both contribute to enrichment.
        # Preserve both observations; a detail-only hash would lose the evidence
        # for why this particular identity was in the source-derived pool.
        response_digest = digest(canonical_bytes(response_chain))
        batch = build_evidence(parsed.cards, category=request.category,
            observation_ref='observation:' + digest(canonical_bytes([request.request_digest,
                response_digest, reply.observed_at.isoformat()])),
            source_revision=permit.source_revision, response_digest=response_digest,
            observed_at=reply.observed_at, expires_at=end, parents=permit.parent_ids,
            policy=policy, now=now, fixture=backend.isolated_fixture)
        # A successful parse with records removed by policy is unavailable, not
        # proof that this source returned no inventory in its bounded request.
        if parsed.cards and not read_json(batch.material)['records']:
            raise EnhanceFault('AUTHORITY_DENIED')
        retained.append((backend, permit, request, policy))
        result = _Result(batch, canonical_bytes({'source_revision': permit.source_revision,
            'source_config_revision': permit.source_config_revision,
            'policy_ref': policy.ref, 'policy_revision': policy.revision,
            'credential_generation': permit.credential_generation, 'cache_hit': False,
            'response_chain': response_chain, 'observation_digest_kind': 'canonical_response_chain',
            'evidence_kind': 'fixture_not_live' if backend.isolated_fixture else 'live_lookup'}))
        # No Moshtix facts are kept in this collector's lazy-eviction cache.
        # A later host cache needs an admitted physical expiry/removal lifecycle;
        # rejecting stale reads alone would not bound idle data retention.
        if (request.source != 'moshtix' and batch.cacheable
                and 'no-store' not in cache_control and 'no-cache' not in cache_control):
            for stale in [k for k, value in self.state.cache.items() if now >= value.batch.expires_at]:
                del self.state.cache[stale]
            if len(self.state.cache) >= 128:
                del self.state.cache[next(iter(self.state.cache))]
            self.state.cache[key] = result
        return result

    def _send(self, backend, spec, request, permit, plan, policy, budget, usage, start):
        require(usage['sends'] < budget.initial_sends, 'LIMIT_EXCEEDED')
        elapsed = self.clock.monotonic() - start
        wait = max(0, self.state.not_before.get(spec.origin, 0) - self.clock.monotonic())
        require(elapsed >= 0 and elapsed + wait + budget.parent_seconds <= budget.collection_seconds,
                'RATE_LIMITED' if wait else 'LIMIT_EXCEEDED')
        if wait:
            self.clock.sleep(wait)
        self._current(backend, permit, request, plan, policy)
        self.state.not_before[spec.origin] = self.clock.monotonic() + spec.interval_seconds
        usage['sends'] += 1  # Reserve even a failed/timeout attempt; never retry.
        remaining = budget.aggregate_bytes - usage['bytes']
        require(remaining > 0, 'LIMIT_EXCEEDED')
        cap = min(budget.response_bytes, spec.response_bytes, remaining)
        sent = self.clock.monotonic()
        sent_at = utc(self.clock.now())
        request_limit = min(budget.request_seconds, 5 if not spec.search else 8)
        parent_limit = min(budget.parent_seconds, 6 if not spec.search else 9)
        reply = backend.request(request, permit, response_bytes=cap,
            request_seconds=request_limit, parent_seconds=parent_limit)
        require(0 <= self.clock.monotonic() - sent <= parent_limit
                and self.clock.monotonic() - start <= budget.collection_seconds, 'LIMIT_EXCEEDED')
        require(type(reply) is SourceReply and reply.request_digest == request.request_digest)
        require(sent_at <= reply.observed_at <= utc(self.clock.now()))
        usage['bytes'] += len(reply.body)
        require(len(reply.body) <= cap and usage['bytes'] <= budget.aggregate_bytes, 'LIMIT_EXCEEDED')
        if reply.status in (403, 429, 503):
            cooldown = 3600 if spec.key in ('gertrude', 'apw') and reply.status in (403, 429) else 60
            raw = dict(reply.headers).get('retry-after', '')
            if raw.isdigit() and len(raw) <= 10:
                cooldown = max(cooldown, int(raw))
            elif raw:
                try:
                    when = parsedate_to_datetime(raw)
                    require(when.tzinfo is not None)
                    cooldown = max(cooldown, (when - utc(self.clock.now())).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
            self.state.not_before[spec.origin] = max(self.state.not_before[spec.origin],
                                                     self.clock.monotonic() + cooldown)
            raise EnhanceFault('RATE_LIMITED' if reply.status == 429 else
                               'AUTHORITY_DENIED' if reply.status == 403 else 'SOURCE_UNAVAILABLE')
        require(reply.status not in (401,), 'CREDENTIAL_REQUIRED')
        require(reply.status == 200, 'SOURCE_UNAVAILABLE')
        return reply

    def _seal(self, plan, registry, outcomes, batches, revisions, end, budget, *, retained=()):
        for backend, permit, request, policy in retained:
            self._current(backend, permit, request, plan, policy)
        value = seal(outcomes, batches, revisions, plan=plan, registry=registry,
                     budget=budget, now=utc(self.clock.now()), expires_at=end)
        with self.state.lock:
            for key in [key for key, item in self._sealed.items()
                        if utc(self.clock.now()) >= item[4]]:
                del self._sealed[key]
            require(len(self._sealed) < 128, 'CAPABILITY_UNAVAILABLE')
            self._sealed[value.collection_ref] = (value.collection_digest, plan, registry,
                                                  retained, value.expires_at)
        return value

    def revalidate_collection(self, value, plan, registry):
        """Broker use gate for this exact server-retained seal, including cache.

        A restart has no retained issuer capabilities and therefore requires
        collection again. Serialized JSON/hashes cannot recreate source authority.
        The integrating broker must call this immediately before inference use.
        """
        require(type(value) is CollectionSeal, 'AUTHORITY_DENIED')
        with self.state.lock:
            retained = self._sealed.get(value.collection_ref)
            require(retained is not None
                    and retained[:3] == (value.collection_digest, plan, registry), 'PREVIEW_STALE')
            value.verify(plan, registry, now=utc(self.clock.now()))
            for backend, permit, request, policy in retained[3]:
                self._current(backend, permit, request, plan, policy)
        return True


def seal(category_outcomes, observation_refs, rights_revisions, *, plan, registry,
         budget, now, expires_at):
    """Seal only immutable admitted evidence against its complete original plan.

    observation_refs are EvidenceBatch values produced by field admission, not
    caller-supplied source facts. The seal is a binding, not a permission issuer.
    """
    require(type(plan) is CataloguePlan and type(registry) is RegistryView
            and type(budget) is CollectionBudget and type(category_outcomes) is tuple
            and type(observation_refs) is tuple and type(rights_revisions) is dict)
    require(all(type(batch) is EvidenceBatch for batch in observation_refs))
    records, observations, policies = [], {}, {}
    for batch in observation_refs:
        material = read_json(batch.material)
        records.extend(material['records'])
        for target, additions in ((observations, material['observations']),
                                  (policies, material['policies'])):
            for key, value in additions.items():
                require(key not in target or target[key] == value, 'PREVIEW_STALE')
                target[key] = value
    require(len(records) <= budget.candidates_total, 'LIMIT_EXCEEDED')
    value = CollectionSeal(str(uuid4()), plan.plan_ref, plan.bindings, category_outcomes,
        canonical_bytes({'records': records, 'observations': observations, 'policies': policies}),
        canonical_bytes(rights_revisions), min(utc(expires_at), plan.expires_at))
    value.verify(plan, registry, now=utc(now))
    return value


def collect(authorized_query_plan, registry_view, budget):
    """Unconfigured public seam is honest unavailable coverage, never legacy I/O."""
    return SourceCollection().collect(authorized_query_plan, registry_view, budget)


def moshtix_response_expiry(reply, *, ttl=900):
    """Intersect freshness clocks; Age/Date/Expires never extend the 15m cap."""
    require(type(reply) is SourceReply and type(ttl) is int and 0 <= ttl <= 900)
    headers = dict(reply.headers)
    try:
        age = headers.get('age', '0')
        require(age.isascii() and age.isdigit() and len(age) <= 10, 'SOURCE_UNAVAILABLE')
        end = reply.observed_at + timedelta(seconds=ttl - int(age))
        if 'date' in headers:
            stamp = parsedate_to_datetime(headers['date'])
            require(stamp.tzinfo is not None, 'SOURCE_UNAVAILABLE')
            end = min(end, stamp.astimezone(timezone.utc) + timedelta(seconds=ttl))
        if 'expires' in headers:
            stamp = parsedate_to_datetime(headers['expires'])
            require(stamp.tzinfo is not None, 'SOURCE_UNAVAILABLE')
            end = min(end, stamp.astimezone(timezone.utc))
        return end
    except (ValueError, TypeError, OverflowError):
        raise EnhanceFault('SOURCE_UNAVAILABLE') from None


