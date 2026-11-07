"""Profile-bound discovery journeys; all source search calls are mocked."""
from html import escape
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from shelf import authored_discovery as storage, nonfiction_views as views
from shelf.authority import SELECTION_KEY
from shelf.models import AuthoredDiscoveryState, Grant, Profile


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class NonfictionViewTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username='SYNTHETIC-nonfiction-owner')
        self.profile = Profile.objects.create(kind='owner', active=True, label='SYNTHETIC owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = self.profile.pk
        session.save()
        self.base = f'/profiles/{self.profile.pk}/nonfiction/'
        views.SOURCE_CACHE.clear()

    def payload(self, action, **changes):
        row = AuthoredDiscoveryState.objects.filter(actor=self.actor, profile=self.profile, mode='household').first()
        return dict(mode='household', action=action, revision=row.revision if row else 0,
                    nonce=row.nonce if row else storage.initial_nonce(self.actor.pk, self.profile.pk, 'household'), **changes)

    def post(self, action, **changes):
        return self.client.post(self.base + 'action/', self.payload(action, **changes))

    def test_get_preserves_diversity_and_has_no_network_or_state_write(self):
        with patch('shelf.podcast_sources.search_cluster') as search:
            response = self.client.get(self.base)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.context['clusters']), 5)
            self.assertEqual({c['id'] for c in response.context['clusters']}, set(views.CLUSTERS))
            self.assertContains(response, 'not independently appraised')
            search.assert_not_called()
        self.assertFalse(AuthoredDiscoveryState.objects.exists())
        directives = {part.strip().lower() for part in response['Cache-Control'].split(',')}
        self.assertIn('no-store', directives)
        self.assertNotIn('public', directives)

    def test_shortlist_survives_cache_expiry_and_export_is_inert(self):
        episode = views.catalog()[0]['episodes'][0]
        self.assertEqual(self.post('shortlist_add', identity=episode['key']).status_code, 302)
        views.SOURCE_CACHE.clear()
        self.client.logout()
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = self.profile.pk
        session.save()
        response = self.client.get(self.base + 'export/')
        self.assertContains(response, escape(episode['title']))
        self.assertContains(response, episode['apple_url'].replace('&', '&amp;'))
        self.assertNotContains(response, 'csrfmiddlewaretoken')
        self.assertNotContains(response, '<form')
        self.assertNotContains(response, '<script')
        self.assertNotContains(response, '/static/')
        self.assertEqual(response['Content-Disposition'], 'attachment; filename="nonfiction-shortlist.html"')
        self.assertEqual(self.post('shortlist_remove', identity=episode['key']).status_code, 302)
        self.assertEqual(AuthoredDiscoveryState.objects.get().data['shortlist'], [])

    def test_authored_wording_is_escaped_and_separate_from_ratings(self):
        self.assertEqual(self.post('seed_add', topic='My topic', terms='soil',
                         source_text='<script>alert(1)</script>').status_code, 302)
        response = self.client.get(self.base)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, 'separate from 1–5 ratings')
        self.assertEqual(AuthoredDiscoveryState.objects.get().data['seeds'][0]['source_text'], '<script>alert(1)</script>')

    def test_authored_request_preserves_supplied_whitespace(self):
        supplied = '  Explore soil fungi.\nKeep this wording.  \n'
        self.assertEqual(self.post('seed_add', topic='Soil', terms='fungi',
                                   source_text=supplied).status_code, 302)
        self.assertEqual(AuthoredDiscoveryState.objects.get().data['seeds'][0]['source_text'], supplied)
        response = self.client.get(self.base)
        self.assertEqual(response.context['seeds'][0]['source_text'], supplied)

    def test_explicit_refresh_calls_once_and_replay_does_not_repeat(self):
        payload = self.payload('refresh', identity='living_soil')
        with patch('shelf.podcast_sources.search_cluster', return_value=[]) as search:
            self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 302)
            self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 302)
            search.assert_called_once()

    def test_unknown_duplicate_oversized_and_sample_refresh_rejected(self):
        payload = self.payload('shortlist_add', identity='invented')
        self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 409)
        payload = self.payload('seed_remove', identity='missing')
        payload['unexpected'] = 'value'
        self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 400)
        payload = self.payload('seed_remove', identity='missing')
        payload['identity'] = ['one', 'two']
        self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 400)
        self.assertEqual(self.post('seed_add', topic='x', terms='y', source_text='z' * 17000).status_code, 400)
        payload = self.payload('refresh', identity='living_soil')
        payload['mode'] = 'sample'
        with patch('shelf.podcast_sources.search_cluster') as search:
            self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 400)
            search.assert_not_called()

    def test_invalid_base_fields_reject_seed_add_without_writes(self):
        for invalid_field, invalid_value in (('nonce', None), ('revision', 'not-an-integer'),
                                             ('mode', 'invalid-mode')):
            with self.subTest(field=invalid_field):
                payload = self.payload('seed_add', topic='Soil', terms='fungi',
                                       source_text='Explore soil fungi.')
                if invalid_value is None:
                    payload.pop(invalid_field)
                else:
                    payload[invalid_field] = invalid_value
                with patch('shelf.podcast_sources.search_cluster') as search:
                    response = self.client.post(self.base + 'action/', payload)
                    self.assertEqual(response.status_code, 400)
                    search.assert_not_called()
                self.assertFalse(AuthoredDiscoveryState.objects.exists())

    def test_csrf_and_revoked_grant_block_mutation_and_export(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        self.assertEqual(client.post(self.base + 'action/', self.payload('seed_remove', identity='x')).status_code, 403)
        self.grant.can_select = False
        self.grant.save()
        self.assertIn(self.client.get(self.base + 'export/').status_code, (403, 404))
        self.assertIn(self.post('seed_remove', identity='x').status_code, (403, 404))
