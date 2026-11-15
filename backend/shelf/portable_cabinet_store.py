"""Durable Enhance metadata on SynthesisRun, with one atomic cabinet pointer.

Normal readiness stays closed until the separately owned evidence/writer producer
is complete. Explicit memory fixtures exercise this core using real owner/session,
profile, consent and recovery authority; a browser dictionary is never authority.
"""
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import uuid

from django.db import IntegrityError
from django.views.decorators.debug import sensitive_variables

from . import grant_authority as ga
from .disclosure import canonical_bytes, read_json
from .enhance_contract import (CATEGORY_STATES, SAFE_CODES, EnhanceBindings,
    EnhanceIntent, RegistryView, digest)
from .inference_contract import validate_result, ContractError
from .models import PortableCabinetManifest, ProfileCabinetState, SynthesisRun
from .portable_authority import AuthorityFault

VERSION = 'enhance/v1'
_FIXTURE = object()


def require(ok, code='CONTRACT_INVALID', status=422):
    if not ok:
        raise AuthorityFault(code, status)


def stamp(value):
    return value.isoformat().replace('+00:00', 'Z')


def checked_source_use(value, categories, *, outcomes=None):
    """Closed status counts; source permission still comes from the producer."""
    require(type(value) is list and len(value) == len(categories) <= 100)
    counts = {}
    for category, item in zip(categories, value):
        require(type(item) is dict
            and set(item) == {'category', 'collected_count', 'model_eligible_count'}
            and item['category'] == category)
        collected, eligible = item['collected_count'], item['model_eligible_count']
        require(type(collected) is int and type(eligible) is int
                and 0 <= eligible <= collected <= 100)
        counts[category] = collected
    require(len(counts) == len(categories) and sum(counts.values()) <= 100)
    if outcomes is not None:
        require(len(outcomes) == len(categories))
        for category, outcome in zip(categories, outcomes):
            require(type(outcome) is dict and outcome.get('category') == category
                and type(outcome.get('candidate_count')) is int
                and outcome['candidate_count'] == counts[category])
    return read_json(canonical_bytes(value))


@dataclass(frozen=True, repr=False)
class CurrentState:
    view: ga.AuthorityView
    bindings: EnhanceBindings
    registry: RegistryView
    source_revision: str
    expires_at: datetime
    source_views: tuple = ()
    local_context: object = None

    def record(self):
        require(type(self.view) is ga.AuthorityView and type(self.bindings) is EnhanceBindings
            and type(self.registry) is RegistryView)
        ga.assert_current(self.view)
        require((self.bindings.installation_id, self.bindings.actor_ref, self.bindings.profile_ref,
                 self.bindings.policy_epoch, self.bindings.restriction_watermark) ==
                (self.view.installation_id, self.view.actor_ref, self.view.profile_ref,
                 self.view.policy_epoch, self.view.restriction_watermark), 'PREVIEW_STALE', 409)
        require(self.bindings.registry_revision == self.registry.revision)
        require(self.bindings.config_revision == self.view.context['config_revision']
            and self.bindings.grants_digest == digest(canonical_bytes(self.view.grants))
            and self.bindings.connection_digest == digest(canonical_bytes(self.view.connection_binding)),
            'PREVIEW_STALE', 409)
        require(type(self.source_revision) is str and 0 < len(self.source_revision) <= 300)
        require(type(self.expires_at) is datetime and self.expires_at.utcoffset() == timedelta(0)
            and self.expires_at <= self.view.expires_at)
        value={'bindings': vars(self.bindings).copy(), 'categories': list(self.registry.categories),
               'source_revision': self.source_revision}
        if self.local_context is not None:
            from .local_event_preferences import LocalEventContext
            require(type(self.local_context) is LocalEventContext and
                self.local_context.profile_ref==self.bindings.profile_ref)
            value['local_selection_digest']=self.local_context.digest
        return value


class StateProvider(ABC):
    """Trusted current evidence/source/floor port; no production implementation here.

    It must distinguish actual taste/legacy inference from portable bookkeeping,
    cover every writer/restore floor, and verify current source rights/expiry.
    """
    @abstractmethod
    def current(self, authority, session_ref, *, profile_ref, connection_ref): ...

    @abstractmethod
    def filter_manifest(self, current, manifest): ...


class CabinetStore:
    def __init__(self, authority, session_ref, *, profile_ref, connection_ref, state_provider=None, _token=None):
        require(type(authority) is ga.PortableAuthority, 'AUTHORITY_DENIED', 403)
        self.authority, self.session_ref = authority, session_ref
        self.profile_ref, self.connection_ref = str(ga._uuid(profile_ref)), str(ga._uuid(connection_ref))
        self.provider, self._token = state_provider, _token

    @classmethod
    def isolated_test(cls, authority, session_ref, **kwargs):
        require(ga._fixture_database(), 'CAPABILITY_UNAVAILABLE', 503)
        require(isinstance(kwargs.get('state_provider'), StateProvider), 'CAPABILITY_UNAVAILABLE', 503)
        return cls(authority, session_ref, _token=_FIXTURE, **kwargs)

    @contextmanager
    def scope(self, *, sensitive=False):
        with ga.recovery_guard(self.authority, self.session_ref, profile_ref=self.profile_ref) as checkpoint, \
             self.authority._lock, ga._checked_transaction(checkpoint):
            cabinet, _ = ga._bound(self.authority, self.session_ref, self.profile_ref, sensitive=sensitive)
            yield cabinet
            checkpoint.check()

    def current(self):
        # No flag uploaded by HTTP or a provider can close the missing production
        # writer/snapshot integration. Only a separately composed memory fixture
        # may supply this port until that actual source change is admitted.
        from .current_profile import CurrentProfile
        require((self._token is _FIXTURE and ga._fixture_database() and isinstance(self.provider, StateProvider))
            or (type(self.provider) is CurrentProfile and self.provider.authority is self.authority),
            'CAPABILITY_UNAVAILABLE', 503)
        current = self.provider.current(self.authority, self.session_ref,
            profile_ref=self.profile_ref, connection_ref=self.connection_ref)
        require(type(current) is CurrentState)
        current.record()
        require(current.view.profile_ref == self.profile_ref
            and current.view.connection_ref == self.connection_ref, 'AUTHORITY_DENIED', 403)
        require(self.authority.now() < current.expires_at, 'PREVIEW_STALE', 409)
        return current

    @contextmanager
    def mutation(self):
        with self.scope(sensitive=True) as cabinet:
            current = self.current()
            before = current.record()
            yield cabinet, current
            require(self.current().record() == before, 'PREVIEW_STALE', 409)

    def _run(self, cabinet, run_ref):
        row = SynthesisRun.objects.filter(pk=ga._uuid(run_ref), contract_version=VERSION,
            installation_id=cabinet.installation_id, actor_id=cabinet.actor_id,
            profile_id=cabinet.profile_id, portable_connection_id=ga._uuid(self.connection_ref)).first()
        require(row is not None, 'AUTHORITY_DENIED', 403)
        return row

    def _match(self, row, current):
        require(row.content_redacted_at is None, 'PREVIEW_STALE', 409)
        require(row.portable_bindings == current.record(), 'PREVIEW_STALE', 409)
        require(row.portable_expires_at is not None and self.authority.now() < row.portable_expires_at,
                'PREVIEW_STALE', 409)

    @sensitive_variables()
    def _change(self, row, **values):
        with ga._write_window():
            changed = SynthesisRun.objects.filter(pk=row.pk, revision=row.revision).update(
                **values, revision=row.revision + 1, updated_at=self.authority.now())
        require(changed == 1, 'REVISION_CONFLICT', 409)
        row.refresh_from_db()
        return row

    def _unresolved(self, cabinet, exclude=None):
        rows = SynthesisRun.objects.filter(profile_id=cabinet.profile_id,
            attempts=1, state__in=['running', 'unknown'])
        if exclude is not None:
            rows = rows.exclude(pk=exclude)
        require(not rows.exists(), 'UNKNOWN_RECONCILIATION_REQUIRED', 409)

    def admit(self, intent, *, local_context=None,selection_policy=None):
        from .enhance_selection import RunSelectionPolicy
        require(selection_policy is None or type(selection_policy) is RunSelectionPolicy)
        if selection_policy is not None:
            from django.db.migrations.recorder import MigrationRecorder
            require(MigrationRecorder.Migration.objects.filter(app='shelf',name='0030_run_selection_retention').exists(),
                    'CAPABILITY_UNAVAILABLE',503)
            ga._synthesis_guards()
        require(type(intent) is EnhanceIntent)
        client, refresh = ga._uuid(intent.client_key), ga._uuid(intent.refresh_nonce)
        with self.mutation() as (cabinet, current):
            require(intent.bindings == current.bindings, 'PREVIEW_STALE', 409)
            if local_context is not None:
                require(current.local_context is not None and local_context.digest==current.local_context.digest,
                        'PREVIEW_STALE',409)
            from .enhance_selection import admission_fingerprint
            fingerprint=admission_fingerprint(current.record(),refresh,
                {'selection_policy':selection_policy.material()} if selection_policy is not None else {})
            prior = SynthesisRun.objects.filter(installation=cabinet.installation, actor=cabinet.actor,
                profile=cabinet.profile, contract_version=VERSION, client_key=client).first()
            if prior is not None:
                require(prior.input_digest == fingerprint, 'IDEMPOTENCY_CONFLICT', 409)
                return prior
            self._unresolved(cabinet)
            require(not SynthesisRun.objects.filter(installation=cabinet.installation, actor=cabinet.actor,
                profile=cabinet.profile, contract_version=VERSION, input_digest=fingerprint).exists(),
                'IDEMPOTENCY_CONFLICT', 409)
            try:
                with ga._write_window():
                    row = SynthesisRun.objects.create(id=ga._uuid(intent.run_ref), actor=cabinet.actor,
                        profile=cabinet.profile, installation=cabinet.installation,
                        portable_connection_id=ga._uuid(self.connection_ref), contract_version=VERSION,
                        mode='portable', client_key=client, refresh_nonce=refresh,
                        input_snapshot={}, input_digest=fingerprint, portable_stage='authorized',
                        portable_bindings=current.record(), portable_expires_at=current.expires_at,
                        evidence={'code': 'OK', 'outcomes': [], 'phase': 'catalogue',
                                  'expected_publication_revision': cabinet.publication_revision,
                                  **({'selection_policy':selection_policy.material()} if selection_policy is not None else {})})
                    if selection_policy is not None:
                        from .models import PortableJourneyState
                        journey,_=PortableJourneyState.objects.get_or_create(cabinet=cabinet,defaults={
                            'created_at':self.authority.now(),'updated_at':self.authority.now()})
                        PortableJourneyState.objects.filter(pk=journey.pk).update(
                            next_noise=selection_policy.noise,updated_at=self.authority.now())
            except IntegrityError:
                raise AuthorityFault('IDEMPOTENCY_CONFLICT', 409) from None
            if cabinet.data_schema_version==2:
                from .portable_taste_history import capture_committed
                capture_committed(self.authority,self.session_ref,self.profile_ref,origin_run_ref=str(row.pk))
            return row

    def freeze_selection(self,run_ref):
        """Freeze deterministic local selection evidence before selector use."""
        from .enhance_selection import RunSelectionPolicy,selection_seed
        with self.mutation() as (cabinet,current):
            row=self._run(cabinet,run_ref);self._match(row,current)
            material=row.evidence.get('selection_policy')
            if material is None:return None,None
            from django.db.migrations.recorder import MigrationRecorder
            require(MigrationRecorder.Migration.objects.filter(app='shelf',name='0030_run_selection_retention').exists(),
                    'CAPABILITY_UNAVAILABLE',503)
            ga._synthesis_guards()
            require(type(material) is dict and set(material)=={'version','noise'})
            policy=RunSelectionPolicy(**material)
            require(len(row.collection_digest)==64,'RUN_STALE',409)
            seed=selection_seed(policy,str(row.pk),row.portable_bindings['bindings']['snapshot_revision'],row.collection_digest)
            if 'selection_seed' in row.evidence:
                require(row.evidence['selection_seed']==seed and row.evidence.get('selection_policy_digest')==policy.digest)
            else:
                self._change(row,evidence=dict(row.evidence,selection_seed=seed,selection_policy_digest=policy.digest))
            return policy,seed

    def status(self, run_ref):
        with self.scope() as cabinet:
            return self._run(cabinet, run_ref)

    def checkpoint(self, run_ref, *, stage, code='OK', outcomes=(), metadata=None, **sealed):
        from .enhance_contract import STAGES, TERMINALS
        require(stage in STAGES + TERMINALS and code in SAFE_CODES)
        require(set(sealed) <= {'collection_digest', 'body_digest', 'sealed_run_digest', 'portable_expires_at'})
        for name, value in sealed.items():
            if name.endswith('digest'):
                require(type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value))
        with self.mutation() as (cabinet, current):
            row = self._run(cabinet, run_ref)
            self._match(row, current)
            require(row.attempts == 0 and row.state == 'queued' and not row.cancel_requested,
                    'RUN_STALE', 409)
            evidence = dict(row.evidence, code=code, outcomes=list(outcomes))
            if metadata:
                require(set(metadata) <= {'phase', 'preview_ref', 'preview_digest', 'session_digest', 'submitted_refs',
                                         'source_use'})
                if 'source_use' in metadata:
                    summary = checked_source_use(metadata['source_use'], current.registry.categories,
                                                 outcomes=evidence['outcomes'])
                    require(len(sealed.get('collection_digest', row.collection_digest)) == 64)
                    # Counts describe the immutable collection, even when a later
                    # publication projects a model-denied category as unavailable.
                    require('source_use' not in row.evidence or row.evidence['source_use'] == summary)
                    metadata = {**metadata, 'source_use': summary}
                evidence.update(metadata)
            canonical_bytes(evidence)
            return self._change(row, portable_stage=stage, evidence=evidence, **sealed)

    def reserve(self, run_ref, *, body_digest, expected_revision):
        require(type(expected_revision) is int and expected_revision >= 0)
        with self.mutation() as (cabinet, current):
            row = self._run(cabinet, run_ref)
            self._match(row, current)
            require(row.revision == expected_revision, 'REVISION_CONFLICT', 409)
            require(row.attempts == 0 and row.state == 'queued' and not row.cancel_requested,
                    'UNKNOWN_RECONCILIATION_REQUIRED', 409)
            require(row.body_digest == body_digest and len(row.collection_digest) == 64
                and len(row.sealed_run_digest) == 64, 'PREVIEW_STALE', 409)
            self._unresolved(cabinet, exclude=row.pk)
            outcomes = row.evidence.get('outcomes')
            require(type(outcomes) is list and [value.get('category') for value in outcomes
                if type(value) is dict] == list(current.registry.categories), 'CONTRACT_INVALID', 422)
            return self._change(row, state='running', portable_stage='reserved', attempts=1,
                                transport_request_id=uuid.uuid4())

    def mark_unknown(self, run_ref):
        with self.scope() as cabinet:
            row = self._run(cabinet, run_ref)
            if row.attempts == 1 and row.state in ('running', 'unknown'):
                self._change(row, state='unknown', portable_stage='unknown',
                    evidence=dict(row.evidence, code='UNKNOWN_RECONCILIATION_REQUIRED'))
            return row

    @sensitive_variables()
    def record_result(self, run_ref, raw):
        with self.mutation() as (cabinet, current):
            row = self._run(cabinet, run_ref)
            self._match(row, current)
            require(row.state == 'running' and row.attempts == 1 and not row.cancel_requested, 'RUN_STALE', 409)
            refs = row.evidence.get('submitted_refs')
            require(type(refs) is dict and set(refs) == {'signals', 'candidates'})
            try:
                output = validate_result(raw, **refs)
            except ContractError:
                return self._change(row, state='failed', portable_stage='failed',
                    evidence=dict(row.evidence, code='CONTRACT_INVALID'))
            return self._change(row, state='completed', portable_stage='validated', output=output,
                                evidence=dict(row.evidence, code='OK'))

    def finish(self, run_ref, *, stage, code, outcomes=None):
        from .enhance_contract import TERMINALS
        require(stage in TERMINALS and code in SAFE_CODES)
        with self.scope() as cabinet:
            row = self._run(cabinet, run_ref)
            if row.attempts == 1 and row.state in ('running', 'unknown'):
                return self._change(row, state='unknown', portable_stage='unknown',
                    evidence=dict(row.evidence, code='UNKNOWN_RECONCILIATION_REQUIRED'))
            if row.state == 'failed':
                return row
            evidence = dict(row.evidence, code=code)
            if outcomes is not None:
                from .enhance_contract import CategoryOutcome
                categories = row.portable_bindings['categories']
                require(type(outcomes) is list and len(outcomes) == len(categories))
                for category, item in zip(categories, outcomes):
                    require(type(item) is dict
                        and set(item) == {'category', 'state', 'candidate_count', 'code'})
                    require(CategoryOutcome(**item).category == category)
                evidence['outcomes'] = read_json(canonical_bytes(outcomes))
            return self._change(row, state='completed' if row.state == 'completed' else 'failed',
                portable_stage=stage, evidence=evidence)

    def cancel(self, run_ref, *, expected_revision):
        with self.scope(sensitive=True) as cabinet:
            row = self._run(cabinet, run_ref)
            require(type(expected_revision) is int and row.revision == expected_revision, 'REVISION_CONFLICT', 409)
            if row.cancel_requested or row.state in ('completed', 'failed'):
                return row
            unknown = row.attempts == 1
            return self._change(row, cancel_requested=True, state='unknown' if unknown else 'failed',
                portable_stage='unknown' if unknown else 'cancelled', evidence=dict(row.evidence,
                    code='UNKNOWN_RECONCILIATION_REQUIRED' if unknown else 'OK'))

    def validate_manifest(self, manifest, current, allowed_candidates=None):
        value = read_json(canonical_bytes(manifest))
        require(type(value) is dict and set(value) == {'schema', 'categories', 'for_you'}
            and value['schema'] == 'cabinet-manifest/v1')
        require(type(value['categories']) is list and len(value['categories']) == len(current.registry.categories))
        require([row.get('category') for row in value['categories'] if type(row) is dict] == list(current.registry.categories))
        seen = set()
        for row in value['categories']:
            require(type(row) is dict and set(row) == {'category', 'state', 'code', 'cards'})
            require(row['state'] in CATEGORY_STATES and row['code'] in SAFE_CODES
                and type(row['cards']) is list and len(row['cards']) <= 20)
            require((row['state'] == 'fresh') == bool(row['cards']))
            for card in row['cards']:
                require(type(card) is dict and type(card.get('key')) is str and card.get('category') == row['category'])
                require(card['key'] not in seen)
                seen.add(card['key'])
                if allowed_candidates is not None:
                    require(card in allowed_candidates)
        require(len(seen) <= 100 and type(value['for_you']) is list and len(value['for_you']) <= 100)
        for reason in value['for_you']:
            require(type(reason) is dict and set(reason) == {'candidate_key', 'text', 'kind', 'evidence_refs'})
            require(reason['candidate_key'] in seen and reason['kind'] in ('explicit', 'source', 'inferred')
                and type(reason['text']) is str and len(reason['text']) <= 1200
                and type(reason['evidence_refs']) is list and len(reason['evidence_refs']) <= 100)
        return value

    @sensitive_variables()
    def publish(self, run_ref, *, manifest, expected_publication_revision, allowed_candidates,selection_report=None):
        with self.mutation() as (cabinet, current):
            row = self._run(cabinet, run_ref)
            self._match(row, current)
            prior = PortableCabinetManifest.objects.filter(run=row).first()
            if prior is not None:
                return prior
            require(row.state == 'completed' and row.attempts == 1 and not row.cancel_requested, 'RUN_STALE', 409)
            require(type(expected_publication_revision) is int and cabinet.publication_revision == expected_publication_revision,
                    'REVISION_CONFLICT', 409)
            require(expected_publication_revision == row.evidence['expected_publication_revision'], 'REVISION_CONFLICT', 409)
            value = self.validate_manifest(manifest, current, allowed_candidates)
            if selection_report is not None:
                from .enhance_selection import checked_selection_report
                selection_report=checked_selection_report(selection_report,current.registry.categories,
                    policy_digest=row.evidence.get('selection_policy_digest'),seed=row.evidence.get('selection_seed'),
                    selected_keys={card['key'] for group in value['categories'] for card in group['cards']})
            revision = cabinet.publication_revision + 1
            with ga._write_window():
                publication = PortableCabinetManifest.objects.create(cabinet=cabinet, run=row,
                    publication_revision=revision, bindings=current.record(), manifest=value,
                    created_at=self.authority.now(), expires_at=min(current.expires_at, row.portable_expires_at))
                changed = ProfileCabinetState.objects.filter(pk=cabinet.pk,
                    publication_revision=expected_publication_revision).update(
                        active_manifest_ref=publication.pk, publication_revision=revision)
            require(changed == 1, 'REVISION_CONFLICT', 409)
            self._change(row, portable_stage='published', evidence=dict(row.evidence,
                publication_revision=revision, publication_ref=str(publication.pk),
                **({'selection_report':selection_report} if selection_report is not None else {})))
            return publication

    def classify_publication(self, cabinet, publication, current):
        require(publication.content_redacted_at is None and self.authority.now() < publication.expires_at,
                'PREVIEW_STALE', 409)
        expected = current.record()
        if publication.bindings == expected:
            return 'fresh'
        from .card_actions import verify_transition_chain
        verify_transition_chain(cabinet, publication, current, self.authority, self.session_ref)
        return 'feedback_only'

    def read_current(self):
        return self._read_publication(interaction=False)

    def read_for_interaction(self):
        return self._read_publication(interaction=True)

    def _read_publication(self, *, interaction):
        self.redact_expired()
        preserve_feedback = False
        try:
            with self.scope() as cabinet:
                if cabinet.active_manifest_ref is None:
                    return None
                current = self.current()
                publication = PortableCabinetManifest.objects.filter(pk=cabinet.active_manifest_ref,
                    cabinet=cabinet, publication_revision=cabinet.publication_revision).first()
                require(publication is not None, 'PREVIEW_STALE', 409)
                classification = self.classify_publication(cabinet, publication, current)
                feedback_only = classification == 'feedback_only'
                # A proven feedback-only view is display authority only. Strict
                # send/current paths still refuse without deleting source cards
                # solely because these exact personal actions changed taste.
                preserve_feedback = feedback_only and not interaction
                require(not preserve_feedback, 'PREVIEW_STALE', 409)
                filtered = self.provider.filter_manifest(current, read_json(canonical_bytes(publication.manifest)))
                require(filtered is not None, 'AUTHORITY_DENIED', 403)
                if feedback_only:
                    filtered = {**filtered, 'for_you': []}
                stored_cards = [card for row in publication.manifest['categories'] for card in row['cards']]
                filtered = self.validate_manifest(filtered, current, stored_cards)
                require(all(reason in publication.manifest['for_you'] for reason in filtered['for_you']),
                        'AUTHORITY_DENIED', 403)
                return (publication, filtered, classification) if interaction else (publication, filtered)
        except AuthorityFault:
            if not preserve_feedback:
                self.redact_profile()
            raise

    def redact_expired(self):
        with self.scope():
            pass
        now = self.authority.now()
        expired = set(SynthesisRun.objects.filter(profile__profilecabinetstate__profile_ref=self.profile_ref,
            contract_version=VERSION, content_redacted_at__isnull=True,
            portable_expires_at__lte=now).values_list('pk', flat=True))
        expired.update(PortableCabinetManifest.objects.filter(cabinet__profile_ref=self.profile_ref,
            content_redacted_at__isnull=True, expires_at__lte=now).values_list('run_id', flat=True))
        if expired:
            self.redact_profile(run_refs=expired)

    def redact_profile(self, *, run_refs=None):
        """Erase content atomically while retaining attempts and publication CAS.

        This is deliberately outside mutation(): deletion moves freshness floors,
        so comparing its old taste view on exit would roll back the privacy work.
        """
        from .models import _content_digest
        from .enhance_selection import redacted_selection_evidence
        import json
        with self.scope(sensitive=False) as cabinet:
            rows = SynthesisRun.objects.filter(profile=cabinet.profile, installation=cabinet.installation,
                actor=cabinet.actor, contract_version=VERSION)
            if run_refs is not None:
                rows = rows.filter(pk__in=run_refs)
            rows = list(rows)
            changed = False
            with ga._write_window():
                for row in rows:
                    for publication in PortableCabinetManifest.objects.filter(run=row, content_redacted_at__isnull=True):
                        PortableCabinetManifest.objects.filter(pk=publication.pk, content_redacted_at__isnull=True).update(
                            content_redacted_at=self.authority.now(), content_digest=_content_digest(json.dumps(publication.manifest, ensure_ascii=False)),
                            manifest={})
                        changed = True
                    if row.content_redacted_at is None:
                        updated = SynthesisRun.objects.filter(pk=row.pk, revision=row.revision, content_redacted_at__isnull=True).update(
                            content_redacted_at=self.authority.now(), content_digest=_content_digest(json.dumps(row.output, ensure_ascii=False), json.dumps(row.evidence, ensure_ascii=False)),
                            output={}, evidence=redacted_selection_evidence(row.evidence), revision=row.revision + 1, updated_at=self.authority.now())
                        require(updated == 1, 'REVISION_CONFLICT', 409)
                        changed = True
                if changed:
                    from django.db.models import F
                    ProfileCabinetState.objects.filter(pk=cabinet.pk).update(policy_epoch=F('policy_epoch') + 1,
                        restriction_watermark=F('restriction_watermark') + 1)
            service = getattr(self.authority, '_enhance_service', None)
            if service is not None:
                service.close()
                cache = getattr(getattr(service, 'catalogue', None), 'state', None)
                if cache is not None:
                    cache.cache.clear()
                sealed = getattr(getattr(service, 'catalogue', None), '_sealed', None)
                if sealed is not None:
                    sealed.clear()
                for backend in getattr(getattr(service, 'catalogue', None), 'backends', {}).values():
                    contexts = getattr(backend, '_contexts', None)
                    if contexts is not None:
                        contexts.clear()
            invalidator = getattr(self.provider, 'invalidate_content', None)
            if callable(invalidator):
                invalidator()
            return changed

    @sensitive_variables()
    def reconcile(self, run_ref, *, receipt_ref, reconciler):
        # The registered backend must authenticate the ORIGINAL route/request.
        # A caller supplies only its opaque receipt reference, never an outcome.
        require(reconciler is not None and callable(getattr(reconciler, 'verify', None)), 'CAPABILITY_UNAVAILABLE', 503)
        receipt_ref = str(ga._uuid(receipt_ref))
        with self.mutation() as (cabinet, current):
            row = self._run(cabinet, run_ref)
            require(row.attempts == 1 and row.state in ('running', 'unknown'), 'RUN_STALE', 409)
            outcome = reconciler.verify(row, receipt_ref)
            require(type(outcome) is tuple and len(outcome) == 3
                and outcome[0] == str(row.transport_request_id), 'AUTHORITY_DENIED', 403)
            if row.content_redacted_at is not None:
                require(outcome[1] in ('proven_no_send', 'completed'), 'AUTHORITY_DENIED', 403)
                from .enhance_selection import redacted_selection_evidence
                retained=redacted_selection_evidence(row.evidence)
                require(row.evidence==retained,'CONTRACT_INVALID',422)
                return self._change(row, state='failed' if outcome[1] == 'proven_no_send' else 'completed',
                    portable_stage='failed' if outcome[1] == 'proven_no_send' else 'validated', output={}, evidence=retained)
            if outcome[1] == 'proven_no_send':
                return self._change(row, state='failed', portable_stage='failed',
                    evidence=dict(row.evidence, code='SOURCE_UNAVAILABLE'))
            require(outcome[1] == 'completed' and type(outcome[2]) is bytes, 'AUTHORITY_DENIED', 403)
            refs = row.evidence.get('submitted_refs')
            require(type(refs) is dict)
            try:
                output = validate_result(outcome[2], **refs)
            except ContractError:
                raise AuthorityFault('CONTRACT_INVALID', 422) from None
            return self._change(row, state='completed', portable_stage='validated', output=output,
                                evidence=dict(row.evidence, code='OK'))
