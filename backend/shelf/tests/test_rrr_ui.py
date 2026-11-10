"""The new read operation uses the existing human-intent and session boundaries."""
from datetime import timedelta
import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from shelf import discovery_sources as sources, discovery_transport as transport, discovery_views as views
from shelf import event_scraping
from shelf.models import DiscoveryState
from shelf.tests import test_discovery_public_ui as existing
from shelf.tests.test_discovery_transport import NOW
from shelf.tests.test_rrr_events import card, listing


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class RRRUITests(TestCase):
    setUp = existing.PublicDiscoveryUITests.setUp
    form = existing.PublicDiscoveryUITests.form

    def test_post_replay_one_read_no_choices_and_both_pages_show_attribution(self):
        with patch.object(transport, 'rrr', return_value=(200, listing())) as send, \
             patch.object(event_scraping, 'collect', wraps=event_scraping.collect) as collect:
            data = self.form('rrr')
            self.client.get('/recommendations/')
            self.assertEqual(self.client.get(self.action).status_code, 405)
            send.assert_not_called()
            self.assertEqual(self.client.post(self.action, data).status_code, 302)
            self.assertEqual(self.client.post(self.action, data).status_code, 302)
            send.assert_called_once()
            collect.assert_called_once_with('rrr', now=NOW)
        state = DiscoveryState.objects.get()
        self.assertEqual(state.history, [])
        for url in (self.url, '/recommendations/'):
            page = self.client.get(url)
            self.assertContains(page, 'SYNTHETIC event')
            self.assertContains(page, 'https://www.rrr.org.au/events/synthetic-1')
            self.assertContains(page, 'Triple R events')
            self.assertContains(page, 'year and start time unverified')
            self.assertContains(page, 'live-automated')
            self.assertContains(page, 'Partial coverage')
        self.assertNotContains(self.client.get(self.url, {'mode': 'sample'}), 'SYNTHETIC event')

    def test_failed_rrr_refresh_preserves_other_source_result(self):
        from shelf.tests.test_discovery_transport import event_document
        with patch.object(transport, 'events', return_value=(200, json.dumps(event_document()).encode())):
            self.client.post(self.action, self.form('events'))
        with patch.object(transport, 'rrr', return_value=(200, listing(card(name='SYNTHETIC RRR')))):
            self.client.post(self.action, self.form('rrr'))
        with patch.object(transport, 'rrr', side_effect=TimeoutError('CANARY')) as send:
            data = self.form('rrr')
            self.client.post(self.action, data)
            self.client.post(self.action, data)
            send.assert_called_once_with()
        page = self.client.get(self.url)
        self.assertNotContains(page, 'SYNTHETIC RRR')
        self.assertNotContains(page, 'CANARY')
        self.assertContains(page, 'SYNTHETIC event')
        self.assertContains(page, 'Moshtix')
        self.assertContains(page, 'timeout')
        self.assertEqual(DiscoveryState.objects.get().history, [])

    def test_invalid_sample_extra_fields_stale_nonce_and_grant_prevent_io(self):
        data = self.form('rrr')
        with patch.object(transport, 'rrr') as send:
            for change in ({'mode': 'sample'}, {'key': 'official:venue:anything'},
                           {'radio_mode': 'medium'}, {'choice': 'interested'}, {'url': 'https://evil.invalid'}):
                self.assertEqual(self.client.post(self.action, dict(data, **change)).status_code, 400)
            self.assertEqual(self.client.post(self.action, dict(data, nonce='stale')).status_code, 409)
            self.assertEqual(Client().post(self.action, data).status_code, 302)
            self.grant.can_select = False; self.grant.save()
            self.assertEqual(self.client.post(self.action, data).status_code, 404)
            send.assert_not_called()

    def test_csrf_protects_explicit_reader(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        session = client.session; session['selected_profile_id'] = 1; session.save()
        with patch.object(transport, 'rrr') as send:
            self.assertEqual(client.post(self.action, self.form('rrr')).status_code, 403)
            send.assert_not_called()

    def test_revocation_during_read_discards_source_result(self):
        data = self.form('rrr')
        def revoked():
            self.grant.can_select = False; self.grant.save()
            return 200, listing()
        with patch.object(transport, 'rrr', side_effect=revoked) as send:
            self.assertEqual(self.client.post(self.action, data).status_code, 404)
            send.assert_called_once()
        self.assertEqual(views.PUBLIC_CACHE._cache, {})
        self.assertEqual(DiscoveryState.objects.get().history, [])

    def test_expiry_keeps_only_judged_identity_and_overview_drops_expired_content(self):
        with patch.object(transport, 'rrr', return_value=(200, listing())):
            self.client.post(self.action, self.form('rrr'))
        page = self.client.get(self.url)
        event = next(row['card'] for row in page.context['events'] if row['card'].kind == 'event')
        self.client.post(self.action, self.form('save', key=event.key))
        history = json.dumps(DiscoveryState.objects.get().history)
        self.assertNotIn('SYNTHETIC event', history)
        self.assertNotIn('Friday, 30 October', history)
        with patch.object(views.timezone, 'now', return_value=NOW + timedelta(minutes=15)), \
             patch.object(transport, 'rrr') as send:
            self.assertNotContains(self.client.get(self.url), 'SYNTHETIC event')
            self.assertContains(self.client.get(self.url), 'Previously judged identity')
            self.assertNotContains(self.client.get('/recommendations/'), 'Previously judged identity')
            self.assertNotContains(self.client.get('/recommendations/'), 'SYNTHETIC event')
            send.assert_not_called()

    def test_source_markup_escaped_and_failed_refresh_replaces_old_cards(self):
        name = 'SYNTHETIC <script>alert(1)</script>'
        with patch.object(transport, 'rrr', return_value=(200, listing(card(name=name)))):
            self.client.post(self.action, self.form('rrr'))
        for url in (self.url, '/recommendations/'):
            response = self.client.get(url)
            self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
            self.assertNotContains(response, '<script>alert(1)</script>')
        with patch.object(transport, 'rrr', return_value=(403, b'CANARY response')) as send:
            self.client.post(self.action, self.form('rrr'))
            send.assert_called_once()
        page = self.client.get(self.url)
        self.assertNotContains(page, 'SYNTHETIC')
        self.assertNotContains(page, 'CANARY')
        self.assertContains(page, 'error')
        self.assertEqual(DiscoveryState.objects.get().history, [])
