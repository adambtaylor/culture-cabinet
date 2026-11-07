"""Owner-scoped durable Enhance commands; no normal source/send capability yet.

The frozen HTTP interface accepts opaque references and explicit confirmations,
never authority/body/source dictionaries. The source-independent store is real;
the unleased current-evidence/writer and personal transport producers remain
explicit gates. No legacy key, private profile, transport or selector fallback.
"""
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from contextlib import contextmanager
import uuid

from django.views.decorators.debug import sensitive_variables
from django.db import connection

from . import grant_authority as ga, disclosure
from .disclosure import canonical_bytes, read_json, DisclosureError
from .enhance_contract import CategoryOutcome, EnhanceFault, StageEvent, digest
from .enhance_coordinator import (EnhanceCoordinator, EnhanceOutcome, PlannedEnhance,
    CollectedEnhance, InferencePreview)
from .models import ProfileCabinetState, SynthesisRun
from .portable_authority import AuthorityFault
from .portable_cabinet_store import CabinetStore, StateProvider, require, stamp

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
        with self.store.scope() as cabinet:
            current = self.store.current()
            row = self.store._run(cabinet, self.run_ref)
            self.store._match(row, current)
            require(intent.bindings == current.bindings, 'PREVIEW_STALE', 409)
            from .current_profile import CurrentProfile
            if type(self.service.state_provider) is CurrentProfile:
                query = next((view for view in current.source_views
                    if view.connection_ref == self.service._memory.get(self.run_ref, {}).get('source_connection_ref')), None)
                require(query is not None, 'PREVIEW_STALE', 409)
                ga.assert_current(query)
                require(plan.bindings.connection_digest == digest(canonical_bytes(query.connection_binding))
                    and plan.bindings.grants_digest == digest(canonical_bytes(query.grants)), 'PREVIEW_STALE', 409)
            if seal is not None:
                seal.verify(plan, current.registry, now=self.service.authority.now())
                if type(self.service.state_provider) is CurrentProfile:
                    require(self.service.catalogue.revalidate_collection(seal, plan, current.registry), 'PREVIEW_STALE', 409)
            if body is not None and row.body_digest:
                require(digest(body) == row.body_digest, 'PREVIEW_STALE', 409)

    def reserve_attempt(self, intent, seal, body):
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
        # The actual personal transport must independently verify this original
        # reservation/current proof after network setup. None is provided by the
        # ordinary service; the registered neutral adapter is never widened here.
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
        authority._enhance_service = self
        authority.boundary._closing.append(self.close)

    @classmethod
    def isolated_test(cls, authority, validation=None, *, pipeline=None, **kwargs):
        require(ga._fixture_database() and isinstance(kwargs.get('state_provider'), StateProvider),
                'CAPABILITY_UNAVAILABLE', 503)
        value = cls(authority, validation, **kwargs)
        value._isolated, value._pipeline = _ISOLATED, pipeline
        return value

    def close(self):
        self._memory.clear()  # Raw bodies/reconstruction refs never survive process loss.

    def _store(self, session_ref, profile_ref, connection_ref):
        kwargs = dict(profile_ref=profile_ref, connection_ref=connection_ref, state_provider=self.state_provider)
        factory = CabinetStore.isolated_test if self._isolated is _ISOLATED else CabinetStore
        return factory(self.authority, session_ref, **kwargs)

    def _available(self):
        from .current_profile import CurrentProfile
        configured = self.catalogue is not None and self.registry is not None
        isolated = (self._isolated is _ISOLATED and ga._fixture_database()
            and isinstance(self.state_provider, StateProvider)
            and callable(getattr(self.personal_transport, 'dispatch', None))
            and callable(getattr(self.selector, 'build', None)))
        concrete = type(self.state_provider) is CurrentProfile and self.state_provider.authority is self.authority
        return configured and (isolated or concrete)

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
        run_ref = str(uuid.uuid4())
        ports = _DurablePorts(self, store, run_ref)
        coordinator = self._pipeline if self._pipeline is not None else EnhanceCoordinator(
            self.catalogue, ports, now=self.authority.now)
        if self._pipeline is not None:
            # Explicit memory-only fixture producer, never a normal registration.
            coordinator.ports = ports
        query_view = current.source_views[0] if current.source_views else current.view
        arguments = {'inference_view': current.view} if current.source_views else {}
        if self._isolated is not _ISOLATED:
            require(current.source_views, 'CAPABILITY_UNAVAILABLE', 503)
        planned = coordinator.plan(query_view, current.registry, run_ref=run_ref,
            client_key=str(client), refresh_nonce=str(refresh), **arguments)
        require(type(planned) is PlannedEnhance)
        row = store.admit(planned.intent)
        if str(row.pk) != run_ref:
            return self._public(row, session_ref)
        self._install_preview(store, row, 'catalogue', planned, planned.plan.body,
            min(planned.plan.expires_at, self.authority.now() + timedelta(minutes=5)), coordinator)
        return self.status(session_ref, run_ref=run_ref)

    @sensitive_variables()
    def _install_preview(self, store, row, phase, work, body, expires_at, coordinator):
        source_preview, issuer = None, None
        if phase == 'catalogue' and self._isolated is not _ISOLATED:
            issuer = next((source for source in self.state_provider.sources
                if source.connection_ref == work.query_view.connection_ref), None)
            require(issuer is not None, 'CAPABILITY_UNAVAILABLE', 503)
            source_preview = issuer.preview(work.plan)
            body = source_preview['body'].encode('utf-8')
        preview_ref = str(uuid.uuid4())
        run_ref = str(row.pk)
        expires = min(expires_at, row.portable_expires_at)
        require(self.authority.now() < expires, 'PREVIEW_STALE', 409)
        metadata = {'phase': phase, 'preview_ref': preview_ref, 'preview_digest': digest(body),
                    'session_digest': digest(store.session_ref.encode())}
        sealed = {'portable_expires_at': expires}
        outcomes = ()
        if phase == 'inference':
            submitted = read_json(read_json(body)['input'].encode())
            refs = {'signals': [{k: signal[k] for k in ('key', 'medium')} for signal in submitted['profile']['signals']],
                'candidates': [{'key': candidate['key'], 'evidence_refs': sorted(set(candidate['source_roots']) |
                                  {fact['id'] for fact in candidate['facts']})} for candidate in submitted['candidates']]}
            metadata['submitted_refs'] = refs
            seal = work.collected.seal
            outcomes = [vars(value) for value in seal.outcomes]
            sealed.update(body_digest=digest(body), collection_digest=seal.collection_digest,
                sealed_run_digest=digest(canonical_bytes({'intent': row.input_digest,
                    'collection': seal.collection_digest, 'body': digest(body), 'bindings': row.portable_bindings})))
        row = store.checkpoint(run_ref, stage='awaiting_preview', outcomes=outcomes, metadata=metadata, **sealed)
        self._memory[run_ref] = {'phase': phase, 'work': work, 'body': body, 'preview_ref': preview_ref,
            'expires_at': expires, 'session_digest': metadata['session_digest'], 'coordinator': coordinator,
            'source_preview': source_preview, 'issuer': issuer,
            'source_connection_ref': work.query_view.connection_ref if phase == 'catalogue' else work.collected.planned.query_view.connection_ref}

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
            source = value['source_preview']
            public['disclosure'].update({key: source[key] for key in
                ('plan_digest', 'operation_digest', 'method', 'origin', 'path', 'query', 'headers')})
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
                collected = coordinator.collect(work, approved_query_digest=work.plan.body_digest if source is not None else body_digest)
                if type(collected) is EnhanceOutcome:
                    store.finish(run_ref, stage=collected.event.stage, code=collected.event.code)
                    self._memory.pop(str(row.pk), None)
                    return self.status(session_ref, run_ref=run_ref)
                require(type(collected) is CollectedEnhance)
                collected.seal.verify(work.plan, work.registry, now=self.authority.now())
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
                manifest = self.selector.build(completed.output, submitted, work.collected.seal.outcomes)
                expected_revision = completed.evidence['expected_publication_revision']
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

    def _saved_access(self, session_ref, profile_ref, *, action='select'):
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
                                   sensitive=action == 'mutate')
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
            _, _, access = self._saved_access(session_ref, evidence.profile_ref, action='mutate')
            fresh = self.resolve_saved_candidate(session_ref, profile_ref=evidence.profile_ref,
                expected_publication_revision=evidence.publication_revision, candidate_key=evidence.candidate_key)
            require(fresh == evidence, 'PREVIEW_STALE', 409)
            # N1 now owns the single SavedItem transaction and expected floor
            # advancement (or duplicate no-write). Save intentionally invalidates
            # the old publication; re-reading it here would reject our own write.
            yield fresh
            require(self._saved_access(session_ref, evidence.profile_ref, action='mutate')[2] is access,
                    'AUTHORITY_DENIED', 403)

    def reconcile(self, session_ref, *, run_ref, receipt_ref):
        _, store = self._find(session_ref, run_ref, sensitive=True)
        store.reconcile(run_ref, receipt_ref=receipt_ref, reconciler=self.reconciler)
        return self.status(session_ref, run_ref=run_ref)


def get_service(authority):
    require(type(authority) is ga.PortableAuthority, 'AUTHORITY_DENIED', 403)
    value = getattr(authority, '_enhance_service', None)
    return value if type(value) is EnhanceService else EnhanceService(authority)
