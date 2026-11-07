"""Saved intent integration: actual templates, local test DBs, no provider I/O."""
from copy import deepcopy
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, connection, connections, transaction
from django.db.backends.sqlite3._functions import register
from django.db.migrations.executor import MigrationExecutor
from django.http import Http404
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings

from cabinet import boundary
from shelf import intake, provider_catalogue as catalogue, saved
from shelf.models import AcquisitionJob, FeedbackEvent, FeedbackProjection, Grant, Profile, SavedItem


class SavedFixture:
    def setUp(self):
        super().setUp()
        self.owner = get_user_model().objects.create_user(pk=1, username='saved-fiction-owner')
        self.other = get_user_model().objects.create_user(pk=2, username='saved-fiction-other')
        self.profile = Profile.objects.create(pk=1, kind='owner', label='Saved fiction owner', active=True)
        self.partner = Profile.objects.create(pk=2, kind='partner', label='Saved fiction partner', active=True)
        self.grant = Grant.objects.create(user=self.owner, profile=self.profile,
            can_view=True, can_select=True, can_mutate=False, can_export=False)
        Grant.objects.create(user=self.other, profile=self.partner, can_view=True, can_select=True)
        self.client.force_login(self.owner)
        self.client.post('/profiles/select/', {'profile_id': 1})
        cards = []
        for provider, medium, row in (
                ('tmdb', 'movie', {'id': 11, 'title': 'Fiction Film'}),
                ('tmdb', 'tv', {'id': 22, 'name': 'Fiction Series'}),
                ('hardcover', 'book', {'id': 33, 'title': 'Fiction Book', 'slug': 'fiction-book'})):
            cards.extend(catalogue.parse_candidates(provider, medium, [row], lane=medium,
                                                    lane_label='Fiction editorial lane'))
        state = intake.new_state(1, 1)
        intake.merge(state, cards)
        intake.select_round(state)
        session = self.client.session
        session[intake.SESSION_KEY] = state
        session.save()
        self.base = '/profiles/1/saved/'
        self.keys = {c.medium: c.key for c in cards}

    def heart(self, medium='movie', kind=None, **extra):
        return self.client.post(self.base + 'save/', {'item_id': self.keys[medium],
            'list_kind': kind or ('read' if medium == 'book' else 'watch'), **extra})

    def item(self, medium='movie', kind=None):
        self.assertEqual(self.heart(medium, kind).status_code, 302)
        return SavedItem.objects.get(actor=self.owner, medium=medium,
                                    list_kind=kind or ('read' if medium == 'book' else 'watch'))

    def review(self, item):
        return self.client.get(f'{self.base}{item.pk}/obtain/')

    def confirmation(self, item):
        response = self.review(item)
        self.assertEqual(response.status_code, 200)
        return {**response.context['obtain_form'].initial, 'edition': 'Explicit fiction edition, English, complete',
                'authorized_content': 'on', 'storage': 'local_library', 'storage_check': 'on'}

    def confirm(self, item, data):
        return self.client.post(f'{self.base}{item.pk}/obtain/confirm/', data)


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class SavedTests(SavedFixture, TestCase):
    def test_hearts_all_media_are_durable_intent_and_not_feedback_or_choices(self):
        before = deepcopy(self.client.session[intake.SESSION_KEY])
        for medium in self.keys:
            self.assertEqual(self.heart(medium).status_code, 302)
            self.assertEqual(self.heart(medium, 'favorites').status_code, 302)
        self.assertEqual(SavedItem.objects.count(), 6)
        self.assertEqual(FeedbackEvent.objects.count(), 0)
        self.assertEqual(FeedbackProjection.objects.count(), 0)
        self.assertEqual(self.client.session[intake.SESSION_KEY], before)
        self.grant.refresh_from_db()
        self.assertFalse(self.grant.can_mutate)
        self.assertFalse(self.grant.can_export)
        response = self.client.get(self.base)
        self.assertTemplateUsed(response, 'cabinet/saved_items.html')
        self.assertContains(response, 'Fiction Film')
        self.assertEqual([len(s['items']) for s in response.context['saved_lists']], [2, 1, 3])

    def test_duplicate_heart_transactional_and_explicit_remove_idempotent(self):
        item = self.item()
        self.assertEqual(self.heart().status_code, 302)
        self.assertEqual(SavedItem.objects.count(), 1)
        self.assertEqual(SavedItem.objects.get().created_at, item.created_at)
        for _ in range(2):
            self.assertEqual(self.client.post(f'{self.base}{item.pk}/remove/').status_code, 302)
        self.assertEqual(SavedItem.objects.count(), 0)

    def test_expired_intake_and_new_login_keep_records_but_cannot_save_stale_candidate(self):
        item = self.item()
        session = self.client.session
        state = session[intake.SESSION_KEY]
        state['expires_at'] = time.time() - 1
        session[intake.SESSION_KEY] = state
        session.save()
        self.assertContains(self.client.get(self.base), item.title)
        self.assertEqual(self.heart().status_code, 409)
        self.client.logout()
        self.assertEqual(self.client.get(self.base).status_code, 302)
        self.client.force_login(self.owner)
        self.client.post('/profiles/select/', {'profile_id': 1})
        self.assertContains(self.client.get(self.base), item.title)
        self.assertNotIn(intake.SESSION_KEY, self.client.session)
        self.assertEqual(self.review(item).status_code, 200)

    def test_profile_and_actor_isolation_and_revalidation(self):
        item = self.item()
        for path in ('/profiles/2/saved/', '/profiles/999/saved/'):
            self.assertEqual(self.client.get(path).status_code, 404)
        other = Client()
        other.force_login(self.other)
        session = other.session
        session['selected_profile_id'] = 1
        session.save()
        self.assertEqual(other.get(self.base).status_code, 404)
        self.assertEqual(other.post(f'{self.base}{item.pk}/remove/').status_code, 404)
        for field in ('can_view', 'can_select'):
            Grant.objects.filter(pk=self.grant.pk).update(**{field: False})
            self.assertEqual(self.heart().status_code, 404)
            self.assertEqual(self.review(item).status_code, 404)
            Grant.objects.filter(pk=self.grant.pk).update(**{field: True})
        Profile.objects.filter(pk=1).update(active=False)
        self.assertEqual(self.client.get(self.base).status_code, 404)

    def test_service_authority_does_not_accept_other_actor_or_profile(self):
        for actor, profile in ((self.other, 1), (self.owner, 2)):
            with self.assertRaises(Http404):
                saved.save_candidate(actor=actor, profile_id=profile, session={},
                                     item_id=self.keys['movie'], list_kind='watch')

    def test_arbitrary_payload_duplicate_fields_and_missing_candidates_rejected(self):
        for extra in ({'url': 'https://evil.invalid/'}, {'title': 'Forged title'},
                      {'provider': 'sab'}, {'payload': 'arbitrary'}):
            self.assertEqual(self.heart(**extra).status_code, 400)
        self.assertEqual(self.client.post(self.base + 'save/',
            {'item_id': [self.keys['movie'], self.keys['book']], 'list_kind': 'watch'}).status_code, 400)
        self.assertEqual(self.client.post(self.base + 'save/',
            {'item_id': 'tmdb:movie:999', 'list_kind': 'watch'}).status_code, 409)
        self.assertEqual(SavedItem.objects.count(), 0)

    def test_invalid_list_medium_and_server_provenance_fail_closed(self):
        for medium, kind in (('movie', 'read'), ('tv', 'read'), ('book', 'watch'), ('book', 'enjoyed')):
            self.assertEqual(self.heart(medium, kind).status_code, 400)
        session = self.client.session
        state = session[intake.SESSION_KEY]
        state['candidates'][self.keys['movie']]['url'] = 'https://evil.invalid/film'
        session[intake.SESSION_KEY] = state
        session.save()
        self.assertEqual(self.heart().status_code, 400)
        self.assertEqual(SavedItem.objects.count(), 0)

    def test_saved_links_revalidated_and_never_include_preferences(self):
        film = self.item()
        book = self.item('book')
        SavedItem.objects.filter(pk=film.pk).update(url='javascript:alert(1)')
        SavedItem.objects.filter(pk=book.pk).update(url='https://hardcover.app.evil.invalid/books/fiction-book')
        response = self.client.get(self.base)
        self.assertContains(response, 'https://www.themoviedb.org/movie/11')
        self.assertNotContains(response, 'javascript:')
        self.assertNotContains(response, 'evil.invalid')
        for shelf in response.context['saved_lists']:
            for row in shelf['items']:
                self.assertNotIn('?', row['url'])
                self.assertNotIn('#', row['url'])

    def test_actual_catalogue_save_forms_have_fixed_urls(self):
        response = self.client.get('/profiles/1/')
        self.assertTrue(response.context['saved_available'])
        self.assertEqual(response.context['saved_url'], self.base)
        self.assertContains(response, 'name="list_kind"')
        self.assertContains(response, f'action="{self.base}save/"', count=3)
        self.assertTrue(all(c['save_url'] == self.base + 'save/' for c in response.context['cards']))

    def test_review_is_exact_scope_and_has_backend_form_contract(self):
        for medium, scope in saved.SCOPES.items():
            item = self.item(medium)
            response = self.review(item)
            self.assertEqual(response.context['obtain_confirmation']['scope'], scope)
            self.assertEqual(response.context['obtain_confirmation']['title'], item.title)
            self.assertIn('unknown', response.context['obtain_confirmation']['storage'])
            self.assertEqual(set(response.context['obtain_form'].fields),
                {'review_token', 'scope', 'edition', 'authorized_content', 'storage', 'storage_check'})
        self.assertEqual(AcquisitionJob.objects.count(), 0)

    def test_all_confirmations_block_configuration_and_never_claim_transfer(self):
        for medium, scope in saved.SCOPES.items():
            item = self.item(medium)
            response = self.confirm(item, self.confirmation(item))
            self.assertEqual(response.status_code, 302)
            job = AcquisitionJob.objects.get(medium=medium)
            self.assertEqual(job.scope, scope)
            self.assertEqual(job.status, 'blocked_configuration')
            status = self.client.get(response['Location'])
            self.assertEqual(status.context['acquisition_job']['status'], 'blocked_configuration')
            self.assertContains(status, 'acquisition is unconfigured')
            for field in ('availability', 'size', 'storage_capacity'):
                self.assertEqual(status.context['acquisition_job'][field], 'unknown')
        self.assertEqual(FeedbackEvent.objects.count(), 0)
        self.assertEqual(FeedbackProjection.objects.count(), 0)

    def test_full_series_season_ebook_and_cross_medium_scopes_are_rejected(self):
        for medium in self.keys:
            item = self.item(medium)
            data = self.confirmation(item)
            for scope in ('series', 'season', 'full_series', 'S01', 'ebook', 'book', 'series_s02e01'):
                self.assertEqual(self.confirm(item, {**data, 'scope': scope}).status_code, 400)
            for scope in set(saved.SCOPES.values()) - {saved.SCOPES[medium]}:
                self.assertEqual(self.confirm(item, {**data, 'scope': scope}).status_code, 400)
        self.assertEqual(AcquisitionJob.objects.count(), 0)

    def test_confirmation_requires_edition_legal_right_storage_and_review_token(self):
        item = self.item()
        valid = self.confirmation(item)
        for field in valid:
            data = dict(valid)
            data.pop(field)
            self.assertIn(self.confirm(item, data).status_code, (400, 409))
        for field, value in (('edition', ' '), ('storage', '/tmp/arbitrary'),
                             ('authorized_content', ''), ('storage_check', ''),
                             ('review_token', 'forged')):
            self.assertIn(self.confirm(item, {**valid, field: value}).status_code, (400, 409))
        self.assertEqual(AcquisitionJob.objects.count(), 0)

    def test_review_token_bound_to_item_and_actor_and_expires(self):
        film = self.item()
        book = self.item('book')
        data = self.confirmation(film)
        self.assertEqual(self.confirm(book, {**data, 'scope': 'audiobook'}).status_code, 409)
        with patch('django.core.signing.time.time', return_value=time.time() + 3602):
            self.assertEqual(self.confirm(film, data).status_code, 409)
        self.assertEqual(AcquisitionJob.objects.count(), 0)

    def test_double_click_and_multiple_reviews_and_lists_use_one_active_job(self):
        item = self.item()
        data = self.confirmation(item)
        first = self.confirm(item, data)
        self.assertEqual(self.confirm(item, data)['Location'], first['Location'])
        self.assertEqual(self.confirm(item, self.confirmation(item))['Location'], first['Location'])
        favorite = self.item(kind='favorites')
        self.assertEqual(self.confirm(favorite, self.confirmation(favorite))['Location'], first['Location'])
        self.assertEqual(AcquisitionJob.objects.count(), 1)
        self.assertEqual(self.confirm(item, {**data, 'edition': 'Different edition'}).status_code, 409)
        self.assertEqual(AcquisitionJob.objects.get().edition, data['edition'])

    def test_cancellation_is_local_idempotent_replay_stays_cancelled_fresh_review_can_retry(self):
        item = self.item('tv')
        data = self.confirmation(item)
        other_review = self.confirmation(item)
        self.confirm(item, data)
        job = AcquisitionJob.objects.get()
        cancel_url = saved.job_context(job)['cancel_url']
        for _ in range(2):
            self.assertEqual(self.client.post(cancel_url).status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')
        self.assertIsNone(job.active_key)
        self.assertIsNotNone(job.cancelled_at)
        for old in (data, other_review):
            self.assertEqual(self.confirm(item, old).status_code, 302)
        self.assertEqual(AcquisitionJob.objects.count(), 1)
        self.assertEqual(self.confirm(item, self.confirmation(item)).status_code, 302)
        self.assertEqual(AcquisitionJob.objects.count(), 2)
        self.assertEqual(AcquisitionJob.objects.filter(status='blocked_configuration').count(), 1)
        self.assertEqual({j.scope for j in AcquisitionJob.objects.all()}, {'series_s01e01'})

    def test_removing_saved_heart_keeps_job_snapshot_and_allows_cancellation(self):
        item = self.item('book')
        self.confirm(item, self.confirmation(item))
        job = AcquisitionJob.objects.get()
        self.client.post(f'{self.base}{item.pk}/remove/')
        job.refresh_from_db()
        self.assertIsNone(job.saved_item_id)
        self.assertEqual(job.title, 'Fiction Book')
        self.assertEqual(job.scope, 'audiobook')
        self.assertEqual(self.client.post(saved.job_context(job)['cancel_url']).status_code, 302)

    def test_get_never_saves_confirms_cancels_or_creates_a_job(self):
        item = self.item()
        for path in (self.base + 'save/', f'{self.base}{item.pk}/remove/',
                     f'{self.base}{item.pk}/obtain/confirm/', self.base + 'jobs/1/cancel/'):
            self.assertEqual(self.client.get(path).status_code, 405)
        for _ in range(2):
            self.assertEqual(self.client.get(self.base).status_code, 200)
            self.assertEqual(self.review(item).status_code, 200)
        self.assertEqual(SavedItem.objects.count(), 1)
        self.assertEqual(AcquisitionJob.objects.count(), 0)

    def test_csrf_on_save_remove_confirm_and_cancel_using_real_template_token(self):
        item = self.item()
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        session = strict.session
        session['selected_profile_id'] = 1
        session[intake.SESSION_KEY] = self.client.session[intake.SESSION_KEY]
        session.save()
        response = strict.get(self.base)
        token = strict.cookies['cc_synthetic_csrf'].value
        paths = (self.base + 'save/', f'{self.base}{item.pk}/remove/',
                 f'{self.base}{item.pk}/obtain/confirm/', self.base + 'jobs/1/cancel/')
        for path in paths:
            self.assertEqual(strict.post(path, {}).status_code, 403)
        self.assertContains(response, 'csrfmiddlewaretoken')
        self.assertEqual(strict.post(self.base + 'save/', {'csrfmiddlewaretoken': token,
            'item_id': self.keys['movie'], 'list_kind': 'watch'}).status_code, 302)
        review = strict.get(f'{self.base}{item.pk}/obtain/')
        self.assertEqual(strict.post(f'{self.base}{item.pk}/obtain/confirm/', {
            **review.context['obtain_form'].initial, 'csrfmiddlewaretoken': token,
            'edition': 'Exact complete film edition', 'authorized_content': 'on',
            'storage': 'local_library', 'storage_check': 'on'}).status_code, 302)
        job = AcquisitionJob.objects.get()
        self.assertEqual(strict.post(saved.job_context(job)['cancel_url'],
            {'csrfmiddlewaretoken': token}).status_code, 302)
        self.assertEqual(strict.post(f'{self.base}{item.pk}/remove/',
            {'csrfmiddlewaretoken': token}).status_code, 302)

    def test_authority_and_job_ownership_checked_at_confirmation_cancel_and_status(self):
        item = self.item()
        data = self.confirmation(item)
        Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
        self.assertEqual(self.confirm(item, data).status_code, 404)
        Grant.objects.filter(pk=self.grant.pk).update(can_select=True)
        self.confirm(item, data)
        job = AcquisitionJob.objects.get()
        AcquisitionJob.objects.filter(pk=job.pk).update(actor=self.other)
        self.assertEqual(self.client.get(saved.job_context(job)['status_url']).status_code, 404)
        self.assertEqual(self.client.post(saved.job_context(job)['cancel_url']).status_code, 404)

    def test_entire_saved_flow_has_no_network_credentials_provider_writes_or_recovery(self):
        with patch('socket.socket', side_effect=AssertionError('no network')), \
             patch('subprocess.Popen', side_effect=AssertionError('no process transport')), \
             patch.object(catalogue, '_request', side_effect=AssertionError('no provider')), \
             patch('shelf.credential_store.open_store', side_effect=AssertionError('no credentials')), \
             patch('shelf.recovery.access_barrier', side_effect=AssertionError('no feedback recovery')):
            item = self.item()
            self.assertEqual(self.client.get(self.base).status_code, 200)
            self.assertEqual(self.confirm(item, self.confirmation(item)).status_code, 302)
            job = AcquisitionJob.objects.get()
            self.assertEqual(self.client.get(saved.job_context(job)['status_url']).status_code, 200)
            self.assertEqual(self.client.post(saved.job_context(job)['cancel_url']).status_code, 302)

    def test_database_constraints_preserve_list_identity_scope_and_active_job(self):
        item = self.item()
        values = {f.name: getattr(item, f.name) for f in SavedItem._meta.fields
                  if f.name not in ('id', 'created_at')}
        for changes in ({}, {'list_kind': 'read'}, {'provider': 'arbitrary'}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                SavedItem.objects.create(**{**values, **changes})
        self.confirm(item, self.confirmation(item))
        job = AcquisitionJob.objects.get()
        values = {f.name: getattr(job, f.name) for f in AcquisitionJob._meta.fields
                  if f.name not in ('id', 'created_at')}
        # Vary the other unique keys so each check fails for its own reason.
        for changes in ({'request_id': uuid.uuid4()},
                        {'active_key': 'b' * 64},
                        {'request_id': uuid.uuid4(), 'active_key': 'c' * 64, 'scope': 'full_series'},
                        {'request_id': uuid.uuid4(), 'active_key': 'd' * 64, 'authorized_content': False}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                AcquisitionJob.objects.create(**{**values, **changes})


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class SavedRaceTests(SavedFixture, TransactionTestCase):
    def test_two_real_connections_cannot_create_duplicate_active_jobs_and_retry_is_safe(self):
        item = self.item('tv')
        data = self.confirmation(item)
        with tempfile.TemporaryDirectory(prefix='saved-race-') as folder:
            path = Path(folder) / 'local.sqlite3'
            raw = sqlite3.connect(path)
            connection.connection.backup(raw)
            raw.close()
            path.chmod(0o600)
            start = threading.Barrier(3)
            results = []

            def worker():
                local = connections['default']
                # Initialize Django's normal SQLite transaction configuration
                # before substituting this worker's disposable disk connection.
                local.ensure_connection()
                db = sqlite3.connect(path, isolation_level=None, timeout=0.2, check_same_thread=False)
                register(db)
                try:
                    with patch.object(local, 'connection', db):
                        local.set_autocommit(True)
                        start.wait(timeout=5)
                        try:
                            job, _ = saved.confirm(actor=self.owner, profile_id=1, item_id=item.pk, data=data)
                            results.append(job.pk)
                        except OperationalError:
                            results.append('retry')
                except BaseException as error:
                    import traceback
                    results.append(traceback.format_exc())
                finally:
                    db.close()
                    local.close()

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            self.assertTrue(any(isinstance(result, int) for result in results), results)
            self.assertTrue(all(isinstance(result, int) or result == 'retry' for result in results), results)
            raw = sqlite3.connect(path, isolation_level=None)
            register(raw)
            try:
                with patch.object(connection, 'connection', raw):
                    for _ in range(2):
                        job, _ = saved.confirm(actor=self.owner, profile_id=1, item_id=item.pk, data=data)
                    self.assertEqual(AcquisitionJob.objects.count(), 1)
                    self.assertEqual(job.scope, 'series_s01e01')
            finally:
                raw.close()


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class SavedMigrationTests(TransactionTestCase):
    def test_actual_0001_is_usable_unprepared_and_additive_migration_preserves_owner_and_denials(self):
        executor = MigrationExecutor(connection)
        executor.migrate([('shelf', '0001_initial')])
        try:
            old = executor.loader.project_state([('shelf', '0001_initial')]).apps
            owner = get_user_model().objects.create_user(pk=1, username='old-fiction-owner')
            profile = old.get_model('shelf', 'Profile').objects.create(pk=1, kind='owner', label='Old fixture', active=True)
            old.get_model('shelf', 'Grant').objects.create(user_id=1, profile_id=1, can_view=True, can_select=True)
            self.client.force_login(owner)
            self.assertEqual(self.client.post('/profiles/select/', {'profile_id': 1}).status_code, 302)
            response = self.client.get('/profiles/1/')
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.context['saved_available'])
            self.assertNotContains(response, 'class="save-form"')
            self.assertEqual(self.client.get('/profiles/1/saved/').status_code, 200)
            self.assertEqual(self.client.post('/profiles/1/saved/save/', {}).status_code, 503)
            executor = MigrationExecutor(connection)
            executor.migrate([('shelf', '0003_saved_intent')])
            self.assertEqual(Profile.objects.get(pk=1).label, profile.label)
            grant = Grant.objects.get(user=owner, profile_id=1)
            self.assertTrue(grant.can_view)
            self.assertTrue(grant.can_select)
            self.assertFalse(grant.can_mutate)
            self.assertFalse(grant.can_export)
            self.assertEqual(FeedbackEvent.objects.count(), 0)
            self.assertEqual(SavedItem.objects.count(), 0)
            self.assertEqual(AcquisitionJob.objects.count(), 0)
            self.assertTrue(saved.available())
            MigrationExecutor(connection).migrate([('shelf', '0002_feedback')])
            self.assertFalse(saved.available())
            self.assertFalse(Grant.objects.get(user=owner).can_mutate)
        finally:
            restored = MigrationExecutor(connection)
            restored.migrate(restored.loader.graph.leaf_nodes())


class SavedPreparationTests(SimpleTestCase):
    def test_prepare_is_explicit_fixed_and_candidate_cli_refuses_without_io(self):
        self.assertEqual(boundary.validate_command(['catalogue_prepare', '--provider-demo', '--plan']), 'catalogue_prepare')
        for options in ([], ['--provider-demo', '--fake'], ['--provider-demo', '--database=private'],
                        ['--provider-demo', '--settings=other']):
            with self.assertRaises(boundary.BoundaryError):
                boundary.validate_command(['catalogue_prepare', *options])
        environment = dict(os.environ)
        environment.pop('DJANGO_SETTINGS_MODULE', None)
        result = subprocess.run([sys.executable, '-B', 'backend/manage.py', 'catalogue_prepare', '--provider-demo', '--plan'],
            cwd=boundary.ROOT, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, 'Refused: fixed synthetic-demo configuration required.\n')
        self.assertFalse(boundary.STORE.exists())
