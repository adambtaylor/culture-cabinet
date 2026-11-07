"""Typed all-category API -> exact preview -> authorized inference coordinator.

No route, persistent broker, transport or publication writer is registered here.
Production composition needs the independently admitted durable I5/I7 host ports.
The existing personal codec/transport capability barriers remain unchanged.
"""
from dataclasses import dataclass, field
from datetime import datetime

from django.views.decorators.debug import sensitive_variables

from . import disclosure, grant_authority, inference_body, profile_snapshot
from .enhance_contract import (CataloguePlan, CollectionBudget, CollectionSeal,
    CategoryOutcome, EnhanceBindings, EnhanceFault, EnhanceIntent, RegistryView,
    SAFE_CODES, StageEvent, digest, require, utc)
from .inference_contract import ContractError, validate_result
from .portable_authority import AuthorityFault


@dataclass(frozen=True, repr=False)
class PlannedEnhance:
    intent: EnhanceIntent
    registry: RegistryView
    plan: CataloguePlan
    query_view: object = field(repr=False)
    snapshot_bytes: bytes = field(repr=False)
    events: tuple[StageEvent, ...]


@dataclass(frozen=True, repr=False)
class CollectedEnhance:
    planned: PlannedEnhance
    seal: CollectionSeal
    events: tuple[StageEvent, ...]


@dataclass(frozen=True, repr=False)
class InferencePreview:
    collected: CollectedEnhance
    inference_view: object = field(repr=False)
    body: bytes = field(repr=False)
    body_digest: str
    expires_at: datetime
    events: tuple[StageEvent, ...]
    omitted_groups: tuple[str, ...] = ()


@dataclass(frozen=True, repr=False)
class EnhanceOutcome:
    event: StageEvent
    events: tuple[StageEvent, ...]
    validated_bytes: bytes | None = field(default=None, repr=False)
    remaining_integration: tuple[str, ...] = ('durable_run_service', 'selection', 'publication_cas', 'filtered_reads')


def _safe_code(error, default='CONTRACT_INVALID'):
    code = getattr(error, 'code', default)
    return code if type(code) is str and code in SAFE_CODES else default


class EnhanceCoordinator:
    """Only backend-selected Catalogue/host ports are accepted at construction.

    No current default host implements durable reservation/personal dispatch.
    A source/model payload cannot select a port, endpoint, profile or capability.
    Calling phases again does not imply a retry grant: the source plan and durable
    host own idempotent admission, usage reservations and unknown-attempt blocking.
    """
    def __init__(self, catalogue, ports=None, *, now, budget=None, on_stage=None):
        require(callable(now))
        require(budget is None or type(budget) is CollectionBudget)
        require(on_stage is None or callable(on_stage))
        self.catalogue, self.ports, self.now = catalogue, ports, now
        self.budget, self.on_stage = budget or CollectionBudget(), on_stage

    def _event(self, run_ref, stage, code='OK', outcomes=()):
        event = StageEvent(run_ref, stage, code, outcomes)
        if self.on_stage is not None:
            self.on_stage(event.public())
        return event

    def _stop(self, run_ref, events, code, *, stage='blocked', outcomes=()):
        event = self._event(run_ref, stage, code, outcomes)
        return EnhanceOutcome(event, tuple(events) + (event,))

    def _current(self, view, purpose):
        require(type(view) is grant_authority.AuthorityView, 'AUTHORITY_DENIED')
        grant_authority.assert_current(view)
        require(view.context['purpose'] == purpose, 'AUTHORITY_DENIED')
        require(utc(self.now()) < view.expires_at, 'PREVIEW_STALE')

    def _host_check(self, intent, plan, seal=None, body=None):
        # A plain dataclass/digest is not an authority receipt. The missing host
        # service is an explicit capability gap, never a no-op authorization.
        require(self.ports is not None and callable(getattr(self.ports, 'revalidate', None)),
                'CAPABILITY_UNAVAILABLE')
        try:
            self.ports.revalidate(intent, plan, seal, body)
        except (EnhanceFault, AuthorityFault, disclosure.DisclosureError):
            raise
        except Exception:
            raise EnhanceFault('CAPABILITY_UNAVAILABLE') from None

    @sensitive_variables()
    def plan(self, query_view, registry, *, run_ref, client_key, refresh_nonce, inference_view=None):
        self._current(query_view, 'catalogue_lookup')
        require(type(registry) is RegistryView)
        snapshot = profile_snapshot.capture(query_view)
        bindings = EnhanceBindings(query_view.installation_id, query_view.actor_ref,
            query_view.profile_ref, query_view.profile_revision_digest, query_view.policy_epoch,
            query_view.restriction_watermark, registry.revision, query_view.context['config_revision'],
            digest(disclosure.canonical_bytes(query_view.grants)),
            digest(disclosure.canonical_bytes(query_view.connection_binding)))
        primary = bindings
        if inference_view is not None:
            self._current(inference_view, 'inference')
            require((inference_view.installation_id, inference_view.actor_ref, inference_view.profile_ref,
                     inference_view.profile_revision_digest, inference_view.policy_epoch, inference_view.restriction_watermark) ==
                    (bindings.installation_id, bindings.actor_ref, bindings.profile_ref,
                     bindings.snapshot_revision, bindings.policy_epoch, bindings.restriction_watermark), 'PREVIEW_STALE')
            # Run identity stays processor-bound; the plan/seal retain their own
            # source grant/config digest. Purpose digests are never equated.
            primary = EnhanceBindings(inference_view.installation_id, inference_view.actor_ref,
                inference_view.profile_ref, inference_view.profile_revision_digest, inference_view.policy_epoch,
                inference_view.restriction_watermark, registry.revision, inference_view.context['config_revision'],
                digest(disclosure.canonical_bytes(inference_view.grants)),
                digest(disclosure.canonical_bytes(inference_view.connection_binding)))
        intent = EnhanceIntent(run_ref, client_key, refresh_nonce, primary)
        plan = inference_body.serialize_catalogue(snapshot, query_view.grants, query_view.context,
            registry, now=self.now(), plan_ref=run_ref, bindings=bindings)
        self._current(query_view, 'catalogue_lookup')
        events = (self._event(run_ref, 'authorized'),)
        return PlannedEnhance(intent, registry, plan, query_view, disclosure.canonical_bytes(snapshot), events)

    @sensitive_variables()
    def collect(self, planned, *, approved_query_digest):
        require(type(planned) is PlannedEnhance)
        run_ref, events = planned.intent.run_ref, list(planned.events)
        try:
            self._current(planned.query_view, 'catalogue_lookup')
            require(approved_query_digest == planned.plan.body_digest, 'PREVIEW_STALE')
            require(self.now() < planned.plan.expires_at, 'PREVIEW_STALE')
            self._host_check(planned.intent, planned.plan)
            events.append(self._event(run_ref, 'collecting'))
            # This ONE port settles initial retrieval AND enrichment for EVERY
            # enabled category. No inference path exists without its complete seal.
            seal = self.catalogue.collect(planned.plan, planned.registry, self.budget)
            require(type(seal) is CollectionSeal)
            seal.verify(planned.plan, planned.registry, now=self.now())
            self._current(planned.query_view, 'catalogue_lookup')
            self._host_check(planned.intent, planned.plan, seal)
            events.append(self._event(run_ref, 'sealed', outcomes=seal.outcomes))
            if not seal.records:
                return self._stop(run_ref, events, 'NO_GROUNDED_CANDIDATES', stage='no_candidates', outcomes=seal.outcomes)
            return CollectedEnhance(planned, seal, tuple(events))
        except (EnhanceFault, AuthorityFault, disclosure.DisclosureError) as error:
            code = _safe_code(error)
            stage = 'stale' if code in ('PREVIEW_STALE', 'RUN_STALE', 'VALIDATION_STALE') else 'blocked'
            outcomes = tuple(CategoryOutcome(key, 'unavailable', 0, code) for key in planned.registry.categories)
            return self._stop(run_ref, events, code, stage=stage, outcomes=outcomes)
        except Exception:
            # Source error prose is untrusted/private. A thrown collector cannot
            # imply that any category succeeded or that the initial pool is sealed.
            outcomes = tuple(CategoryOutcome(key, 'failed', 0, 'SOURCE_UNAVAILABLE') for key in planned.registry.categories)
            return self._stop(run_ref, events, 'SOURCE_UNAVAILABLE', stage='failed', outcomes=outcomes)

    @sensitive_variables()
    def preview(self, collected, inference_view):
        require(type(collected) is CollectedEnhance)
        planned, seal = collected.planned, collected.seal
        self._current(planned.query_view, 'catalogue_lookup')
        self._current(inference_view, 'inference')
        # Catalogue and inference grants are independent. Compare subject/revision
        # identity, never pretend their purpose-specific grant digests are equal.
        require((inference_view.installation_id, inference_view.actor_ref, inference_view.profile_ref,
                 inference_view.profile_revision_digest, inference_view.policy_epoch, inference_view.restriction_watermark) ==
                (seal.bindings.installation_id, seal.bindings.actor_ref, seal.bindings.profile_ref,
                 seal.bindings.snapshot_revision, seal.bindings.policy_epoch, seal.bindings.restriction_watermark), 'PREVIEW_STALE')
        seal.verify(planned.plan, planned.registry, now=self.now())
        snapshot = profile_snapshot.capture(inference_view)
        encoded = inference_body.serialize(snapshot, inference_view.grants, inference_view.context, seal, now=self.now())
        self._host_check(planned.intent, planned.plan, seal, encoded.body)
        event = self._event(planned.intent.run_ref, 'awaiting_preview', outcomes=seal.outcomes)
        return InferencePreview(collected, inference_view, encoded.body, digest(encoded.body),
            min(encoded.expires_at, seal.expires_at, planned.plan.expires_at, inference_view.expires_at),
            collected.events + (event,), encoded.omitted_groups)

    @sensitive_variables()
    def infer(self, preview, *, approved_body_digest):
        require(type(preview) is InferencePreview)
        collected = preview.collected
        planned, seal = collected.planned, collected.seal
        run_ref, events = planned.intent.run_ref, list(preview.events)
        try:
            require(approved_body_digest == preview.body_digest and self.now() < preview.expires_at, 'PREVIEW_STALE')
            # Reconstruct from original server-held refs, not a browser-supplied
            # body or mutable query. Host then rechecks config/credential/usage and
            # reserves durably before a personal dispatch can be attempted.
            current = self.preview(collected, preview.inference_view)
            require(current.body == preview.body and current.body_digest == preview.body_digest, 'PREVIEW_STALE')
            self._host_check(planned.intent, planned.plan, seal, preview.body)
            require(callable(getattr(self.ports, 'reserve_attempt', None))
                and callable(getattr(self.ports, 'dispatch', None)), 'CAPABILITY_UNAVAILABLE')
            events.append(self._event(run_ref, 'ready', outcomes=seal.outcomes))
            reservation = self.ports.reserve_attempt(planned.intent, seal, preview.body)
            require(reservation is not None, 'CAPABILITY_UNAVAILABLE')
            events.append(self._event(run_ref, 'reserved', outcomes=seal.outcomes))
            # Reservation can race a reaction/revocation. Recheck before handing
            # bytes to dispatch; only the durable host may settle this attempt.
            self._current(planned.query_view, 'catalogue_lookup')
            self._current(preview.inference_view, 'inference')
            self._host_check(planned.intent, planned.plan, seal, preview.body)
        except (EnhanceFault, AuthorityFault, disclosure.DisclosureError) as error:
            code = _safe_code(error)
            stage = 'unknown' if code == 'UNKNOWN_RECONCILIATION_REQUIRED' else ('stale' if code.endswith('STALE') else 'blocked')
            return self._stop(run_ref, events, code, stage=stage, outcomes=seal.outcomes)
        except Exception:
            return self._stop(run_ref, events, 'UNKNOWN_RECONCILIATION_REQUIRED', stage='unknown', outcomes=seal.outcomes)
        events.append(self._event(run_ref, 'in_flight', outcomes=seal.outcomes))
        try:
            raw = self.ports.dispatch(reservation, preview.body)
        except Exception:
            # Once reserved/handoff is possible, never infer a safe retry from an
            # exception. The durable host preserves the unresolved attempt.
            return self._stop(run_ref, events, 'UNKNOWN_RECONCILIATION_REQUIRED', stage='unknown', outcomes=seal.outcomes)
        try:
            wire = disclosure.read_json(preview.body)
            submitted = disclosure.read_json(wire['input'].encode('utf-8'))
            signals = [{key: row[key] for key in ('key', 'medium')} for row in submitted['profile']['signals']]
            candidates = [{'key': row['key'], 'evidence_refs': sorted(set(row['source_roots']) | {f['id'] for f in row['facts']})}
                          for row in submitted['candidates']]
            value = validate_result(raw, signals=signals, candidates=candidates)
        except (ContractError, disclosure.DisclosureError, KeyError, TypeError, UnicodeError):
            return self._stop(run_ref, events, 'CONTRACT_INVALID', stage='failed', outcomes=seal.outcomes)
        event = self._event(run_ref, 'validated', outcomes=seal.outcomes)
        return EnhanceOutcome(event, tuple(events) + (event,), disclosure.canonical_bytes(value))
