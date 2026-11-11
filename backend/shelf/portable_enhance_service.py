"""Owner-scoped durable Enhance composition with explicit source/send authority.

The HTTP interface accepts opaque references and explicit confirmations. Normal
composition authenticates source and personal ports independently; it never
imports a legacy key/profile or substitutes the neutral probe for personal work.
"""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, replace
import math
import time
import uuid

from django.views.decorators.debug import sensitive_variables
from django.db import connection

from . import grant_authority as ga, disclosure, profile_snapshot
from .disclosure import canonical_bytes, read_json, DisclosureError
from .enhance_contract import CategoryOutcome, EnhanceFault, StageEvent, digest
from .enhance_coordinator import (EnhanceCoordinator, EnhanceOutcome, PlannedEnhance,
    CollectedEnhance, InferencePreview)
from .models import ProfileCabinetState, SynthesisRun, PortableConnection
from .portable_authority import AuthorityFault
from .portable_cabinet_store import CabinetStore, StateProvider, checked_source_use, require, stamp

_ISOLATED = object()


class SavedCandidateUnavailable(AuthorityFault):
    message = ('Saving this source title, URL and attribution is unavailable: '
               'permission for a durable saved copy has not been established.')

    def __init__(self):
        super().__init__('CAPABILITY_UNAVAILABLE', 503)


@dataclass(frozen=True, repr=False)
class SavedCandidateEvidence:
    """Bounded attribution from a current publication; never a Save capability."""
    installation_ref: str
    owner_revision: int
    profile_ref: str
    actor_id: int
    profile_id: int
    publication_ref: str
    publication_revision: int
    run_ref: str
    candidate_key: str
    candidate_digest: str
    source_evidence_digest: str
    provider: str
    medium: str
    source_id: int
    title: str
    url: str
    fetched_at: datetime
    source_valid_until: datetime
    schema: str = 'portable-saved-candidate/v1'

    def __post_init__(self):
        require(self.schema == 'portable-saved-candidate/v1')
        for value in (self.installation_ref, self.profile_ref, self.publication_ref, self.run_ref):
            require(type(value) is str and str(ga._uuid(value)) == value)
        for value in (self.owner_revision, self.actor_id, self.profile_id, self.publication_revision):
            require(type(value) is int and 0 < value < 2**53)
        require(type(self.source_id) is int and 0 < self.source_id <= 2147483647)
        require(type(self.provider) is str and type(self.medium) is str
                and (self.provider, self.medium) in {('tmdb', 'movie'), ('tmdb', 'tv'), ('hardcover', 'book')},
                'CAPABILITY_UNAVAILABLE', 503)
        for value, limit in ((self.candidate_key, 300), (self.title, 300), (self.url, 500)):
            require(type(value) is str and 0 < len(value) <= limit and value.strip())
        for value in (self.candidate_digest, self.source_evidence_digest):
            require(type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value))
        require(all(type(value) is datetime and value.utcoffset() == timedelta(0)
                    for value in (self.fetched_at, self.source_valid_until)))
        require(self.fetched_at < self.source_valid_until)
        from .saved import canonical_url
        require(canonical_url(self.provider, self.medium, self.source_id, self.url) == self.url)


class _DurablePorts:
    def __init__(self, service, store, run_ref):
        self.service, self.store, self.run_ref = service, store, run_ref
        self.reservation = None

    def revalidate(self, intent, plan, seal, body):
        with self.service._collection_lock(self.service._memory.get(self.run_ref)), self.store.scope() as cabinet:
            current = self.store.current()
            row = self.store._run(cabinet, self.run_ref)
            self.store._match(row, current)
            require(intent.bindings == current.bindings, 'PREVIEW_STALE', 409)
            from .current_profile import CurrentProfile
            if type(self.store.provider) is CurrentProfile:
                memory = self.service._memory.get(self.run_ref, {})
                planned = self.service._planned(memory)
                plans = dict(planned.source_plans)
                for query in current.source_views:
                    selected = plans.get(query.connection_ref)
                    require(selected is not None, 'PREVIEW_STALE', 409)
                    ga.assert_current(query)
                    require(selected.bindings.connection_digest == digest(canonical_bytes(query.connection_binding))
                        and selected.bindings.grants_digest == digest(canonical_bytes(query.grants)), 'PREVIEW_STALE', 409)
                require(set(plans) == {view.connection_ref for view in current.source_views}, 'PREVIEW_STALE', 409)
            if seal is not None:
                seal.verify(plan, current.registry, now=self.service.authority.now())
                if type(self.store.provider) is CurrentProfile:
                    require(memory['coordinator'].catalogue.revalidate_collection(seal, plan, current.registry,
                        source_plans=dict(planned.source_plans)), 'PREVIEW_STALE', 409)
                    preparation = memory.get('preparatory')
                    if preparation is not None:
                        producer, prepared_seal = preparation
                        require(producer.revalidate_collection(prepared_seal, plan, current.registry,
                            source_plans=dict(planned.source_plans)), 'PREVIEW_STALE', 409)
            if body is not None and row.body_digest:
                require(digest(body) == row.body_digest, 'PREVIEW_STALE', 409)

    def reserve_attempt(self, intent, seal, body):
        require(type(body) is bytes and 0 < len(body) <= 16384, 'LIMIT_EXCEEDED', 422)
        row = self.store.status(self.run_ref)
        row = self.store.reserve(self.run_ref, body_digest=digest(body), expected_revision=row.revision)
        self.reservation = row.transport_request_id
        return row.transport_request_id

    @sensitive_variables()
    def dispatch(self, reservation, body):
        with self.store.scope() as cabinet:
            current = self.store.current()
            row = self.store._run(cabinet, self.run_ref)
            self.store._match(row, current)
            require(reservation == self.reservation == row.transport_request_id
                and row.state == 'running' and row.attempts == 1 and not row.cancel_requested
                and row.body_digest == digest(body), 'RUN_STALE', 409)
            self.store._change(row, portable_stage='in_flight')
        # The distinct personal permit rechecks the original reservation after
        # network setup. The neutral adapter retains its probe-only contract.
        from .inference_adapters import PersonalResponsesAdapter
        if type(self.service.personal_transport) is PersonalResponsesAdapter:
            from .inference_transport import personal_permit
            memory = self.service._memory[self.run_ref]
            @contextmanager
            def credential_scope():
                # Preserve recovery -> owner -> vault lock order while checking
                # the original durable reservation at the final send boundary.
                with self.service._collection_lock(memory), ga.recovery_guard(self.service.authority, self.store.session_ref,
                                       profile_ref=self.store.profile_ref), self.service.authority._lock:
                    planned = self.service._planned(memory)
                    self.revalidate(planned.intent, planned.plan, memory['work'].collected.seal, body)
                    with self.store.scope() as cabinet:
                        current = self.store.current()
                        latest = self.store._run(cabinet, self.run_ref)
                        self.store._match(latest, current)
                        require(latest.transport_request_id == reservation and latest.state == 'running'
                            and latest.attempts == 1 and not latest.cancel_requested
                            and latest.body_digest == digest(body), 'RUN_STALE', 409)
                    validation = self.service.validation
                    require(validation.status(self.store.session_ref, ga._uuid(self.store.connection_ref),
                        current.view.context['config_revision'])['connection_ready'], 'VALIDATION_STALE', 409)
                    with validation.vault.ephemeral(self.store.session_ref, ga._uuid(self.store.connection_ref),
                        current.view.context['config_revision'],
                        credential_generation=current.view.connection_binding['credential_generation']) as secret:
                        yield secret
            permit = personal_permit(digest(body), credential_scope, memory['expires_at'], self.service.authority.now)
            raw = self.service.personal_transport.dispatch(reservation, body, permit=permit)
        else:
            require(self.service._isolated is _ISOLATED, 'CAPABILITY_UNAVAILABLE', 503)
            raw = self.service.personal_transport.dispatch(reservation, body)
        self.store.record_result(self.run_ref, raw)
        return raw


class EnhanceService:
    def __init__(self, authority, validation=None, *, catalogue=None, registry=None,
                 personal_transport=None, state_provider=None, selector=None, reconciler=None):
        require(type(authority) is ga.PortableAuthority, 'AUTHORITY_DENIED', 403)
        require(not hasattr(authority, '_enhance_service'), 'REVISION_CONFLICT', 409)
        self.authority, self.validation = authority, validation
        self.catalogue, self.registry = catalogue, registry
        self.personal_transport, self.state_provider = personal_transport, state_provider
        self.selector, self.reconciler = selector, reconciler
        self._isolated, self._pipeline, self._memory = None, None, {}
        self._normal, self._contexts, self._policy_provider, self._clock = False, {}, None, None
        self._owner_sources = {}
        authority._enhance_service = self
        authority.boundary._closing.append(self.close)

    @classmethod
    def compose(cls, authority, validation, *, policy_provider=None, registry=None, clock=None):
        from .category_registry import registry_view
        from .inference_adapters import PersonalResponsesAdapter
        from .enhance_selection import GroundedSelector
        value = cls(authority, validation, registry=registry or registry_view(),
                    personal_transport=PersonalResponsesAdapter(), selector=GroundedSelector())
        value._normal, value._policy_provider, value._clock = True, policy_provider, clock
        authority._revokers.append(value._revoke_context)
        return value

    def _revoke_context(self, ref):
        for key, (_, issuer, _) in list(self._owner_sources.items()):
            if key[0] == ref:
                issuer.close()
                del self._owner_sources[key]
        for key, (_, provider, _) in list(self._contexts.items()):
            if key[0] == ref:
                provider.invalidate_content()
                del self._contexts[key]
        for key, memory in list(self._memory.items()):
            if memory['session_digest'] == digest(ref.encode()):
                del self._memory[key]

    def _normal_context(self, session_ref, profile_ref):
        from .current_profile import CurrentProfile
        from .source_authority import SourceAuthority
        from .source_adapters import MediaBackend, MoshtixBackend, GallerySourceBackend, registered_source
        from .source_collection import SourceCollection, CollectionClock
        from .discovery_transport import MediaHostLimiter, MoshtixHostLimiter
        with ga.recovery_guard(self.authority, session_ref, profile_ref=profile_ref), self.authority._lock:
            ga._bound(self.authority, session_ref, profile_ref)
            rows = list(PortableConnection.objects.filter(installation_id=self.authority.boundary.installation_id,
                removed=False).exclude(adapter='openai_responses').order_by('pk'))
            signature = tuple((str(row.pk), row.adapter, row.config_revision, row.credential_generation) for row in rows)
            key = (session_ref, str(profile_ref))
            prior = self._contexts.get(key)
            if prior and prior[0] == signature:
                return prior[1:]
            require(len(self._contexts) < 32 or prior is not None, 'CAPABILITY_UNAVAILABLE', 503)
            if prior:
                prior[1].invalidate_content()
            clock = self._clock or CollectionClock()
            sources, backends, configured, limiters = [], {}, {}, {}
            for row in rows:
                if row.adapter not in ('tmdb_movie', 'tmdb_tv', 'hardcover', 'moshtix_public',
                                       'musicbrainz_recording', 'musicbrainz_artist', 'apple_podcast','apw','gertrude'):
                    continue
                issuer = SourceAuthority(self.authority, session_ref, profile_ref=profile_ref,
                    connection_ref=str(row.pk), policy_provider=self._policy_provider)
                name = 'moshtix' if row.adapter == 'moshtix_public' else row.adapter
                require(name not in backends, 'REVISION_CONFLICT', 409)
                sources.append(issuer)
                try:
                    if name in ('apw','gertrude'):
                        backend=GallerySourceBackend(name,issuer,clock=clock)
                    elif name == 'apple_podcast':
                        from .apple_podcast_native import ApplePodcastBackend
                        backend = ApplePodcastBackend(issuer, MediaHostLimiter('apple_podcast'), clock=clock)
                    elif name in ('musicbrainz_recording', 'musicbrainz_artist'):
                        from .musicbrainz_source import MusicBrainzBackend
                        # Both fixed operations share one host reservation and
                        # pacing state; adding artists cannot double the rate.
                        if 'musicbrainz' not in limiters:
                            limiters['musicbrainz'] = MediaHostLimiter('musicbrainz')
                        backend = MusicBrainzBackend(issuer, limiters['musicbrainz'], clock=clock, source=name)
                    elif name == 'moshtix':
                        backend = MoshtixBackend(issuer, MoshtixHostLimiter(), clock=clock)
                    else:
                        provider_name = 'hardcover' if name == 'hardcover' else 'tmdb'
                        limiter = limiters.setdefault(provider_name, MediaHostLimiter(provider_name))
                        backend = MediaBackend(issuer, self.validation.vault, limiter, clock=clock)
                    backends[name] = backend
                except (AuthorityFault, EnhanceFault):
                    # Keep unavailable sources in CurrentProfile's revision;
                    # one missing credential must not erase other categories.
                    pass
                for category in registered_source(name).categories:
                    if category in self.registry.categories:
                        configured.setdefault(category, []).append(name)
            provider = CurrentProfile(self.authority, sources=tuple(sources), registry=self.registry)
            catalogue = SourceCollection(tuple((key, tuple(value)) for key, value in configured.items()),
                                         backends, clock=clock)
            self._contexts[key] = (signature, provider, catalogue)
            return provider, catalogue

    def owner_tv_source(self, session_ref, profile_ref):
        """Use the ordinary source stack without inference/profile capture.

        The separately admitted local export policy belongs only to this owner
        query path. It must never widen the existing Enhance source policy.
        """
        from django.conf import settings
        from .source_authority import SourceAuthority, SourcePolicyProvider
        from .source_adapters import MediaBackend
        from .source_collection import CollectionClock
        from .discovery_transport import MediaHostLimiter
        with ga.recovery_guard(self.authority, session_ref, profile_ref=profile_ref), self.authority._lock:
            state, _ = ga._bound(self.authority, session_ref, profile_ref, sensitive=True)
            provider = getattr(settings, 'PORTABLE_OWNER_TV_POLICY_PROVIDER', None)
            require(isinstance(provider, SourcePolicyProvider), 'CAPABILITY_UNAVAILABLE')
            rows = list(PortableConnection.objects.filter(installation_id=state.installation_id,
                adapter='tmdb_tv', removed=False)[:2])
            require(len(rows) == 1, 'CAPABILITY_UNAVAILABLE')
            row = rows[0]; key = (session_ref, str(profile_ref))
            signature = (str(row.pk), row.config_revision, row.credential_generation, id(provider))
            prior = self._owner_sources.get(key)
            if prior and prior[0] == signature:
                return prior[1:]
            require(prior is not None or len(self._owner_sources) < 32, 'CAPABILITY_UNAVAILABLE')
            if prior:
                prior[1].close()
            issuer = SourceAuthority(self.authority, session_ref, profile_ref=profile_ref,
                connection_ref=str(row.pk), policy_provider=provider)
            backend = MediaBackend(issuer, getattr(self.authority, '_source_vault', None),
                MediaHostLimiter('tmdb'), clock=self._clock or CollectionClock())
            self._owner_sources[key] = (signature, issuer, backend)
            return issuer, backend

    @staticmethod
    def _collection_lock(memory):
        # Producer collection holds its state lock before entering issuer
        # recovery/owner scope. Every consumer uses that SAME order, including
        # final-send validation; RLock alone cannot fix cross-thread inversion.
        from .source_collection import SourceCollection
        catalogue = memory['coordinator'].catalogue if memory else None
        return catalogue.state.lock if type(catalogue) is SourceCollection else nullcontext()

    @staticmethod
    def _settled_outcomes(memory, outcomes):
        retained = memory.get('preparatory_outcomes', {}) if memory else {}
        denied = {row['category'] for row in (memory or {}).get('source_use', ())
                  if row['collected_count'] and not row['model_eligible_count']}
        # Collection counts remain sealed. Only the publication/status outcome
        # changes: facts that cannot reach the model are unavailable, not empty.
        return tuple(CategoryOutcome(row.category, 'unavailable', 0, 'AUTHORITY_DENIED')
                     if row.category in denied else retained.get(row.category, row) for row in outcomes)

    @sensitive_variables()
    def _source_use(self, memory, collected):
        planned, seal = collected.planned, collected.seal
        producer = memory['coordinator'].catalogue
        query = getattr(producer, 'model_eligible_keys', None)
        require(callable(query), 'CAPABILITY_UNAVAILABLE', 503)
        with self._collection_lock(memory):
            eligible = query(seal, planned.plan, planned.registry,
                source_plans=dict(planned.source_plans) if planned.source_plans else None)
            seal.verify(planned.plan, planned.registry, now=self.authority.now())
            require(type(eligible) is frozenset)
            records = seal.records
            require(eligible <= {record['candidate']['key'] for record in records})
            counts = {category: 0 for category in planned.registry.categories}
            for record in records:
                candidate = record['candidate']
                if candidate['key'] in eligible:
                    counts[candidate['category']] += 1
            outcomes = {row.category: row for row in seal.outcomes}
            return checked_source_use([{'category': category,
                'collected_count': outcomes[category].candidate_count,
                'model_eligible_count': counts[category]} for category in planned.registry.categories],
                planned.registry.categories)

    @staticmethod
    def _planned(memory):
        work = memory['work']
        return work if type(work) is PlannedEnhance else work.collected.planned

    @classmethod
    def isolated_test(cls, authority, validation=None, *, pipeline=None, **kwargs):
        require(ga._fixture_database() and isinstance(kwargs.get('state_provider'), StateProvider),
                'CAPABILITY_UNAVAILABLE', 503)
        value = cls(authority, validation, **kwargs)
        value._isolated, value._pipeline = _ISOLATED, pipeline
        return value

    def close(self):
        self._memory.clear()  # Raw bodies/reconstruction refs never survive process loss.
        for _, provider, _ in self._contexts.values():
            provider.invalidate_content()
        self._contexts.clear()
        for _, issuer, _ in self._owner_sources.values():
            issuer.close()
        self._owner_sources.clear()

    def _store(self, session_ref, profile_ref, connection_ref):
        provider = self._normal_context(session_ref, profile_ref)[0] if self._normal else self.state_provider
        kwargs = dict(profile_ref=profile_ref, connection_ref=connection_ref, state_provider=provider)
        factory = CabinetStore.isolated_test if self._isolated is _ISOLATED else CabinetStore
        return factory(self.authority, session_ref, **kwargs)

    def _available(self):
        from .current_profile import CurrentProfile
        configured = (self.catalogue is not None or self._normal) and self.registry is not None
        isolated = (self._isolated is _ISOLATED and ga._fixture_database()
            and isinstance(self.state_provider, StateProvider)
            and callable(getattr(self.personal_transport, 'dispatch', None))
            and callable(getattr(self.selector, 'build', None)))
        concrete = type(self.state_provider) is CurrentProfile and self.state_provider.authority is self.authority
        return configured and (isolated or concrete or self._normal)

    def _inference_available(self):
        return callable(getattr(self.personal_transport, 'dispatch', None)) and callable(getattr(self.selector, 'build', None))

    @sensitive_variables()
    def _find(self, session_ref, run_ref, *, sensitive=False):
        # Only use DB-held scope after authenticating installation and owner.
        with ga.recovery_guard(self.authority, session_ref), self.authority._lock:
            installation, session = ga._owner(self.authority, session_ref, sensitive=sensitive)
            row = SynthesisRun.objects.select_related('profile').filter(pk=ga._uuid(run_ref),
                contract_version='enhance/v1', installation=installation, actor_id=session.owner_id).first()
            require(row is not None, 'AUTHORITY_DENIED', 403)
            cabinet = ProfileCabinetState.objects.filter(profile_id=row.profile_id,
                installation=installation, actor_id=session.owner_id).first()
            require(cabinet is not None, 'AUTHORITY_DENIED', 403)
            ga._bound(self.authority, session_ref, cabinet.profile_ref, sensitive=sensitive)
            store = self._store(session_ref, str(cabinet.profile_ref), str(row.portable_connection_id))
            store.redact_expired()
            row.refresh_from_db()
            return row, store

    @sensitive_variables()
    def _public(self, row, session_ref=None):
        redacted = row.content_redacted_at is not None
        code = ('UNKNOWN_RECONCILIATION_REQUIRED' if row.state == 'unknown' else 'PREVIEW_STALE') if redacted else row.evidence.get('code', 'OK')
        outcomes = tuple(CategoryOutcome(**value) for value in row.evidence.get('outcomes', []))
        revision = row.evidence.get('publication_revision') if row.portable_stage == 'published' else None
        stage = ('unknown' if row.state == 'unknown' else 'stale') if redacted else row.portable_stage or 'authorized'
        event = StageEvent(str(row.pk), stage, code, outcomes, revision)
        value = event.public()
        memory = self._memory.get(str(row.pk))
        actions = ['status']
        if row.state == 'queued' and not row.cancel_requested:
            actions.append('cancel')
            if memory and session_ref is not None and memory['session_digest'] == digest(session_ref.encode()) and self.authority.now() < memory['expires_at']:
                if not memory.get('busy') and memory.get('phase') != 'source_sealed':
                    actions.extend(['preview', 'confirm'])
            else:
                value.update(state='stale', stage='stale', code='PREVIEW_STALE', terminal=True)
        elif row.state in ('running', 'unknown'):
            actions.append('cancel')
        if row.portable_stage == 'published' and not redacted:
            actions.append('current')
        if (memory and memory.get('detail_dependency')) or row.evidence.get('phase') == 'inference_detail_confirmation_unavailable':
            value['dependency'] = 'exact_hardcover_detail_confirmation_unavailable'
        if not redacted and 'source_use' in row.evidence:
            value['source_use'] = checked_source_use(row.evidence['source_use'], row.portable_bindings['categories'])
            if row.evidence.get('phase') == 'source_model_context_unavailable':
                value['dependency'] = 'source_model_context_unavailable'
        value.update(revision=row.revision, actions=actions, allowed_actions=list(actions),
                     cancel_requested=row.cancel_requested)
        return value

    def readiness(self, session_ref, *, profile_ref=None, connection_ref=None):
        self.authority.authorize(session_ref, touch=False)
        available, run_ready = False, False
        if profile_ref is not None and connection_ref is not None:
            store = self._store(session_ref, profile_ref, connection_ref)
            with store.scope():
                available = bool(self._available())
                if available and self._isolated is not _ISOLATED:
                    current = store.current()
                    available = bool(current.source_views)
                    run_ready = (available and self._inference_available()
                                 and current.view.context['isolated_fixture'] is False)
        return {'available': available, 'run_ready': run_ready, 'code': 'OK' if available else 'CAPABILITY_UNAVAILABLE',
                'actions': ['start'] if available else [],
                'reason': 'ready' if run_ready else 'source_only' if available else 'enhance_unavailable'}

    @sensitive_variables()
    def start(self, session_ref, *, profile_ref, connection_ref, client_key, refresh_nonce):
        client, refresh = ga._uuid(client_key), ga._uuid(refresh_nonce)
        store = self._store(session_ref, profile_ref, connection_ref)
        with store.scope(sensitive=True) as cabinet:
            prior = SynthesisRun.objects.filter(installation=cabinet.installation, actor=cabinet.actor,
                profile=cabinet.profile, contract_version='enhance/v1', client_key=client).first()
            if prior is not None:
                require(prior.refresh_nonce == refresh and str(prior.portable_connection_id) == store.connection_ref,
                        'IDEMPOTENCY_CONFLICT', 409)
                if self._available():
                    require(prior.portable_bindings == store.current().record(), 'IDEMPOTENCY_CONFLICT', 409)
                return self.status(session_ref, run_ref=str(prior.pk))
            require(self._available(), 'CAPABILITY_UNAVAILABLE', 503)
            current = store.current()
        for key, value in list(self._memory.items()):
            if self.authority.now() >= value['expires_at']:
                self._memory.pop(key, None)
        require(len(self._memory) < 128, 'CAPABILITY_UNAVAILABLE', 503)
        run_ref = str(uuid.uuid4())
        ports = _DurablePorts(self, store, run_ref)
        coordinator = self._pipeline if self._pipeline is not None else EnhanceCoordinator(
            self._normal_context(session_ref, profile_ref)[1] if self._normal else self.catalogue,
            ports, now=self.authority.now)
        if self._pipeline is not None:
            # Explicit memory-only fixture producer, never a normal registration.
            coordinator.ports = ports
        query_view = current.source_views[0] if current.source_views else current.view
        arguments = {'inference_view': current.view, 'source_views': current.source_views} if current.source_views else {}
        if self._isolated is not _ISOLATED:
            require(current.source_views, 'CAPABILITY_UNAVAILABLE', 503)
        planned = coordinator.plan(query_view, current.registry, run_ref=run_ref,
            client_key=str(client), refresh_nonce=str(refresh), **arguments)
        require(type(planned) is PlannedEnhance)
        row = store.admit(planned.intent,local_context=current.local_context)
        if str(row.pk) != run_ref:
            return self._public(row, session_ref)
        self._install_preview(store, row, 'catalogue', planned, planned.plan.body,
            min(planned.plan.expires_at, self.authority.now() + timedelta(minutes=5)), coordinator)
        return self.status(session_ref, run_ref=run_ref)

    @sensitive_variables()
    def _install_preview(self, store, row, phase, work, body, expires_at, coordinator):
        prior = self._memory.get(str(row.pk), {})
        source_preview, issuer, source_previews = None, None, []
        missing_artist = False
        if phase == 'catalogue' and self._isolated is not _ISOLATED:
            for key, plan in work.source_plans:
                source = next((source for source in store.provider.sources if source.connection_ref == key), None)
                require(source is not None, 'CAPABILITY_UNAVAILABLE', 503)
                try:
                    source_previews.append((source, source.preview(plan)))
                except EnhanceFault as error:
                    if error.code not in ('NO_GROUNDED_CANDIDATES', 'CAPABILITY_UNAVAILABLE', 'AUTHORITY_DENIED'):
                        raise
                    if error.code == 'NO_GROUNDED_CANDIDATES' and source.source_key() in (
                            'musicbrainz_recording', 'musicbrainz_artist'):
                        missing_artist = True
            require(source_previews, 'NO_GROUNDED_CANDIDATES' if missing_artist else 'CAPABILITY_UNAVAILABLE',
                    422 if missing_artist else 503)
            issuer, source_preview = source_previews[0]
            body = source_preview['body'].encode('utf-8')
        preview_ref = str(uuid.uuid4())
        run_ref = str(row.pk)
        expires = min(expires_at, row.portable_expires_at)
        require(self.authority.now() < expires, 'PREVIEW_STALE', 409)
        metadata = {'phase': phase, 'preview_ref': preview_ref, 'preview_digest': digest(body),
                    'session_digest': digest(store.session_ref.encode())}
        if phase == 'inference' and prior.get('detail_dependency'):
            metadata['phase'] = 'inference_detail_confirmation_unavailable'
        sealed = {'portable_expires_at': expires}
        outcomes = ()
        if phase == 'inference':
            submitted = read_json(read_json(body)['input'].encode())
            refs = {'signals': [{k: signal[k] for k in ('key', 'medium')} for signal in submitted['profile']['signals']],
                'candidates': [{'key': candidate['key'], 'evidence_refs': sorted(set(candidate['source_roots']) |
                                  {fact['id'] for fact in candidate['facts']})} for candidate in submitted['candidates']]}
            metadata['submitted_refs'] = refs
            seal = work.collected.seal
            outcomes = [vars(value) for value in self._settled_outcomes(prior, seal.outcomes)]
            sealed.update(body_digest=digest(body), collection_digest=seal.collection_digest,
                sealed_run_digest=digest(canonical_bytes({'intent': row.input_digest,
                    'collection': seal.collection_digest, 'body': digest(body), 'bindings': row.portable_bindings})))
        row = store.checkpoint(run_ref, stage='awaiting_preview', outcomes=outcomes, metadata=metadata, **sealed)
        self._memory[run_ref] = {'phase': phase, 'work': work, 'body': body, 'preview_ref': preview_ref,
            'expires_at': expires, 'session_digest': metadata['session_digest'], 'coordinator': coordinator,
            'source_preview': source_preview, 'issuer': issuer, 'source_previews': source_previews,
            'source_index': 0, 'details_prepared': False,
            'preparatory': prior.get('preparatory'), 'preparatory_outcomes': prior.get('preparatory_outcomes', {}),
            'detail_dependency': prior.get('detail_dependency'),
            'source_use': prior.get('source_use', []),
            'source_connection_ref': work.query_view.connection_ref if phase == 'catalogue' else work.collected.planned.query_view.connection_ref}

    def _next_source_preview(self, store, row, memory):
        index = memory['source_index'] + 1
        if index >= len(memory['source_previews']):
            return False
        issuer, source = memory['source_previews'][index]
        body = source['body'].encode()
        preview_ref = str(uuid.uuid4())
        store.checkpoint(str(row.pk), stage='awaiting_preview', metadata={'phase': 'catalogue',
            'preview_ref': preview_ref, 'preview_digest': digest(body), 'session_digest': memory['session_digest']})
        memory.update(source_index=index, issuer=issuer, source_preview=source,
                      body=body, preview_ref=preview_ref)
        return True

    def _prepare_details(self, store, row, memory):
        from .source_collection import SourceCollection
        planned = memory['work']; catalogue = memory['coordinator'].catalogue
        memory['details_prepared'] = True
        book = catalogue.backends.get('hardcover') if type(catalogue) is SourceCollection else None
        if book is None or book.issuer.connection_ref not in dict(planned.source_plans):
            return False
        # Only Hardcover search runs in this preparatory collection. Other
        # sources remain unsent until the derived IDs get their OWN confirmation.
        # Sharing the producer's bounded state resumes its original search permit;
        # it never retries that search or promotes a proposed ID into permission.
        stage = SourceCollection((('book', ('hardcover',)),), dict(catalogue.backends),
                                 clock=catalogue.clock, state=catalogue.state)
        began = catalogue.clock.monotonic()
        seal = stage.collect(planned.plan, planned.registry, memory['coordinator'].budget,
                             source_plans=dict(planned.source_plans))
        usage = read_json(seal.source_revisions)['_collection']
        budget = memory['coordinator'].budget
        seconds = max(1, math.ceil(catalogue.clock.monotonic()-began))
        require(usage['sends'] < budget.initial_sends and usage['response_bytes'] < budget.aggregate_bytes
                and seconds < budget.collection_seconds, 'LIMIT_EXCEEDED', 422)
        memory['coordinator'].budget = replace(budget, initial_sends=budget.initial_sends-usage['sends'],
            aggregate_bytes=budget.aggregate_bytes-usage['response_bytes'], collection_seconds=budget.collection_seconds-seconds)
        details = book.issuer.pending_details(planned.plan.plan_ref)
        if not details or not catalogue.state.pending:
            # Keep the authenticated preparatory seal, including actual empty or
            # failure provenance. Only issuer-retained derived requests prove a
            # successful search reached the detail handoff. A search denial
            # has no such proof, even when a network send was charged.
            memory['preparatory'] = (stage, seal)
            actual = next(outcome for outcome in seal.outcomes if outcome.category == 'book')
            memory['preparatory_outcomes'] = {'book': actual}

            configured = tuple((key, tuple(name for name in names if name != 'hardcover'))
                               for key, names in catalogue.configured.items())
            memory['coordinator'].catalogue = SourceCollection(configured, dict(catalogue.backends),
                clock=catalogue.clock, state=catalogue.state)
            memory['detail_dependency'] = ('exact_hardcover_detail_confirmation_unavailable'
                if details and actual.code == 'AUTHORITY_DENIED' else None)
            return False
        own = dict(planned.source_plans)[book.issuer.connection_ref]
        memory['source_previews'] = [(book.issuer, book.issuer.preview(own, request=request)) for request in details]
        memory['source_index'] = -1
        return self._next_source_preview(store, row, memory)

    def status(self, session_ref, *, run_ref):
        row, store = self._find(session_ref, run_ref)
        if row.attempts == 1 and row.state == 'running' and str(row.pk) not in self._memory:
            row = store.mark_unknown(run_ref)
        return self._public(row, session_ref)

    @sensitive_variables()
    def preview(self, session_ref, *, run_ref):
        row, store = self._find(session_ref, run_ref, sensitive=True)
        value = self._memory.get(str(row.pk))
        require(value is not None and value.get('phase') in ('catalogue', 'inference')
            and self.authority.now() < value['expires_at'], 'PREVIEW_STALE', 409)
        require(value['session_digest'] == digest(session_ref.encode()), 'AUTHORITY_DENIED', 403)
        with store.scope():
            store._match(row, store.current())
        # Exact destinations must come from a backend plan, not source prose.
        if value['phase'] == 'catalogue':
            if value.get('source_preview') is not None:
                source = value['source_preview']
                value['issuer']._check(value['issuer']._previews[source['preview_ref']], self.authority.now())
                destinations = (source['origin'],)
            else:
                destination_port = getattr(self.catalogue, 'destinations_for', None)
                require(callable(destination_port), 'CAPABILITY_UNAVAILABLE', 503)
                destinations = tuple(destination_port(value['work'].plan))
        else:
            destinations = (value['work'].inference_view.context['destination_origin'],)
        require(destinations and all(type(item) is str and 0 < len(item) <= 300 for item in destinations))
        public = self._public(row, session_ref)
        if value['phase'] == 'catalogue':
            planned = value['work']
            prepared = disclosure.prepare(read_json(planned.snapshot_bytes), planned.query_view.grants,
                planned.query_view.context, [], {}, {}, now=self.authority.now())
            omitted = prepared.omitted_classes
        else:
            omitted = value['work'].omitted_groups
        public['disclosure'] = {'kind': value['phase'], 'preview_ref': value['preview_ref'],
            'body': value['body'].decode('utf-8'), 'body_digest': digest(value['body']),
            'destination': destinations[0], 'destinations': list(destinations),
            'expires_at': stamp(value['expires_at']), 'omitted_groups': list(omitted)}
        if value.get('source_preview') is not None:
            public['disclosure'].update(source_number=value['source_index'] + 1,
                source_count=len(value['source_previews']))
            source = value['source_preview']
            public['disclosure'].update({key: source[key] for key in
                ('plan_digest', 'operation_digest', 'method', 'origin', 'path', 'query')})
            # Public headers are adapter-specific; missing required wire fields
            # must still fail instead of becoming an incomplete disclosure.
            if 'headers' in source:
                public['disclosure']['headers'] = source['headers']
        return public

    @sensitive_variables()
    def confirm(self, session_ref, *, run_ref, preview_ref, body_digest, confirmed):
        require(confirmed is True)
        row, store = self._find(session_ref, run_ref, sensitive=True)
        if row.attempts or row.state != 'queued' or row.portable_stage == 'sealed':
            return self.status(session_ref, run_ref=run_ref)  # Replayed confirmation never dispatches.
        self.preview(session_ref, run_ref=run_ref)
        memory = self._memory[str(row.pk)]
        with self.authority._lock:
            if memory.get('busy'):
                return self.status(session_ref, run_ref=run_ref)
            require(str(ga._uuid(preview_ref)) == memory['preview_ref'] and body_digest == digest(memory['body']),
                    'PREVIEW_STALE', 409)
            memory['busy'] = True
        coordinator, work = memory['coordinator'], memory['work']
        try:
            if memory['phase'] == 'catalogue':
                store.checkpoint(run_ref, stage='collecting')
                source = memory.get('source_preview')
                if source is not None:
                    memory['issuer'].confirm(source['preview_ref'], plan_digest=source['plan_digest'],
                        operation_digest=source['operation_digest'], confirmed=True)
                if source is not None and self._next_source_preview(store, row, memory):
                    return self.status(session_ref, run_ref=run_ref)
                if source is not None and not memory['details_prepared']:
                    if self._prepare_details(store, row, memory):
                        return self.status(session_ref, run_ref=run_ref)
                collected = coordinator.collect(work, approved_query_digest=work.plan.body_digest if source is not None else body_digest)
                if type(collected) is EnhanceOutcome:
                    # A policy-denied local pool can be empty without being an
                    # empty search. Keep the producer's all-category explanation.
                    store.finish(run_ref, stage=collected.event.stage, code=collected.event.code,
                                 outcomes=[vars(value) for value in collected.event.outcomes])
                    self._memory.pop(str(row.pk), None)
                    return self.status(session_ref, run_ref=run_ref)
                require(type(collected) is CollectedEnhance)
                collected.seal.verify(work.plan, work.registry, now=self.authority.now())
                summary = self._source_use(memory, collected)
                memory['source_use'] = summary
                eligible = sum(value['model_eligible_count'] for value in summary)
                expiry = min(memory['expires_at'], collected.seal.expires_at)
                store.checkpoint(run_ref, stage='sealed', code='OK' if eligible else 'AUTHORITY_DENIED',
                    outcomes=[vars(value) for value in collected.seal.outcomes],
                    metadata={'source_use': summary, **({} if eligible else
                        {'phase': 'source_model_context_unavailable'})},
                    collection_digest=collected.seal.collection_digest, portable_expires_at=expiry)
                if not eligible:
                    memory.update(phase='source_sealed', work=collected, body=b'', expires_at=expiry)
                    return self.status(session_ref, run_ref=run_ref)
                if not self._inference_available():
                    store.checkpoint(run_ref, stage='sealed', code='CAPABILITY_UNAVAILABLE',
                        outcomes=[vars(value) for value in collected.seal.outcomes],
                        collection_digest=collected.seal.collection_digest)
                    memory.update(phase='source_sealed', work=collected, body=b'')
                    return self.status(session_ref, run_ref=run_ref)
                inference_view = ga.current_view(self.authority, session_ref,
                    profile_ref=store.profile_ref, connection_ref=store.connection_ref, purpose='inference')
                inference_preview = coordinator.preview(collected, inference_view)
                require(type(inference_preview) is InferencePreview)
                self._install_preview(store, store.status(run_ref), 'inference', inference_preview,
                    inference_preview.body, inference_preview.expires_at, coordinator)
                return self.status(session_ref, run_ref=run_ref)
            result = coordinator.infer(work, approved_body_digest=body_digest)
            require(type(result) is EnhanceOutcome)
            if result.event.stage != 'validated':
                store.finish(run_ref, stage=result.event.stage, code=result.event.code)
            else:
                completed = store.status(run_ref)
                require(completed.state == 'completed', 'RUN_STALE', 409)
                submitted = read_json(read_json(memory['body'])['input'].encode())
                from .enhance_selection import GroundedSelector
                arguments = {'snapshot': profile_snapshot.capture(work.inference_view),
                    'local_context':store.current().local_context} if type(self.selector) is GroundedSelector else {}
                manifest = self.selector.build(completed.output, submitted,
                    self._settled_outcomes(memory, work.collected.seal.outcomes), **arguments)
                expected_revision = completed.evidence['expected_publication_revision']
                with self._collection_lock(memory):
                    coordinator._host_check(work.collected.planned.intent, work.collected.planned.plan,
                                            work.collected.seal, memory['body'])
                    store.publish(run_ref, manifest=manifest, expected_publication_revision=expected_revision,
                                  allowed_candidates=submitted['candidates'])
            self._memory.pop(str(row.pk), None)
            return self.status(session_ref, run_ref=run_ref)
        except (AuthorityFault, EnhanceFault, DisclosureError) as error:
            code = getattr(error, 'code', 'CONTRACT_INVALID')
            stage = 'stale' if code.endswith('STALE') else 'blocked'
            try:
                store.finish(run_ref, stage=stage, code=code)
            except AuthorityFault:
                pass  # A lost owner cannot authorize new writes; the reservation remains durable.
            self._memory.pop(str(row.pk), None)
            raise AuthorityFault(code, getattr(error, 'status', 409)) from None
        except Exception:
            row = store.status(run_ref)
            store.mark_unknown(run_ref) if row.attempts else store.finish(run_ref, stage='failed', code='CONTRACT_INVALID')
            self._memory.pop(str(row.pk), None)
            raise AuthorityFault('UNKNOWN_RECONCILIATION_REQUIRED' if row.attempts else 'CONTRACT_INVALID', 409) from None
        finally:
            memory['busy'] = False

    def cancel(self, session_ref, *, run_ref, expected_revision):
        _, store = self._find(session_ref, run_ref, sensitive=True)
        row = store.cancel(run_ref, expected_revision=expected_revision)
        self._memory.pop(str(row.pk), None)
        return self._public(row, session_ref)

    def current(self, session_ref, *, profile_ref):
        with ga.recovery_guard(self.authority, session_ref, profile_ref=profile_ref):
            cabinet, _ = ga._bound(self.authority, session_ref, profile_ref)
            if cabinet.active_manifest_ref is None:
                return {'schema': 'cabinet-current/v1', 'published': False,
                        'publication_revision': None, 'manifest': None, 'code': 'OK'}
            from .models import PortableCabinetManifest
            publication = PortableCabinetManifest.objects.select_related('run').filter(
                pk=cabinet.active_manifest_ref, cabinet=cabinet).first()
            publication_run = publication.run if publication is not None else None
            require(publication_run is not None, 'PREVIEW_STALE', 409)
            store = self._store(session_ref, profile_ref, str(publication_run.portable_connection_id))
            publication, manifest = store.read_current()
            from .portable_reactions import target_ref
            reactions = {card['key']: target_ref(str(profile_ref), card['key'])
                         for group in manifest['categories'] for card in group['cards']}
            return {'schema': 'cabinet-current/v1', 'published': True, 'reaction_targets': reactions,
                    'publication_revision': publication.publication_revision, 'manifest': manifest, 'code': 'OK'}

    def current_for_interaction(self, session_ref, *, profile_ref):
        with ga.recovery_guard(self.authority, session_ref, profile_ref=profile_ref),self.authority._lock:
            cabinet, _ = ga._bound(self.authority, session_ref, profile_ref)
            if cabinet.active_manifest_ref is None:
                return {'schema': 'cabinet-current/v1', 'published': False,
                        'publication_ref': None, 'publication_revision': None, 'manifest': None,
                        'ranking_state': 'none', 'code': 'OK'}
            from .models import PortableCabinetManifest
            publication = PortableCabinetManifest.objects.select_related('run').filter(
                pk=cabinet.active_manifest_ref, cabinet=cabinet).first()
            require(publication is not None, 'PREVIEW_STALE', 409)
            store = self._store(session_ref, profile_ref, str(publication.run.portable_connection_id))
            publication, manifest, classification = store.read_for_interaction()
            from .local_event_ranking import display,LOCAL_CATEGORIES
            current=store.current()
            overlays={}
            if current.local_context is not None:
                # Names pass the same sole purpose/lineage filter as inference.
                # No old prompt, raw denied title or cached explanation is read.
                prepared=disclosure.prepare(profile_snapshot.capture(current.view),current.view.grants,
                    current.view.context,[],{},{},now=self.authority.now())
                require(prepared.ok,prepared.code,403)
                profile=read_json(prepared.profile_bytes)['profile']
                names=tuple(signal['title'] for signal in profile['signals']
                    if signal['medium']=='visual_artist' and signal['basis']=='typed_feedback'
                    and signal['value']['signal']=='saved' and signal['value']['value']=={'value':True})
                overlays={card['key']:display(current.local_context,card,publication_ref=str(publication.pk),
                    publication_revision=publication.publication_revision,now=self.authority.now(),
                    permitted_visual_names=names) for group in manifest['categories'] for card in group['cards']
                    if card['category'] in LOCAL_CATEGORIES}
            from .portable_reactions import target_ref
            return {'schema': 'cabinet-current/v1', 'published': True,
                'publication_ref': str(publication.pk), 'publication_revision': publication.publication_revision,
                'reaction_targets': {card['key']: target_ref(str(profile_ref), card['key'])
                    for group in manifest['categories'] for card in group['cards']},
                'manifest': manifest,'local_event_display':overlays,
                'ranking_state': 'feedback_pending' if classification == 'feedback_only' else 'fresh',
                'code': 'OK'}

    def _saved_access(self, session_ref, profile_ref, *, action='save'):
        # Evidence and matching numeric IDs cannot create or replace the live PA1
        # capability. In particular, a refused portable scope never falls back.
        try:
            from .portable_saved_authority import PortableSavedAccess, active_portable_access
        except ModuleNotFoundError as error:
            if error.name != 'shelf.portable_saved_authority':
                raise
            raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503) from None
        access = active_portable_access()
        require(type(access) is PortableSavedAccess and access.authority is self.authority
                and access.session_ref == session_ref and access.profile_ref == profile_ref,
                'AUTHORITY_DENIED', 403)
        state, session = ga._bound(self.authority, session_ref, profile_ref,
                                   sensitive=action == 'save')
        require(state.data_schema_version == 2, 'CAPABILITY_UNAVAILABLE', 503)
        identity = access.require(actor_id=state.actor_id, profile_id=state.profile_id, action=action)
        require(identity.actor_id == state.actor_id and identity.profile_id == state.profile_id,
                'AUTHORITY_DENIED', 403)
        return state, session, access

    def _saved_retention(self, card):
        # Current display/retain bits carry a finite expiry. They do not prove
        # permission to copy attribution for the lifetime of a durable saved row.
        # Until that distinct host-owned right is available, Save fails closed.
        raise SavedCandidateUnavailable()

    def _saved_evidence(self, state, session, publication, card):
        from .saved import canonical_url
        from .category_registry import canonical_category
        require(type(card) is dict and set(card) == disclosure.CANDIDATE_FIELDS)
        provider, medium = card.get('provider'), card.get('medium')
        require(type(provider) is str and type(medium) is str
                and (provider, medium) in {('tmdb', 'movie'), ('tmdb', 'tv'), ('hardcover', 'book')},
                'CAPABILITY_UNAVAILABLE', 503)
        try:
            category = canonical_category(card.get('category'))
        except EnhanceFault:
            raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503) from None
        require(category == medium, 'CAPABILITY_UNAVAILABLE', 503)
        source_id = card.get('source_id')
        require(type(source_id) is str and source_id.isascii() and source_id.isdecimal()
                and 0 < len(source_id) <= 10 and str(int(source_id)) == source_id)
        source_id = int(source_id)
        require(0 < source_id <= 2147483647)
        url = card.get('source_url')
        require(type(url) is str and url and canonical_url(provider, medium, source_id, url) == url)
        require(card.get('identity_status') == 'confirmed'
                or (self._isolated is _ISOLATED and ga._fixture_database()
                    and card.get('identity_status') == 'fixture_identity_only'), 'AUTHORITY_DENIED', 403)
        require(card['evidence_type'] in ('live_lookup', 'cached_lookup')
                or (self._isolated is _ISOLATED and ga._fixture_database()
                    and card['evidence_type'] == 'fixture_not_live'), 'AUTHORITY_DENIED', 403)
        times = []
        for name in ('observed_at', 'valid_until'):
            raw = card.get(name)
            require(type(raw) is str and 0 < len(raw) <= 40)
            try:
                value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            except ValueError:
                raise AuthorityFault('CONTRACT_INVALID', 422) from None
            require(value.utcoffset() is not None)
            times.append(value.astimezone(timezone.utc))
        observed, end = times
        require(observed <= self.authority.now() < end, 'PREVIEW_STALE', 409)
        require(type(card.get('rights_ref')) is str and 0 < len(card['rights_ref']) <= 300)
        roots = card.get('source_roots')
        require(type(roots) is list and 0 < len(roots) <= 100
                and all(type(value) is str and 0 < len(value) <= 300 for value in roots)
                and len(set(roots)) == len(roots))
        provenance = {name: card[name] for name in ('provider', 'medium', 'source_id', 'source_url',
            'rights_ref', 'source_roots', 'observed_at', 'valid_until')}
        return SavedCandidateEvidence(str(state.installation_id), session.owner_revision,
            str(state.profile_ref), state.actor_id, state.profile_id, str(publication.pk),
            publication.publication_revision, str(publication.run_id), card['key'],
            digest(canonical_bytes(card)), digest(canonical_bytes(provenance)), provider,
            medium, source_id, card.get('title'), url, observed, end)

    @sensitive_variables()
    def resolve_saved_candidate(self, session_ref, *, profile_ref,
                                expected_publication_revision, candidate_key):
        require(type(expected_publication_revision) is int
                and 0 < expected_publication_revision < 2**53)
        require(type(candidate_key) is str and 0 < len(candidate_key) <= 300)
        profile_ref = str(ga._uuid(profile_ref))
        # read_current may commit privacy redaction. An enclosing Save transaction
        # could resurrect removed payload on refusal, so refuse that call order.
        require(not connection.in_atomic_block, 'CAPABILITY_UNAVAILABLE', 503)
        with ga.recovery_guard(self.authority, session_ref, profile_ref=profile_ref), self.authority._lock:
            state, session, access = self._saved_access(session_ref, profile_ref)
            current = self.current(session_ref, profile_ref=profile_ref)
            require(current['published'] and current['publication_revision'] == expected_publication_revision,
                    'PREVIEW_STALE', 409)
            from .models import PortableCabinetManifest
            publication = PortableCabinetManifest.objects.filter(pk=state.active_manifest_ref,
                cabinet=state, publication_revision=expected_publication_revision,
                content_redacted_at__isnull=True).first()
            require(publication is not None and self.authority.now() < publication.expires_at,
                    'PREVIEW_STALE', 409)
            cards = [card for group in current['manifest']['categories'] for card in group['cards']
                     if card['key'] == candidate_key]
            require(len(cards) == 1, 'PREVIEW_STALE', 409)
            evidence = self._saved_evidence(state, session, publication, cards[0])
            self._saved_retention(cards[0])
            require(self._saved_access(session_ref, profile_ref)[2] is access, 'AUTHORITY_DENIED', 403)
            return evidence

    @contextmanager
    def saved_candidate_window(self, session_ref, *, evidence):
        require(type(evidence) is SavedCandidateEvidence, 'AUTHORITY_DENIED', 403)
        require(not connection.in_atomic_block, 'CAPABILITY_UNAVAILABLE', 503)
        with ga.recovery_guard(self.authority, session_ref, profile_ref=evidence.profile_ref), self.authority._lock:
            _, _, access = self._saved_access(session_ref, evidence.profile_ref, action='save')
            fresh = self.resolve_saved_candidate(session_ref, profile_ref=evidence.profile_ref,
                expected_publication_revision=evidence.publication_revision, candidate_key=evidence.candidate_key)
            require(fresh == evidence, 'PREVIEW_STALE', 409)
            # N1 now owns the single SavedItem transaction and expected floor
            # advancement (or duplicate no-write). Save intentionally invalidates
            # the old publication; re-reading it here would reject our own write.
            yield fresh
            require(self._saved_access(session_ref, evidence.profile_ref, action='save')[2] is access,
                    'AUTHORITY_DENIED', 403)

    def reconcile(self, session_ref, *, run_ref, receipt_ref):
        _, store = self._find(session_ref, run_ref, sensitive=True)
        store.reconcile(run_ref, receipt_ref=receipt_ref, reconciler=self.reconciler)
        return self.status(session_ref, run_ref=run_ref)


def get_service(authority):
    require(type(authority) is ga.PortableAuthority, 'AUTHORITY_DENIED', 403)
    value = getattr(authority, '_enhance_service', None)
    return value if type(value) is EnhanceService else EnhanceService(authority)
