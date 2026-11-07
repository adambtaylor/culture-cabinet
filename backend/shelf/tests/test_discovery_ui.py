"""Actual Django forms with isolated users/database; no runtime or provider I/O."""
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from shelf.discovery_reducer import replay
from shelf.models import DiscoveryState, Profile, Grant, IntakeRun, SavedItem, AcquisitionJob, FeedbackEvent
from shelf import discovery_sources as sources


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class DiscoveryUITests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='discovery-fiction-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Synthetic owner')
        Profile.objects.create(pk=2, kind='partner', active=False, label='Lisa')
        Profile.objects.create(pk=3, kind='family', active=False, label='Family')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client.force_login(self.actor)
        session = self.client.session; session['selected_profile_id'] = 1; session.save()
        self.url = '/profiles/1/discovery/'
        self.action = self.url + 'action/'
        self.key = sources.sample_candidates()[0].key

    def data(self, mode='sample', **extra):
        response = self.client.get(self.url, {'mode': mode})
        self.assertEqual(response.status_code, 200)
        return dict(mode=mode, revision=response.context['revision'], nonce=response.context['nonce'],
                    operation='respond', key=self.key, choice='interested', **extra)

    def post(self, operation='respond', mode='sample', **extra):
        data = self.data(mode)
        data.update(operation=operation, **extra)
        if operation in ('undo', 'restart'):
            data.update(key='', choice='')
        elif operation != 'respond':
            data['choice'] = ''
        return self.client.post(self.action, data)

    def test_get_has_no_provider_operations_or_history_writes(self):
        with patch.object(sources, 'fetch_radio_fixture', side_effect=AssertionError('provider on GET')):
            response = self.client.get(self.url)
            self.assertContains(response, 'Music &amp; events')
            self.assertContains(response, 'Household confirmation')
            self.assertContains(response, 'deribin theater')
            self.assertContains(response, 'unconfirmed; not auto-mapped')
            self.assertContains(response, 'Maintainer contact unavailable')
            self.assertEqual(self.client.get('/discovery/').status_code, 200)
        self.assertEqual(DiscoveryState.objects.count(), 0)
        self.assertEqual(self.client.get(self.action).status_code, 405)

    def test_fixture_labels_links_unknown_distance_and_coverage(self):
        response = self.client.get(self.url, {'mode': 'sample'})
        for text in ('FIXTURE SAMPLE', 'Distance unknown', 'coordinates unavailable', 'Listening link unavailable',
                     'Coverage gaps', 'Stale', 'Source rank', 'No home location is assumed'):
            self.assertContains(response, text)
        self.assertNotContains(response, 'https://moshtix.com.au/v2/event/')
        self.assertNotContains(response, 'name="url"')
        response = self.client.get(self.url, {'mode': 'sample', 'category': 'regional_day_trip'})
        self.assertContains(response, 'SAMPLE: Invented regional day trip event')
        self.assertContains(response, 'Date/timezone unknown')

    def mindbuffer_article(self, response):
        return next(article for article in re.findall(
            r'<article\b[^>]*>.*?</article>', response.content.decode(), re.S)
            if '<h3>MindBuffer</h3>' in article)

    def test_mindbuffer_visible_official_link_awaits_human_feedback(self):
        with patch.object(sources, 'fetch_public', side_effect=AssertionError('provider on GET')):
            response = self.client.get(self.url)
        self.assertContains(response, 'Reported seeds')
        article = self.mindbuffer_article(response)
        self.assertIn('Identity: official:artist:mindbuffer', article)
        self.assertIn('href="https://mindbuffer.net/works/"', article)
        self.assertIn('manual-attributed', article)
        self.assertIn('Example curator-confirmed AV studio/performance seed', article)
        self.assertIn('matching provider identity unverified', article)
        self.assertNotIn('musicbrainz', article.lower())
        self.assertNotIn('Your current response:', article)
        for choice in ('love_it', 'intrigued', 'mildly_interested', 'sounds_boring', 'hate_it', 'unknown', 'skipped'):
            self.assertIn(f'name="choice" value="{choice}"', article)
        row, = [r for r in response.context['music'] if r['card'].key == 'official:artist:mindbuffer']
        self.assertEqual(row['choice'], '')
        self.assertFalse(row['saved'])
        self.assertEqual(response.context['responses'], [])
        self.assertEqual(response.context['lanes'], [])
        self.assertEqual(response.context['revision'], 0)
        for model in (DiscoveryState, IntakeRun, SavedItem, AcquisitionJob, FeedbackEvent):
            self.assertEqual(model.objects.count(), 0)

    def test_mindbuffer_has_no_radio_action_or_forged_radio_request(self):
        response = self.client.get(self.url)
        self.assertNotIn('value="radio"', self.mindbuffer_article(response))
        self.assertNotIn('name="radio_mode"', self.mindbuffer_article(response))
        self.assertContains(response, 'value="radio"', count=4)
        with patch.object(sources, 'fetch_public', side_effect=AssertionError('unmapped radio')) as fetch:
            response = self.post('radio', mode='household', key='official:artist:mindbuffer',
                                 radio_mode='medium')
            self.assertEqual(response.status_code, 400)
            fetch.assert_not_called()
        self.assertFalse(DiscoveryState.objects.exists())

    def test_mindbuffer_only_explicit_responses_create_empty_lanes(self):
        key = 'official:artist:mindbuffer'
        with patch.object(sources, 'fetch_public', side_effect=AssertionError('provider on response')):
            for count, choice in enumerate(('enjoyed', 'interested', 'unknown', 'not_for_me'), 1):
                with self.subTest(choice=choice):
                    self.assertEqual(self.post(mode='household', key=key, choice=choice).status_code, 302)
                    state = DiscoveryState.objects.get(mode='household')
                    self.assertEqual(len(state.history), count)
                    self.assertEqual(replay(state.history)['choices'], {key: choice})
                    self.assertEqual(state.history[-1]['origin'], 'authenticated-human-post')
                    self.assertEqual(state.history[-1]['source']['provenance'], 'manual-attributed')
                    response = self.client.get(self.url)
                    self.assertEqual(response.context['lanes'], [
                        {'key': key, 'title': 'MindBuffer', 'choice': choice, 'count': 0}
                    ] if choice in ('enjoyed', 'interested') else [])
                    self.assertEqual([(r['card'].key, r['choice']) for r in response.context['responses']],
                                     [(key, choice)])
        self.assertFalse(DiscoveryState.objects.filter(mode='sample').exists())
        for model in (IntakeRun, SavedItem, AcquisitionJob, FeedbackEvent):
            self.assertEqual(model.objects.count(), 0)

    def test_mindbuffer_cannot_enter_sample_or_inherit_sample_feedback(self):
        key = 'official:artist:mindbuffer'
        response = self.client.get(self.url, {'mode': 'sample'})
        self.assertNotContains(response, key)
        self.assertNotContains(response, 'https://mindbuffer.net/works/')
        self.assertEqual(self.post(mode='sample', key=key).status_code, 400)
        self.assertFalse(DiscoveryState.objects.exists())
        self.assertEqual(self.post(mode='sample', choice='enjoyed').status_code, 302)
        response = self.client.get(self.url)
        self.assertIn('official:artist:mindbuffer', self.mindbuffer_article(response))
        self.assertEqual(response.context['responses'], [])
        self.assertEqual(response.context['lanes'], [])
        self.assertFalse(DiscoveryState.objects.filter(mode='household').exists())

    def test_human_response_correction_idempotence_and_restart(self):
        data = self.data()
        self.assertEqual(self.client.post(self.action, data).status_code, 302)
        self.assertEqual(self.client.post(self.action, data).status_code, 302)
        state = DiscoveryState.objects.get()
        self.assertEqual(state.revision, 1)
        self.assertEqual(len(state.history), 1)
        self.assertEqual(self.post(choice='not_for_me').status_code, 302)
        state.refresh_from_db(); self.assertEqual(replay(state.history)['choices'][self.key], 'not_for_me')
        self.assertEqual(self.post('undo').status_code, 302)
        state.refresh_from_db(); self.assertEqual(replay(state.history)['choices'][self.key], 'interested')
        self.assertEqual(self.post('restart').status_code, 302)
        state.refresh_from_db(); self.assertEqual(replay(state.history)['choices'], {})
        self.assertEqual(self.post('undo').status_code, 302)
        state.refresh_from_db(); self.assertEqual(replay(state.history)['choices'][self.key], 'interested')

    def test_unknown_negative_and_save_never_become_enjoyment(self):
        self.assertEqual(self.post('save').status_code, 302)
        state = DiscoveryState.objects.get()
        self.assertEqual(replay(state.history)['choices'], {})
        self.assertEqual(self.post(choice='unknown').status_code, 302)
        state.refresh_from_db(); self.assertEqual(replay(state.history)['choices'][self.key], 'unknown')
        self.assertEqual(self.post(choice='not_for_me').status_code, 302)
        response = self.client.get(self.url, {'mode': 'sample'})
        self.assertEqual(response.context['lanes'], [])
        self.assertTrue(any(row['card'].key == self.key for row in response.context['responses']))
        self.assertEqual(self.post('unsave').status_code, 302)
        state.refresh_from_db(); self.assertEqual(replay(state.history)['saved'], [])
        for model in (IntakeRun, SavedItem, AcquisitionJob, FeedbackEvent):
            self.assertEqual(model.objects.count(), 0)

    def test_sample_cannot_enter_household_history(self):
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(self.post(mode='household').status_code, 400)
        self.assertFalse(DiscoveryState.objects.filter(mode='household').exists())
        real_key = sources.household_candidates()[0].key
        self.assertEqual(self.post(mode='household', key=real_key, choice='enjoyed').status_code, 302)
        self.assertEqual(self.post(mode='sample', key=real_key).status_code, 400)
        household = DiscoveryState.objects.get(mode='household')
        self.assertEqual(replay(household.history)['choices'], {real_key: 'enjoyed'})

    def test_logout_reload_replays_durable_state_without_session_history(self):
        self.post()
        self.client.logout(); self.client.force_login(self.actor)
        session = self.client.session; session['selected_profile_id'] = 1; session.save()
        response = self.client.get(self.url, {'mode': 'sample'})
        self.assertEqual(response.context['revision'], 1)
        self.assertEqual(response.context['lanes'][0]['key'], self.key)
        stored = DiscoveryState.objects.get()
        self.assertEqual(set(stored.history[0]['source']), {'name', 'provenance', 'rank', 'retrieved_at'})
        self.assertNotIn('title', stored.history[0])

    def test_stale_nonce_revision_and_repurposed_receipts_rejected(self):
        original = self.data()
        self.client.post(self.action, original)
        self.assertEqual(self.client.post(self.action, dict(original, choice='enjoyed')).status_code, 409)
        current = self.data()
        self.assertEqual(self.client.post(self.action, dict(current, revision=0)).status_code, 409)
        self.assertEqual(self.client.post(self.action, dict(current, nonce='wrong')).status_code, 409)
        self.assertEqual(DiscoveryState.objects.get().revision, 1)

    def test_unauth_cross_profile_current_selection_and_revocation(self):
        self.assertEqual(Client().get(self.url).status_code, 302)
        self.assertEqual(Client().post(self.action, {}).status_code, 302)
        for profile in (2, 3, 999):
            self.assertEqual(self.client.get(f'/profiles/{profile}/discovery/').status_code, 404)
            self.assertEqual(self.client.post(f'/profiles/{profile}/discovery/action/', {}).status_code, 404)
        data = self.data(); self.grant.can_select = False; self.grant.save()
        self.assertEqual(self.client.post(self.action, data).status_code, 404)
        self.grant.can_select = True; self.grant.save()
        session = self.client.session; session['selected_profile_id'] = 2; session.save()
        self.assertEqual(self.client.post(self.action, data).status_code, 404)
        other = get_user_model().objects.create_user(pk=2, username='discovery-other')
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertFalse(Profile.objects.get(pk=2).active)
        self.assertFalse(Profile.objects.get(pk=3).active)

    def test_csrf_required_and_valid_form_works(self):
        client = Client(enforce_csrf_checks=True); client.force_login(self.actor)
        session = client.session; session['selected_profile_id'] = 1; session.save()
        response = client.get(self.url, {'mode': 'sample'})
        data = dict(mode='sample', revision=0, nonce=response.context['nonce'],
                    operation='respond', key=self.key, choice='unknown')
        self.assertEqual(client.post(self.action, data).status_code, 403)
        data['csrfmiddlewaretoken'] = client.cookies['cc_synthetic_csrf'].value
        self.assertEqual(client.post(self.action, data).status_code, 302)

    def test_arbitrary_url_duplicate_and_oversize_inputs_fail(self):
        data = self.data()
        self.assertEqual(self.client.post(self.action, dict(data, url='https://example.invalid')).status_code, 400)
        self.assertEqual(self.client.post(self.action, dict(data, choice=['enjoyed', 'unknown'])).status_code, 400)
        self.assertEqual(self.client.post(self.action, dict(data, key='x' * 5000)).status_code, 400)
        self.assertEqual(self.client.get(self.url, {'url': 'https://example.invalid'}).status_code, 404)
        self.assertEqual(DiscoveryState.objects.count(), 0)

    def test_active_household_discovery_loads_external_feedback_scripts(self):
        response = self.client.get(self.url, {'mode': 'household', 'queue': 'artist'})
        self.assertContains(response, 'src="/static/card-feedback.js"')
        self.assertContains(response, 'src="/static/enhancement.js"')
        self.assertTrue(response.context['feedback_enabled'])
        self.assertNotContains(response, 'onclick=')

    def test_sample_and_unprepared_discovery_keep_scripts_disabled(self):
        sample = self.client.get(self.url, {'mode': 'sample'})
        self.assertFalse(sample.context['feedback_enabled'])
        self.assertNotContains(sample, 'src="/static/card-feedback.js"')
        self.assertNotContains(sample, 'src="/static/enhancement.js"')
        with patch('shelf.discovery_views.connection.introspection.table_names', return_value=[]):
            readonly = self.client.get(self.url, {'mode': 'household'})
        self.assertEqual(readonly.status_code, 200)
        self.assertFalse(readonly.context['available'])
        self.assertFalse(readonly.context['feedback_enabled'])
        self.assertNotContains(readonly, 'src="/static/card-feedback.js"')
        self.assertNotContains(readonly, 'src="/static/enhancement.js"')
