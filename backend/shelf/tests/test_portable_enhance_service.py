"""Isolated service composition; real authority/store/codecs, synthetic producers.

The pipeline double supplies the unleased portable-bookkeeping snapshot adapter.
It does not register a normal capability or substitute for runtime acceptance.
"""
import uuid

from django.test import SimpleTestCase

from shelf import grant_authority as ga, inference_body, profile_snapshot
from shelf.disclosure import SCALARS, canonical_bytes, read_json
from shelf.enhance_contract import (CataloguePlan, CollectionSeal, CategoryOutcome,
    EnhanceIntent, StageEvent, digest)
from shelf.enhance_coordinator import EnhanceCoordinator, PlannedEnhance, CollectedEnhance, InferencePreview, EnhanceOutcome
from shelf.models import PortableCabinetManifest, SynthesisRun
from shelf.portable_enhance_service import EnhanceService, get_service
from .test_portable_cabinet_store import StoreCase, result
from .test_disclosure import source, resign


class FixtureCatalogue:
    def __init__(self, case):
        self.case, self.calls, self.on_collect = case, 0, None
        self.denied_categories, self.extra_films, self.last_seal = set(), False, None

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
        records = [record]
        if self.extra_films:
            film = read_json(canonical_bytes(record))
            film['record_ref'] += ':film'
            film['candidate'].update(key='fixture:film:2', category='films', title='Fictional denied film')
            film['candidate']['rights_ref'] = film['record_ref']
            for binding in film['fields']:
                resign(film, binding)
            records.append(film)
        for item in records:
            if item['candidate']['category'] in self.denied_categories:
                for binding in item['fields']:
                    binding['rights']['disposition'] = 'unknown'
        fresh = {item['candidate']['category'] for item in records}
        outcomes = tuple(CategoryOutcome(key, 'fresh', 1) if key in fresh else
            CategoryOutcome(key, 'unavailable', 0, 'CAPABILITY_UNAVAILABLE') for key in registry.categories)
        self.last_seal = CollectionSeal('fixture-collection', plan.plan_ref, plan.bindings, outcomes,
            canonical_bytes({'records': records, 'observations': {'observation:1': {'revision': 7, 'digest': 'c' * 64}},
                             'policies': {'policy:1': {'revision': 3}}}), b'{}', self.case.expires)
        return self.last_seal

    def model_eligible_keys(self, seal, plan, registry, *, source_plans=None):
        # Isolated producer custody only; production uses Catalogue's real seal
        # revalidation port, never caller-supplied eligibility flags.
        self.case.assertIs(seal, self.last_seal)
        seal.verify(plan, registry, now=self.case.clock.wall)
        return frozenset(record['candidate']['key'] for record in seal.records
            if all(any(binding['target'] == target and binding['rights']['disposition'] == 'allowed'
                       for binding in record['fields']) for target in SCALARS))


class FixtureTransport:
    def __init__(self):
        self.calls, self.fail = 0, False
        self.bodies = []

    def dispatch(self, reservation, body):
        self.calls += 1
        self.bodies.append(body)
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
    # The isolated pipeline must enforce the same final authority gate as the
    # real coordinator; publication cannot inherit a fixture-only bypass.
    _host_check = EnhanceCoordinator._host_check

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

        # Exercise the final publication gate through the real supplied ports.
        # Earlier plan/infer checks remain current; only the final check sees a
        # changed binding or a missing owner session, so a no-op would publish.
        from dataclasses import replace
        from unittest.mock import patch
        for changed in ('stale', 'denied'):
            pending = self.confirm_run(self.start_run())
            memory = self.enhance._memory[pending['run_ref']]
            pipeline, work = memory['coordinator'], memory['work']
            store = pipeline.ports.store
            current = store.current()
            host_check = pipeline._host_check
            def final_check(intent, plan, seal=None, body=None):
                self.assertIs(intent, work.collected.planned.intent)
                self.assertIs(plan, work.collected.planned.plan)
                self.assertIs(seal, work.collected.seal)
                self.assertEqual(body, work.body)
                if changed == 'stale':
                    stale = replace(current, bindings=replace(current.bindings, snapshot_revision='0' * 64))
                    with patch.object(store, 'current', return_value=stale):
                        return host_check(intent, plan, seal, body)
                with patch.object(store, 'session_ref', 'unknown-isolated-session'):
                    return host_check(intent, plan, seal, body)
            with patch.object(pipeline, '_host_check', side_effect=final_check):
                self.fault('PREVIEW_STALE' if changed == 'stale' else 'UNAUTHENTICATED',
                           self.confirm_run, pending)
            self.assertEqual(self.enhance.current(self.ref, profile_ref=self.profile_ref)['publication_revision'], 1)
            self.assertEqual(PortableCabinetManifest.objects.count(), 1)

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


class PreparatoryOutcomeTests(__import__('unittest').TestCase):
    def prepare(self, state, code, sends, *, derived=False):
        from types import SimpleNamespace as N
        from unittest.mock import patch
        from shelf.source_adapters import SourceBackend
        from shelf.source_collection import SourceCollection
        from shelf.enhance_contract import CollectionBudget, RegistryView
        class Backend(SourceBackend):
            isolated_fixture = False
            def authorize(self, *a, **kw): raise AssertionError('no resend')
            def revalidate(self, *a, **kw): raise AssertionError('no resend')
            def policy(self, *a, **kw): raise AssertionError('no resend')
            def request(self, *a, **kw): raise AssertionError('no resend')
        book = Backend(); book.issuer = N(connection_ref='book-connection', pending_details=lambda ref:('issuer-authenticated-detail-request',) if derived else ())
        clock = N(now=lambda:None, monotonic=lambda:0)
        catalogue = SourceCollection((('book', ('hardcover',)),), {'hardcover':book}, clock=clock)
        planned = N(plan=N(plan_ref='original'), registry=RegistryView('r', ('book',)), source_plans=(('book-connection',object()),))
        coordinator = N(catalogue=catalogue, budget=CollectionBudget())
        memory = dict(work=planned, coordinator=coordinator)
        outcome = CategoryOutcome('book', state, 0, code)
        seal = N(outcomes=(outcome,), source_revisions=canonical_bytes({'_collection':{'sends':sends,'response_bytes':0}}))
        with patch.object(SourceCollection, 'collect', return_value=seal) as collect:
            self.assertFalse(EnhanceService._prepare_details(object(), None, None, memory))
            collect.assert_called_once()
        actual = EnhanceService._settled_outcomes(memory, (CategoryOutcome('book','unavailable',0,'CAPABILITY_UNAVAILABLE'),))
        self.assertEqual(actual, (outcome,))
        self.assertIs(memory['preparatory'][1], seal)
        self.assertNotIn('hardcover', coordinator.catalogue.configured['book'])
        return memory

    def test_empty_search_retains_empty_success_and_no_detail_dependency(self):
        self.assertIsNone(self.prepare('empty_bounded', 'OK', 1)['detail_dependency'])

    def test_unretained_detail_preserves_denial_and_specific_dependency(self):
        self.assertEqual(self.prepare('unavailable', 'AUTHORITY_DENIED', 1, derived=True)['detail_dependency'],
                         'exact_hardcover_detail_confirmation_unavailable')

    def test_search_http403_without_derived_ids_retains_denial_without_detail_claim(self):
        # The collector maps search HTTP403 to this same aggregate denial;
        # only issuer-derived requests distinguish the later detail handoff.
        memory = self.prepare('unavailable', 'AUTHORITY_DENIED', 1)
        self.assertEqual(memory['preparatory_outcomes']['book'].code, 'AUTHORITY_DENIED')
        self.assertIsNone(memory['detail_dependency'])

    def test_source_failure_retains_its_failure_code_without_detail_claim(self):
        self.assertIsNone(self.prepare('failed', 'SOURCE_UNAVAILABLE', 1)['detail_dependency'])

    def test_collection_lock_is_acquired_before_owner_validation(self):
        import threading
        from contextlib import contextmanager
        from types import SimpleNamespace as N
        from shelf.source_collection import SourceCollection
        from shelf.portable_enhance_service import _DurablePorts
        catalogue = SourceCollection(); owner = threading.RLock()
        held, started, released, entered = [threading.Event() for _ in range(4)]
        binding = object()
        @contextmanager
        def scope():
            with owner:
                entered.set(); yield object()
        store = N(scope=scope, current=lambda:N(bindings=binding), provider=object(),
            _run=lambda *a:N(body_digest=''), _match=lambda *a:None)
        service = N(_memory={'run':{'coordinator':N(catalogue=catalogue)}},
                    _collection_lock=EnhanceService._collection_lock)
        failures = []
        def collect():
            try:
                with catalogue.state.lock:
                    held.set(); self.assertTrue(started.wait(1))
                    self.assertFalse(entered.wait(.05))
                    with owner: released.set()
            except BaseException as e: failures.append(type(e).__name__)
        def validate():
            try:
                self.assertTrue(held.wait(1)); started.set()
                _DurablePorts(service,store,'run').revalidate(N(bindings=binding),None,None,None)
            except BaseException as e: failures.append(type(e).__name__)
        threads=[threading.Thread(target=fn,daemon=True) for fn in (collect,validate)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertTrue(released.is_set()); self.assertTrue(entered.is_set())
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])


class SourceUseStageTests(ServiceCase):
    def test_unavailable_local_pool_keeps_actual_category_failures(self):
        from unittest.mock import patch
        def unavailable(planned, **kwargs):
            outcomes = tuple(CategoryOutcome(key, 'unavailable', 0, 'AUTHORITY_DENIED')
                             for key in self.registry.categories)
            event = StageEvent(planned.intent.run_ref, 'no_candidates', 'NO_GROUNDED_CANDIDATES', outcomes)
            return EnhanceOutcome(event, planned.events + (event,))
        started = self.start_run()
        with patch.object(self.pipeline, 'collect', side_effect=unavailable):
            status = self.confirm_run(started)
        self.assertEqual(status['stage'], 'no_candidates')
        row = SynthesisRun.objects.get(pk=started['run_ref'])
        self.assertEqual([item['category'] for item in row.evidence['outcomes']], list(self.registry.categories))
        self.assertTrue(all(item['state'] == 'unavailable' and item['code'] == 'AUTHORITY_DENIED'
                            for item in row.evidence['outcomes']))
        self.assertEqual(row.attempts, 0)
        self.assertEqual((self.transport.calls, PortableCabinetManifest.objects.count()), (0, 0))

    def test_local_collection_without_model_use_stays_sealed_without_attempt_or_publication(self):
        self.catalogue.denied_categories.add('books')
        started = self.start_run()
        query = self.enhance.preview(self.ref, run_ref=started['run_ref'])['disclosure']
        status = self.confirm_run(started)
        self.assertEqual((status['stage'], status['code'], status['dependency']),
                         ('sealed', 'AUTHORITY_DENIED', 'source_model_context_unavailable'))
        self.assertEqual(set(status['actions']), {'status', 'cancel'})
        books = next(item for item in status['source_use'] if item['category'] == 'books')
        self.assertEqual(books, {'category': 'books', 'collected_count': 1, 'model_eligible_count': 0})
        row = SynthesisRun.objects.get(pk=started['run_ref'])
        self.assertEqual((row.attempts, row.state, row.evidence['phase']),
                         (0, 'queued', 'source_model_context_unavailable'))
        self.assertEqual(row.collection_digest, self.catalogue.last_seal.collection_digest)
        self.assertEqual((self.transport.calls, PortableCabinetManifest.objects.count()), (0, 0))

        replay = self.enhance.confirm(self.ref, run_ref=started['run_ref'],
            preview_ref=query['preview_ref'], body_digest=query['body_digest'], confirmed=True)
        self.assertEqual(replay['dependency'], status['dependency'])
        self.assertEqual((self.catalogue.calls, self.transport.calls), (1, 0))

    def test_mixed_pool_keeps_denied_category_unavailable_and_exact_one_send_publication(self):
        self.catalogue.extra_films = True
        self.catalogue.denied_categories.add('films')
        inference = self.confirm_run(self.start_run())
        disclosure = self.enhance.preview(self.ref, run_ref=inference['run_ref'])['disclosure']
        submitted = read_json(read_json(disclosure['body'].encode())['input'].encode())
        self.assertEqual([card['key'] for card in submitted['candidates']], ['fixture:book:1'])
        self.assertNotIn('Fictional denied film', disclosure['body'])
        before = self.catalogue.last_seal.collection_digest
        published = self.confirm_run(inference)
        self.assertEqual(published['stage'], 'published')
        manifest = self.enhance.current(self.ref, profile_ref=self.profile_ref)['manifest']
        films = next(row for row in manifest['categories'] if row['category'] == 'films')
        self.assertEqual((films['state'], films['code'], films['cards']), ('unavailable', 'AUTHORITY_DENIED', []))
        self.assertEqual(self.catalogue.last_seal.collection_digest, before)
        self.assertEqual(next(row for row in self.catalogue.last_seal.outcomes if row.category == 'films').candidate_count, 1)
        self.assertEqual(self.transport.bodies, [disclosure['body'].encode()])
        self.assertEqual(PortableCabinetManifest.objects.count(), 1)
        self.enhance.confirm(self.ref, run_ref=inference['run_ref'], preview_ref=disclosure['preview_ref'],
            body_digest=disclosure['body_digest'], confirmed=True)
        self.assertEqual((self.transport.calls, PortableCabinetManifest.objects.count()), (1, 1))

    def test_missing_or_forged_producer_eligibility_never_reaches_inference(self):
        from unittest.mock import patch
        for answer in (None, frozenset({'foreign-source-key'})):
            with self.subTest(answer=answer):
                started = self.start_run()
                with patch.object(self.catalogue, 'model_eligible_keys',
                        **({'new': None} if answer is None else {'return_value': answer})):
                    self.fault('CAPABILITY_UNAVAILABLE' if answer is None else 'CONTRACT_INVALID',
                               self.confirm_run, started)
                row = SynthesisRun.objects.get(pk=started['run_ref'])
                self.assertEqual(row.attempts, 0)
        self.assertEqual((self.transport.calls, PortableCabinetManifest.objects.count()), (0, 0))


class SourceProducerPortTests(SimpleTestCase):
    def test_real_producer_authenticates_mixed_counts_and_rejects_revoked_seal(self):
        from dataclasses import replace
        from types import SimpleNamespace as N
        from unittest.mock import patch
        from .test_source_collection import Backend, Clock, plan
        from shelf.enhance_contract import CollectionBudget, EnhanceFault
        from shelf.source_collection import SourceCollection
        registry, query = plan(('movie', 'tv'))
        clock = Clock()
        movie = Backend('tmdb_movie', clock, [(200, {'results': [{'id': 81001, 'title': 'Synthetic movie'}]}, ())])
        tv = Backend('tmdb_tv', clock, [(200, {'results': [{'id': 81002, 'name': 'Synthetic series'}]}, ())])
        movie.rights = replace(movie.rights, permissions=tuple(
            (target, tuple(use for use in uses if use != 'model_context'))
            for target, uses in movie.rights.permissions))
        producer = SourceCollection((('movie', ('tmdb_movie',)), ('tv', ('tmdb_tv',))),
            {'tmdb_movie': movie, 'tmdb_tv': tv}, clock=clock, isolated_fixture=True)
        with patch('socket.socket.connect', side_effect=AssertionError('No provider calls')):
            seal = producer.collect(query, registry, CollectionBudget())
            service = object.__new__(EnhanceService)
            service.authority = N(now=clock.now)
            memory = {'coordinator': N(catalogue=producer)}
            collected = N(planned=N(plan=query, registry=registry, source_plans=()), seal=seal)
            summary = service._source_use(memory, collected)
            self.assertEqual(summary, [
                {'category': 'movie', 'collected_count': 1, 'model_eligible_count': 0},
                {'category': 'tv', 'collected_count': 1, 'model_eligible_count': 1}])
            movie.revoked = True
            with self.assertRaises(EnhanceFault):
                service._source_use(memory, collected)
