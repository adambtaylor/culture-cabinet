"""Provider contract plus actual M templates and original-schema integration."""
from copy import deepcopy
from html.parser import HTMLParser
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.core.management.base import CommandError
from django.core.management.commands.runserver import Command as Runserver
from django.http import QueryDict
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import Resolver404, resolve

from cabinet import boundary
from shelf import intake, provider_catalogue as catalogue
from shelf.management.commands.catalogue_runserver import Command
from shelf.models import Profile, Grant, FeedbackEvent, FeedbackProjection
from shelf.tests.fixtures import TEMPLATES as LEGACY_TEMPLATES
from shelf.tests.test_intake import fixture_cards

TEMPLATES = deepcopy(LEGACY_TEMPLATES)
TEMPLATES[0]['DIRS'] = [boundary.ROOT / 'templates']
TEMPLATES[0]['OPTIONS']['loaders'].append('django.template.loaders.filesystem.Loader')
TEMPLATES[0]['OPTIONS']['loaders'][0][1]['cabinet/provider_catalogue.html'] = (
    '<h1>{{ selected_profile.label }}</h1>{{ message }}{{ form.errors }}'
    '{{ temporary_notice }}{{ identity_notice }}{{ crossmedia_notice }}{{ neighbor_notice }}'
    '{{ tmdb_attribution }}'
    '{% for card in cards %}<article>{{ card.title }} {{ card.description }} '
    '{{ card.selection_reason }} {{ card.provider }} {{ card.medium }} {{ card.fetched_at }}'
    '<a href="{{ card.url }}">Source</a>{{ card.trailer_status }}</article>{% endfor %}'
    '<form method="post" action="{{ action_url }}">{% csrf_token %}'
    '<input name="nonce" value="{{ nonce }}"><input name="revision" value="{{ revision }}">'
    '<button name="action" value="start">Start</button></form>'
)


class ProviderFixture:
    def setUp(self):
        self.owner = get_user_model().objects.create_user(pk=1, username='fiction-owner', password='fiction-password-2839')
        self.other = get_user_model().objects.create_user(pk=2, username='fiction-other')
        self.profile = Profile.objects.create(pk=1, kind='owner', label='Fiction owner', active=True)
        Profile.objects.create(pk=2, kind='partner', label='Lisa', active=False)
        Profile.objects.create(pk=3, kind='family', label='Family', active=False)
        self.grant = Grant.objects.create(user=self.owner, profile=self.profile,
                                         can_view=True, can_select=True, can_mutate=False, can_export=False)
        self.client.force_login(self.owner)
        self.client.post('/profiles/select/', {'profile_id': '1'})
        self.url = '/profiles/1/catalogue/action/'

    def state(self, client=None):
        client = client or self.client
        client.get('/profiles/1/')
        return client.session[intake.SESSION_KEY]

    def post(self, action, **extra):
        state = self.state()
        return self.client.post(self.url, {'action': action, 'revision': state['revision'],
                                          'nonce': state['nonce'], **extra})

    def start(self):
        with patch.object(catalogue, 'initial_deck', return_value=(fixture_cards(), [])) as source:
            response = self.post('start')
        self.assertEqual(response.status_code, 302)
        source.assert_called_once_with(actor_id=1, profile_id=1)
        return self.state()


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'], TEMPLATES=TEMPLATES)
class ProviderViewsTests(ProviderFixture, TestCase):
    def test_login_profile_and_get_have_no_provider_io_or_recovery_claim(self):
        with patch.object(catalogue, '_request', side_effect=AssertionError('network forbidden')), \
                patch('shelf.recovery.access_barrier', side_effect=AssertionError('legacy recovery forbidden')):
            self.assertEqual(self.client.get('/').status_code, 200)
            self.assertEqual(self.client.get('/profiles/').status_code, 200)
            response = self.client.get('/profiles/1/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'provider identity is not verified')
        self.assertFalse(response.context['started'])
        self.assertContains(response, 'not endorsed or certified')
        self.client.logout()
        self.assertEqual(self.client.get('/profiles/1/').status_code, 302)

    def test_csrf_required_and_no_get_mutations(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post('/profiles/select/', {'profile_id': '1'}).status_code, 403)
        session = client.session
        session['selected_profile_id'] = 1
        session.save()
        response = client.get('/profiles/1/')
        state = client.session[intake.SESSION_KEY]
        data = {'action': 'start', 'revision': state['revision'], 'nonce': state['nonce']}
        with patch.object(catalogue, 'initial_deck', return_value=(fixture_cards(), [])) as source:
            self.assertEqual(client.post(self.url, data).status_code, 403)
            source.assert_not_called()
            token = client.cookies['cc_synthetic_csrf'].value
            data['csrfmiddlewaretoken'] = token
            self.assertEqual(client.post(self.url, data).status_code, 302)
        self.assertEqual(client.get(self.url).status_code, 405)

    def test_authority_checked_every_action_and_same_selected_profile(self):
        with patch.object(catalogue, '_request') as source:
            for profile in (2, 3, 999):
                self.assertEqual(self.client.get(f'/profiles/{profile}/').status_code, 404)
                self.assertEqual(self.client.post(f'/profiles/{profile}/catalogue/action/', {}).status_code, 404)
            self.grant.can_select = False
            self.grant.save()
            self.assertEqual(self.post('start').status_code, 404)
            source.assert_not_called()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/profiles/1/').status_code, 404)
        self.assertEqual(self.client.post('/profiles/select/', {'profile_id': 1}).status_code, 404)

    def test_revocation_after_io_does_not_save_provider_data(self):
        self.state()
        def fetch(**kwargs):
            Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
            return fixture_cards(), []
        with patch.object(catalogue, 'initial_deck', side_effect=fetch):
            self.assertEqual(self.post('start').status_code, 404)
        self.assertNotIn(intake.SESSION_KEY, self.client.session)

    def test_round_choice_next_revision_and_replay_no_official_writes(self):
        state = self.start()
        key = state['current'][0]
        data = {'action': 'choose', 'revision': state['revision'], 'nonce': state['nonce'],
                'candidate_id': key, 'choice': 'interested'}
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        state = self.state()
        self.assertEqual(state['choices'][key], 'interested')
        self.assertNotIn(key, state['current'])
        self.assertEqual(self.client.post(self.url, data).status_code, 409)
        response = self.client.get('/profiles/1/')
        self.assertEqual(response.context['choice_counts']['interested'], 1)
        self.assertEqual(response.context['choice_counts']['enjoyed'], 0)
        self.assertEqual(FeedbackEvent.objects.count(), 0)
        self.assertEqual(FeedbackProjection.objects.count(), 0)

    def test_invalid_duplicate_fields_and_unknown_choices_fail_closed(self):
        self.start()
        self.assertEqual(self.post('choose', candidate_id='invented', choice='enjoyed').status_code, 400)
        self.assertEqual(self.post('choose', candidate_id='a00', choice='automatic').status_code, 400)
        state = self.state()
        data = QueryDict('', mutable=True)
        data.update(action='search', revision=str(state['revision']), nonce=state['nonce'], medium='book', query='safe')
        data.setlist('query', ['safe', 'second'])
        with patch.object(catalogue, 'search') as source:
            self.assertEqual(self.client.post(self.url, data.urlencode(), content_type='application/x-www-form-urlencoded').status_code, 400)
            self.assertEqual(self.post('search', medium='book', query='x'*101).status_code, 400)
            source.assert_not_called()

    def test_search_add_only_existing_server_results_and_no_implicit_signal(self):
        result = fixture_cards()[-1]
        with patch.object(catalogue, 'search', return_value=[result]) as search:
            self.assertEqual(self.post('search', medium='book', query='Obscure title').status_code, 302)
        search.assert_called_once_with('book', 'Obscure title', actor_id=1, profile_id=1)
        self.assertEqual(self.post('add', candidate_id='invented').status_code, 400)
        self.assertEqual(self.post('add', candidate_id=result.key).status_code, 302)
        state = self.state()
        self.assertIn(result.key, state['current'])
        self.assertEqual(state['choices'], {})

    def test_neighbors_require_human_positive_root_and_only_expand_once(self):
        state = self.start()
        key = state['current'][0]
        with patch.object(catalogue, 'neighbors', return_value=[]) as source:
            self.assertEqual(self.post('neighbors', candidate_id=key).status_code, 400)
            source.assert_not_called()
            self.assertEqual(self.post('choose', candidate_id=key, choice='enjoyed').status_code, 302)
            self.assertEqual(self.post('neighbors', candidate_id=key).status_code, 302)
            source.assert_called_once()
            self.assertEqual(self.post('neighbors', candidate_id=key).status_code, 400)

    def test_expiry_reset_logout_clear_temporary_state(self):
        self.start()
        self.assertEqual(self.post('reset').status_code, 302)
        self.assertEqual(self.state()['candidates'], {})
        session = self.client.session
        session[intake.SESSION_KEY]['expires_at'] = time.time()-1
        session.save()
        self.assertEqual(self.state()['choices'], {})
        self.assertGreater(self.state()['expires_at'], time.time())
        self.assertEqual(self.client.post('/logout/').status_code, 302)
        self.assertNotIn(intake.SESSION_KEY, self.client.session)

    def test_provider_error_only_fixed_status_is_exposed(self):
        old = self.state()
        with patch.object(catalogue, 'search', side_effect=catalogue.CatalogueError('graphql')):
            response = self.post('search', medium='book', query='Fiction')
        self.assertEqual(response.status_code, 502)
        self.assertContains(response, 'Provider request unavailable: graphql', status_code=502)
        self.assertEqual(self.client.session[intake.SESSION_KEY]['nonce'], response.context['nonce'])
        self.assertNotEqual(old['nonce'], response.context['nonce'])
        with patch.object(catalogue, 'search') as source:
            self.assertEqual(self.client.post(self.url, {'action': 'search', 'medium': 'book',
                'query': 'Fiction', 'revision': old['revision'], 'nonce': old['nonce']}).status_code, 409)
            source.assert_not_called()

    def test_provider_text_autoescaped_in_template_contract(self):
        from dataclasses import replace
        card = replace(fixture_cards()[0], title='<script>bad</script>', description='<b>plain</b>')
        with patch.object(catalogue, 'initial_deck', return_value=([card], [])):
            self.post('start')
        response = self.client.get('/profiles/1/')
        self.assertContains(response, '&lt;script&gt;bad&lt;/script&gt;')
        self.assertContains(response, '&lt;b&gt;plain&lt;/b&gt;')
        self.assertNotContains(response, '<script>bad</script>')

    def test_no_legacy_endpoints_in_provider_urlconf(self):
        for url in ('/profiles/1/feedback-export/', '/profiles/1/feedback/anything/', '/recovery/', '/import/'):
            self.assertEqual(self.client.get(url).status_code, 404)


class HiddenFields(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.fields = {}
        self.feed(content.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('type') == 'hidden':
            name = attrs.get('name')
            if name in {'csrfmiddlewaretoken', 'revision', 'nonce'}:
                self.fields[name] = attrs.get('value', '')


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class ProviderIntegrationTests(ProviderFixture, TestCase):
    """Normal template loader, real auth/CSRF, fictional provider responses only."""

    def flow(self):
        client = Client(enforce_csrf_checks=True)
        with patch.object(catalogue, '_request', side_effect=AssertionError('network forbidden')):
            login = client.get('/login/')
            self.assertTemplateUsed(login, 'registration/provider_login.html')
            self.assertContains(login, 'Follow your curiosity.')
            credentials = {'username': self.owner.username, 'password': 'fiction-password-2839'}
            self.assertEqual(client.post('/login/', credentials).status_code, 403)
            response = client.post('/login/', {**HiddenFields(login.content).fields, **credentials}, follow=True)
            self.assertTemplateUsed(response, 'cabinet/provider_profiles.html')
            self.assertContains(response, 'Which shelf today?')
            self.assertNotContains(response, 'Choose Lisa')
            self.assertNotContains(response, 'Choose Family')
            response = client.post('/profiles/select/', {
                **HiddenFields(response.content).fields, 'profile_id': '1'}, follow=True)
            self.assertTemplateUsed(response, 'cabinet/provider_catalogue.html')
            self.assertContains(response, 'Start discovery')
        return client, response

    def submit(self, client, response, action, **fields):
        return client.post(self.url, {**HiddenFields(response.content).fields,
                                     'action': action, **fields}, follow=True)

    def test_actual_templates_csrf_login_to_unknown_search_add_reset_logout(self):
        client, response = self.flow()
        with patch.object(catalogue, 'initial_deck', return_value=(fixture_cards(), [])) as source:
            response = self.submit(client, response, 'start')
        self.assertEqual(response.status_code, 200)
        source.assert_called_once_with(actor_id=1, profile_id=1)
        self.assertContains(response, 'Don’t know it')
        key = response.context['cards'][0]['key']
        replay = {**HiddenFields(response.content).fields, 'action': 'choose',
                  'candidate_id': key, 'choice': 'unknown'}
        response = client.post(self.url, replay, follow=True)
        self.assertEqual(response.context['choice_counts']['unknown'], 1)
        self.assertEqual(response.context['choice_counts']['enjoyed'], 0)
        self.assertEqual(response.context['positive_roots'], [])
        self.assertEqual(client.post(self.url, replay).status_code, 409)
        result = fixture_cards()[-1]
        with patch.object(catalogue, 'search', return_value=[result]) as source:
            response = self.submit(client, response, 'search', medium='book', query='Fiction title')
        source.assert_called_once_with('book', 'Fiction title', actor_id=1, profile_id=1)
        self.assertContains(response, f'Add {result.title} to this round')
        response = self.submit(client, response, 'add', candidate_id=result.key)
        self.assertIn(result.key, [card['key'] for card in response.context['cards']])
        self.assertEqual(client.session[intake.SESSION_KEY]['choices'], {key: 'unknown'})
        response = self.submit(client, response, 'reset')
        self.assertFalse(response.context['started'])
        self.assertEqual(client.session[intake.SESSION_KEY]['candidates'], {})
        self.assertEqual(client.session[intake.SESSION_KEY]['choices'], {})
        response = client.post('/logout/', HiddenFields(response.content).fields, follow=True)
        self.assertTemplateUsed(response, 'registration/provider_login.html')
        self.assertNotIn(intake.SESSION_KEY, client.session)
        self.assertNotIn('selected_profile_id', client.session)
        self.grant.refresh_from_db()
        self.assertFalse(self.grant.can_mutate)
        self.assertFalse(self.grant.can_export)
        self.assertEqual(FeedbackEvent.objects.count(), 0)
        self.assertEqual(FeedbackProjection.objects.count(), 0)

    def test_exact_old_grant_columns_support_session_actions_and_revocation(self):
        # DDL is restricted to Django's disposable in-memory test connection.
        self.assertTrue(connection.creation.is_in_memory_db(connection.settings_dict['NAME']))
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE shelf_grant DROP COLUMN can_mutate')
            cursor.execute('ALTER TABLE shelf_grant DROP COLUMN can_export')
            cursor.execute('PRAGMA table_info(shelf_grant)')
            self.assertEqual({row[1] for row in cursor.fetchall()},
                             {'id', 'can_view', 'can_select', 'user_id', 'profile_id'})
        client, response = self.flow()
        with patch.object(catalogue, 'initial_deck', return_value=(fixture_cards(), [])):
            response = self.submit(client, response, 'start')
        self.assertEqual(response.status_code, 200)
        response = self.submit(client, response, 'choose',
                               candidate_id=response.context['cards'][0]['key'], choice='unknown')
        self.assertEqual(response.context['choice_counts']['unknown'], 1)
        with connection.cursor() as cursor:
            cursor.execute('UPDATE shelf_grant SET can_select = 0 WHERE id = %s', [self.grant.pk])
        with patch.object(catalogue, 'search') as source:
            self.assertEqual(self.submit(client, response, 'search', medium='movie', query='Fiction').status_code, 404)
            source.assert_not_called()
        self.assertNotIn(intake.SESSION_KEY, client.session)

    def test_actual_forms_reject_stale_revision_and_unauthorized_actor_before_io(self):
        client, response = self.flow()
        fields = HiddenFields(response.content).fields
        with patch.object(catalogue, 'initial_deck') as source:
            self.assertEqual(client.post(self.url, {**fields, 'revision': 999, 'action': 'start'}).status_code, 409)
            client.force_login(self.other)
            self.assertEqual(client.post(self.url, {**fields, 'action': 'start'}).status_code, 404)
            source.assert_not_called()

    def test_current_owner_view_select_authority_required_before_io(self):
        for model, pk, field in ((Grant, self.grant.pk, 'can_select'),
                                  (Grant, self.grant.pk, 'can_view'),
                                  (Profile, self.profile.pk, 'active'),
                                  (get_user_model(), self.owner.pk, 'is_active')):
            with self.subTest(field=field):
                client, response = self.flow()
                model.objects.filter(pk=pk).update(**{field: False})
                with patch.object(catalogue, 'initial_deck') as source:
                    denied = client.post(self.url, {**HiddenFields(response.content).fields, 'action': 'start'})
                    self.assertEqual(denied.status_code, 302 if field == 'is_active' else 404)
                    source.assert_not_called()
                model.objects.filter(pk=pk).update(**{field: True})

    def test_current_authority_repeated_after_success_and_provider_failure(self):
        for failure in (False, True):
            for model, pk, field in ((Grant, self.grant.pk, 'can_select'),
                                      (Grant, self.grant.pk, 'can_view'),
                                      (Profile, self.profile.pk, 'active'),
                                      (get_user_model(), self.owner.pk, 'is_active')):
                with self.subTest(failure=failure, field=field):
                    client, response = self.flow()
                    def fetch(**kwargs):
                        model.objects.filter(pk=pk).update(**{field: False})
                        if failure:
                            raise catalogue.CatalogueError('graphql')
                        return fixture_cards(), []
                    with patch.object(catalogue, 'initial_deck', side_effect=fetch):
                        self.assertEqual(self.submit(client, response, 'start').status_code, 404)
                    self.assertNotIn(intake.SESSION_KEY, client.session)
                    model.objects.filter(pk=pk).update(**{field: True})

    def test_actual_template_provider_failure_is_generic_and_context_is_fixed(self):
        client, response = self.flow()
        with patch.object(catalogue, 'search', side_effect=catalogue.CatalogueError('graphql')):
            response = self.submit(client, response, 'search', medium='book', query='Fiction')
        self.assertContains(response, 'That request could not finish', status_code=502)
        self.assertEqual(response.context['message'], 'Provider request unavailable: graphql')


@override_settings(ROOT_URLCONF='cabinet.provider_urls')
class ProviderCommandTests(SimpleTestCase):
    def test_explicit_command_contract_without_listener(self):
        self.assertEqual(boundary.validate_command(['catalogue_runserver', '--provider-demo']), 'catalogue_runserver')
        for args in (['catalogue_runserver'], ['catalogue_runserver', '--provider-demo', '0.0.0.0:8000'],
                     ['catalogue_runserver', '--provider-demo', '--settings=private.settings']):
            with self.assertRaises(boundary.BoundaryError):
                boundary.validate_command(args)
        command = Command()
        with patch.object(Runserver, 'handle') as parent, patch('resource.setrlimit'):
            command.handle(provider_demo=True)
        self.assertEqual(parent.call_args.kwargs['addrport'], '127.0.0.1:8000')
        self.assertFalse(parent.call_args.kwargs['use_threading'])
        self.assertFalse(parent.call_args.kwargs['use_reloader'])
        for options in ({}, {'provider_demo': True, 'addrport': '0.0.0.0:8000'}):
            with self.assertRaises(CommandError):
                command.handle(**options)
        with override_settings(ROOT_URLCONF='cabinet.urls'):
            with self.assertRaises(CommandError):
                command.handle(provider_demo=True)

    def test_urlconf_runtime_selection_does_not_alter_old_default(self):
        with patch.object(boundary, '_provider', False):
            self.assertEqual(boundary.runtime_urlconf(), 'cabinet.urls')
        with patch.object(boundary, '_provider', True):
            self.assertEqual(boundary.runtime_urlconf(), 'cabinet.provider_urls')
