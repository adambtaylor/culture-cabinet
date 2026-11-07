"""Real owner/grant/source issuer; only bounded source I/O is a synthetic double."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from shelf import grant_authority as ga
from shelf.category_registry import registry_view, fixed_moshtix_configuration
from shelf.disclosure import canonical_bytes
from shelf.enhance_contract import CollectionBudget, EnhanceFault
from shelf.enhance_coordinator import EnhanceCoordinator
from shelf.discovery_transport import MoshtixHostLimiter
from shelf.models import PortableConnection, SynthesisRun
from shelf.source_adapters import MoshtixBackend, SourceRequest
from shelf.source_authority import (SourceAuthority, SourcePolicyProvider, SourceRights,
                                    configure_source, current_binding)
from shelf.source_collection import SourceCollection
from shelf.source_evidence import MOSHTIX_POLICY_REF, MOSHTIX_FACTS
from .test_grant_authority import AuthorityCase
from .test_source_evidence import policy


class RightsFixture(SourcePolicyProvider):
    def __init__(self):
        self.value = SourceRights(replace(policy(facts=tuple(sorted(MOSHTIX_FACTS))),
            ref=MOSHTIX_POLICY_REF), 1, 'synthetic-terms', 'synthetic-privacy',
            'synthetic-owner-VIC-consent', 'CultureCabinet/fixture (example.invalid)', 'synthetic-processor')

    def current(self, authority, session_ref, **kwargs):
        return self.value


class FixtureBackend(MoshtixBackend):
    isolated_fixture = True


class SourceAuthorityTests(AuthorityCase):
    def test_moshtix_material_origin_survives_preview_confirm_and_authorize(self):
        preview = self.approved()
        operation = self.issuer.operation_for(self.request, self.query, now=self.authority.now())
        permit = self.issuer.authorize(self.request, self.query, operation, now=self.authority.now())
        self.assertEqual(preview['destination'], 'https://api.moshtix.com')
        self.assertEqual(permit.origin, operation.material['origin'])

    def test_derived_proposals_obey_retention_and_expire_or_invalidate(self):
        from shelf.source_adapters import SourceReply
        from shelf.disclosure import read_json
        request = SourceRequest('hardcover', 'book', 'book_search', ('Synthetic',))
        now = self.authority.now()
        permit = SimpleNamespace(expires_at=now + timedelta(seconds=300))
        approval = SimpleNamespace(request=request, plan=self.query, rights=self.rights.value,
            operation=object(), expires_at=permit.expires_at)
        self.issuer._permits[id(permit)] = (permit, approval)
        reply = SourceReply(request.request_digest, 200,
            canonical_bytes({'data': {'search': {'ids': [81001], 'error': None}}}), now)
        # Isolate retention from owner/history fixtures; normal authority checks
        # remain covered by the existing confirmation/revocation regressions.
        with patch.object(self.issuer, '_check', return_value=True), \
             patch.object(self.issuer, 'revalidate', return_value=True):
            for denied in ('cache', 'retain'):
                approval.rights = replace(self.rights.value, policy=replace(self.rights.value.policy,
                    permissions=tuple((key, tuple(use for use in uses if use != denied))
                                      for key, uses in self.rights.value.policy.permissions)))
                self.assertIsNone(self.issuer.remember_details(permit, reply))
                self.assertEqual(self.issuer._derived, {})
            approval.rights = self.rights.value
            self.assertIsNone(self.issuer.remember_details(permit, replace(reply, headers=(('cache-control', 'no-store'),))))
            self.assertIsNone(self.issuer.remember_details(permit, replace(reply, headers=(('cache-control', 'max-age=0'),))))
            detail = self.issuer.remember_details(permit, reply)
            self.assertEqual(detail.ids, (81001,))
            self.clock.advance(301)
            self.assertEqual(self.issuer.pending_details(self.query.plan_ref), ())
            self.assertEqual(self.issuer._derived, {})

    def test_media_identity_cannot_borrow_moshtix_rights_or_inference_purpose(self):
        from shelf.disclosure import read_json
        from shelf.enhance_contract import digest
        from shelf.provider_catalogue import BOOK_SEARCH, BOOK_DETAILS
        from shelf.source_authority import source_identity

        # The profile revision hashes this full template with the strict JSON codec.
        # Keep the tuple API for callers, but bind every component on the JSON wire.
        expected = (
            ('tmdb_movie', ['tmdb-movie-fixed-v1', 'https://api.themoviedb.org',
                            '/3/search/movie'], ['search_movie'], []),
            ('tmdb_tv', ['tmdb-tv-fixed-v1', 'https://api.themoviedb.org',
                         '/3/search/tv'], ['search_tv'], []),
            ('hardcover', ['hardcover-fixed-v1', 'https://api.hardcover.app',
                           '/v1/graphql'], ['book_search', 'book_details'],
             [BOOK_SEARCH, BOOK_DETAILS]),
        )
        for source, identity, operations, documents in expected:
            with self.subTest(source=source), patch.object(self.issuer, 'source_key', return_value=source):
                self.assertIs(type(source_identity(source)), tuple)
                self.assertEqual(source_identity(source), tuple(identity))
                template = self.issuer.operation_template()
                encoded = canonical_bytes(template)
                self.assertEqual(read_json(encoded), {'source': source, 'identity': identity,
                    'operations': operations, 'documents': documents})
                bound_digest = digest(encoded)
                for field in ('identity', 'operations', 'documents'):
                    for index in range(len(template[field])):
                        changed = read_json(encoded)
                        changed[field][index] += '-changed'
                        self.assertNotEqual(digest(canonical_bytes(changed)), bound_digest)
                changed = read_json(encoded)
                changed['source'] += '-changed'
                self.assertNotEqual(digest(canonical_bytes(changed)), bound_digest)

        vault = self.authority._source_vault
        row = vault.configure(self.ref, {'adapter': 'tmdb_movie', 'model': '', 'reasoning': ''})
        vault.replace(self.ref, row.pk, row.config_revision, b'SYNTHETIC-SOURCE')
        ga.change_grant(self.authority, self.ref, profile_ref=self.profile_ref,
            expected_revision=0, purpose='catalogue_lookup', connection_ref=str(row.pk),
            allowed_data_classes=['canonical_seed'], expires_at=self.expires, action='grant')
        issuer = SourceAuthority(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(row.pk), policy_provider=self.rights)
        self.assertEqual(issuer._view().context['adapter_id'], 'tmdb_movie')
        with self.assertRaises(EnhanceFault):
            issuer._rights(self.authority.now())
        self.rights.value = replace(self.rights.value, source='tmdb_movie')
        self.assertEqual(issuer._rights(self.authority.now()).source, 'tmdb_movie')
        self.fault('AUTHORITY_DENIED', ga.current_view, self.authority, self.ref,
            profile_ref=self.profile_ref, connection_ref=str(row.pk), purpose='inference')

    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.source_ref = configure_source(self.authority, self.ref, confirmed=True)
        ga.change_grant(self.authority, self.ref, profile_ref=self.profile_ref,
            expected_revision=0, purpose='catalogue_lookup', connection_ref=self.source_ref,
            allowed_data_classes=['canonical_seed'], expires_at=self.expires, action='grant')
        self.rights = RightsFixture()
        self.issuer = self.new_issuer()
        self.registry = registry_view()
        self.query = EnhanceCoordinator(None, now=self.authority.now).plan(
            current_binding(self.authority, self.ref, profile_ref=self.profile_ref, connection_ref=self.source_ref),
            self.registry, run_ref=str(uuid.uuid4()), client_key=str(uuid.uuid4()),
            refresh_nonce=str(uuid.uuid4())).plan
        self.request = SourceRequest('moshtix', 'event', 'events')

    def new_issuer(self):
        return SourceAuthority(self.authority, self.ref, profile_ref=self.profile_ref,
                               connection_ref=self.source_ref, policy_provider=self.rights)

    def approved(self):
        preview = self.issuer.preview(self.query)
        self.issuer.confirm(preview['preview_ref'], plan_digest=preview['plan_digest'],
            operation_digest=preview['operation_digest'], confirmed=True)
        return preview

    def test_dedicated_no_credential_configuration_is_explicit_idempotent_and_purpose_bound(self):
        self.assertEqual(configure_source(self.authority, self.ref, confirmed=True), self.source_ref)
        self.fault('CONTRACT_INVALID', configure_source, self.authority, self.ref, confirmed=False)
        row = PortableConnection.objects.get(pk=self.source_ref)
        self.assertEqual((row.adapter, row.model, row.reasoning, row.credential_generation),
                         ('moshtix_public', '', '', 0))
        self.fault('AUTHORITY_DENIED', ga.current_view, self.authority, self.ref,
            profile_ref=self.profile_ref, connection_ref=self.source_ref, purpose='inference')
        self.fault('AUTHORITY_DENIED', ga.change_grant, self.authority, self.ref,
            profile_ref=self.profile_ref, connection_ref=self.source_ref, purpose='inference',
            expected_revision=0, allowed_data_classes=['canonical_seed'], expires_at=self.expires, action='grant')
        self.assertEqual(self.fake.calls, 1)  # Only the preexisting neutral fixture probe.

    def test_exact_operation_requires_confirmation_of_both_plan_and_wire_digest(self):
        with self.assertRaises(EnhanceFault):
            self.issuer.operation_for(self.request, self.query, now=self.authority.now())
        p = self.issuer.preview(self.query)
        for overrides in ({'confirmed': False}, {'plan_digest': '0' * 64}, {'operation_digest': '0' * 64}):
            arguments = dict(plan_digest=p['plan_digest'], operation_digest=p['operation_digest'], confirmed=True)
            arguments.update(overrides)
            with self.assertRaises(EnhanceFault):
                self.issuer.confirm(p['preview_ref'], **arguments)
        self.issuer.confirm(p['preview_ref'], plan_digest=p['plan_digest'], operation_digest=p['operation_digest'], confirmed=True)
        operation = self.issuer.operation_for(self.request, self.query, now=self.authority.now())
        self.assertEqual(operation.body, p['body'].encode())
        self.assertEqual(operation.material['origin'], 'https://api.moshtix.com')
        self.assertNotIn(self.profile_ref.encode(), operation.body)
        self.assertNotIn(self.ref.encode(), operation.body)
        self.assertNotIn(b'SYNTHETIC-CONNECTION-CANARY-KEY', operation.body)
        with self.assertRaises(EnhanceFault):
            self.issuer.operation_for(self.request, replace(self.query, plan_ref=str(uuid.uuid4())), now=self.authority.now())

    def test_real_source_backend_seals_all_categories_before_any_inference_reservation(self):
        preview = self.approved()
        clock = SimpleNamespace(now=self.authority.now, monotonic=lambda: self.clock.tick, sleep=self.clock.advance)
        backend = FixtureBackend(self.issuer, MoshtixHostLimiter(), clock=clock)
        collector = SourceCollection(fixed_moshtix_configuration(self.registry), {'moshtix': backend},
                                     clock=clock, isolated_fixture=True)
        body = canonical_bytes({'data': {'viewer': {'getEvents': {'totalCount': 1, 'items': [
            {'id': 81001, 'name': 'Synthetic event', 'startDate': (self.clock.wall + timedelta(days=1)).isoformat(),
             'venue': {'id': 81002, 'name': 'Synthetic venue', 'address': {'locality': 'Synthetic locality', 'region': 'VIC'}}}
        ]}}}})
        def send(operation, *, revalidate, **bounds):
            self.assertTrue(revalidate())
            self.assertEqual(operation.body, preview['body'].encode())
            self.assertEqual(bounds, {'response_bytes': 131072, 'request_seconds': 5, 'parent_seconds': 6})
            self.assertTrue(revalidate())
            return 200, body, (('cache-control', 'max-age=300'),)
        with patch('shelf.discovery_transport._MOSHTIX_RATE_FILE', Path(self.directory.name) / 'rate'), \
             patch('shelf.discovery_transport.events_authorized', side_effect=send) as sent:
            seal = collector.collect(self.query, self.registry, CollectionBudget())
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(len(seal.outcomes), 13)
        self.assertEqual([v.category for v in seal.outcomes if v.state == 'fresh'], ['event'])
        self.assertEqual(len(seal.records), 1)
        self.assertEqual(seal.records[0]['candidate']['evidence_type'], 'fixture_not_live')
        self.assertEqual(collector.state.cache, {})
        self.assertTrue(collector.revalidate_collection(seal, self.query, self.registry))
        self.assertFalse(SynthesisRun.objects.filter(attempts=1).exists())

    def test_rights_session_config_and_grant_changes_cannot_reuse_approval(self):
        self.approved()
        op = self.issuer.operation_for(self.request, self.query, now=self.authority.now())
        permit = self.issuer.authorize(self.request, self.query, op, now=self.authority.now())
        self.assertTrue(self.issuer.revalidate(permit, self.request, self.query, op, now=self.authority.now()))
        # One combined lookup validates once; calling either legacy entrypoint
        # separately must still perform a new full check, not reuse that result.
        now = self.authority.now()
        with patch.object(self.issuer, '_check', wraps=self.issuer._check) as checked:
            self.assertEqual(self.issuer.policy_context(permit, op, now=now),
                             (self.rights.value.policy, True))
            self.assertEqual(checked.call_count, 1)
            self.assertIs(self.issuer.policy(permit, op, now=now), self.rights.value.policy)
            self.assertTrue(self.issuer.model_context_current(permit, op, now=now))
            self.assertEqual(checked.call_count, 3)
        with self.assertRaises(EnhanceFault):
            self.issuer.policy_context(replace(permit), op, now=now)
        with self.assertRaises(EnhanceFault):
            self.issuer.policy_context(permit, object(), now=now)
        with self.assertRaises(EnhanceFault):
            self.issuer.policy_context(permit, op, now=now + timedelta(minutes=11))

        saved = self.issuer._permits[id(permit)][1]
        from shelf.enhance_contract import digest
        changed_body = saved.plan.body + b' '
        changed_binding = dict(saved.view.connection_binding)
        changed_binding['config_revision'] += 1
        stale_approvals = (
            replace(saved, request=SourceRequest('tmdb_movie', 'movie', 'search_movie', ('Other',))),
            replace(saved, plan=replace(saved.plan, plan_ref=str(uuid.uuid4()))),
            replace(saved, plan=replace(saved.plan, body=changed_body, body_digest=digest(changed_body))),
            replace(saved, view=replace(saved.view, actor_ref='foreign-owner')),
            replace(saved, view=replace(saved.view, profile_ref=str(uuid.uuid4()))),
            replace(saved, view=replace(saved.view, _session='unknown-session')),
            replace(saved, view=replace(saved.view, policy_epoch=saved.view.policy_epoch + 1)),
            replace(saved, view=replace(saved.view, profile_revision_digest='0' * 64)),
            replace(saved, view=replace(saved.view, _connection_binding=canonical_bytes(changed_binding))),
        )
        for index, stale in enumerate(stale_approvals):
            with self.subTest(binding=index), \
                 patch.dict(self.issuer._permits, {id(permit): (permit, stale)}), \
                 self.assertRaises((EnhanceFault, ga.AuthorityFault)):
                self.issuer.policy_context(permit, op, now=now)
        # Even matching saved/current rights cannot grant a missing processor.
        missing_processor = replace(saved.rights, processor_ref=None)
        with patch.object(self.rights, 'value', missing_processor), \
             patch.dict(self.issuer._permits, {id(permit): (permit, replace(saved, rights=missing_processor))}), \
             self.assertRaises(EnhanceFault):
            self.issuer.policy_context(permit, op, now=now)
        expired_rights = replace(saved.rights,
            policy=replace(saved.rights.policy, checked_at=now - timedelta(seconds=2),
                           expires_at=now - timedelta(seconds=1)))
        with patch.object(self.rights, 'value', expired_rights), \
             patch.dict(self.issuer._permits, {id(permit): (permit, replace(saved, rights=expired_rights))}), \
             self.assertRaises(EnhanceFault):
            self.issuer.policy_context(permit, op, now=now)
        from contextlib import contextmanager
        original_guard = ga.recovery_guard
        @contextmanager
        def deny_on_exit(*args, **kwargs):
            with original_guard(*args, **kwargs) as checkpoint:
                yield checkpoint
            raise ga.AuthorityFault('CAPABILITY_UNAVAILABLE', 503)
        with patch.object(ga, 'recovery_guard', deny_on_exit), self.assertRaises(ga.AuthorityFault):
            self.issuer.policy_context(permit, op, now=now)
        self.assertEqual(self.issuer.policy_context(permit, op, now=now),
                         (self.rights.value.policy, True))
        self.rights.value = replace(self.rights.value, revision=2)
        with self.assertRaises(EnhanceFault):
            self.issuer.revalidate(permit, self.request, self.query, op, now=self.authority.now())
        for method in (self.issuer.policy_context, self.issuer.policy, self.issuer.model_context_current):
            with self.assertRaises(EnhanceFault):
                method(permit, op, now=now)
        self.rights.value = replace(self.rights.value, revision=1)
        ga.change_grant(self.authority, self.ref, profile_ref=self.profile_ref,
            expected_revision=1, purpose='catalogue_lookup', connection_ref=self.source_ref,
            allowed_data_classes=['canonical_seed'], expires_at=self.expires, action='revoke')
        with self.assertRaises(ga.AuthorityFault):
            self.issuer.revalidate(permit, self.request, self.query, op, now=self.authority.now())
        with self.assertRaises(ga.AuthorityFault):
            self.issuer.policy_context(permit, op, now=now)

    def test_absent_policy_provider_or_new_issuer_cannot_mint_or_reconstruct_authority(self):
        unconfigured = SourceAuthority(self.authority, self.ref, profile_ref=self.profile_ref,
                                       connection_ref=self.source_ref)
        with self.assertRaises(EnhanceFault):
            unconfigured.preview(self.query)
        p = self.approved()
        other = self.new_issuer()
        with self.assertRaises(EnhanceFault):
            other.confirm(p['preview_ref'], plan_digest=p['plan_digest'],
                          operation_digest=p['operation_digest'], confirmed=True)
        self.clock.advance(301)
        with self.assertRaises(EnhanceFault):
            self.issuer.operation_for(self.request, self.query, now=self.authority.now())


    def test_concrete_source_only_service_uses_primary_inference_binding_and_never_reserves_model(self):
        from shelf.current_profile import CurrentProfile
        from shelf.portable_enhance_service import EnhanceService
        from shelf.models import ProfileCabinetState
        state = ProfileCabinetState.objects.get()
        ga.bind_existing_profile(self.authority, self.ref, profile_id=state.profile_id, confirmed=True)
        self.consent(self.profile_ref)
        clock = SimpleNamespace(now=self.authority.now, monotonic=lambda: self.clock.tick, sleep=self.clock.advance)
        backend = FixtureBackend(self.issuer, MoshtixHostLimiter(), clock=clock)
        collector = SourceCollection(fixed_moshtix_configuration(self.registry), {'moshtix': backend},
            clock=clock, isolated_fixture=True)
        provider = CurrentProfile(self.authority, sources=(self.issuer,))
        service = EnhanceService(self.authority, self.service, catalogue=collector,
            registry=self.registry, state_provider=provider)
        ready = service.readiness(self.ref, profile_ref=self.profile_ref, connection_ref=str(self.config.pk))
        self.assertTrue(ready['available']); self.assertFalse(ready['run_ready'])
        run = service.start(self.ref, profile_ref=self.profile_ref, connection_ref=str(self.config.pk),
            client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
        preview = service.preview(self.ref, run_ref=run['run_ref'])
        wire = preview['disclosure']
        self.assertEqual(wire['origin'], 'https://api.moshtix.com')
        self.assertNotIn(self.profile_ref, wire['body'])
        def send(operation, *, revalidate, **bounds):
            self.assertTrue(revalidate()); self.assertEqual(operation.body, wire['body'].encode())
            return 200, canonical_bytes({'data': {'viewer': {'getEvents': {'totalCount': 1, 'items': [
                {'id': 81001, 'name': 'Synthetic concrete event',
                 'startDate': (self.clock.wall + timedelta(days=1)).isoformat(),
                 'venue': {'id': 81002, 'name': 'Synthetic venue',
                           'address': {'locality': 'Synthetic locality', 'region': 'VIC'}}}]}}}}), ()
        with patch('shelf.discovery_transport._MOSHTIX_RATE_FILE', Path(self.directory.name) / 'concrete-rate'), \
             patch('shelf.discovery_transport.events_authorized', side_effect=send) as sent:
            result = service.confirm(self.ref, run_ref=run['run_ref'], preview_ref=wire['preview_ref'],
                body_digest=wire['body_digest'], confirmed=True)
        self.assertEqual(result['stage'], 'sealed')
        self.assertEqual(sent.call_count, 1)
        self.assertNotIn('confirm', result['actions'])
        row = SynthesisRun.objects.get(pk=run['run_ref'])
        self.assertEqual(row.attempts, 0)
        self.assertEqual(str(row.portable_connection_id), str(self.config.pk))
        self.assertNotEqual(row.portable_bindings['bindings']['connection_digest'],
            self.issuer._view().connection_binding['config_digest'])
