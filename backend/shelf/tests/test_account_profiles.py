"""CCB-11 / FB03-S01: account ownership through the normal caller, below HTTP."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock, patch
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

    def numeric_legacy_owner(self):
        User = get_user_model()
        actor = User.objects.filter(pk=1).first()
        if actor is None:
            actor = User.objects.create_user(pk=1, username='numeric-collision-owner')
        actor.is_active = True
        actor.save(update_fields=['is_active'])
        profile = Profile.objects.filter(pk=1).first()
        if profile is None:
            self.profile.kind = Profile.Kind.PERSONAL
            self.profile.owner = self.actor
            self.profile.save()
            profile = Profile.objects.create(pk=1, kind=Profile.Kind.OWNER,
                label='Legacy household', active=True)
        elif profile.pk != self.profile.pk:
            self.profile.kind = Profile.Kind.PERSONAL
            self.profile.owner = self.actor
            self.profile.save()
        profile.kind = Profile.Kind.OWNER
        profile.active = True
        if hasattr(profile, 'owner_id'):
            profile.owner = None
        profile.save()
        grant, _ = Grant.objects.get_or_create(user=actor, profile=profile)
        grant.can_view = grant.can_select = grant.can_mutate = True
        grant.save(update_fields=['can_view', 'can_select', 'can_mutate'])
        return actor, profile

    def restore_legacy_owner(self, profile):
        if profile.pk != self.profile.pk:
            profile.delete()
        self.profile.kind = Profile.Kind.OWNER
        self.profile.owner = None
        self.profile.save()

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
            chooser = client.get(reverse('profiles'))
            self.assertContains(chooser, f'value="{profile.pk}"')
            self.assertContains(chooser, 'Create my empty private profile')
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
        grant.delete()
        provision_personal_profile(friend)
        self.assertFalse(Grant.objects.filter(user=friend, profile=profile).exists())
        with self.assertRaises(Http404):
            require_profile(friend, profile.pk, 'select')
        for actor in (AnonymousUser(), get_user_model().objects.create(username='inactive', is_active=False)):
            with self.assertRaises(Http404):
                provision_personal_profile(actor)

    def test_interruption_rollback_and_lost_response_readback(self):
        friend = self.friends[0]
        with patch('shelf.account_profiles.Grant.objects.create', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                provision_personal_profile(friend)
        self.assertFalse(Profile.objects.filter(owner=friend).exists())
        profile = provision_personal_profile(friend)
        # Simulate losing the committed response; reload by authoritative account.
        reloaded = get_user_model().objects.get(pk=friend.pk)
        self.assertEqual(provision_personal_profile(reloaded).pk, profile.pk)
        self.assertEqual(Grant.objects.filter(user=friend, profile=profile).count(), 1)
        # A profile without durable grant evidence stays revoked on retry.
        second = Profile.objects.create(owner=self.friends[1], kind='personal', active=True, label='Empty')
        self.assertEqual(provision_personal_profile(self.friends[1]).pk, second.pk)
        self.assertFalse(Grant.objects.filter(user=self.friends[1], profile=second).exists())

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

    @override_settings(ROOT_URLCONF='cabinet.provider_urls')
    def test_profile_switch_imports_only_authorized_outgoing_prototype(self):
        from shelf import intake, durable_intake
        from shelf.tests.test_durable_intake import fiction_cards
        from shelf.authority import SELECTION_KEY
        from shelf.provider_views import _profiles
        actor, profile = self.numeric_legacy_owner()
        destination = provision_personal_profile(actor)
        client = Client()
        client.force_login(actor)
        state = intake.new_state(actor.pk, profile.pk)
        intake.merge(state, fiction_cards())
        state['choices'] = {next(iter(state['candidates'])): 'interested'}
        session = client.session
        session[SELECTION_KEY] = profile.pk
        session[intake.SESSION_KEY] = state
        session.save()
        with patch('shelf.provider_views.durable.available', return_value=True), patch(
                'shelf.provider_views.durable.upgrade', wraps=durable_intake.upgrade) as upgrade:
            self.assertEqual(client.post(reverse('select_profile'), {'profile_id': destination.pk}).status_code, 302)
            self.assertEqual(upgrade.call_args.args[1], profile.pk)
        run = durable_intake.IntakeRun.objects.get(actor=actor, profile=profile)
        self.assertEqual(len(run.history), 1)
        self.assertFalse(durable_intake.IntakeRun.objects.filter(profile=destination).exists())
        owner_grant = Grant.objects.get(user=actor, profile=profile)
        owner_grant.can_select = False
        owner_grant.save()
        self.assertFalse(_profiles(actor, 'select').filter(pk=profile.pk).exists())
        session = client.session
        session[SELECTION_KEY] = profile.pk
        session[intake.SESSION_KEY] = state
        session.save()
        with patch('shelf.provider_views.durable.available', return_value=True), patch(
                'shelf.provider_views.durable.upgrade') as upgrade:
            self.assertEqual(client.post(reverse('select_profile'), {'profile_id': destination.pk}).status_code, 302)
            upgrade.assert_not_called()

    @override_settings(ROOT_URLCONF='cabinet.provider_urls')
    def test_personal_profile_never_authorizes_owner_discovery_seeds(self):
        from types import SimpleNamespace
        from shelf.discovery_views import _household_authorized
        actor, owner_profile = self.numeric_legacy_owner()
        profile = provision_personal_profile(self.friends[0])
        self.assertFalse(_household_authorized(SimpleNamespace(user=self.friends[0]), profile.pk))
        self.assertTrue(_household_authorized(SimpleNamespace(user=actor), owner_profile.pk))
        self.assertFalse(_household_authorized(SimpleNamespace(user=actor), profile.pk))

    def test_personal_numeric_owner_collision_denies_sensitive_operations(self):
        from types import SimpleNamespace
        from shelf.acquisition_contract import Blocked
        from shelf import pick_enhancement, synthesis
        from shelf.models import ObtainPreview, SynthesisRun
        from shelf.obtain_preview import search, search_backend
        from shelf.provider_catalogue import CatalogueError, Status, _request
        from django.contrib.sessions.backends.db import SessionStore
        actor, profile = self.numeric_legacy_owner()
        self.assertEqual((actor.pk, profile.pk), (1, 1))
        profile.kind = Profile.Kind.PERSONAL
        profile.owner = actor
        profile.save()
        session = SessionStore()
        session['selected_profile_id'] = profile.pk
        session.save()
        request = SimpleNamespace(user=actor, method='POST',
            session=SessionStore(session_key=session.session_key))
        try:
            with patch('shelf.credential_store.open_store', side_effect=AssertionError('credential access')):
                with self.assertRaises(CatalogueError) as denied:
                    _request('search_movie', 'query', actor_id=actor.pk, profile_id=profile.pk)
            self.assertEqual(denied.exception.status, Status.AUTH)
            with self.assertRaises(Blocked):
                synthesis.snapshot(actor, profile.pk)
            with self.assertRaises(Blocked):
                synthesis.request(request, profile.pk, '0' * 64)
            with self.assertRaises(Blocked):
                pick_enhancement.authorize(request, profile.pk, 'movie')
            self.assertFalse(SynthesisRun.objects.filter(actor=actor, profile=profile).exists())
            with patch('shelf.credential_store.open_store', side_effect=AssertionError('credential access')) as opener:
                with self.assertRaises(Blocked):
                    search(actor=actor, profile_id=profile.pk, item_id='missing')
                with self.assertRaises(Blocked):
                    search_backend(actor=actor, profile_id=profile.pk)
            opener.assert_not_called()
            self.assertFalse(ObtainPreview.objects.filter(actor=actor, profile=profile).exists())
        finally:
            self.restore_legacy_owner(profile)

class AccountProfileMigrationTests(TransactionTestCase):
    def test_0031_legacy_profile_authority_catalogue_and_provider_denial(self):
        from django.db.migrations.executor import MigrationExecutor
        old, current = [('shelf', '0031_beta_problem_reports')], [('shelf', '0032_account_personal_profile')]
        MigrationExecutor(connection).migrate(old)
        try:
            apps = MigrationExecutor(connection).loader.project_state(old).apps
            User, Profile, Grant = (apps.get_model('auth', 'User'), apps.get_model('shelf', 'Profile'),
                                    apps.get_model('shelf', 'Grant'))
            user = User.objects.create(pk=1, username='generic-legacy-owner')
            profile = Profile.objects.create(pk=1, kind='owner', label='Legacy household', active=True)
            Grant.objects.create(user_id=user.pk, profile_id=profile.pk,
                can_view=True, can_select=True, can_mutate=True, can_export=True)
            actor = get_user_model().objects.get(pk=user.pk)
            self.assertEqual([item.pk for item in permitted_profiles(actor, 'select')], [profile.pk])
            client = Client()
            client.force_login(actor)
            other = User.objects.create(pk=2, username='legacy-denied-account')
            Grant.objects.create(user_id=other.pk, profile_id=profile.pk, can_view=True, can_select=True)
            with override_settings(ROOT_URLCONF='cabinet.urls'):
                chooser = client.get('/profiles/')
                self.assertContains(chooser, 'value="1"')
                self.assertNotContains(chooser, 'Create my empty private profile')
                self.assertEqual(client.post('/profiles/select/', {'profile_id': '1'}).status_code, 302)
                self.assertEqual(client.get('/profiles/1/').status_code, 200)
            with override_settings(ROOT_URLCONF='cabinet.provider_urls'):
                page = client.get('/profiles/')
                self.assertContains(page, f'value="{profile.pk}"')
                self.assertNotContains(page, 'Create my empty private profile')
                self.assertEqual(client.post('/profiles/select/', {'profile_id': profile.pk}).status_code, 302)
                self.assertEqual(client.get(f'/profiles/{profile.pk}/').status_code, 200)
                denied = Client()
                denied.force_login(get_user_model().objects.get(pk=other.pk))
                self.assertNotContains(denied.get('/profiles/'), f'value="{profile.pk}"')
                self.assertEqual(denied.get(f'/profiles/{profile.pk}/').status_code, 404)
                self.assertEqual(denied.post('/profiles/select/', {'profile_id': profile.pk}).status_code, 404)
        finally:
            MigrationExecutor(connection).migrate(current)

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

    def test_pre_0032_provider_search_works_and_provisioning_is_unavailable(self):
        from datetime import timedelta
        from django.db.migrations.executor import MigrationExecutor
        from django.urls import reverse
        from shelf import credential_store, provider_catalogue
        from shelf.tests.fixtures_credentials import CANARY
        old = [('shelf', '0031_beta_problem_reports')]
        current = [('shelf', '0032_account_personal_profile')]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        try:
            apps = executor.loader.project_state(old).apps
            User = apps.get_model('auth', 'User')
            LegacyProfile, LegacyGrant = apps.get_model('shelf', 'Profile'), apps.get_model('shelf', 'Grant')
            actor = User.objects.create(pk=1, username='pre0032-provider-owner')
            profile = LegacyProfile.objects.create(pk=1, kind='owner', label='Legacy', active=True)
            LegacyGrant.objects.create(user_id=actor.pk, profile_id=profile.pk,
                can_view=True, can_select=True, can_mutate=True)
            before = LegacyProfile.objects.count()
            now = credential_store.utc_now()
            metadata = credential_store.EntryMetadata(credential_store.Binding('FICTION_ACCOUNT',
                str(actor.pk), str(profile.pk), 'tmdb', 'culture-cabinet-tmdb-backend'), now,
                now + timedelta(days=1), now + timedelta(days=2), credential_store.TMDB_SCOPES)
            handle = type('Handle', (), {'context': credential_store._provider_context('tmdb'),
                'uid': 1000, 'gid': 1000, 'close': Mock()})()
            record = credential_store.PrivateRecord(metadata, bytearray(CANARY), 'FICTION', None)
            client = Client()
            client.force_login(get_user_model().objects.get(pk=actor.pk))
            with override_settings(ROOT_URLCONF='cabinet.urls'), \
                    patch.object(provider_catalogue.os, 'getuid', return_value=1000), \
                    patch.object(provider_catalogue.os, 'geteuid', return_value=1000), \
                    patch.object(provider_catalogue.os, 'getgid', return_value=1000), \
                    patch.object(provider_catalogue.os, 'getegid', return_value=1000), \
                    patch.object(credential_store, 'open_store', return_value=handle), \
                    patch.object(credential_store, 'lock_store'), \
                    patch.object(credential_store, 'read_record', return_value=record), \
                    patch.object(provider_catalogue, '_send', return_value={'results': []}) as send:
                self.assertEqual(provider_catalogue._request('search_movie', 'fiction',
                    actor_id=actor.pk, profile_id=profile.pk), {'results': []})
                send.assert_called_once()
                response = client.post(reverse('provision_profile'))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(LegacyProfile.objects.count(), before)
            self.assertEqual(LegacyGrant.objects.count(), 1)
            session = client.session
            session['selected_profile_id'] = profile.pk
            session.save()
            with override_settings(ROOT_URLCONF='cabinet.provider_urls'):
                event = client.post('/profiles/1/event-plans/start/', {'session_id':'fixture-session-15',
                    'quantity':1,'contacts':[],'purchase_scenario':'confirmed','calendar_scenario':'confirmed'})
                self.assertEqual(event.status_code, 302)
                page = client.get(event.url)
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, 'SYNTHETIC concert')
                message = client.post('/profiles/1/messages/start/', {'kind':'invitation',
                    'chat_id':'fixture-chat-alex-personal','session_id':'fixture-session-15',
                    'message_id':'','scenario':'sent','access':'ready'})
                self.assertEqual(message.status_code, 302)
                page = client.get(message.url)
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, 'fixture-session-15')
                self.assertContains(page, 'fictional rehearsal invitation')
        finally:
            MigrationExecutor(connection).migrate(current)
