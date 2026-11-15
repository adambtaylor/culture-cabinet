"""Actual authenticated provider-discovery actions, fictional listing responses."""
from unittest.mock import patch
from django.test import TestCase, Client, override_settings
from shelf import event_listing_transport as transport, discovery_views as views
from shelf.models import DiscoveryState
from shelf.tests import test_discovery_public_ui as existing
from shelf.tests.test_venue303_events import card, listing
from shelf.tests.test_discovery_transport import NOW


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class VenueEventUITests(TestCase):
    setUp = existing.PublicDiscoveryUITests.setUp
    form = existing.PublicDiscoveryUITests.form

    def test_sessions_survive_post_cache_reload_replay_and_only_scoped_cancel_removed(self):
        raw = listing(card(clock='7:30 PM'), card(clock='9:30 PM'))
        with patch.object(transport, 'venue303', return_value=(200, raw)) as read:
            data = self.form('venue303')
            self.client.get(self.url)
            read.assert_not_called()
            self.assertEqual(self.client.get(self.action).status_code, 405)
            self.assertEqual(self.client.post(self.action, data).status_code, 302)
            self.client.post(self.action, data)
            for _ in range(2):
                page = self.client.get(self.url)
                cards = [r['card'] for r in page.context['events'] + page.context['more_source_events']
                         if getattr(r['card'], 'source_id', '') == 'venue303']
                self.assertEqual(len(cards), 2)
                self.assertNotEqual(cards[0].key, cards[1].key)
                self.assertEqual(cards[0].url, cards[1].url)
                self.assertContains(page, 'timezone')
            read.assert_called_once_with()
        with patch.object(transport, 'venue303', return_value=(200,
                listing(card(clock='7:30 PM', notice='cancelled'), card(clock='9:30 PM')))):
            self.client.post(self.action, self.form('venue303'))
        outcome = self.client.get(self.url).context['public_results'][0]['outcome']
        self.assertEqual(len(outcome.candidates), 1)
        self.assertIn('21:30', outcome.candidates[0].source_start)
        self.assertEqual(DiscoveryState.objects.get().history, [])

    def test_invalid_authority_csrf_and_revocation_do_not_publish(self):
        data = self.form('venue303')
        with patch.object(transport, 'venue303') as read:
            for change in ({'mode': 'sample'}, {'key': 'https://evil.invalid'}, {'radio_mode': 'medium'},
                           {'url': 'https://evil.invalid'}):
                self.assertEqual(self.client.post(self.action, dict(data, **change)).status_code, 400)
            self.assertEqual(self.client.post(self.action, dict(data, nonce='stale')).status_code, 409)
            csrf = Client(enforce_csrf_checks=True);csrf.force_login(self.actor)
            self.assertEqual(csrf.post(self.action, data).status_code, 403)
            read.assert_not_called()
        def revoke():
            self.grant.can_select = False;self.grant.save()
            return 200, listing()
        with patch.object(transport, 'venue303', side_effect=revoke) as read:
            self.assertEqual(self.client.post(self.action, data).status_code, 404)
            read.assert_called_once_with()
        self.assertEqual(views.PUBLIC_CACHE._cache, {})

    def test_failure_replaces_source_success_without_untrusted_text(self):
        with patch.object(transport, 'venue303', return_value=(200, listing(card(title='<script>SYNTHETIC</script>')))):
            self.client.post(self.action, self.form('venue303'))
        self.assertContains(self.client.get(self.url), '&lt;script&gt;SYNTHETIC&lt;/script&gt;')
        with patch.object(transport, 'venue303', side_effect=transport.ListingIntervalPending('CANARY')):
            self.client.post(self.action, self.form('venue303'))
        page = self.client.get(self.url)
        self.assertNotContains(page, 'SYNTHETIC')
        self.assertNotContains(page, 'CANARY')
        self.assertContains(page, 'Read deferred')
        self.assertEqual(page.context['public_results'][0]['outcome'].status, 'rate-limited')

    def test_forum_actual_action_replay_and_rendered_same_url_sessions(self):
        from shelf.tests.test_forum_events import listing as forum_listing, session
        with patch.object(transport, 'forum', return_value=(200,
                forum_listing(session(),session('2026-11-01T21:00:00+11:00')))) as read:
            data = self.form('forum')
            self.assertEqual(self.client.post(self.action,data).status_code, 302)
            self.client.post(self.action,data)
            page = self.client.get(self.url)
            events = page.context['public_results'][0]['outcome'].candidates
            self.assertEqual(len(events), 2)
            self.assertNotEqual(events[0].key, events[1].key)
            self.assertContains(page, 'Forum Melbourne shows')
            read.assert_called_once_with()

    def test_forum_revocation_and_failed_refresh_are_safe(self):
        from shelf.tests.test_forum_events import listing as forum_listing
        with patch.object(transport, 'forum', return_value=(200, forum_listing())):
            self.client.post(self.action, self.form('forum'))
        with patch.object(transport, 'forum', side_effect=TimeoutError('CANARY')) as read:
            self.client.post(self.action, self.form('forum'))
            page = self.client.get(self.url)
            self.assertContains(page, 'Read timed out')
            self.assertEqual(page.context['public_results'][0]['outcome'].status, 'timeout')
            self.assertNotContains(page, 'CANARY')
            read.assert_called_once_with()
        data = self.form('forum')
        def revoke():
            self.grant.can_select=False;self.grant.save()
            return 200, forum_listing()
        with patch.object(transport, 'forum', side_effect=revoke):
            self.assertEqual(self.client.post(self.action, data).status_code, 404)
        self.assertEqual(DiscoveryState.objects.get().history, [])
