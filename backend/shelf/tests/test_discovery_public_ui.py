"""Authenticated explicit public-read flows; every transport is mocked."""
from dataclasses import replace
from datetime import timedelta
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from shelf import discovery_sources as s, discovery_transport as t, discovery_views as v
from shelf.discovery_reducer import replay
from shelf.models import DiscoveryState
from shelf.tests import test_discovery_ui as existing
from shelf.tests.test_discovery_transport import NOW, event_document
from shelf.tests.test_discovery_sources import radio


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class PublicDiscoveryUITests(TestCase):
    def setUp(self):
        existing.DiscoveryUITests.setUp(self)
        v.PUBLIC_CACHE.clear()
        self.addCleanup(v.PUBLIC_CACHE.clear)
        clock = patch.object(v.timezone, 'now', return_value=NOW)
        clock.start(); self.addCleanup(clock.stop)
        network = patch.object(t, '_send', side_effect=AssertionError('Unmocked transport forbidden'))
        network.start(); self.addCleanup(network.stop)

    def form(self, operation='radio', **extra):
        response = self.client.get(self.url)
        data = dict(mode='household', revision=response.context['revision'],
                    nonce=response.context['nonce'], operation=operation, choice='',
                    key=s.household_candidates()[0].key if operation == 'radio' else '',
                    radio_mode='medium' if operation == 'radio' else '')
        data.update(extra)
        return data

    def test_only_explicit_post_calls_and_identical_replay_does_not(self):
        with patch.object(t, 'radio', return_value=(200, json.dumps(radio()).encode())) as send:
            data = self.form()
            self.assertEqual(self.client.get(self.action).status_code, 405)
            self.client.get(self.url, {'mode': 'sample'})
            send.assert_not_called()
            self.assertEqual(self.client.post(self.action, data).status_code, 302)
            self.assertEqual(self.client.post(self.action, data).status_code, 302)
            self.assertEqual(send.call_count, 1)
            state = DiscoveryState.objects.get()
            self.assertEqual(state.history, [])
            self.assertEqual(replay(state.history)['choices'], {})
            response = self.client.get(self.url)
            self.assertContains(response, 'ListenBrainz artist radio')
            self.assertContains(response, 'title unavailable')
            self.assertContains(response, 'Source position 1')
            self.assertNotContains(self.client.get(self.url, {'mode': 'sample'}), 'Recording by Synthetic artist')
            self.assertEqual(self.client.post(self.action, self.form()).status_code, 302)
            self.assertEqual(send.call_count, 2)

    def test_invalid_sample_stale_duplicate_fields_and_nonce_never_issue_io(self):
        original = self.form()
        with patch.object(s, 'fetch_public') as fetch:
            for change in ({'mode': 'sample'}, {'radio_mode': 'all'}, {'radio_mode': ''},
                           {'key': 'https://example.invalid'}, {'url': 'https://example.invalid'},
                           {'choice': 'enjoyed'}, {'operation': ['radio', 'events']}):
                self.assertEqual(self.client.post(self.action, dict(original, **change)).status_code, 400)
            self.assertEqual(self.client.post(self.action, dict(original, nonce='wrong')).status_code, 409)
            self.assertEqual(self.client.post(self.action, dict(original, revision=4)).status_code, 409)
            fetch.assert_not_called()
        with patch.object(t, 'radio', return_value=(200, b'{}')) as send:
            self.client.post(self.action, original)
            self.assertEqual(self.client.post(self.action, dict(original, radio_mode='hard')).status_code, 409)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(DiscoveryState.objects.get().history, [])

    def test_csrf_and_auth_current_profile_and_grant_gate_transport(self):
        client = Client(enforce_csrf_checks=True); client.force_login(self.actor)
        session = client.session; session['selected_profile_id'] = 1; session.save()
        response = client.get(self.url)
        data = self.form('events'); data['nonce'] = response.context['nonce']
        with patch.object(t, 'events', return_value=(200, json.dumps(event_document()).encode())) as send:
            self.assertEqual(client.post(self.action, data).status_code, 403)
            self.assertEqual(Client().post(self.action, data).status_code, 302)
            self.assertEqual(client.post('/profiles/2/discovery/action/', data).status_code, 403)
            send.assert_not_called()
            data['csrfmiddlewaretoken'] = client.cookies['cc_synthetic_csrf'].value
            self.assertEqual(client.post(self.action, data).status_code, 302)
            send.assert_called_once()
        with patch.object(s, 'fetch_public') as fetch:
            data = self.form()
            self.grant.can_select = False; self.grant.save()
            self.assertEqual(self.client.post(self.action, data).status_code, 404)
            self.grant.can_select = True; self.grant.save()
            session = self.client.session; session['selected_profile_id'] = 2; session.save()
            self.assertEqual(self.client.post(self.action, data).status_code, 404)
            self.assertEqual(self.client.post('/profiles/2/discovery/action/', data).status_code, 404)
            fetch.assert_not_called()

    def test_recheck_revocation_actor_and_persisted_selection_before_result(self):
        for change in ('grant', 'actor', 'profile', 'selection', 'logout'):
            v.PUBLIC_CACHE.clear()
            self.actor.is_active = True; self.actor.save()
            self.profile.active = True; self.profile.save()
            self.grant.can_select = True; self.grant.save()
            self.client.force_login(self.actor)
            session = self.client.session; session['selected_profile_id'] = 1; session.save()
            data = self.form('events')
            def mutate(now):
                if change == 'grant':
                    self.grant.can_select = False; self.grant.save()
                if change == 'actor':
                    get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
                if change == 'profile':
                    self.profile.active = False; self.profile.save()
                if change == 'selection':
                    other = self.client.session; other['selected_profile_id'] = 2; other.save()
                if change == 'logout':
                    self.client.session.flush()
                return 200, json.dumps(event_document()).encode()
            with self.subTest(change=change), patch.object(t, 'events', side_effect=mutate) as send:
                self.assertEqual(self.client.post(self.action, data).status_code, 404)
                send.assert_called_once()
                self.assertEqual(DiscoveryState.objects.get().history, [])
                self.assertEqual(v.PUBLIC_CACHE._cache, {})

    def test_concurrent_action_discards_result_and_preserves_choice(self):
        data = self.form('events')
        def newer(now):
            current = self.form('respond', key=s.household_candidates()[0].key, choice='unknown')
            self.assertEqual(self.client.post(self.action, current).status_code, 302)
            return 200, json.dumps(event_document()).encode()
        with patch.object(t, 'events', side_effect=newer):
            self.assertEqual(self.client.post(self.action, data).status_code, 409)
        self.assertEqual(v.PUBLIC_CACHE._cache, {})
        self.assertEqual(list(replay(DiscoveryState.objects.get().history)['choices'].values()), ['unknown'])

    def test_cache_expires_but_minimal_judged_identity_survives_without_io(self):
        with patch.object(t, 'radio', return_value=(200, json.dumps(radio()).encode())):
            self.client.post(self.action, self.form())
        response = self.client.get(self.url)
        card = next(row['card'] for row in response.context['music'] if row['card'].kind == 'recording')
        with patch.object(s, 'fetch_public') as fetch:
            self.assertEqual(self.client.post(self.action, self.form('save', key=card.key)).status_code, 302)
            self.assertEqual(self.client.post(self.action, self.form('respond', key=card.key, choice='unknown')).status_code, 302)
            history = DiscoveryState.objects.get().history
            self.assertNotIn('Synthetic artist', json.dumps(history))
            self.assertNotIn('title', history[0])
            self.assertEqual(history[0]['source']['roots'], list(card.roots))
            self.assertEqual(replay(history)['choices'][card.key], 'unknown')
            with patch.object(v.timezone, 'now', return_value=NOW + timedelta(minutes=15)):
                expired = self.client.get(self.url)
                self.assertNotContains(expired, 'Recording by Synthetic artist')
                self.assertContains(expired, 'Previously judged identity')
                self.assertEqual(expired.context['public_results'], [])
                self.assertEqual(self.client.post(self.action, self.form('respond', key=card.key, choice='not_for_me')).status_code, 302)
                self.assertEqual(self.client.post(self.action, self.form('unsave', key=card.key)).status_code, 302)
                self.assertEqual(self.client.post(self.action, self.form('undo')).status_code, 302)
            fetch.assert_not_called()

    def test_failure_empty_labels_retry_new_nonce_and_unknown_event_price(self):
        for payload, expected in [(b'{}', 'invalid'), (b'{"errors":[{"message":"CANARY"}]}', 'invalid'),
                                  (b'{"data":{"viewer":{"getEvents":{"totalCount":0,"items":[]}}}}', 'empty'),
                                  (json.dumps(event_document()).encode(), 'ready')]:
            with patch.object(t, 'events', return_value=(200, payload)) as send:
                data = self.form('events')
                self.client.post(self.action, data)
                self.client.post(self.action, data)
                send.assert_called_once()
                response = self.client.get(self.url)
                self.assertEqual(response.context['public_results'][0]['outcome'].status, expected)
                self.assertNotContains(response, 'CANARY')
        self.assertContains(response, 'Artwork price unknown; check official source')
        self.assertNotContains(response, 'within budget')
        self.assertContains(response, 'Entry fee: unknown; check official source')
        self.assertEqual(DiscoveryState.objects.get().history, [])

    def test_artwork_price_used_by_actual_card_template_and_entry_fee_separate(self):
        card = next(c for c in s.sample_candidates() if c.category == 'gallery')
        for price, currency, per, expected in [('2499.99', 'AUD', 'individual artwork', 'within budget'),
                ('2500', 'AUD', 'individual artwork', 'outside budget'),
                (None, 'AUD', 'individual artwork', 'Artwork price unknown'),
                ('10', 'USD', 'individual artwork', 'Artwork price unknown'),
                ('10', 'AUD', 'admission', 'Artwork price unknown')]:
            priced = replace(card, artwork_price=price, artwork_currency=currency, price_per=per, entry_fee='AUD 10 (sample)')
            with patch.object(s, 'sample_candidates', return_value=[priced]):
                response = self.client.get(self.url, {'mode': 'sample', 'category': 'gallery'})
                self.assertContains(response, expected)
                self.assertContains(response, 'Entry fee: AUD 10 (sample)')
                if expected != 'within budget': self.assertNotContains(response, 'within budget')

    def test_normalized_registry_drives_ui_and_attribution_stays_separate(self):
        registry = s.registry()
        self.assertEqual(len(registry['sources']), 30)
        self.assertFalse(any(r.get('verification') == 'pending' for r in registry['event_sources']))
        changed = dict(registry, sources=[dict(r, url='https://thornburytheatre.com/verified-reference')
                                         if r['label'] == 'Thornbury Theatre' else r for r in registry['sources']])
        with patch.object(s, 'registry', return_value=changed):
            response = self.client.get(self.url)
            self.assertContains(response, 'https://thornburytheatre.com/verified-reference')
            self.assertEqual(next(c for c in s.household_candidates() if c.title == 'Thornbury Theatre').url,
                             'https://thornburytheatre.com/verified-reference')
        for row in registry['sources']: self.assertContains(response, row['label'])
        for text in ('inferred-adjacent', 'candidate-for-unconfirmed-deribin', 'unconfirmed; not auto-mapped',
                     'finished', 'August 2027; not current', 'discount overlay', 'year-round',
                     'gallery openings', 'community festivals', 'regional winery/day-trip'):
            self.assertContains(response, text)
        self.assertFalse(DiscoveryState.objects.exists())
