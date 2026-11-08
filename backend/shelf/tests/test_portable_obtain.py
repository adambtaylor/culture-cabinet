"""Native request authority and migration proofs on disposable synthetic stores.

No Django force_login, forged grants, private records or provider connections.
The existing recovery fixture supplies the real coordinator and in-memory DB.
"""
from contextvars import Context
from importlib import import_module
import json
import uuid
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone

from shelf import grant_authority as ga, recovery, saved, obtain_preview as preview
from shelf import obtain_acquisition as acquire
from shelf.acquisition_backend import PrivateSearch
from shelf.acquisition_contract import ConfirmedSelection
from shelf.acquisition_pipeline import AdapterUnavailable, Evidence, Snapshot, scope_family
from shelf.models import Profile, ProfileCabinetState, SavedItem, ObtainTransfer, AcquisitionJob
from shelf.portable_authority import AuthorityFault
from shelf.portable_saved_authority import (portable_saved_scope, select_portable_profile,
    selected_context, active_portable_access)
from .test_grant_authority import RecoveryCase
from .test_portable_authority import PASSWORD
from .test_obtain_preview import item, payload
from .test_obtain_acquisition import FixtureRetriever, FixtureBackend


class PortableObtainTests(RecoveryCase):
    def claim_owner(self, password=PASSWORD):
        # Advance genuine fixture sequences without adopting an existing user.
        dummy = get_user_model().objects.create(username='discarded-sequence-fixture')
        dummy.delete()
        self.options = self.settings(ROOT_URLCONF='cabinet.portable_urls', PORTABLE_AUTHORITY=self.authority,
            SESSION_COOKIE_NAME=self.boundary.session_cookie, CSRF_COOKIE_NAME=self.boundary.csrf_cookie,
            MIDDLEWARE=['cabinet.portable_boundary.PortableSecurityMiddleware',
                'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.csrf.CsrfViewMiddleware'])
        self.options.enable()
        self.addCleanup(self.options.disable)
        self.http = Client(enforce_csrf_checks=True)
        self.headers = {'HTTP_HOST': self.boundary.origin.removeprefix('http://'), 'HTTP_ORIGIN': self.boundary.origin}
        response = self.json_post('/setup/claim', {'code': self.challenge(), 'passphrase': password})
        self.assertEqual(response.status_code, 200)
        return self.http.session['portable_session']

    def setUp(self):
        super().setUp()
        unused = Profile.objects.create(kind='owner', label='Discarded fixture', active=True)
        unused.delete()
        self.profile_ref = self.bind()
        self.state = ProfileCabinetState.objects.get()
        self.profile_ref = ga.bind_existing_profile(self.authority, self.ref,
            profile_id=self.state.profile_id, confirmed=True)
        self.state.refresh_from_db()
        self.actor = get_user_model().objects.get(pk=self.state.actor_id)
        self.assertNotEqual(self.actor.pk, 1)
        self.assertNotEqual(self.state.profile_id, 1)
        self.selection = select_portable_profile(self.authority, self.ref, profile_ref=self.profile_ref)
        self.selection_token = selected_context(self.authority, self.ref, self.selection)
        session = self.http.session
        session['portable_saved_selection'] = self.selection
        session.save()

    def csrf(self):
        return self.http.get('/setup/status', **self.headers).json()['csrf_token']

    def json_post(self, path, data):
        return self.http.post(path, json.dumps(data), content_type='application/json',
            HTTP_X_CSRFTOKEN=self.csrf(), **self.headers)

    def post(self, path, data=None, *, csrf=True):
        fields = {'profile_ref': self.profile_ref, 'selection_token': self.selection_token}
        fields.update(data or {})
        return self.http.post(path, fields,
            **({'HTTP_X_CSRFTOKEN': self.csrf()} if csrf else {}), **self.headers)

    def scope(self):
        return portable_saved_scope(self.authority, self.ref, profile_ref=self.profile_ref,
            selection_token=self.selection_token, mutate=True)

    def saved_item(self, *, lineage=None, source_id=11):
        with ga.current_writer(self.authority, self.ref, self.profile_ref), transaction.atomic(), ga.writer_window(self.actor, self.state.profile_id):
            return SavedItem.objects.create(actor=self.actor, profile_id=self.state.profile_id,
                provider='tmdb', medium='movie', source_id=source_id, list_kind='watch',
                title='Fiction Film', url=f'https://www.themoviedb.org/movie/{source_id}',
                fetched_at=timezone.now(), publication_lineage=lineage)

    def lineage(self):
        return dict(schema='portable-saved-lineage/v1', installation_ref=str(self.state.installation_id),
            owner_revision=self.state.owner_revision, profile_ref=self.profile_ref,
            publication_ref=str(uuid.uuid4()), publication_revision=1, run_ref=str(uuid.uuid4()),
            candidate_key='tmdb:movie:11', candidate_digest='a'*64, source_evidence_digest='b'*64)

    def choice(self):
        row = self.saved_item()
        backend = Mock()
        backend.search.return_value = PrivateSearch(payload(item()))
        receipt = preview.search(actor=self.actor, profile_id=self.state.profile_id,
            item_id=row.pk, factory=lambda **kw: backend)
        result = receipt.results.get()
        job, _ = preview.confirm(actor=self.actor, profile_id=self.state.profile_id,
            preview_id=receipt.pk, result_id=result.pk, data=dict(review_token=preview.review_token(receipt, result),
                scope=receipt.scope, edition=result.metadata['title'], authorized_content='on',
                storage='local_library', storage_check='on'))
        return job

    def submit_fixture(self, job):
        context = acquire.OperatorContext(self.actor.pk, self.state.profile_id, 'offline_test', True)
        snapshot = Snapshot.from_saved_job(job)
        media, episode = scope_family(snapshot.provider, snapshot.medium, snapshot.scope)
        evidence = Evidence(snapshot, ConfirmedSelection(f'{snapshot.provider}:{snapshot.medium}:{snapshot.source_id}',
            media, snapshot.edition, snapshot.storage, True, True, True, True, True, True, episode))
        retriever, backend = FixtureRetriever(), FixtureBackend()
        # Distinct exact-result fixtures must carry distinct bytes: the existing
        # global NZB dedupe guard deliberately rejects cross-job byte reuse.
        retriever.payload = ('<?xml version="1.0"?><nzb><file subject="synthetic ' + str(job.request_id) + '"/></nzb>').encode()
        review = acquire.review_submission(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
            context=context, evidence=evidence, max_bytes=20000, retriever=retriever)
        return dict(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
            context=context, token=review['token'], confirmed=True, retriever=retriever, backend=backend)

    def test_live_generation_rejects_stale_snapshot_and_switch_back_without_fallback(self):
        with self.scope() as access:
            self.assertEqual(saved.authority(self.actor, self.state.profile_id).profile_id, self.state.profile_id)
            def rotate():
                select_portable_profile(self.authority, self.ref, profile_ref=self.profile_ref)
                select_portable_profile(self.authority, self.ref, profile_ref=self.profile_ref)
            Context().run(rotate)  # Another request, same real authority/lock.
            with self.assertRaises(AuthorityFault):
                saved.authority(self.actor, self.state.profile_id)
            with self.assertRaises(AuthorityFault):
                access.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)
        self.assertIsNone(active_portable_access())
        with self.assertRaises(AuthorityFault):
            access.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)

    def test_native_selection_csrf_strict_fields_login_logout_and_read_only_get(self):
        row = self.saved_item()
        self.assertEqual(self.http.get('/cabinet/saved/select/', **self.headers).status_code, 405)
        self.assertEqual(self.post('/cabinet/saved/select/', csrf=False).status_code, 403)
        self.assertEqual(self.post('/cabinet/saved/select/').status_code, 422)  # token not allowed here
        with patch.object(acquire, 'transfer_for', side_effect=AssertionError('GET must not reconcile')), \
             patch.object(acquire, 'repair_registration', side_effect=AssertionError('GET must not repair')):
            response = self.http.get('/cabinet/saved/', **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, row.title)
        self.assertNotIn('/profiles/', response.content.decode())
        self.assertEqual(getattr(self.authority, '_saved_selections')[self.ref][1], self.selection['nonce'])
        self.assertEqual(self.json_post('/setup/logout', {}).status_code, 200)
        self.assertNotIn(self.ref, self.authority._saved_selections)
        self.assertEqual(self.http.get('/cabinet/saved/', **self.headers).status_code, 403)
        self.assertEqual(self.json_post('/setup/login', {'passphrase': PASSWORD}).status_code, 200)
        self.assertEqual(self.http.get('/cabinet/saved/', **self.headers).status_code, 403)

    def test_native_selection_rejects_foreign_profile_and_duplicate_or_forged_save_fields(self):
        token = self.csrf()
        response = self.http.post('/cabinet/saved/select/', {'profile_ref': str(uuid.uuid4())},
            HTTP_X_CSRFTOKEN=token, **self.headers)
        self.assertEqual(response.status_code, 403)
        response = self.http.post('/cabinet/saved/select/', {'profile_ref': [self.profile_ref, self.profile_ref]},
            HTTP_X_CSRFTOKEN=token, **self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.post('/cabinet/saved/save/', dict(publication_revision='1', candidate_key='tmdb:movie:11',
            list_kind='watch', title='Forged title')).status_code, 422)
        self.assertFalse(SavedItem.objects.exists())

    def test_authority_expiry_recovery_and_cross_identity_are_denied(self):
        with self.scope() as access:
            for actor, profile in ((1, self.state.profile_id), (self.actor.pk, 1)):
                with self.assertRaises(AuthorityFault):
                    access.require(actor_id=actor, profile_id=profile)
            from shelf.models import Grant
            with ga.current_writer(self.authority, self.ref, self.profile_ref), transaction.atomic(), ga.writer_window(self.actor, self.state.profile_id):
                Grant.objects.filter(user=self.actor, profile_id=self.state.profile_id).update(can_export=False)
            with self.assertRaises(AuthorityFault):
                access.require(actor_id=self.actor.pk, profile_id=self.state.profile_id, action='export')
            self.clock.advance(3600)
            with self.assertRaises(AuthorityFault):
                access.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)

    def test_caps_profile_rotation_prevents_search_or_result_write(self):
        row = self.saved_item()
        backend = Mock()
        backend.caps.side_effect = lambda **kw: Context().run(select_portable_profile,
            self.authority, self.ref, profile_ref=self.profile_ref)
        with self.scope(), self.assertRaises(AuthorityFault):
            preview.search(actor=self.actor, profile_id=self.state.profile_id, item_id=row.pk, factory=lambda **kw: backend)
        backend.search.assert_not_called()
        from shelf.models import ObtainResult
        self.assertFalse(ObtainResult.objects.exists())

    def test_retrieval_rotation_denies_transfer_and_provider_creation(self):
        with self.scope():
            job = self.choice()
            args = self.submit_fixture(job)
            retrieve = args['retriever'].retrieve
            def rotated(exact):
                result = retrieve(exact)
                Context().run(select_portable_profile, self.authority, self.ref, profile_ref=self.profile_ref)
                return result
            with patch.object(args['retriever'], 'retrieve', side_effect=rotated), self.assertRaises(AuthorityFault):
                acquire.submit(**args)
            self.assertFalse(ObtainTransfer.objects.exists())
            self.assertFalse(args['backend'].calls)
            self.assertFalse(hasattr(args['backend'], 'binding'))

    def test_possible_send_exit_denial_preserves_unknown_and_never_resends(self):
        with self.scope():
            job = self.choice()
            args = self.submit_fixture(job)
            args['backend'].action = lambda: Context().run(select_portable_profile,
                self.authority, self.ref, profile_ref=self.profile_ref)
            args['backend'].error = AdapterUnavailable('synthetic lost acknowledgment')
            with self.assertRaises(AuthorityFault):
                acquire.submit(**args)
        transfer = ObtainTransfer.objects.get()
        self.assertEqual(transfer.state, 'outcome_unknown')
        self.selection = self.authority._saved_selections[self.ref]
        selection = {'profile_ref': self.profile_ref, 'nonce': self.selection[1]}
        self.selection_token = selected_context(self.authority, self.ref, selection)
        with self.scope(), patch.object(args['retriever'], 'retrieve', side_effect=AssertionError('no retry')):
            again = acquire.submit(**args)
        self.assertEqual(again.pk, transfer.pk)
        self.assertEqual(len(args['backend'].calls), 1)

    def test_lineage_raw_bulk_and_null_backfill_guards_preserve_floors(self):
        lineage = self.lineage()
        row = self.saved_item(lineage=lineage)
        legacy = None
        with ga.current_writer(self.authority, self.ref, self.profile_ref), transaction.atomic(), ga.writer_window(self.actor, self.state.profile_id):
            legacy = SavedItem.objects.create(actor=self.actor, profile_id=self.state.profile_id, provider='tmdb',
                medium='movie', source_id=12, list_kind='watch', title='Legacy null', fetched_at=timezone.now())
            for target, value in ((row, None), (row, dict(lineage, publication_revision=2)), (legacy, lineage)):
                with self.assertRaises(DatabaseError), transaction.atomic():
                    SavedItem.objects.filter(pk=target.pk).update(publication_lineage=value)
            with self.assertRaises(DatabaseError), transaction.atomic():
                SavedItem.objects.bulk_create([SavedItem(actor=self.actor, profile_id=self.state.profile_id, provider='tmdb',
                    medium='movie', source_id=13, list_kind='watch', title='Malformed', fetched_at=timezone.now(),
                    publication_lineage=dict(lineage, owner_revision=True))])
        before = ProfileCabinetState.objects.get().policy_epoch
        with self.assertRaises(DatabaseError), transaction.atomic():
            SavedItem.objects.filter(pk=row.pk).update(title='raw unauthorized')
        with self.assertRaises(DatabaseError), transaction.atomic():
            SavedItem.objects.filter(pk=row.pk).delete()
        self.assertEqual(ProfileCabinetState.objects.get().policy_epoch, before)
        row.refresh_from_db()
        self.assertEqual(row.publication_lineage, lineage)
        self.assertIsNone(SavedItem.objects.get(pk=legacy.pk).publication_lineage)

    def test_real_reverse_forward_retains_rows_and_exact_guards(self):
        row = self.saved_item(lineage=self.lineage())
        target = [('shelf', '0023_enhance_current_state')]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        self.assert_trusted_lineage_inventory(applied=False)
        historical = executor.loader.project_state(target).apps.get_model('shelf', 'SavedItem')
        self.assertEqual(historical.objects.get(pk=row.pk).title, row.title)
        ga._synthesis_guards()
        recovery._effective_guards(connection.connection)
        with self.assertRaises(DatabaseError), transaction.atomic():
            historical.objects.filter(pk=row.pk).update(title='still protected')
        MigrationExecutor(connection).migrate([('shelf', '0024_portable_saved_lineage')])
        self.assert_trusted_lineage_inventory(applied=True)
        row.refresh_from_db()
        self.assertIsNone(row.publication_lineage)  # Explicit rollback attribution loss only.
        ga._synthesis_guards()
        recovery._effective_guards(connection.connection)
        current = import_module('shelf.migrations.0024_portable_saved_lineage')
        self.assertEqual(len(current.TRIGGERS), 79)
        with self.assertRaises(DatabaseError), transaction.atomic():
            SavedItem.objects.filter(pk=row.pk).delete()

    def test_migration_install_failure_rolls_back_columns_records_and_all_guards(self):
        MigrationExecutor(connection).migrate([('shelf', '0023_enhance_current_state')])
        migration = import_module('shelf.migrations.0024_portable_saved_lineage')
        with patch.object(migration, 'forward_install', side_effect=RuntimeError('synthetic DDL failure')):
            # Migration operations retain their callable: patch its underlying
            # trusted installer, not the already captured RunPython reference.
            with patch.object(migration, 'install', side_effect=RuntimeError('synthetic DDL failure')):
                with self.assertRaisesRegex(RuntimeError, 'synthetic DDL failure'):
                    MigrationExecutor(connection).migrate([('shelf', '0024_portable_saved_lineage')])
        columns = {r[1] for r in connection.connection.execute('PRAGMA table_info(shelf_saveditem)')}
        self.assertNotIn('publication_lineage', columns)
        ga._synthesis_guards()
        recovery._effective_guards(connection.connection)
        MigrationExecutor(connection).migrate([('shelf', '0024_portable_saved_lineage')])

    def test_altered_guard_view_and_migration_record_mismatch_are_rejected(self):
        migration = import_module('shelf.migrations.0024_portable_saved_lineage')
        raw = connection.connection
        sql = raw.execute("SELECT sql FROM sqlite_master WHERE name='saved_lineage_immutable'").fetchone()[0]
        try:
            raw.execute('DROP TRIGGER saved_lineage_immutable')
            raw.execute(sql.replace('immutable saved lineage', 'altered definition'))
            with self.assertRaises(AuthorityFault):
                ga._synthesis_guards()
            with self.assertRaises(recovery.RecoveryBlocked):
                recovery._effective_guards(raw)
        finally:
            raw.execute('DROP TRIGGER saved_lineage_immutable')
            raw.execute(sql)
        raw.execute('CREATE VIEW rogue_saved_view AS SELECT id FROM shelf_saveditem')
        try:
            with self.assertRaises(recovery.RecoveryBlocked):
                recovery._effective_guards(raw)
        finally:
            raw.execute('DROP VIEW rogue_saved_view')
        with transaction.atomic():
            raw.execute("DELETE FROM django_migrations WHERE app='shelf' AND name='0024_portable_saved_lineage'")
            with self.assertRaises(AuthorityFault):
                ga._synthesis_guards()
            transaction.set_rollback(True)

    def test_native_three_media_search_selection_submit_and_readback_are_separate(self):
        """Fixture starts from deliberate saved intent, not an invented C4 Save."""
        from shelf import obtain_runtime
        from shelf.models import FeedbackEvent, ObtainPreview
        cases = [('tmdb', 'movie', 11, 'Fiction Film', 'Fiction Film 2024 1080p', '2000', 'watch'),
            ('tmdb', 'tv', 22, 'Fiction Series', 'Fiction Series S01E01 1080p', '5000', 'watch'),
            ('hardcover', 'book', 33, 'Fiction Book', 'Fiction Book complete audiobook', '3030', 'read')]
        for provider, medium, source_id, title, listing, category, kind in cases:
            with self.subTest(medium=medium):
                with ga.current_writer(self.authority, self.ref, self.profile_ref), transaction.atomic(), ga.writer_window(self.actor, self.state.profile_id):
                    row = SavedItem.objects.create(actor=self.actor, profile_id=self.state.profile_id,
                        provider=provider, medium=medium, source_id=source_id, list_kind=kind, title=title,
                        url=saved.canonical_url(provider, medium, source_id, 'https://hardcover.app/books/fiction-book'),
                        fetched_at=timezone.now())
                backend = Mock()
                backend.search.return_value = PrivateSearch(payload(item(title=listing, category=category, guid=str(source_id))))
                with patch.object(preview, 'search_backend', return_value=backend):
                    response = self.post(f'/cabinet/saved/{row.pk}/search/')
                self.assertEqual(response.status_code, 302)
                receipt = ObtainPreview.objects.get(saved_identity=row.pk)
                result = receipt.results.get()
                self.assertTrue(result.metadata['selectable'])
                root = f'/cabinet/saved/previews/{receipt.pk}/'
                self.assertEqual(response['Location'], root)
                response = self.http.get(root, **self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('/profiles/', response.content.decode())
                review_url = root + f'{result.pk}/review/'
                self.assertEqual(self.http.get(review_url, **self.headers).status_code, 200)
                confirmed = self.post(root + f'{result.pk}/confirm/', dict(review_token=preview.review_token(receipt, result),
                    scope=receipt.scope, edition=result.metadata['title'], authorized_content='on',
                    storage='local_library', storage_check='on'))
                self.assertEqual(confirmed.status_code, 302)
                job = AcquisitionJob.objects.get(request_id=receipt.pk)
                native_job = f'/cabinet/saved/jobs/{job.pk}/'
                self.assertEqual(confirmed['Location'], native_job)
                self.assertFalse(ObtainTransfer.objects.filter(job=job).exists())
                with self.scope():
                    args = self.submit_fixture(job)
                    exact, _ = acquire.exact_result(job, fresh=True)
                media, episode = scope_family(provider, medium, job.scope)
                evidence = Evidence(exact.snapshot, ConfirmedSelection(f'{provider}:{medium}:{source_id}', media,
                    job.edition, job.storage, True, True, True, True, True, True, episode))
                services = obtain_runtime.SubmissionServices(args['context'], evidence, 20000,
                    args['retriever'], args['backend'])
                with patch.object(obtain_runtime, 'submission_factory', return_value=services):
                    response = self.post(native_job + 'submit/review/')
                    self.assertEqual(response.status_code, 200)
                    token = response.context['obtain_submit_form'].initial['token']
                    data = dict(token=token, exact_selection='on', rights='on', scope='on', size='on', duplicates='on')
                    self.assertEqual(self.post(native_job + 'submit/', data).status_code, 302)
                    self.assertEqual(self.post(native_job + 'submit/', data).status_code, 302)
                self.assertEqual(len(args['backend'].calls), 1)
                with patch.object(acquire, 'repair_registration', side_effect=AssertionError('GET repair forbidden')), \
                     patch.object(acquire, 'transfer_for', side_effect=AssertionError('GET reconcile forbidden')):
                    response = self.http.get(native_job, **self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'queued')
                self.assertNotIn('/profiles/', response.content.decode())
                self.assertEqual(ObtainTransfer.objects.get(job=job).state, 'queued')
        self.assertEqual(ObtainTransfer.objects.count(), 3)
        self.assertFalse(FeedbackEvent.objects.exists())

    def test_native_ps1_missing_publication_and_foreign_recovery_fail_without_save(self):
        from .test_grant_authority import AuthorityCase
        with self.scope() as access:
            with AuthorityCase.controlled_recovery_state(self, 'foreign_witness'):
                with self.assertRaises(AuthorityFault):
                    access.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)
            access.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)
        response = self.post('/cabinet/saved/save/', dict(publication_revision='1',
            candidate_key='tmdb:movie:11', list_kind='watch'))
        self.assertEqual(response.status_code, 409)
        self.assertFalse(SavedItem.objects.exists())

    def test_save_consumer_expected_floors_duplicates_and_legacy_null_with_synthetic_ps1_port(self):
        """Consumer transaction proof only: PS1 rights/publication are a double.

        This must never stand in for a native successful Save or durable rights.
        Real PS1 remains fail-closed until that upstream contract is supplied.
        """
        from contextlib import contextmanager
        from dataclasses import replace
        from shelf.portable_enhance_service import EnhanceService, SavedCandidateEvidence
        service = EnhanceService(self.authority)
        lineage = self.lineage()
        evidence = SavedCandidateEvidence(str(self.state.installation_id), self.state.owner_revision,
            self.profile_ref, self.actor.pk, self.state.profile_id, lineage['publication_ref'], 1,
            lineage['run_ref'], lineage['candidate_key'], lineage['candidate_digest'], lineage['source_evidence_digest'],
            'tmdb', 'movie', 11, 'Synthetic consumer attribution', 'https://www.themoviedb.org/movie/11',
            self.authority.now(), self.expires)
        @contextmanager
        def synthetic_window(session_ref, *, evidence):
            with ga.recovery_guard(self.authority, session_ref), self.authority._lock:
                yield evidence
        with self.scope() as access, patch.object(service, 'saved_candidate_window', side_effect=synthetic_window), \
             patch.object(service, 'resolve_saved_candidate', return_value=evidence) as resolver:
            def consume():
                return saved.save_publication_candidate(access=access, service=service,
                    expected_publication_revision=resolver.return_value.publication_revision,
                    candidate_key=resolver.return_value.candidate_key, list_kind='watch')
            before = ProfileCabinetState.objects.get()
            row, created = consume()
            self.assertTrue(created)
            after = ProfileCabinetState.objects.get()
            self.assertEqual((after.policy_epoch, after.restriction_watermark),
                (before.policy_epoch+1, before.restriction_watermark+1))
            original = SavedItem.objects.filter(pk=row.pk).values().get()
            resolver.return_value = replace(evidence, title='Later different attribution', publication_revision=2,
                publication_ref=str(uuid.uuid4()))
            duplicate, created = consume()
            self.assertFalse(created)
            self.assertEqual(SavedItem.objects.filter(pk=duplicate.pk).values().get(), original)
            current = ProfileCabinetState.objects.get()
            self.assertEqual((current.policy_epoch, current.restriction_watermark),
                (after.policy_epoch, after.restriction_watermark))
            with ga.current_writer(self.authority, self.ref, self.profile_ref), transaction.atomic(), ga.writer_window(self.actor, self.state.profile_id):
                legacy = SavedItem.objects.create(actor=self.actor, profile_id=self.state.profile_id, provider='tmdb',
                    medium='movie', source_id=12, list_kind='watch', title='Original legacy intent', fetched_at=self.authority.now())
            resolver.return_value = replace(evidence, source_id=12, candidate_key='tmdb:movie:12',
                url='https://www.themoviedb.org/movie/12')
            duplicate, created = consume()
            self.assertFalse(created)
            self.assertEqual(duplicate.pk, legacy.pk)
            self.assertEqual(duplicate.title, 'Original legacy intent')
            self.assertIsNone(duplicate.publication_lineage)

    def assert_trusted_lineage_inventory(self, *, applied):
        import re
        name = '0024_portable_saved_lineage' if applied else '0023_enhance_current_state'
        definitions = import_module('shelf.migrations.' + name).TRIGGERS
        expected = set()
        for sql in definitions:
            match = re.match(r'CREATE TRIGGER (\w+)\s+(?:BEFORE|AFTER) (?:INSERT|UPDATE|DELETE) ON (\w+)\b', sql)
            self.assertIsNotNone(match)
            expected.add((match[1], match[2], sql.strip().removesuffix(';').rstrip()))
        raw = connection.connection
        actual = {(n, t, sql.strip().removesuffix(';').rstrip()) for n, t, sql in
            raw.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'")}
        self.assertEqual(actual, expected)
        recorded = raw.execute("SELECT 1 FROM django_migrations WHERE app='shelf' AND name='0024_portable_saved_lineage'").fetchone() is not None
        self.assertEqual(recorded, applied)
        self.assertEqual('publication_lineage' in {r[1] for r in raw.execute('PRAGMA table_info(shelf_saveditem)')}, applied)


    def rotate_before_effect(self, original, *, ordinal=1):
        from contextlib import contextmanager
        calls = []
        @contextmanager
        def window(actor, profile_id, **kwargs):
            calls.append(True)
            if len(calls) == ordinal:
                Context().run(select_portable_profile, self.authority, self.ref, profile_ref=self.profile_ref)
            with original(actor, profile_id, **kwargs):
                yield
        return window


    def test_pre_caps_stale_generation_never_acquires_backend_or_caps(self):
        row = self.saved_item()
        backend, factory = Mock(), Mock()
        factory.return_value = backend
        original = preview.effect_for
        with self.scope(), patch.object(preview, 'effect_for', self.rotate_before_effect(original, ordinal=2)),              self.assertRaises(AuthorityFault):
            preview.search(actor=self.actor, profile_id=self.state.profile_id, item_id=row.pk, factory=factory)
        factory.assert_not_called()
        backend.caps.assert_not_called()
        backend.search.assert_not_called()
        self.assertFalse(ObtainTransfer.objects.exists())


    def test_pre_retrieve_stale_generation_never_creates_transfer_or_provider(self):
        with self.scope():
            job = self.choice()
            args = self.submit_fixture(job)
            with patch.object(args['retriever'], 'retrieve') as retrieved,                  patch.object(args['backend'], 'provider') as provider,                  patch.object(acquire, 'effect_for', self.rotate_before_effect(acquire.effect_for)),                  self.assertRaises(AuthorityFault):
                acquire.submit(**args)
            retrieved.assert_not_called()
            provider.assert_not_called()
            self.assertFalse(ObtainTransfer.objects.exists())


    def test_pre_review_stale_generation_never_creates_reviewed_descriptor(self):
        with self.scope():
            job = self.choice()
            args = self.submit_fixture(job)
            exact, _ = acquire.exact_result(job, fresh=True)
            media, episode = scope_family(job.provider, job.medium, job.scope)
            evidence = Evidence(exact.snapshot, ConfirmedSelection(f'{job.provider}:{job.medium}:{job.source_id}',
                media, job.edition, job.storage, True, True, True, True, True, True, episode))
            with patch.object(args['retriever'], 'review', create=True) as reviewed,                  patch.object(args['retriever'], 'retrieve') as retrieved,                  patch.object(acquire, 'effect_for', self.rotate_before_effect(acquire.effect_for)),                  self.assertRaises(AuthorityFault):
                acquire.review_submission(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk,
                    context=args['context'], evidence=evidence, max_bytes=20000, retriever=args['retriever'])
            reviewed.assert_not_called()
            retrieved.assert_not_called()
            from shelf.models import ObtainReviewedNZB
            self.assertFalse(ObtainReviewedNZB.objects.exists())
            self.assertFalse(ObtainTransfer.objects.exists())


    def test_threaded_logout_waits_for_completed_effect_and_denies_next_effect(self):
        import threading
        started, completed = threading.Event(), threading.Event()
        errors, events = [], []
        def logout():
            started.set()
            try:
                self.authority.logout(self.ref)
                events.append('logout')
            except BaseException as error:
                errors.append(error)
            finally:
                completed.set()
        worker = threading.Thread(target=logout)
        self.addCleanup(lambda: worker.join(2) if worker.ident is not None else None)
        with self.scope() as access:
            try:
                with access.effect_window(action='select'):
                    worker.start()
                    self.assertTrue(started.wait(1))
                    self.assertFalse(completed.wait(0.05))
                    events.append('completed earlier effect')
            except AuthorityFault as fault:
                # Logout may win after owner-lock release and before the outer
                # recovery checkpoint exits. That truthful exit denial must not
                # erase a completed earlier effect or authorize the next one.
                self.assertEqual(fault.code, 'UNAUTHENTICATED')
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            self.assertEqual(events, ['completed earlier effect', 'logout'])
            later = Mock()
            with self.assertRaises(AuthorityFault):
                with access.effect_window(action='select'):
                    later()
            later.assert_not_called()


    def test_upgraded_actual_backup_restore_preserves_saved_intent(self):
        """Actual isolated restore, without bypassing writer/neutralization guards."""
        import sqlite3
        from pathlib import Path
        self.saved_item()
        historical = dict(self.lineage(), candidate_key='tmdb:movie:99')
        self.saved_item(lineage=historical, source_id=99)
        root = Path(self.directory.name)
        coordinator = recovery.CurrentRecoveryCoordinator.provision_empty(
            store_id=recovery.raw_store(connection.connection), journal_directory=root/'restore-journal',
            trusted_empty_db=connection.connection)
        self.addCleanup(coordinator.close)
        folder = root/'restore-backups'
        folder.mkdir(mode=0o700)
        live = folder/'source.sqlite3'
        disk = sqlite3.connect(live)
        try:
            connection.connection.backup(disk)
        finally:
            disk.close()
        live.chmod(0o600)
        source = sqlite3.connect(f'file:{live}?mode=ro', uri=True)
        source.execute('PRAGMA query_only=ON')
        with recovery.using_recovery(coordinator):
            try:
                manifest = recovery.consistent_backup(source=source,
                    destination=recovery.ExclusiveScratchFile(folder/'backup.sqlite3'), recovery=coordinator)
            finally:
                source.close()
            sandbox = root/'restored'
            sandbox.mkdir(mode=0o700)
            failed_sql = []
            class ObservedConnection(sqlite3.Connection):
                def execute(self, statement, *args, **kwargs):
                    try:
                        return super().execute(statement, *args, **kwargs)
                    except sqlite3.Error as error:
                        failed_sql.append((statement, str(error)))
                        raise
            original = sqlite3.connect
            def observed(*args, **kwargs):
                return original(*args, factory=ObservedConnection, **kwargs)
            with patch.object(recovery.sqlite3, 'connect', side_effect=observed):
                report = recovery.isolated_restore(backup=recovery.ReadOnlyBackup(folder/'backup.sqlite3', manifest),
                    sandbox=sandbox, recovery=coordinator)
            self.assertEqual(report.state, 'ISOLATED_READY', (report, failed_sql))
            restored = sqlite3.connect(report.isolated_database)
            try:
                recovery._effective_guards(restored)
                self.assertEqual(restored.execute('SELECT count(*) FROM shelf_saveditem').fetchone()[0], 2)
                values = restored.execute('SELECT publication_lineage FROM shelf_saveditem ORDER BY source_id').fetchall()
                self.assertIsNone(values[0][0])
                self.assertEqual(json.loads(values[1][0]), historical)
                self.assertEqual(restored.execute('SELECT count(*) FROM django_session').fetchone()[0], 0)
                self.assertEqual(restored.execute('SELECT count(*) FROM auth_user WHERE is_active!=0').fetchone()[0], 0)
                self.assertEqual(restored.execute('SELECT count(*) FROM shelf_grant WHERE can_view OR can_select OR can_mutate OR can_export').fetchone()[0], 0)
                from hashlib import sha256
                trusted = import_module('shelf.migrations.0024_portable_saved_lineage')
                print('N1_ISOLATED_RESTORE_PROOF ' + json.dumps(dict(state=report.state, checks=report.checks,
                    saved_rows=2, lineage_versions=['null', 'v1'], backup_sha256=manifest.database_sha256,
                    restored_sha256=sha256(Path(report.isolated_database).read_bytes()).hexdigest(),
                    exact_trigger_count=len(trusted.TRIGGERS), temporary_databases_removed_by_fixture=True)))
                # Restore data checks and runtime inventory admission are distinct:
                # the same restored store must still reject an altered definition.
                guard = trusted.LINEAGE_GUARDS[1]
                restored.execute('DROP TRIGGER saved_lineage_immutable')
                restored.execute(guard.replace('BEGIN ', 'BEGIN SELECT 1; ', 1))
                with self.assertRaises(recovery.RecoveryBlocked):
                    recovery._effective_guards(restored)
            finally:
                restored.close()

    def test_real_session_replacement_nested_and_closed_scopes_never_reuse_selection(self):
        with self.scope() as outer:
            with self.scope() as inner:
                with self.assertRaises(AuthorityFault):
                    outer.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)
                inner.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)
            outer.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)
            with self.assertRaises(AuthorityFault):
                inner.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)
            previous = self.ref
            response = self.json_post('/setup/login', {'passphrase': PASSWORD})
            self.assertEqual(response.status_code, 200)
            replacement = self.http.session['portable_session']
            self.assertNotEqual(replacement, previous)
            self.assertNotIn(previous, self.authority._saved_selections)
            with self.assertRaises(AuthorityFault):
                outer.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)
        with self.assertRaises(AuthorityFault):
            outer.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)
        with self.assertRaises(AuthorityFault), portable_saved_scope(self.authority, replacement,
                profile_ref=self.profile_ref, selection_token=self.selection_token):
            self.fail('Old session selection accepted under replacement session')
        selection = select_portable_profile(self.authority, replacement, profile_ref=self.profile_ref)
        token = selected_context(self.authority, replacement, selection)
        with portable_saved_scope(self.authority, replacement, profile_ref=self.profile_ref,
                selection_token=token) as fresh:
            fresh.require(actor_id=self.actor.pk, profile_id=self.state.profile_id)
            with self.assertRaises(AuthorityFault):
                fresh.require(actor_id=1, profile_id=self.state.profile_id)
        self.assertNotIn('_auth_user_id', self.http.session)

    def test_component_precommit_generation_loss_rolls_back_saved_row_and_floors(self):
        """Synthetic PS1 port only; genuine PA1 and SQL precommit rollback.

        This exercises a reentrant invalidation at the consumer boundary, not a
        second owner, threaded concurrency or successful native retention rights.
        """
        from contextlib import contextmanager
        from shelf.portable_enhance_service import EnhanceService, SavedCandidateEvidence
        service = EnhanceService(self.authority)
        lineage = self.lineage()
        evidence = SavedCandidateEvidence(str(self.state.installation_id), self.state.owner_revision,
            self.profile_ref, self.actor.pk, self.state.profile_id, lineage['publication_ref'], 1,
            lineage['run_ref'], lineage['candidate_key'], lineage['candidate_digest'], lineage['source_evidence_digest'],
            'tmdb', 'movie', 11, 'Synthetic consumer attribution', 'https://www.themoviedb.org/movie/11',
            self.authority.now(), self.expires)
        @contextmanager
        def synthetic_window(session_ref, *, evidence):
            with ga.recovery_guard(self.authority, session_ref), self.authority._lock:
                yield evidence
        before = ProfileCabinetState.objects.get(pk=self.state.pk)
        manager = SavedItem.objects
        original = manager.get_or_create
        def create_then_invalidate(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertTrue(connection.in_atomic_block)
            self.assertTrue(result[1])
            Context().run(select_portable_profile, self.authority, self.ref, profile_ref=self.profile_ref)
            return result
        with self.scope() as access, patch.object(service, 'saved_candidate_window', side_effect=synthetic_window), \
             patch.object(service, 'resolve_saved_candidate', return_value=evidence), \
             patch.object(manager, 'get_or_create', side_effect=create_then_invalidate), self.assertRaises(AuthorityFault):
            saved.save_publication_candidate(access=access, service=service,
                expected_publication_revision=1, candidate_key=evidence.candidate_key, list_kind='watch')
        self.assertFalse(SavedItem.objects.exists())
        after = ProfileCabinetState.objects.get(pk=before.pk)
        self.assertEqual((after.policy_epoch, after.restriction_watermark),
                         (before.policy_epoch, before.restriction_watermark))
        self.assertFalse(AcquisitionJob.objects.exists())
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_native_unconfigured_submission_review_keeps_page_and_owned_status_navigation(self):
        from shelf import obtain_runtime
        from shelf.models import ObtainReviewedNZB
        with self.scope():
            job = self.choice()
        def content_state():
            state = ProfileCabinetState.objects.get(pk=self.state.pk)
            return (SavedItem.objects.count(), AcquisitionJob.objects.count(),
                ObtainTransfer.objects.count(), ObtainReviewedNZB.objects.count(),
                state.policy_epoch, state.restriction_watermark)
        before = content_state()
        with self.settings(OBTAIN_SUBMISSION_POLICY=None), \
             patch.object(obtain_runtime.store, 'open_store', side_effect=AssertionError('No private reads')) as opened, \
             patch.object(obtain_runtime, 'check_capacity', side_effect=AssertionError('No storage reads')) as capacity, \
             patch.object(obtain_runtime, 'Backend', side_effect=AssertionError('No provider')) as backend, \
             patch.object(acquire, 'review_submission', side_effect=AssertionError('No descriptor')) as reviewed, \
             patch.object(acquire, 'repair_registration', side_effect=AssertionError('No repair')) as repaired, \
             patch.object(acquire, 'transfer_for', side_effect=AssertionError('No reconciliation')) as reconciled:
            response = self.post(f'/cabinet/saved/jobs/{job.pk}/submit/review/')
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response['Content-Type'].startswith('text/html'))
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertContains(response, '<title>Enhance · Culture Cabinet</title>', status_code=409)
        self.assertContains(response, '<h1>Your saved items and Obtain requests</h1>', status_code=409, html=True)
        self.assertContains(response, 'role="alert"', status_code=409)
        self.assertContains(response, 'Submission review is unavailable or has changed.', status_code=409)
        self.assertContains(response, f'href="/cabinet/saved/jobs/{job.pk}/"', status_code=409)
        self.assertContains(response, f'href="/setup/cabinet?profile_ref={self.profile_ref}"', status_code=409)
        self.assertContains(response, 'href="/setup/cabinet"', status_code=409)
        self.assertNotContains(response, 'Confirm and submit this exact selection once', status_code=409)
        for effect in (opened, capacity, backend, reviewed, repaired, reconciled):
            effect.assert_not_called()
        self.assertEqual(content_state(), before)

    def test_unavailable_submission_review_never_projects_after_selection_loss(self):
        from shelf import obtain_runtime, portable_obtain_views
        with self.scope():
            job = self.choice()
        before = ProfileCabinetState.objects.get(pk=self.state.pk)
        def invalidate_then_refuse(**kwargs):
            Context().run(select_portable_profile, self.authority, self.ref, profile_ref=self.profile_ref)
            raise acquire.Blocked('SYNTHETIC_PRIVATE_PROVIDER_ERROR')
        with patch.object(obtain_runtime, 'submission_factory', side_effect=invalidate_then_refuse) as factory, \
             patch.object(portable_obtain_views, '_render', side_effect=AssertionError('No private projection')) as rendered:
            response = self.post(f'/cabinet/saved/jobs/{job.pk}/submit/review/')
        factory.assert_called_once()
        rendered.assert_not_called()
        self.assertContains(response, 'SELECTION_STALE', status_code=409)
        self.assertNotContains(response, job.title, status_code=409)
        self.assertNotContains(response, 'SYNTHETIC_PRIVATE_PROVIDER_ERROR', status_code=409)
        # The old form remains stale on the next request: it cannot acquire the
        # backend or obtain private context just to display its earlier error.
        with patch.object(obtain_runtime, 'submission_factory', side_effect=AssertionError('Stale factory')) as factory, \
             patch.object(portable_obtain_views, '_render', side_effect=AssertionError('Stale projection')) as rendered:
            response = self.post(f'/cabinet/saved/jobs/{job.pk}/submit/review/')
        self.assertEqual(response.status_code, 409)
        factory.assert_not_called()
        rendered.assert_not_called()
        self.assertNotContains(response, job.title, status_code=409)
        self.assertFalse(ObtainTransfer.objects.exists())
        after = ProfileCabinetState.objects.get(pk=before.pk)
        self.assertEqual((after.policy_epoch, after.restriction_watermark),
                         (before.policy_epoch, before.restriction_watermark))
