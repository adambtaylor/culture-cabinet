"""Synthetic server-port traces only; no model/source/HTTP/runtime activation."""
import json
from datetime import timedelta

from shelf import grant_authority as ga
from shelf.disclosure import canonical_bytes, read_json
from shelf.enhance_contract import CategoryOutcome, CollectionSeal, EnhanceFault, RegistryView
from shelf.enhance_coordinator import (CollectedEnhance, EnhanceCoordinator, EnhanceOutcome)
from .test_disclosure import resign, source
from .test_profile_snapshot import SnapshotCase


class SyntheticCollector:
    def __init__(self, case, trace):
        self.case, self.trace, self.mode = case, trace, 'partial'

    def collect(self, plan, registry, budget):
        self.trace.append(('collect', registry.categories))
        if self.mode == 'error':
            raise RuntimeError('PRIVATE-SOURCE-ERROR-CANARY')
        records = []
        if self.mode not in ('empty',):
            record = source()
            start = (self.case.clock.wall - timedelta(seconds=1)).isoformat().replace('+00:00', 'Z')
            end = self.case.expires.isoformat().replace('+00:00', 'Z')
            record['candidate'].update(observed_at=start, valid_until=end)
            for binding in record['fields']:
                binding['observation'].update(observed_at=start, expires_at=end)
                binding['rights'].update(checked_at=start, expires_at=end)
                resign(record, binding)
            # This isolated query used the confirmed reaction, not its denied
            # note. Preserve that actual personal ancestry in the candidate pool.
            record['direct_parent_ids'] = [node['id'] for node in self.case.snapshot()['evidence']
                                           if node['basis'] == 'anticipated_reaction']
            records.append(record)
        self.trace.append(('enrich', tuple(row['candidate']['key'] for row in records)))
        outcomes = tuple(CategoryOutcome(key, 'fresh', 1) if key == 'books' and records else
                         CategoryOutcome(key, 'unavailable', 0, 'CAPABILITY_UNAVAILABLE') for key in registry.categories)
        if self.mode == 'incomplete':
            outcomes = outcomes[:-1]
        self.trace.append(('seal', tuple(row.category for row in outcomes)))
        return CollectionSeal('isolated-seal', plan.plan_ref, plan.bindings, outcomes,
            canonical_bytes({'records': records, 'observations': {'observation:1': {'revision': 7, 'digest': 'c' * 64}},
                             'policies': {'policy:1': {'revision': 3}}}),
            canonical_bytes({'synthetic-source': '1'}), self.case.expires)


class SyntheticHostPorts:
    def __init__(self, trace):
        self.trace, self.reserved, self.mode = trace, False, 'valid'

    def revalidate(self, intent, plan, seal, body):
        self.trace.append(('check', intent.bindings.profile_ref))

    def reserve_attempt(self, intent, seal, body):
        if self.reserved:
            raise EnhanceFault('UNKNOWN_RECONCILIATION_REQUIRED')
        self.reserved = True
        self.trace.append(('reserve', intent.client_key))
        return object()  # Explicit isolated host double, not a product capability.

    def dispatch(self, reservation, body):
        self.trace.append(('infer', None))
        if self.mode == 'unknown':
            raise RuntimeError('PRIVATE-POST-SEND-CANARY')
        if self.mode == 'malformed':
            return b'{"invented":"PRIVATE-MODEL-CANARY"}'
        submitted = read_json(read_json(body)['input'].encode())
        signals = submitted['profile']['signals']
        graph = None if not signals else {'basis': 'inferred_taste_association', 'clusters': [
            {'label': 'Synthetic only', 'members': [signals[0]['key']], 'explanation': 'A tentative association.'}], 'links': []}
        rows = [{'candidate_key': row['key'], 'decision': 'uncertain', 'signal_keys': [],
                 'evidence_refs': row['source_roots'], 'hypothesis': 'Isolated technical assessment, not a recommendation.'}
                for row in submitted['candidates']]
        return canonical_bytes({'schema': 'cabinet-inference-result/v1', 'associations': graph, 'candidate_assessments': rows})


class EnhanceCoordinatorTests(SnapshotCase):
    def setUp(self):
        super().setUp()
        self.consent(self.profile_ref, purpose='catalogue_lookup', classes=self.classes)
        self.event(choice='intrigued', note='DENIED-EXACT-NOTE-CANARY')
        self.registry = RegistryView('1', ('books', 'films', 'tv', 'music', 'events', 'outings', 'podcasts'))
        self.trace = []
        self.collector = SyntheticCollector(self, self.trace)
        self.host = SyntheticHostPorts(self.trace)
        self.stages = []
        self.coordinator = EnhanceCoordinator(self.collector, self.host, now=lambda: self.clock.wall,
                                              on_stage=lambda event: self.stages.append(event['stage']))

    def planned(self, coordinator=None):
        view = ga.current_view(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), purpose='catalogue_lookup')
        return (coordinator or self.coordinator).plan(view, self.registry,
            run_ref='server-run', client_key='server-intent-key', refresh_nonce='server-refresh')

    def collected(self):
        planned = self.planned()
        return self.coordinator.collect(planned, approved_query_digest=planned.plan.body_digest)

    def previewed(self):
        return self.coordinator.preview(self.collected(), self.view(self.profile_ref))

    def test_initial_all_category_collection_enrichment_and_seal_precede_inference(self):
        planned = self.planned()
        self.assertFalse(any(row[0] in ('collect', 'infer') for row in self.trace))
        collected = self.coordinator.collect(planned, approved_query_digest=planned.plan.body_digest)
        self.assertIsInstance(collected, CollectedEnhance)
        self.assertEqual(next(row[1] for row in self.trace if row[0] == 'collect'), self.registry.categories)
        self.assertEqual({row.category for row in collected.seal.outcomes}, set(self.registry.categories))
        preview = self.coordinator.preview(collected, self.view(self.profile_ref))
        self.assertNotIn(b'DENIED-EXACT-NOTE-CANARY', planned.plan.body)
        self.assertNotIn(b'DENIED-EXACT-NOTE-CANARY', preview.body)
        self.assertFalse(any(row[0] == 'infer' for row in self.trace))
        outcome = self.coordinator.infer(preview, approved_body_digest=preview.body_digest)
        self.assertEqual(outcome.event.stage, 'validated')
        order = [row[0] for row in self.trace]
        self.assertLess(order.index('collect'), order.index('enrich'))
        self.assertLess(order.index('enrich'), order.index('seal'))
        self.assertLess(order.index('seal'), order.index('reserve'))
        self.assertLess(order.index('reserve'), order.index('infer'))
        self.assertIn('publication_cas', outcome.remaining_integration)
        self.assertFalse(outcome.event.public()['published'])

    def test_partial_unsupported_empty_and_incomplete_coverage_remain_truthful(self):
        collected = self.collected()
        self.assertEqual([row.category for row in collected.seal.outcomes if row.state == 'fresh'], ['books'])
        self.assertTrue(all(row.code == 'CAPABILITY_UNAVAILABLE' for row in collected.seal.outcomes if row.category != 'books'))
        self.collector.mode = 'empty'
        empty = self.collected()
        self.assertEqual((empty.event.stage, empty.event.code), ('no_candidates', 'NO_GROUNDED_CANDIDATES'))
        self.assertEqual(len(empty.event.outcomes), len(self.registry.categories))
        self.collector.mode = 'incomplete'
        incomplete = self.collected()
        self.assertEqual(incomplete.event.code, 'CONTRACT_INVALID')
        self.assertFalse(any(row[0] == 'infer' for row in self.trace))

    def test_query_confirmation_and_missing_durable_host_block_before_any_api(self):
        planned = self.planned()
        refused = self.coordinator.collect(planned, approved_query_digest='changed-body')
        self.assertEqual(refused.event.stage, 'stale')
        unwired = EnhanceCoordinator(self.collector, now=lambda: self.clock.wall)
        planned = self.planned(unwired)
        unavailable = unwired.collect(planned, approved_query_digest=planned.plan.body_digest)
        self.assertEqual(unavailable.event.code, 'CAPABILITY_UNAVAILABLE')
        self.assertFalse(any(row[0] in ('collect', 'reserve', 'infer') for row in self.trace))

    def test_changed_profile_or_confirmation_cannot_reserve_or_send(self):
        preview = self.previewed()
        wrong = self.coordinator.infer(preview, approved_body_digest='different-preview')
        self.assertEqual(wrong.event.stage, 'stale')
        self.history.history[0]['note'] = 'Changed denied note invalidates full local revision'
        self.history.save(update_fields=['history'])
        stale = self.coordinator.infer(preview, approved_body_digest=preview.body_digest)
        self.assertEqual(stale.event.code, 'PREVIEW_STALE')
        self.assertFalse(any(row[0] in ('reserve', 'infer') for row in self.trace))
        current = self.previewed()
        original_reserve = self.host.reserve_attempt
        def racing_reservation(intent, seal, body):
            reserved = original_reserve(intent, seal, body)
            self.history.history[0]['note'] = 'Changed during durable reservation'
            self.history.save(update_fields=['history'])
            return reserved
        self.host.reserve_attempt = racing_reservation
        raced = self.coordinator.infer(current, approved_body_digest=current.body_digest)
        self.assertEqual(raced.event.code, 'PREVIEW_STALE')
        self.assertEqual(sum(row[0] == 'reserve' for row in self.trace), 1)
        self.assertFalse(any(row[0] == 'infer' for row in self.trace))

    def test_source_failure_and_malformed_result_return_fixed_codes_without_fabrication(self):
        self.collector.mode = 'error'
        failed = self.collected()
        self.assertEqual((failed.event.stage, failed.event.code), ('failed', 'SOURCE_UNAVAILABLE'))
        self.assertEqual({row.category for row in failed.event.outcomes}, set(self.registry.categories))
        self.assertNotIn('PRIVATE', json.dumps(failed.event.public()))
        self.collector.mode = 'partial'
        preview = self.previewed()
        self.host.mode = 'malformed'
        invalid = self.coordinator.infer(preview, approved_body_digest=preview.body_digest)
        self.assertEqual((invalid.event.stage, invalid.event.code), ('failed', 'CONTRACT_INVALID'))
        self.assertIsNone(invalid.validated_bytes)
        self.assertNotIn('PRIVATE', json.dumps(invalid.event.public()))

    def test_unknown_handoff_stays_unknown_and_host_prevents_a_second_send(self):
        preview = self.previewed()
        self.host.mode = 'unknown'
        first = self.coordinator.infer(preview, approved_body_digest=preview.body_digest)
        second = self.coordinator.infer(preview, approved_body_digest=preview.body_digest)
        self.assertEqual(first.event.stage, 'unknown')
        self.assertEqual(second.event.stage, 'unknown')
        self.assertEqual(first.event.code, 'UNKNOWN_RECONCILIATION_REQUIRED')
        self.assertEqual(sum(row[0] == 'infer' for row in self.trace), 1)
        self.assertEqual(sum(row[0] == 'reserve' for row in self.trace), 1)
        self.assertNotIn('PRIVATE', json.dumps(first.event.public()))


class SourcePlanMapTests(SnapshotCase):
    def test_server_plan_retains_each_view_and_does_not_import_a_client_map(self):
        import uuid
        from shelf.category_registry import registry_view
        self.consent(self.profile_ref, purpose='catalogue_lookup')
        view = ga.current_view(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), purpose='catalogue_lookup')
        planned = EnhanceCoordinator(None, now=self.authority.now).plan(view, registry_view(),
            run_ref=str(uuid.uuid4()), client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()),
            source_views=(view,))
        self.assertEqual(planned.source_views, (view,))
        self.assertEqual(dict(planned.source_plans)[view.connection_ref].body, planned.plan.body)
        self.assertEqual(dict(planned.source_plans)[view.connection_ref].bindings, planned.plan.bindings)
