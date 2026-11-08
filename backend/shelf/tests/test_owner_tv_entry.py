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
    def __init__(self, delivery):
        self.delivery = delivery

    def delivery_binding(self, access, job):
        access.require(actor_id=job.actor_id, profile_id=job.profile_id, action='mutate')
        return self.delivery

    def current(self, context):
        raise AssertionError('Submission fixture must not launch a service worker')


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
        from shelf.obtain_runtime import SubmissionContext, SubmissionPolicy
        from shelf.acquisition_contract import COMPLETION_SCRIPT
        complete = Path(self.directory.name) / 'source-complete'; complete.mkdir()
        policy = SubmissionPolicy(True, str(complete), 20000, 0, True,
            str(Path(self.directory.name) / 'source-registry'), True, COMPLETION_SCRIPT)
        options = self.settings(OBTAIN_SUBMISSION_POLICY=asdict(policy)); options.enable(); self.addCleanup(options.disable)
        from shelf.jellyfin_native import TV_ROOTS
        self.delivery = DeliveryBinding(DestinationBinding('synthetic-destination', 'synthetic-mac', TV_ROOTS[0],
            ServiceGeneration('destination_file', 'd' * 64, 'synthetic-config1', 'synthetic-key1')),
            'Fiction Series 2024', False)
        self.delivery_provider = FixtureTVProvider(self.delivery)
        options = self.settings(PORTABLE_OBTAIN_TV_SERVICES=self.delivery_provider)
        options.enable(); self.addCleanup(options.disable)
        return job, SubmissionContext(self.actor.pk, self.state.profile_id, job.pk, policy)

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
