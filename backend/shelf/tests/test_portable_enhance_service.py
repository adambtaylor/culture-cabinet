"""Isolated service composition; real authority/store/codecs, synthetic producers.

The pipeline double supplies the unleased portable-bookkeeping snapshot adapter.
It does not register a normal capability or substitute for runtime acceptance.
"""
import uuid

from shelf import grant_authority as ga, inference_body, profile_snapshot
from shelf.disclosure import canonical_bytes, read_json
from shelf.enhance_contract import (CataloguePlan, CollectionSeal, CategoryOutcome,
    EnhanceIntent, StageEvent, digest)
from shelf.enhance_coordinator import PlannedEnhance, CollectedEnhance, InferencePreview, EnhanceOutcome
from shelf.models import PortableCabinetManifest, SynthesisRun
from shelf.portable_enhance_service import EnhanceService, get_service
from .test_portable_cabinet_store import StoreCase, result
from .test_disclosure import source, resign


class FixtureCatalogue:
    def __init__(self, case):
        self.case, self.calls, self.on_collect = case, 0, None

    def destinations_for(self, plan):
        return ('isolated://catalogue-no-network',)

    def collect(self, plan, registry, budget=None):
        self.calls += 1
        if self.on_collect:
            self.on_collect()
        record = source('fixture:book:1')
        record['candidate'].update(observed_at='2026-01-01T00:00:00Z', valid_until='2026-01-01T00:10:00Z')
        for binding in record['fields']:
            binding['observation'].update(observed_at='2026-01-01T00:00:00Z', expires_at='2026-01-01T00:10:00Z')
            binding['rights'].update(checked_at='2026-01-01T00:00:00Z', expires_at='2026-01-01T00:10:00Z')
            resign(record, binding)
        outcomes = tuple(CategoryOutcome(key, 'fresh', 1) if key == 'books' else
            CategoryOutcome(key, 'unavailable', 0, 'CAPABILITY_UNAVAILABLE') for key in registry.categories)
        return CollectionSeal('fixture-collection', plan.plan_ref, plan.bindings, outcomes,
            canonical_bytes({'records': [record], 'observations': {'observation:1': {'revision': 7, 'digest': 'c' * 64}},
                             'policies': {'policy:1': {'revision': 3}}}), b'{}', self.case.expires)


class FixtureTransport:
    def __init__(self):
        self.calls, self.fail = 0, False

    def dispatch(self, reservation, body):
        self.calls += 1
        if self.fail:
            raise RuntimeError('PRIVATE-UNCERTAIN-HANDOFF')
        return result()


class FixtureSelector:
    def build(self, output, submitted, outcomes):
        return {'schema': 'cabinet-manifest/v1', 'categories': [
            {'category': row.category, 'state': row.state, 'code': row.code,
             'cards': [card for card in submitted['candidates'] if card['category'] == row.category]}
            for row in outcomes], 'for_you': []}


class FixturePipeline:
    def __init__(self, case, catalogue):
        self.case, self.catalogue, self.ports = case, catalogue, None
        current = case.provider.current(case.authority, case.ref,
            profile_ref=case.profile_ref, connection_ref=str(case.config.pk))
        self.empty_native_snapshot = profile_snapshot.capture(current.view)

    def plan(self, query_view, registry, *, run_ref, client_key, refresh_nonce):
        current = self.case.provider.current(self.case.authority, self.case.ref,
            profile_ref=self.case.profile_ref, connection_ref=str(self.case.config.pk))
        # This fixture's native history is empty and unchanged. Portable metadata
        # must not masquerade as new taste; a real production adapter is unleased.
        self.case.assertEqual(current.bindings.snapshot_revision, self.empty_native_snapshot['profile_revision_digest'])
        snapshot = read_json(canonical_bytes(self.empty_native_snapshot))
        plan = inference_body.serialize_catalogue(snapshot, query_view.grants, query_view.context,
            registry, now=self.case.clock.wall, plan_ref=run_ref, bindings=current.bindings)
        intent = EnhanceIntent(run_ref, client_key, refresh_nonce, current.bindings)
        return PlannedEnhance(intent, registry, plan, query_view, canonical_bytes(snapshot), (StageEvent(run_ref, 'authorized'),))

    def collect(self, planned, *, approved_query_digest):
        self.case.assertEqual(approved_query_digest, planned.plan.body_digest)
        self.ports.revalidate(planned.intent, planned.plan, None, None)
        seal = self.catalogue.collect(planned.plan, planned.registry)
        seal.verify(planned.plan, planned.registry, now=self.case.clock.wall)
        return CollectedEnhance(planned, seal, planned.events + (StageEvent(planned.intent.run_ref, 'sealed', outcomes=seal.outcomes),))

    def preview(self, collected, inference_view):
        # Only the explicitly isolated missing-state-producer double can reuse
        # these original immutable refs across bookkeeping rows. Actual authority,
        # consent/floor checks and the ONE body codec still execute.
        planned = collected.planned
        self.ports.revalidate(planned.intent, planned.plan, collected.seal, None)
        encoded = inference_body.serialize(read_json(planned.snapshot_bytes), inference_view.grants,
            inference_view.context, collected.seal, now=self.case.clock.wall)
        return InferencePreview(collected, inference_view, encoded.body, digest(encoded.body), encoded.expires_at,
                                collected.events + (StageEvent(planned.intent.run_ref, 'awaiting_preview'),), encoded.omitted_groups)

    def infer(self, preview, *, approved_body_digest):
        planned, seal = preview.collected.planned, preview.collected.seal
        self.case.assertEqual(approved_body_digest, preview.body_digest)
        self.ports.revalidate(planned.intent, planned.plan, seal, preview.body)
        reservation = self.ports.reserve_attempt(planned.intent, seal, preview.body)
        try:
            self.ports.dispatch(reservation, preview.body)
        except Exception:
            event = StageEvent(planned.intent.run_ref, 'unknown', 'UNKNOWN_RECONCILIATION_REQUIRED', seal.outcomes)
            return EnhanceOutcome(event, preview.events + (event,))
        event = StageEvent(planned.intent.run_ref, 'validated', outcomes=seal.outcomes)
        return EnhanceOutcome(event, preview.events + (event,), result())


class ServiceCase(StoreCase):
    def setUp(self):
        super().setUp()
        self.catalogue, self.transport = FixtureCatalogue(self), FixtureTransport()
        self.pipeline = FixturePipeline(self, self.catalogue)
        self.enhance = EnhanceService.isolated_test(self.authority, self.service, pipeline=self.pipeline,
            catalogue=self.catalogue, registry=self.registry, personal_transport=self.transport,
            state_provider=self.provider, selector=FixtureSelector())

    def start_run(self, **overrides):
        arguments = dict(profile_ref=self.profile_ref, connection_ref=str(self.config.pk),
                         client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
        arguments.update(overrides)
        return self.enhance.start(self.ref, **arguments)

    def confirm_run(self, status):
        receipt = self.enhance.preview(self.ref, run_ref=status['run_ref'])['disclosure']
        return self.enhance.confirm(self.ref, run_ref=status['run_ref'], preview_ref=receipt['preview_ref'],
            body_digest=receipt['body_digest'], confirmed=True)


class PortableEnhanceServiceTests(ServiceCase):
    def test_query_and_inference_confirmations_are_distinct_exact_and_idempotent(self):
        key, refresh = str(uuid.uuid4()), str(uuid.uuid4())
        started = self.start_run(client_key=key, refresh_nonce=refresh)
        replay = self.start_run(client_key=key, refresh_nonce=refresh)
        self.assertEqual(started['run_ref'], replay['run_ref'])
        query = self.enhance.preview(self.ref, run_ref=started['run_ref'])['disclosure']
        self.assertEqual(query['kind'], 'catalogue')
        self.assertEqual((self.catalogue.calls, self.transport.calls), (0, 0))
        self.fault('PREVIEW_STALE', self.enhance.confirm, self.ref, run_ref=started['run_ref'],
            preview_ref=query['preview_ref'], body_digest='wrong', confirmed=True)
        competing = []
        self.catalogue.on_collect = lambda: competing.append(self.enhance.confirm(self.ref,
            run_ref=started['run_ref'], preview_ref=query['preview_ref'], body_digest=query['body_digest'], confirmed=True))
        inference = self.confirm_run(started)
        self.assertEqual(competing[0]['stage'], 'collecting')
        self.assertNotIn('confirm', competing[0]['actions'])
        self.assertEqual((self.catalogue.calls, self.transport.calls), (1, 0))
        self.assertEqual(self.enhance.preview(self.ref, run_ref=started['run_ref'])['disclosure']['kind'], 'inference')
        published = self.confirm_run(inference)
        self.assertTrue(published['published'])
        self.assertEqual(published['publication_revision'], 1)
        repeated = self.enhance.confirm(self.ref, run_ref=published['run_ref'],
            preview_ref=query['preview_ref'], body_digest=query['body_digest'], confirmed=True)
        self.assertTrue(repeated['published'])
        self.assertEqual((self.catalogue.calls, self.transport.calls), (1, 1))

    def test_lost_memory_and_wrong_session_scope_cannot_reconstruct_or_resend(self):
        started = self.start_run()
        self.fault('UNAUTHENTICATED', self.enhance.status, 'missing-session', run_ref=started['run_ref'])
        self.enhance.close()
        stale = self.enhance.status(self.ref, run_ref=started['run_ref'])
        self.assertEqual(stale['stage'], 'stale')
        self.assertNotIn('confirm', stale['actions'])
        self.fault('PREVIEW_STALE', self.enhance.preview, self.ref, run_ref=started['run_ref'])
        self.assertEqual((self.catalogue.calls, self.transport.calls), (0, 0))
        cancelled = self.enhance.cancel(self.ref, run_ref=started['run_ref'], expected_revision=stale['revision'])
        self.assertEqual(cancelled['stage'], 'cancelled')

    def test_unknown_keeps_previous_publication_and_blocks_blind_new_key(self):
        first = self.confirm_run(self.confirm_run(self.start_run()))
        current = self.enhance.current(self.ref, profile_ref=self.profile_ref)
        self.assertTrue(current['published'])
        next_run = self.confirm_run(self.start_run())
        self.transport.fail = True
        unknown = self.confirm_run(next_run)
        self.assertEqual(unknown['stage'], 'unknown')
        self.assertNotIn('confirm', unknown['actions'])
        self.assertEqual(self.enhance.current(self.ref, profile_ref=self.profile_ref)['publication_revision'],
                         first['publication_revision'])
        self.fault('UNKNOWN_RECONCILIATION_REQUIRED', self.start_run)
        self.assertEqual(PortableCabinetManifest.objects.count(), 1)


class NormalCompositionReadinessTests(StoreCase):
    def test_explicit_composition_without_sources_or_policy_stays_closed(self):
        service = EnhanceService.compose(self.authority, self.service)
        self.assertTrue(service._normal)
        from shelf.inference_adapters import PersonalResponsesAdapter
        self.assertIs(type(service.personal_transport), PersonalResponsesAdapter)
        # Existing fake neutral validation cannot become personal send authority.
        self.assertFalse(self.service.status(self.ref, self.config.pk, self.config.config_revision)['connection_ready'])
        self.assertEqual(service._contexts, {})
        service.close()
        self.assertEqual(service._memory, {})
