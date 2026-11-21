"""CCB-11 / FB03-S01: account ownership through the normal caller, below HTTP."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.http import Http404
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from shelf.account_profiles import provision_personal_profile
from shelf.authority import permitted_profiles, require_profile
from shelf.feedback import Busy, feedback_context, mutate
from shelf.models import FeedbackEvent, FeedbackProjection, FeedbackSubject, Grant, PortableInstallation, Profile
from shelf.tests.synthetic_feedback import FeedbackCase


@override_settings(ALLOWED_HOSTS=['testserver'])
class AccountProfileTests(FeedbackCase):
    def setUp(self):
        super().setUp()
        self.friends = [get_user_model().objects.create_user(username=f'friend-{i}') for i in range(2)]

    def test_two_account_normal_provision_choose_read_write(self):
        profiles = []
        for friend in self.friends:
            client = Client()
            client.force_login(friend)
            self.assertEqual(client.get(reverse('provision_profile')).status_code, 405)
            self.assertEqual(client.post(reverse('provision_profile')).status_code, 302)
            profile = Profile.objects.get(owner=friend)
            profiles.append(profile)
            self.assertEqual(provision_personal_profile(friend).pk, profile.pk)
            self.assertEqual(list(permitted_profiles(friend, 'select')), [profile])
            self.assertContains(client.get(reverse('profiles')), f'value="{profile.pk}"')
            self.assertEqual(client.post(reverse('select_profile'), {'profile_id': profile.pk}).status_code, 302)
            self.assertEqual(client.get(reverse('profile_catalogue', args=[profile.pk])).status_code, 200)
            grant = Grant.objects.get(user=friend, profile=profile)
            self.assertEqual((grant.can_view, grant.can_select, grant.can_mutate, grant.can_export),
                             (True, True, True, False))
            self.assertFalse(FeedbackSubject.objects.filter(profile=profile).exists())
            self.assertFalse(FeedbackEvent.objects.filter(profile=profile).exists())
            self.assertFalse(FeedbackProjection.objects.filter(profile=profile).exists())
            command = self.command()
            mutate(actor=friend, profile_id=profile.pk, command=command)
            context = feedback_context(actor=friend, profile_id=profile.pk,
                                       target_id=command.target_id, include_hidden=True)
            self.assertEqual(context.selected_profile.pk, profile.pk)
            self.assertEqual(next(row['value'] for row in context.signals
                                  if row['name'] == 'enjoyment'), command.value)
            self.assertEqual(FeedbackProjection.objects.get(profile=profile).value, command.value)
        self.assertNotEqual(profiles[0].pk, profiles[1].pk)
        self.assertFalse(FeedbackEvent.objects.filter(profile=self.profile).exists())

    def test_foreign_grant_and_installation_owner_claims_deny_below_http(self):
        friend, other = self.friends
        profile = provision_personal_profile(friend)
        PortableInstallation.objects.create(id=uuid4(), root_identity='a' * 64,
            owner=self.actor, owner_revision=1, process_generation=uuid4())
        for actor in (other, self.actor):
            actor.is_staff = actor.is_superuser = True
            actor.save()
            Grant.objects.create(user=actor, profile=profile, can_view=True, can_select=True, can_mutate=True)
            client = Client()
            client.force_login(actor)
            self.assertEqual(client.post(reverse('select_profile'), {'profile_id': profile.pk}).status_code, 404)
            self.assertEqual(client.get(reverse('profile_catalogue', args=[profile.pk])).status_code, 404)
            for action in ('view', 'select', 'mutate'):
                with self.assertRaises(Http404):
                    require_profile(actor, profile.pk, action)
            with self.assertRaises(Http404):
                mutate(actor=actor, profile_id=profile.pk, command=self.command())
        self.assertFalse(FeedbackEvent.objects.exists())
        grant = Grant.objects.get(user=friend, profile=profile)
        grant.can_mutate = False
        grant.save()
        provision_personal_profile(friend)
        with self.assertRaises(Http404):
            mutate(actor=friend, profile_id=profile.pk, command=self.command())
        for actor in (AnonymousUser(), get_user_model().objects.create(username='inactive', is_active=False)):
            with self.assertRaises(Http404):
                provision_personal_profile(actor)

    def test_interruption_rollback_and_lost_response_readback(self):
        friend = self.friends[0]
        with patch('shelf.account_profiles.Grant.objects.get_or_create', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                provision_personal_profile(friend)
        self.assertFalse(Profile.objects.filter(owner=friend).exists())
        profile = provision_personal_profile(friend)
        # Simulate losing the committed response; reload by authoritative account.
        reloaded = get_user_model().objects.get(pk=friend.pk)
        self.assertEqual(provision_personal_profile(reloaded).pk, profile.pk)
        self.assertEqual(Grant.objects.filter(user=friend, profile=profile).count(), 1)
        # Reconcile a pre-existing owned profile whose grant creation was interrupted.
        second = Profile.objects.create(owner=self.friends[1], kind='personal', active=True, label='Empty')
        self.assertEqual(provision_personal_profile(self.friends[1]).pk, second.pk)
        self.assertEqual(Grant.objects.filter(user=self.friends[1], profile=second).count(), 1)

    def test_concurrent_provision_has_one_owner_generation(self):
        friend_id = self.friends[0].pk
        start = Barrier(2)
        def provision():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=friend_id)
                start.wait(timeout=5)
                try:
                    return provision_personal_profile(actor).pk
                except Busy:
                    return None  # Caller must reconcile after the winning transaction.
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: provision(), range(2)))
        profile = provision_personal_profile(self.friends[0])
        self.assertIn(profile.pk, results)
        self.assertTrue(all(value in (None, profile.pk) for value in results))
        self.assertEqual(Profile.objects.filter(owner_id=friend_id).count(), 1)
        self.assertEqual(Grant.objects.filter(user_id=friend_id, profile=profile).count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Profile.objects.create(owner_id=friend_id, kind='personal', active=True, label='Duplicate')

    @override_settings(ROOT_URLCONF='cabinet.provider_urls')
    def test_provider_chooser_uses_account_scope_and_dynamic_profile_id(self):
        from shelf.provider_views import _profiles
        for friend in self.friends:
            client = Client()
            client.force_login(friend)
            with patch('shelf.recovery.access_barrier', side_effect=AssertionError('provider has no recovery authority')):
                self.assertEqual(client.post(reverse('provision_profile'), {'owner_id': self.actor.pk,
                    'profile_id': self.profile.pk}).status_code, 302)
            profile = Profile.objects.get(owner=friend)
            self.assertEqual(list(_profiles(friend, 'select')), [profile])
            self.assertContains(client.get(reverse('profiles')), f'value="{profile.pk}"')
            self.assertEqual(client.post(reverse('select_profile'), {'profile_id': profile.pk}).status_code, 302)
            self.assertEqual(client.session['selected_profile_id'], profile.pk)
            self.assertEqual(client.get(reverse('profile_catalogue', args=[profile.pk])).status_code, 200)
            from shelf.durable_intake import new_run, owned
            run = new_run(friend, profile.pk)
            self.assertEqual(owned(friend, profile.pk, run.pk).pk, run.pk)
        foreign = Profile.objects.get(owner=self.friends[0])
        Grant.objects.create(user=self.friends[1], profile=foreign, can_view=True, can_select=True)
        self.assertEqual(client.post(reverse('select_profile'), {'profile_id': foreign.pk}).status_code, 404)
        self.assertEqual(client.get(reverse('profile_catalogue', args=[foreign.pk])).status_code, 404)
        with self.assertRaises(Http404):
            new_run(self.friends[1], foreign.pk)


class AccountProfileMigrationTests(TransactionTestCase):
    def test_legacy_identity_grants_and_portable_binding_trigger_survive(self):
        from django.db.migrations.executor import MigrationExecutor
        from importlib import import_module
        old = [('shelf', '0031_beta_problem_reports')]
        current = [('shelf', '0032_account_personal_profile')]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            user = apps.get_model('auth', 'User').objects.create(username='legacy-account')
            profile = apps.get_model('shelf', 'Profile').objects.create(kind='owner', label='Retained', active=True)
            grant = apps.get_model('shelf', 'Grant').objects.create(user_id=user.pk,
                profile_id=profile.pk, can_view=True, can_select=True, can_mutate=True)
        finally:
            MigrationExecutor(connection).migrate(current)
        retained = Profile.objects.get(pk=profile.pk)
        self.assertEqual((retained.kind, retained.label, retained.owner_id), ('owner', 'Retained', None))
        self.assertTrue(Grant.objects.get(pk=grant.pk, profile=retained, user_id=user.pk).can_mutate)
        expected = import_module('shelf.migrations.0032_account_personal_profile').BINDING
        with connection.cursor() as cursor:
            cursor.execute("SELECT sql FROM sqlite_master WHERE name='cabinet_binding_insert'")
            self.assertEqual(cursor.fetchone()[0], expected)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Profile.objects.create(kind='owner', label='Duplicate legacy')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Profile.objects.create(kind='personal', label='Owner missing')
