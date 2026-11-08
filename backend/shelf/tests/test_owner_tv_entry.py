"""Normal native owner-query callers; synthetic source/indexer I/O only."""
from dataclasses import replace
from dataclasses import asdict
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
import re
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db import DatabaseError

from shelf import grant_authority as ga, obtain_preview as preview
from shelf.acquisition_backend import PrivateSearch
from shelf.acquisition_contract import Blocked
from shelf.disclosure import SCALARS, canonical_bytes, read_json
from shelf.enhance_contract import EnhanceFault, digest
from shelf.models import (AcquisitionJob, FeedbackEvent, FeedbackProjection, PortableCabinetManifest,
    PortableConnection, Profile, ProfileCabinetState, SavedItem, SynthesisRun)
from shelf.portable_saved_authority import select_portable_profile, selected_context
from shelf.portable_owner_tv import (OwnerLookupForm, get_owner_tv, owner_backend,
                                     ObtainOriginSelectorForm)
from shelf.source_authority import SourcePolicyProvider, SourceRights
from shelf.source_evidence import FieldPolicy
from shelf.obtain_tv_flow import TVServiceProvider, DeliveryBinding, DestinationBinding, ServiceGeneration
from .test_grant_authority import RecoveryCase
from .test_portable_obtain import PortableObtainTests
from .test_obtain_preview import item, payload


class OwnerPolicy(SourcePolicyProvider):
    def __init__(self, now):
        permissions = (('*', ('fetch',)),) + tuple((key, ('fetch', 'display', 'retain') +
            (('export',) if key == 'title' else ())) for key in sorted(SCALARS)) + (
                ('tv.first_air_date', ('fetch', 'display', 'retain')), ('tv.first_air_year', ('export',)))
        self.value = SourceRights(FieldPolicy('synthetic-owner-tv', 1, now - timedelta(seconds=1),
            now + timedelta(minutes=10), permissions), 2, 'synthetic-terms', 'synthetic-privacy',
            'synthetic-locale', 'CultureCabinet/fixture (example.invalid)', source='tmdb_tv')

    def current(self, authority, session_ref, **kwargs):
        return self.value


class FixtureTVProvider(TVServiceProvider):
    def __init__(self, delivery, actor_id, profile_id, installation_ref, until, clock):
        self.delivery, self.actor_id, self.profile_id = delivery, actor_id, profile_id
        self.installation_ref, self.until, self.clock = installation_ref, until, clock

    def delivery_binding(self, access, job):
        access.require(actor_id=job.actor_id, profile_id=job.profile_id, action='mutate')
        return self.delivery

    def current(self, context):
        from uuid import UUID
        from shelf.obtain_tv_flow import SelectedTVServices
        from shelf.sab_selected import SelectedSabPorts
        from shelf.jellyfin_tv_import import DestinationImportPorts
        from shelf.jellyfin_native import ServiceBinding, ORIGIN
        if (context.actor_id, context.profile_id) != (self.actor_id, self.profile_id):
            raise AssertionError('Foreign synthetic service request')
        history = ServiceGeneration('sab_history', 'a' * 64, 'synthetic-history-config', 'synthetic-history-key')
        observer = SelectedSabPorts(history, lambda: history, Mock(side_effect=AssertionError('No observer effect in submission')),
            Mock(), Mock(), self.clock.now)
        destination = DestinationImportPorts(self.delivery.destination.service_generation,
            lambda: self.delivery.destination.service_generation, Mock(), Mock(), Mock(),
            self.clock.now, context.authority_deadline)
        library = ServiceBinding(ORIGIN, '1' * 32, 'synthetic-version', '2' * 32, '3' * 32,
            UUID(self.installation_ref).hex, self.actor_id, self.profile_id, '4' * 32, '5' * 32,
            self.delivery.destination.root, 'selected_tv_read', self.until)
        return SelectedTVServices(self.delivery, observer, destination, library, lambda: library,
            Mock(), self.clock, Mock(), Mock())


class OwnerTVEntryTests(RecoveryCase):
    claim_owner = PortableObtainTests.claim_owner
    csrf = PortableObtainTests.csrf
    json_post = PortableObtainTests.json_post
    post = PortableObtainTests.post
    scope = PortableObtainTests.scope

    def setUp(self):
        super().setUp()
        unused = Profile.objects.create(kind='owner', label='Discarded fixture', active=True)
        unused.delete()
        self.profile_ref = ga.create_owner_profile(self.authority, self.ref,
            expected_owner_revision=self.authority.authorize(self.ref).owner_revision)
        self.state = ProfileCabinetState.objects.get()
        self.actor = get_user_model().objects.get(pk=self.state.actor_id)
        self.assertNotEqual(self.actor.pk, 1)
        self.assertNotEqual(self.state.profile_id, 1)
        self.selection = select_portable_profile(self.authority, self.ref, profile_ref=self.profile_ref)
        self.selection_token = selected_context(self.authority, self.ref, self.selection)
        session = self.http.session; session['portable_saved_selection'] = self.selection; session.save()
        # Remove the shared fixture's inference setup through its real API. The
        # flow below has no usable inference connection and cannot publish.
        self.vault.remove(self.ref, self.config.pk, self.config.config_revision, disable=True)
        self.tv = self.vault.configure(self.ref, {'adapter': 'tmdb_tv', 'model': '', 'reasoning': ''})
        self.vault.replace(self.ref, self.tv.pk, self.tv.config_revision, b'SYNTHETIC-TV-SOURCE')
        self.expires = self.clock.wall + timedelta(minutes=10)
        ga.change_grant(self.authority, self.ref, profile_ref=self.profile_ref, connection_ref=str(self.tv.pk),
            expected_revision=0, purpose='catalogue_lookup', allowed_data_classes=['canonical_seed'],
            expires_at=self.expires, action='grant')
        self.policy = OwnerPolicy(self.clock.wall)
        options = self.settings(PORTABLE_OWNER_TV_POLICY_PROVIDER=self.policy)
        options.enable(); self.addCleanup(options.disable)
        from shelf.portable_enhance_service import EnhanceService
        EnhanceService.compose(self.authority, self.service,
            clock=SimpleNamespace(now=self.authority.now, monotonic=lambda: self.clock.tick,
                                  sleep=self.clock.advance))
        self.sent = []
        self.inference_before = self.fake.calls
        from shelf import credential_store as store
        metadata = store.EntryMetadata(store.Binding('synthetic-hydra-account', str(self.actor.pk),
            str(self.state.profile_id), 'hydra', store._provider_context('hydra').purpose),
            self.clock.wall - timedelta(seconds=1), self.expires, self.expires, store.HYDRA_SCOPES)
        self.hydra = preview.SearchRecipient(metadata, uuid4().hex)
        self.real_recipient_reader = preview.read_search_recipient
        self.enterContext(patch('shelf.obtain_preview.read_search_recipient', return_value=self.hydra))

    def hydra_backend(self, private):
        from shelf.acquisition_contract import Capabilities
        backend = Mock(); backend._grants = {'hydra': self.hydra.metadata}
        backend.caps.return_value = Capabilities(frozenset({'search'}), ((5000, 'TV'),), True)
        backend.search.return_value = private
        return backend

    def search_ref(self, access, origin, scope):
        return preview.issue_search_review(access=access, origin=origin, scope=scope)['search_review_ref']

    def source_send(self, operation, *, revalidate, **kwargs):
        self.assertTrue(revalidate())
        self.sent.append(operation.body)
        self.assertTrue(revalidate())
        return 200, canonical_bytes({'results': [{'id': 81001, 'name': 'Fiction Series',
            'first_air_date': '2024-05-08'}]}), (('cache-control', 'max-age=300'),)

    def source_preview(self):
        with self.scope() as access:
            return get_owner_tv(access).preview(title='Fiction Series', series_year=2024, season=2, episode=1)

    def lookup_fields(self, value):
        return {key: value[key] for key in OwnerLookupForm.base_fields if key != 'confirmed'} | {'confirmed': 'on'}

    def selected(self):
        value = self.source_preview()
        with self.scope() as access, patch('shelf.discovery_transport.media_authorized', side_effect=self.source_send):
            state = get_owner_tv(access)
            matches = state.lookup(self.lookup_fields(value), owner_backend(access))
            match = matches[0]
            ref = state.select({key: match[key] for key in ('intent_ref', 'match_ref', 'match_digest')} |
                               {'confirmed': 'on'})
            return ref, state.resolve(ref, now=self.clock.wall)

    def test_normal_native_entry_source_match_options_hydra_without_inference_publication_or_save(self):
        baseline = (FeedbackEvent.objects.count(), FeedbackProjection.objects.count(), SynthesisRun.objects.count())
        self.assertFalse(PortableConnection.objects.filter(adapter='openai_responses', removed=False).exists())
        response = self.http.get('/cabinet/obtain/tv/', **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="series_year"')
        response = self.post('/cabinet/obtain/tv/preview/', {'title': 'Fiction Series', 'series_year': '2024',
                                                          'season': '2', 'episode': '1'})
        self.assertEqual(response.status_code, 200)
        with self.scope() as access:
            state = get_owner_tv(access); intent = next(iter(state.intents.values()))
            approval_ref, approval = next(iter(state.issuer._previews.items()))
            fields = {'intent_ref': intent.intent_ref, 'source_preview_ref': approval_ref,
                'plan_digest': approval.plan.body_digest, 'operation_digest': approval.operation.operation_digest,
                'confirmed': 'on'}
            expected = approval.operation.body
        with patch('shelf.discovery_transport.media_authorized', side_effect=self.source_send):
            response = self.post('/cabinet/obtain/tv/lookup/', fields)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.sent, [expected])
        with self.scope() as access:
            state = get_owner_tv(access); match = next(iter(state.matches.values()))
            permit = next(iter(state.issuer._permits.values()))[0]
            self.assertEqual(permit.parent_ids, (intent.parent_ref,))
            self.assertNotIn('profile', read_json(approval.plan.body))
        response = self.post('/cabinet/obtain/tv/select/',
            {key: match[key] for key in ('intent_ref', 'match_ref', 'match_digest')} | {'confirmed': 'on'})
        self.assertEqual(response.status_code, 302)
        options = self.http.get(response['Location'], **self.headers)
        self.assertEqual(options.status_code, 200)
        self.assertContains(options, 'name="owner_selection_ref"')
        self.assertNotContains(options, 'name="publication_revision"')
        ref = response['Location'].split('owner_selection_ref=')[1]
        private = PrivateSearch(payload(item(title='Fiction Series 2024 S02E01 1080p', category='5000')))
        backend = self.hydra_backend(private)
        search_ref = re.search(r'name="search_review_ref" value="([^"]+)"', options.content.decode())[1]
        self.assertContains(options, 'Fiction Series 2024 S02E01')
        self.assertContains(options, 'http://127.0.0.1:5076/api')
        with patch('shelf.obtain_preview._reviewed_backend', return_value=backend):
            response = self.post('/cabinet/obtain/search/', {'owner_selection_ref': ref,
                'search_review_ref': search_ref, 'search_confirmed': 'on'})
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(backend.search.call_args.args[1:3], ('Fiction Series 2024 S02E01', 'episode'))
        receipt = preview.ObtainPreview.objects.get()
        self.assertEqual(receipt.state, 'results')
        self.assertTrue(all(getattr(receipt, key) is None for key in ('provider', 'medium', 'source_id', 'title', 'saved_identity')))
        self.assertEqual(receipt.publication_origin['schema'], 'obtain-owner-selection-origin/v1')
        self.assertFalse(any(private._payload))
        self.assertEqual((FeedbackEvent.objects.count(), FeedbackProjection.objects.count(), SynthesisRun.objects.count()), baseline)
        self.assertFalse(PortableCabinetManifest.objects.exists())
        self.assertFalse(SavedItem.objects.exists())
        self.assertFalse(AcquisitionJob.objects.exists())
        self.assertEqual(self.fake.calls, self.inference_before)

    def test_issuer_membership_body_and_one_attempt_fail_closed(self):
        value = self.source_preview()
        with self.scope() as access:
            state = get_owner_tv(access); intent = state.intents[value['intent_ref']]
            with self.assertRaises(EnhanceFault):
                state.plan(replace(intent))
            plan = state.plan(intent)
            body = read_json(plan.body); body['queries'][0]['terms'] = ['Other Series']
            forged = replace(plan, body=canonical_bytes(body), body_digest=digest(canonical_bytes(body)))
            with self.assertRaises(EnhanceFault):
                state.issuer.preview(forged)
            with patch('shelf.discovery_transport.media_authorized', side_effect=self.source_send) as sent:
                state.lookup(self.lookup_fields(value), owner_backend(access))
                with self.assertRaises(EnhanceFault):
                    state.lookup(self.lookup_fields(value), owner_backend(access))
                self.assertEqual(sent.call_count, 1)

    def test_missing_wrong_or_ambiguous_version_and_response_restrictions_never_select(self):
        cases = [([{'id': 81001, 'name': 'Fiction Series'}], ()),
            ([{'id': 81001, 'name': 'Fiction Series', 'first_air_date': '2015-01-01'}], ()),
            ([{'id': value, 'name': 'Fiction Series', 'first_air_date': '2024-01-01'} for value in (81001, 81002)], ()),
            ([{'id': 81001, 'name': 'Fiction Series', 'first_air_date': '2024-01-01'}], (('cache-control', 'no-store'),)),
            ([{'id': 81001, 'name': 'Fiction Series', 'first_air_date': '2024-01-01'}], (('cache-control', 'max-age=0'),))]
        for rows, headers in cases:
            value = self.source_preview()
            with self.scope() as access, patch('shelf.discovery_transport.media_authorized',
                    return_value=(200, canonical_bytes({'results': rows}), headers)):
                state = get_owner_tv(access)
                with self.assertRaises((EnhanceFault, ga.AuthorityFault)):
                    state.lookup(self.lookup_fields(value), owner_backend(access))
                self.assertNotIn(value['intent_ref'], state.matches)
                self.assertFalse(state.selections)

    def test_denied_retention_export_and_changed_destination_block_before_effect(self):
        self.policy.value = replace(self.policy.value, policy=replace(self.policy.value.policy,
            permissions=tuple((key, tuple(use for use in uses if use != 'retain'))
                              for key, uses in self.policy.value.policy.permissions)))
        value = self.source_preview()
        with self.scope() as access, patch('shelf.discovery_transport.media_authorized') as sent:
            with self.assertRaises(EnhanceFault):
                get_owner_tv(access).lookup(self.lookup_fields(value), owner_backend(access))
            sent.assert_not_called()
        self.policy.value = OwnerPolicy(self.clock.wall).value
        ref, (origin, target) = self.selected()
        backend = self.hydra_backend(None)
        with self.scope() as access:
            search_ref = self.search_ref(access, origin, target.scope)
            with patch('shelf.obtain_preview.read_search_recipient', side_effect=[self.hydra, self.hydra,
                    replace(self.hydra, credential_generation=uuid4().hex)]), self.assertRaises(preview.saved.SavedError):
                preview.search_origin(access=access, origin=origin, scope=target.scope,
                                      search_review_ref=search_ref, factory=lambda **kw: backend)
        backend.caps.assert_called_once()
        backend.search.assert_not_called()
        self.policy.value = replace(self.policy.value, policy=replace(self.policy.value.policy,
            permissions=tuple((key, tuple(use for use in uses if use != 'export'))
                              for key, uses in self.policy.value.policy.permissions)))
        ref, (origin, target) = self.selected()
        backend = Mock()
        with self.scope() as access:
            with self.assertRaises(Blocked):
                self.search_ref(access, origin, target.scope)
        backend.caps.assert_not_called()
        self.policy.value = OwnerPolicy(self.clock.wall).value
        ref, (origin, target) = self.selected()
        with self.scope() as access, ga.current_writer(self.authority, self.ref, self.profile_ref), \
                transaction.atomic(), ga.writer_window(self.actor, self.state.profile_id):
            from shelf.models import Grant
            Grant.objects.filter(user=self.actor, profile_id=self.state.profile_id).update(can_export=False)
        backend = Mock()
        with self.scope() as access:
            with self.assertRaises(ga.AuthorityFault):
                self.search_ref(access, origin, target.scope)
        backend.caps.assert_not_called()

    def test_selection_expiry_and_native_generation_change_purge_source_facts(self):
        ref, (_, target) = self.selected()
        with self.scope() as access:
            state = get_owner_tv(access)
            self.clock.advance(301)
            state.purge()
            self.assertFalse(state.matches)
            self.assertFalse(state.selections)
            self.assertFalse(state.intents)
            with self.assertRaises(EnhanceFault):
                state.resolve(ref, now=self.clock.wall)

    def test_closed_selector_and_constructor_grammars_reject_forged_variants(self):
        ref, _ = self.selected()
        self.assertTrue(ObtainOriginSelectorForm({'owner_selection_ref': ref}).is_valid())
        for fields in ({'owner_selection_ref': ref, 'publication_revision': ''},
                       {'owner_selection_ref': ref, 'candidate_key': ''}, {},
                       {'owner_selection_ref': str(uuid4()), 'publication_revision': '1', 'candidate_key': 'x'}):
            self.assertFalse(ObtainOriginSelectorForm(fields).is_valid())
        with self.scope() as access:
            state = get_owner_tv(access); intent = next(iter(state.intents.values())); plan = state.plan(intent)
            for change in ({'profile': {}}, {'intent_ref': str(uuid4())}, {'intent_digest': 'bad'},
                           {'queries': []}, {'queries': [{'category': 'tv', 'terms': ['one', 'two']}]}):
                value = {**read_json(plan.body), **change}; raw = canonical_bytes(value)
                with self.assertRaises(EnhanceFault):
                    replace(plan, body=raw, body_digest=digest(raw))

    def test_source_choice_is_digest_only_and_restore_preserves_closed_origin_ownership(self):
        import sqlite3
        from shelf import recovery
        ref, (origin, target) = self.selected()
        backend = self.hydra_backend(PrivateSearch(payload(item(title='Fiction Series 2024 S02E01 1080p', category='5000'))))
        with self.scope() as access:
            receipt = preview.search_origin(access=access, origin=origin, scope=target.scope,
                search_review_ref=self.search_ref(access, origin, target.scope), factory=lambda **kw: backend)
            result = receipt.results.get()
            data = {'review_token': preview.review_token(receipt, result), 'scope': receipt.scope,
                'edition': result.metadata['title'], 'storage': 'local_library',
                'authorized_content': 'on', 'storage_check': 'on'}
            job, created = preview.confirm(actor=self.actor, profile_id=self.state.profile_id,
                preview_id=receipt.pk, result_id=result.pk, data=data)
            self.assertTrue(created)
            same, created = preview.confirm(actor=self.actor, profile_id=self.state.profile_id,
                preview_id=receipt.pk, result_id=result.pk, data=data)
            self.assertFalse(created)
            self.assertEqual(same.pk, job.pk)
        self.assertTrue(all(getattr(job, key) is None for key in ('provider', 'medium', 'source_id', 'title', 'saved_item_id')))
        self.assertEqual(job.publication_origin, origin)
        with self.assertRaises(DatabaseError), transaction.atomic():
            AcquisitionJob.objects.filter(pk=job.pk).update(reason='unscoped')
        raw = sqlite3.connect(':memory:')
        self.addCleanup(raw.close)
        connection.connection.backup(raw)
        recovery._closed_sql_guards(raw)
        recovery._effective_guards(raw)
        recovery.semantic_check(raw)
        with self.scope() as access:
            owner = get_owner_tv(access)
            self.clock.advance(301)
            # Expiry removes RAM facts but never invalidates historical receipts.
            owner.purge()
        recovery.semantic_check(raw)
        for name in ('obtain_origin_acquisitionjob_update', 'obtain_origin_acquisitionjob_immutable',
                     'obtain_native_acquisitionjob_update'):
            raw.execute('DROP TRIGGER ' + name)
        changed = {**origin, 'profile_ref': str(uuid4())}
        raw.execute('UPDATE shelf_acquisitionjob SET publication_origin=? WHERE id=?',
                    (canonical_bytes(changed).decode(), job.pk))
        raw.commit()
        with self.assertRaises(recovery.RecoveryBlocked):
            recovery.semantic_check(raw)

    def test_key_generation_foreign_reference_and_profile_reselection_cannot_reuse_facts(self):
        ref, (origin, target) = self.selected()
        with self.scope() as access:
            state = get_owner_tv(access)
            with self.assertRaises(EnhanceFault):
                state.resolve(str(uuid4()), now=self.clock.wall)
            self.vault.replace(self.ref, self.tv.pk, self.tv.config_revision, b'SYNTHETIC-REPLACEMENT')
            with self.assertRaises((EnhanceFault, ga.AuthorityFault)):
                state.resolve(ref, now=self.clock.wall)
            self.assertFalse(state.selections)
        # A new synthetic lookup needs explicit consent for the new key binding.
        for action, revision in (('revoke', 1), ('regrant', 2)):
            ga.change_grant(self.authority, self.ref, profile_ref=self.profile_ref, connection_ref=str(self.tv.pk),
                expected_revision=revision, purpose='catalogue_lookup', allowed_data_classes=['canonical_seed'],
                expires_at=self.expires, action=action)
        ref, _ = self.selected()
        with self.scope() as access:
            state = get_owner_tv(access)
        # Even choosing the same profile rotates its native selection generation.
        select_portable_profile(self.authority, self.ref, profile_ref=self.profile_ref)
        state.purge()
        self.assertFalse(state.intents)
        self.assertFalse(state.selections)

    def test_native_csrf_selection_return_and_mixed_selector_rejection(self):
        response = self.post('/cabinet/obtain/tv/preview/', {'title': 'Fiction Series', 'series_year': '2024',
                                                          'season': '2', 'episode': '1'}, csrf=False)
        self.assertEqual(response.status_code, 403)
        session = self.http.session; del session['portable_saved_selection']; session.save()
        response = self.http.get('/cabinet/obtain/tv/?profile_ref=' + self.profile_ref, **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="return_to" value="owner_tv"')
        self.assertNotIn('portable_saved_selection', self.http.session)
        for destination in ('https://foreign.invalid/', '/cabinet', ''):
            response = self.http.post('/cabinet/saved/select/',
                {'profile_ref': self.profile_ref, 'return_to': destination}, HTTP_X_CSRFTOKEN=self.csrf(), **self.headers)
            self.assertEqual(response.status_code, 422)
        response = self.http.post('/cabinet/saved/select/',
            {'profile_ref': self.profile_ref, 'return_to': 'owner_tv'}, HTTP_X_CSRFTOKEN=self.csrf(), **self.headers)
        self.assertEqual((response.status_code, response['Location']), (303, '/cabinet/obtain/tv/'))
        response = self.http.get('/cabinet/obtain/options/?owner_selection_ref=' + str(uuid4()) +
                                 '&publication_revision=', **self.headers)
        self.assertEqual(response.status_code, 422)

    def test_source_failure_consumes_attempt_and_exact_title_limit_never_truncates(self):
        value = self.source_preview()
        with self.scope() as access, patch('shelf.discovery_transport.media_authorized',
                side_effect=EnhanceFault('SOURCE_UNAVAILABLE')) as sent:
            state = get_owner_tv(access)
            for _ in range(2):
                with self.assertRaises(EnhanceFault):
                    state.lookup(self.lookup_fields(value), owner_backend(access))
            self.assertEqual(sent.call_count, 1)
            self.assertFalse(state.matches)
            with self.assertRaises(EnhanceFault):
                state.preview(title='X' * 101, series_year=2024, season=2, episode=1)
            self.assertEqual(sent.call_count, 1)

    def source_job(self):
        ref, (origin, target) = self.selected()
        backend = self.hydra_backend(PrivateSearch(payload(item(title='Fiction Series 2024 S02E01 1080p', category='5000'))))
        with self.scope() as access:
            receipt = preview.search_origin(access=access, origin=origin, scope=target.scope,
                search_review_ref=self.search_ref(access, origin, target.scope), factory=lambda **kw: backend)
            result = receipt.results.get()
            job, _ = preview.confirm(actor=self.actor, profile_id=self.state.profile_id, preview_id=receipt.pk,
                result_id=result.pk, data={'review_token': preview.review_token(receipt, result), 'scope': receipt.scope,
                    'edition': result.metadata['title'], 'storage': 'local_library',
                    'authorized_content': 'on', 'storage_check': 'on'})
        from shelf.obtain_runtime import submission_context, SubmissionPolicy
        from shelf.acquisition_contract import COMPLETION_SCRIPT
        complete = Path(self.directory.name) / 'source-complete'; complete.mkdir()
        policy = SubmissionPolicy(True, str(complete), 20000, 0, True,
            str(Path(self.directory.name) / 'source-registry'), True, COMPLETION_SCRIPT)
        options = self.settings(OBTAIN_SUBMISSION_POLICY=asdict(policy)); options.enable(); self.addCleanup(options.disable)
        from shelf.jellyfin_native import TV_ROOTS
        self.delivery = DeliveryBinding(DestinationBinding('synthetic-destination', 'synthetic-mac', TV_ROOTS[0],
            ServiceGeneration('destination_file', 'd' * 64, 'synthetic-config1', 'synthetic-key1')),
            'Fiction Series 2024', False)
        self.delivery_provider = FixtureTVProvider(self.delivery, self.actor.pk, self.state.profile_id,
            str(self.state.installation_id), self.expires,
            SimpleNamespace(now=self.authority.now, monotonic=lambda: self.clock.tick))
        options = self.settings(PORTABLE_OBTAIN_TV_SERVICES=self.delivery_provider)
        options.enable(); self.addCleanup(options.disable)
        with self.scope():
            context = submission_context(actor=self.actor, profile_id=self.state.profile_id, job=job, policy=policy)
        return job, context

    def test_native_source_submission_uses_reviewed_bytes_and_duplicate_receipt_precedes_factories(self):
        import html, re
        from shelf import obtain_acquisition as acquire, obtain_runtime
        from shelf.acquisition_pipeline import Evidence
        from shelf.models import ObtainReviewedNZB, ObtainTransfer
        from shelf.reviewed_nzb import ReviewedRetriever
        from .test_obtain_acquisition import FixtureBackend, NZB
        job, context = self.source_job()
        descriptor_source = Mock(); descriptor_source.retrieve.return_value = NZB
        retriever = ReviewedRetriever(descriptor_source); backend = FixtureBackend()
        with self.scope():
            exact, _ = acquire.exact_result(job, fresh=True)
            evidence = Evidence(exact.snapshot, acquire._source_selection(exact.snapshot))
        services = obtain_runtime.SubmissionServices(context, evidence, 20000, retriever, backend,
                                                    delivery_binding=self.delivery)
        with patch('shelf.obtain_runtime.submission_factory', return_value=services):
            response = self.post(f'/cabinet/saved/jobs/{job.pk}/submit/review/')
        self.assertEqual(response.status_code, 200, response.content)
        token = html.unescape(re.search(r'name="token" value="([^"]+)"', response.content.decode())[1])
        row = ObtainReviewedNZB.objects.get()
        self.assertNotIn('snapshot', row.exact)
        for key in ('provider', 'medium', 'source_id', 'title', 'item_id'):
            self.assertNotIn(('"' + key + '"').encode(), canonical_bytes(row.exact))
        fields = {key: 'on' for key in ('exact_selection', 'rights', 'scope', 'size', 'duplicates')}
        fields['token'] = token
        descriptor_source.retrieve.side_effect = AssertionError('Must use the reviewed descriptor')
        with patch('shelf.obtain_runtime.submission_factory', return_value=services):
            response = self.post(f'/cabinet/saved/jobs/{job.pk}/submit/', fields)
        self.assertEqual(response.status_code, 302, response.content)
        transfer = ObtainTransfer.objects.get()
        self.assertEqual(transfer.state, 'queued')
        self.assertEqual(transfer.binding['schema'], 'obtain-owner-selection-submission/v1')
        self.assertEqual(set(transfer.binding['exact']), {'result_id', 'guid', 'digest', 'size'})
        self.assertEqual(set(transfer.binding['selection']), {'edition', 'storage'})
        self.assertNotIn(b'81001', canonical_bytes(transfer.binding))
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(descriptor_source.retrieve.call_count, 1)
        from shelf.obtain_native_context import native_job_context
        from shelf.completion_dispatch import _native_cas
        with self.scope() as access:
            first = native_job_context(access, job_id=job.pk, operation='observe')
            concurrent = native_job_context(access, job_id=job.pk, operation='observe')
            _native_cas(first, dict(transfer.native_progress))
            with self.assertRaises(acquire.Blocked):
                _native_cas(concurrent, {**transfer.native_progress, 'reason': 'stale_worker'})
        transfer.refresh_from_db()
        self.assertEqual(transfer.native_progress['reason'], '')
        with patch('shelf.obtain_runtime.submission_factory', side_effect=AssertionError('Duplicate opened services')), \
                patch('shelf.portable_obtain_origin.resolve_obtain_origin_again', side_effect=AssertionError('Duplicate resolved source')):
            response = self.post(f'/cabinet/saved/jobs/{job.pk}/submit/', fields)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(backend.calls), 1)

    def test_unknown_source_attempt_and_expired_selection_replay_never_retrieve_or_resend(self):
        from shelf import obtain_acquisition as acquire
        from shelf.acquisition_pipeline import AdapterUnavailable, Evidence
        from .test_obtain_acquisition import FixtureBackend, FixtureRetriever
        from .test_portable_authority import PASSWORD
        job, context = self.source_job(); retriever = FixtureRetriever(); backend = FixtureBackend()
        backend.error = AdapterUnavailable('synthetic lost acknowledgement')
        with self.scope():
            exact, _ = acquire.exact_result(job, fresh=True)
            evidence = Evidence(exact.snapshot, acquire._source_selection(exact.snapshot))
            review = acquire.review_submission(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
                context=context, evidence=evidence, max_bytes=20000, retriever=retriever, delivery_binding=self.delivery)
            transfer = acquire.submit(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
                context=context, token=review['token'], confirmed=True, retriever=retriever, backend=backend,
                delivery_binding=self.delivery)
        self.assertEqual(transfer.state, 'outcome_unknown')
        count = len(retriever.calls)
        self.clock.advance(301)
        self.authority.reauthenticate(self.ref, PASSWORD)
        with self.scope(), patch('shelf.portable_obtain_origin.resolve_obtain_origin_again',
                side_effect=AssertionError('Expired receipt attempted source reconstruction')):
            same = acquire.submit(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
                context=None, token=review['token'], confirmed=True, retriever=None, backend=None)
        self.assertEqual(same.pk, transfer.pk)
        self.assertEqual(same.state, 'outcome_unknown')
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(len(retriever.calls), count)

    def test_source_binding_tag_conflict_and_missing_live_context_fail_before_enqueue(self):
        from django.core import signing
        from shelf import obtain_acquisition as acquire
        from shelf.acquisition_pipeline import Evidence
        from shelf.models import ObtainTransfer
        from .test_obtain_acquisition import FixtureBackend, FixtureRetriever
        job, context = self.source_job(); retriever = FixtureRetriever(); backend = FixtureBackend()
        with self.scope():
            exact, _ = acquire.exact_result(job, fresh=True)
            evidence = Evidence(exact.snapshot, acquire._source_selection(exact.snapshot))
            review = acquire.review_submission(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
                context=context, evidence=evidence, max_bytes=20000, retriever=retriever, delivery_binding=self.delivery)
            for change in ({'schema': 'obtain-publication-submission/v1'}, {'snapshot_digest': '0' * 64},
                           {'selection': {'edition': 'Changed', 'storage': 'local_library'}},
                           {'exact': {**review['binding']['exact'], 'snapshot': asdict(exact.snapshot)}}):
                token = signing.dumps({**review['binding'], **change}, salt=acquire.SALT)
                with self.assertRaises(acquire.ContractError):
                    acquire.submit(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
                        context=context, token=token, confirmed=True, retriever=retriever, backend=backend,
                        delivery_binding=self.delivery)
            with self.assertRaises(acquire.Blocked):
                acquire.submit(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
                    context=Mock(), token=review['token'], confirmed=True, retriever=retriever, backend=backend,
                    delivery_binding=self.delivery)
        self.assertFalse(backend.calls)
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_confirmed_delivery_changes_refuse_before_descriptor_reservation_or_enqueue(self):
        from shelf import obtain_acquisition as acquire
        from shelf.acquisition_pipeline import Evidence
        from shelf.models import ObtainTransfer
        from .test_obtain_acquisition import FixtureBackend, FixtureRetriever
        job, context = self.source_job(); retriever = FixtureRetriever(); backend = FixtureBackend()
        original = self.delivery
        destination = original.destination
        changes = (replace(original, relative_parent='Other Series'),
            replace(original, create_parent=True),
            replace(original, destination=replace(destination, host='other-host')),
            replace(original, destination=replace(destination, ref='other-destination')),
            replace(original, destination=replace(destination, service_generation=replace(
                destination.service_generation, credential_generation='changed-generation'))))
        with self.scope():
            exact, _ = acquire.exact_result(job, fresh=True)
            review = acquire.review_submission(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
                context=context, evidence=Evidence(exact.snapshot, acquire._source_selection(exact.snapshot)),
                max_bytes=20000, retriever=retriever, delivery_binding=original)
            count = len(retriever.calls)
            for changed in changes:
                with self.subTest(delivery=changed):
                    self.delivery_provider.delivery = changed
                    with self.assertRaises((acquire.Blocked, acquire.ContractError)):
                        acquire.submit(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
                            context=context, token=review['token'], confirmed=True, retriever=retriever,
                            backend=backend, delivery_binding=original)
                    self.assertEqual(len(retriever.calls), count)
                    self.assertFalse(ObtainTransfer.objects.exists())
            self.delivery_provider.delivery = original
        self.assertFalse(backend.calls)

    def test_unknown_advance_and_get_are_read_only_and_stale_revision_refuses_before_ports(self):
        from shelf import obtain_acquisition as acquire
        from shelf.acquisition_pipeline import AdapterUnavailable, Evidence
        from .test_obtain_acquisition import FixtureBackend, FixtureRetriever
        job, context = self.source_job(); retriever = FixtureRetriever(); backend = FixtureBackend()
        backend.error = AdapterUnavailable('synthetic uncertain acknowledgement')
        with self.scope():
            exact, _ = acquire.exact_result(job, fresh=True)
            review = acquire.review_submission(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
                context=context, evidence=Evidence(exact.snapshot, acquire._source_selection(exact.snapshot)),
                max_bytes=20000, retriever=retriever, delivery_binding=self.delivery)
            transfer = acquire.submit(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
                context=context, token=review['token'], confirmed=True, retriever=retriever, backend=backend,
                delivery_binding=self.delivery)
        initial = (transfer.revision, transfer.native_progress, transfer.library_verification)
        url = f'/cabinet/obtain/jobs/{job.pk}/advance/'
        with patch('shelf.obtain_runtime.selected_services', side_effect=AssertionError('Read opened ports')):
            self.assertEqual(self.http.get(url, **self.headers).status_code, 405)
            self.assertEqual(self.post(url, {'expected_revision': str(transfer.revision)}, csrf=False).status_code, 403)
            self.assertEqual(self.post(url, {'expected_revision': str(transfer.revision + 1)}).status_code, 409)
            self.assertEqual(self.post(url, {'expected_revision': str(transfer.revision)}).status_code, 302)
            self.assertEqual(self.http.get(f'/cabinet/obtain/jobs/{job.pk}/status/', **self.headers).status_code, 200)
        transfer.refresh_from_db()
        self.assertEqual((transfer.revision, transfer.native_progress, transfer.library_verification), initial)
        self.assertEqual(len(backend.calls), 1)

    def test_native_effect_deadline_does_not_renew_after_actual_reauthentication(self):
        from shelf.obtain_native_context import native_job_context
        from .test_portable_authority import PASSWORD
        job, _ = self.source_job()
        with self.scope() as access:
            context = native_job_context(access, job_id=job.pk, operation='submit')
            original = context.authority_deadline()
            self.clock.advance(100)
            self.authority.reauthenticate(self.ref, PASSWORD)
            self.assertEqual(context.authority_deadline(), original)
            fresh = native_job_context(access, job_id=job.pk, operation='submit')
            self.assertGreater(fresh.authority_deadline(), original)
            with self.settings(PORTABLE_OBTAIN_POLL_UNTIL=original - timedelta(seconds=1)):
                self.assertEqual(context.authority_deadline(), original - timedelta(seconds=1))

    def test_publication_choose_only_selects_native_owner_and_navigates_without_source_work(self):
        from urllib.parse import parse_qs, urlsplit
        session = self.http.session; del session['portable_saved_selection']; session.save()
        fields = {'profile_ref': self.profile_ref, 'publication_revision': '7', 'candidate_key': 'synthetic-card'}
        url = '/cabinet/obtain/choose/'
        before = AcquisitionJob.objects.count()
        with patch('shelf.portable_obtain_origin.resolve_current_origin', side_effect=AssertionError('Choose resolved source')):
            self.assertEqual(self.http.get(url, **self.headers).status_code, 405)
            self.assertEqual(self.http.post(url, fields, **self.headers).status_code, 403)
            response = self.http.post(url, fields, HTTP_X_CSRFTOKEN=self.csrf(), **self.headers)
        self.assertEqual(response.status_code, 303)
        target = urlsplit(response['Location'])
        self.assertEqual(target.path, '/cabinet/obtain/options/')
        self.assertEqual(parse_qs(target.query), {key: [value] for key, value in fields.items()})
        self.assertEqual(self.http.session['portable_saved_selection']['profile_ref'], self.profile_ref)
        self.assertEqual(AcquisitionJob.objects.count(), before)
        self.assertFalse(self.sent)

    def test_selected_worker_monitor_stops_owned_ports_when_actual_selection_rotates(self):
        from shelf.obtain_native_context import native_job_context
        from shelf.obtain_runtime import _SelectedWorker
        job, _ = self.source_job()
        with self.scope() as access:
            context = native_job_context(access, job_id=job.pk, operation='observe')
            services = self.delivery_provider.current(context)
            worker = _SelectedWorker(access, context, services)
        session = self.authority._sessions[self.ref]
        original = vars(session).copy()
        worker.monitor.start()
        try:
            select_portable_profile(self.authority, self.ref, profile_ref=self.profile_ref)
            worker.monitor.join(timeout=2)
            self.assertFalse(worker.monitor.is_alive())
            self.assertTrue(worker.stop_event.is_set())
            services.stop_owned_children.assert_called_once_with()
            self.assertEqual(worker.reason, 'original_authority_window_ended')
            self.assertEqual(vars(session), original)
        finally:
            worker.done.set()
            worker.monitor.join(timeout=2)

    def test_reviewed_options_reject_recipient_or_key_rotation_before_backend_or_caps(self):
        ref, _ = self.selected()
        for changed in (replace(self.hydra, credential_generation=uuid4().hex),
                        replace(self.hydra, metadata=replace(self.hydra.metadata,
                            binding=replace(self.hydra.metadata.binding, account_id='changed-synthetic-account')))):
            response = self.http.get('/cabinet/obtain/options/?owner_selection_ref=' + ref, **self.headers)
            self.assertEqual(response.status_code, 200)
            token = re.search(r'name="search_review_ref" value="([^"]+)"', response.content.decode())[1]
            with patch('shelf.obtain_preview.read_search_recipient', return_value=changed), \
                    patch('shelf.obtain_preview._reviewed_backend', side_effect=AssertionError('Changed review opened backend')) as factory:
                rejected = self.post('/cabinet/obtain/search/', {'owner_selection_ref': ref,
                    'search_review_ref': token, 'search_confirmed': 'on'})
            self.assertEqual(rejected.status_code, 409)
            factory.assert_not_called()
        self.assertFalse(preview.ObtainPreview.objects.exists())

    def test_reviewed_query_origin_episode_and_expiry_changes_refuse_without_caps(self):
        from shelf.acquisition_contract import PublicRequest
        ref, _ = self.selected()
        for change in ('query', 'origin', 'episode', 'unconfirmed', 'expiry'):
            response = self.http.get('/cabinet/obtain/options/?owner_selection_ref=' + ref, **self.headers)
            token = re.search(r'name="search_review_ref" value="([^"]+)"', response.content.decode())[1]
            data = {'owner_selection_ref': ref, 'search_review_ref': token, 'search_confirmed': 'on'}
            if change == 'origin':
                data['owner_selection_ref'] = str(uuid4())
            if change == 'episode':
                data.update(season='3', episode='1')
            if change == 'unconfirmed':
                data.pop('search_confirmed')
            if change == 'expiry':
                # Expire this review alone while the source/session remains live.
                self.authority._obtain_search_reviews[token]['expires_at'] = self.clock.wall
            original = preview.origin_search_request
            with patch('shelf.obtain_preview._reviewed_backend', side_effect=AssertionError('Changed review opened backend')) as factory, \
                    patch('shelf.obtain_preview.origin_search_request', side_effect=(
                        lambda target: PublicRequest('search', (('q', 'Other Series 2024 S02E01'), ('cat', '5000'))))
                        if change == 'query' else original):
                rejected = self.post('/cabinet/obtain/search/', data)
            self.assertIn(rejected.status_code, (409, 422))
            factory.assert_not_called()
        self.assertFalse(preview.ObtainPreview.objects.exists())

    def test_credential_open_rejects_actual_replaced_record_generation(self):
        from shelf import credential_store as store
        from .fixtures_credentials import FakeTree
        from .test_acquisition_backend import grant
        # The real store walk requires a private owned parent, too.
        with patch('tempfile.tempdir', self.directory.name):
            tree = FakeTree(store, provider='hydra')
        self.addCleanup(tree.temporary.cleanup)
        metadata = grant('hydra')
        metadata = replace(metadata, binding=replace(metadata.binding,
            actor_id=str(self.actor.pk), profile_id=str(self.state.profile_id)))
        receipt = store.commit_entry(tree.store, metadata, bytearray(b'SYNTHETIC-HYDRA-ONE'))
        self.assertEqual(receipt.outcome, 'stored')
        tree.store.close()
        def opener(*, provider):
            return store._fixture_store(tree.path, provider=provider)
        with self.scope(), patch.object(store, 'open_store', side_effect=opener):
            reviewed = self.real_recipient_reader(actor=self.actor, profile_id=self.state.profile_id)
            handle = opener(provider='hydra'); store.lock_store(handle)
            record = store.read_record(handle); expected = record.identity; record.close()
            receipt = store.commit_entry(handle, metadata, bytearray(b'SYNTHETIC-HYDRA-TWO'), expected=expected)
            handle.close()
            self.assertEqual(receipt.outcome, 'stored')
            current = self.real_recipient_reader(actor=self.actor, profile_id=self.state.profile_id)
            self.assertNotEqual(current.credential_generation, reviewed.credential_generation)
            backend = preview._reviewed_backend(actor=self.actor, profile_id=self.state.profile_id,
                                                 recipient=reviewed, revalidate=lambda actual: None)
            with patch.object(type(backend._client), 'caps', side_effect=AssertionError('Changed key reached transport')) as transport:
                with self.assertRaises(Blocked):
                    backend.caps(actor_id=self.actor.pk, profile_id=self.state.profile_id)
                transport.assert_not_called()

    def test_actual_reviewed_backend_reaches_caps_search_and_closes_single_lock(self):
        from shelf import credential_store as store
        from .fixtures_credentials import FakeTree
        from .test_acquisition_backend import grant
        from .test_acquisition_contract import CAPS
        ref, _ = self.selected()
        with patch('tempfile.tempdir', self.directory.name):
            tree = FakeTree(store, provider='hydra')
        self.addCleanup(tree.temporary.cleanup)
        metadata = grant('hydra')
        metadata = replace(metadata, binding=replace(metadata.binding,
            actor_id=str(self.actor.pk), profile_id=str(self.state.profile_id)))
        self.assertEqual(store.commit_entry(tree.store, metadata,
            bytearray(b'SYNTHETIC-HYDRA-SUCCESS')).outcome, 'stored')
        tree.store.close(); handles = []; operations = []
        def opener(*, provider):
            handle = store._fixture_store(tree.path, provider=provider)
            handles.append(handle)
            return handle
        def send(request, *, private_params, nzb=None):
            # Only public requests are retained; synthetic credential bytes do
            # not enter this evidence or replace the real credential boundary.
            operations.append(request.public_dict())
            return CAPS if request.operation == 'caps' else payload(
                item(title='Fiction Series 2024 S02E01 1080p', category='5000'))
        transport = Mock(); transport.send.side_effect = send
        with patch.object(store, 'open_store', side_effect=opener), \
                patch('shelf.obtain_preview.read_search_recipient', side_effect=self.real_recipient_reader), \
                patch('shelf.acquisition_backend.LoopbackTransport', return_value=transport):
            response = self.http.get('/cabinet/obtain/options/?owner_selection_ref=' + ref, **self.headers)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertContains(response, 'Fiction Series 2024 S02E01')
            token = re.search(r'name="search_review_ref" value="([^"]+)"', response.content.decode())[1]
            response = self.post('/cabinet/obtain/search/', {'owner_selection_ref': ref,
                'search_review_ref': token, 'search_confirmed': 'on'})
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual([operation['params']['t'] for operation in operations], ['caps', 'search'])
        self.assertEqual(operations[-1]['params']['q'], 'Fiction Series 2024 S02E01')
        self.assertEqual(preview.ObtainPreview.objects.get().state, 'results')
        self.assertTrue(handles)
        self.assertTrue(all(handle.fd == -1 and (handle._lock is None or handle._lock.fd == -1) for handle in handles))

    def test_native_continuation_verifies_real_private_destination_and_library_then_reload(self):
        import json, hashlib, html, socket
        from contextlib import contextmanager
        from shelf import obtain_runtime, completion_registry as registry, completion_files
        from shelf import jellyfin_native as jf, jellyfin_tv_import as imp
        from shelf import obtain_acquisition as acquire
        from shelf.acquisition_pipeline import Evidence
        from shelf.models import ObtainTransfer
        from .test_obtain_acquisition import FixtureBackend, FixtureRetriever
        from .test_jellyfin_native import Connection, Response
        root = Path(self.directory.name) / 'synthetic-destination'; root.mkdir(mode=0o700)
        with patch.object(jf, 'TV_ROOTS', (str(root),)):
            job, context = self.source_job()
            self.delivery = replace(self.delivery, destination=replace(self.delivery.destination, host=socket.gethostname()))
            (root / self.delivery.relative_parent).mkdir(mode=0o700)
            outer = self
            class FileProvider(FixtureTVProvider):
                def current(self, native):
                    value = super().current(native)
                    def entry(nzo):
                        document = json.loads(Path(context.policy.registry_path).read_text())
                        return registry.registry_entry(document, nzo)
                    def history(nzo):
                        return {'nzo_id': nzo, 'name': 'CC-' + str(job.request_id), 'status': 'Completed',
                            'storage': context.policy.intent(str(job.request_id))['root'], 'fail_message': '',
                            'script': 'culture-cabinet-completion',
                            'completed': int((outer.downloaded_at - timedelta(seconds=1)).timestamp())}
                    def hook(request):
                        selected = entry('SABnzbd_nzo_synthetic')
                        data = completion_files.postprocess_evidence(selected, outer.download_files, 4096)
                        return completion_files.completion_receipt(data, (outer.downloaded_at - timedelta(seconds=2)).isoformat())
                    observer = replace(value.observer, read_registration=entry, read_history=history, read_hook=hook)
                    destination = replace(value.destination,
                        inspect=lambda plan: imp.receive_selected(plan, None, operation='inspect',
                            allowed_roots=(str(root),), clock=outer.authority.now),
                        receive=lambda plan, source: imp.receive_selected(plan, source, operation='receive',
                            allowed_roots=(str(root),), clock=outer.authority.now))
                    @contextmanager
                    def credential(binding):
                        yield jf.ScopedCredential(jf._digest(binding.material()), 'SYNTHETIC-LIBRARY-NOT-A-KEY')
                    def target(current, selected, destination_receipt, series_ref):
                        from datetime import datetime
                        end = value.library.expires_at if series_ref else datetime.fromisoformat(
                            job.publication_origin['valid_until'].replace('Z', '+00:00'))
                        return jf.EpisodeResolutionTarget(selected.request_id, selected.binding_digest,
                            selected.content_identity_digest, destination_receipt.destination_binding_digest,
                            None if series_ref else 81001, selected.episode_scope,
                            None if series_ref else 'Fiction Series', series_ref, end)
                    return replace(value, observer=observer, destination=destination,
                                   library_credential_scope=credential, target_for=target)
            provider = FileProvider(self.delivery, self.actor.pk, self.state.profile_id, str(self.state.installation_id),
                self.expires, SimpleNamespace(now=self.authority.now, monotonic=lambda: self.clock.tick))
            with self.settings(PORTABLE_OBTAIN_TV_SERVICES=provider):
                backend, retriever = FixtureBackend(), FixtureRetriever()
                with self.scope():
                    exact, _ = acquire.exact_result(job, fresh=True)
                    evidence = Evidence(exact.snapshot, acquire._source_selection(exact.snapshot))
                intent = context.policy.intent(str(job.request_id))
                services = obtain_runtime.SubmissionServices(context, evidence, 20000, retriever, backend,
                    context.policy.complete_root, True, intent, self.delivery)
                with patch('shelf.obtain_runtime.submission_factory', return_value=services):
                    page = self.post(f'/cabinet/saved/jobs/{job.pk}/submit/review/')
                    self.assertEqual(page.status_code, 200, page.content)
                    token = html.unescape(re.search(r'name="token" value="([^"]+)"', page.content.decode())[1])
                    fields = {key: 'on' for key in ('exact_selection', 'rights', 'scope', 'size', 'duplicates')}
                    fields['token'] = token
                    posted = self.post(f'/cabinet/saved/jobs/{job.pk}/submit/', fields)
                self.assertEqual(posted.status_code, 302, posted.content)
                transfer = ObtainTransfer.objects.get(job=job)
                self.assertEqual(transfer.state, 'queued')
                source_root = Path(intent['root']); source_root.mkdir()
                self.downloaded_at = self.clock.wall
                video = source_root / 'Fiction.Series.2024.S02E01.mkv'; video.write_bytes(b'x' * 4096)
                self.download_files = [{'path': video.name, 'bytes': 4096, 'sha256': hashlib.sha256(video.read_bytes()).hexdigest()}]
                output = root / self.delivery.relative_parent / (job.edition + '.mkv')
                series = {'Id': '7' * 32, 'ServerId': '1' * 32, 'Type': 'Series', 'Name': 'Fiction Series',
                    'ProviderIds': {'Tmdb': '81001'}, 'Path': str(root / self.delivery.relative_parent)}
                episode = {'Id': '8' * 32, 'ServerId': '1' * 32, 'Type': 'Episode', 'SeriesId': '7' * 32,
                    'ParentId': '9' * 32, 'IndexNumber': 1, 'ParentIndexNumber': 2, 'Path': str(output),
                    'MediaSources': [{'Id': 'c' * 32, 'Path': str(output), 'Size': 4096, 'Protocol': 'File', 'IsRemote': False}]}
                pending = [Connection(Response(json.dumps(value).encode())) for value in
                           ({'Items': [series]}, {'Items': [episode]}, {'Items': [episode]})]
                with patch.object(jf, '_Connection', side_effect=pending):
                    response = self.post(f'/cabinet/obtain/jobs/{job.pk}/advance/', {'expected_revision': str(transfer.revision)})
                self.assertEqual(response.status_code, 302, response.content)
                transfer.refresh_from_db()
                self.assertEqual(transfer.native_progress['stage'], 'verified', transfer.native_progress)
                copy_intent = transfer.native_progress['copy_intent']
                self.assertEqual(output.read_bytes(), video.read_bytes())
                # Reauthorize the actual synthetic owner after the source facts
                # expire. The verified series binding and original copy intent
                # can finish; the app neither resolves old source IDs nor copies
                # bytes again, and observation clocks are never renewed.
                from .test_portable_authority import PASSWORD
                self.clock.advance(301)
                expired = self.http.get(f'/cabinet/obtain/jobs/{job.pk}/status/', **self.headers)
                self.assertEqual(expired.status_code, 200)
                self.assertNotEqual(expired.json()['state'], 'done')
                self.authority.reauthenticate(self.ref, PASSWORD)
                connections = [Connection(Response(json.dumps(value).encode())) for value in
                               ({'Items': [series]}, {'Items': [episode]}, {'Items': [episode]})]
                with patch.object(jf, '_Connection', side_effect=connections):
                    with patch.object(imp, '_selected_source', side_effect=AssertionError('Second copy')), \
                            patch('shelf.portable_obtain_origin.resolve_obtain_origin_again', side_effect=AssertionError('Expired source lookup')):
                        response = self.post(f'/cabinet/obtain/jobs/{job.pk}/advance/', {'expected_revision': str(transfer.revision)})
                self.assertEqual(response.status_code, 302, response.content)
                transfer.refresh_from_db()
                self.assertEqual(transfer.native_progress['stage'], 'verified', transfer.native_progress)
                self.assertEqual(transfer.native_progress['copy_intent'], copy_intent)
                self.assertEqual(output.read_bytes(), video.read_bytes())
                self.assertEqual(transfer.binding['delivery_binding']['relative_parent'], self.delivery.relative_parent)
                with patch.object(provider, 'current', side_effect=AssertionError('GET opened services')):
                    status = self.http.get(f'/cabinet/obtain/jobs/{job.pk}/status/', **self.headers)
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.json()['state'], 'done')
                self.assertEqual(len(backend.calls), 1)
                self.assertTrue(all(connection.closed for connection in connections))
                from shelf.obtain_tv_flow import validate_native_progress
                from shelf.disclosure import read_json
                for field, changed in (('stage', 'invented_state'), ('source_id', 81001),
                                       ('series_ref', '0' * 32)):
                    malformed = read_json(canonical_bytes(transfer.native_progress))
                    malformed[field] = changed
                    with self.subTest(progress_field=field), self.assertRaises(acquire.ContractError):
                        validate_native_progress(malformed, binding=transfer.binding, job_id=job.pk,
                            verification=transfer.library_verification, provider_job=transfer.provider_job)
                malformed = read_json(canonical_bytes(transfer.native_progress))
                malformed['destination']['relative_path'] = 'Other Series/episode.mkv'
                with self.assertRaises(acquire.ContractError):
                    validate_native_progress(malformed, binding=transfer.binding, job_id=job.pk,
                        verification=transfer.library_verification, provider_job=transfer.provider_job)
                # Persisted verification JSON alone cannot survive issuer loss.
                self.authority._obtain_verified.clear()
                status = self.http.get(f'/cabinet/obtain/jobs/{job.pk}/status/', **self.headers)
                self.assertEqual(status.json()['state'], 'working')
                self.assertIsNone(status.json()['verification_until'])
