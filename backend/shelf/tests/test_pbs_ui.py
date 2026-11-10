"""PBS HTTP integration with synthetic HTML and mocked external reads only."""
from datetime import timedelta
import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from shelf import discovery_sources as sources, discovery_transport as transport, discovery_views as views
from shelf import event_scraping
from shelf.models import DiscoveryState
from shelf.tests import test_discovery_public_ui as existing
from shelf.tests.test_discovery_transport import NOW, event_document
from shelf.tests.test_pbs_events import card, listing
from shelf.tests.test_rrr_events import card as rrr_card, listing as rrr_listing


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class PBSUITests(TestCase):
    setUp = existing.PublicDiscoveryUITests.setUp
    form = existing.PublicDiscoveryUITests.form

    def test_explicit_read_replay_attribution_and_no_inferred_choice(self):
        with patch.object(transport, 'pbs', return_value=(200, listing())) as read, \
             patch.object(event_scraping, 'collect', wraps=event_scraping.collect) as collect:
            page = self.client.get(self.url)
            self.assertContains(page, 'Read PBS Events')
            self.client.get('/recommendations/')
            self.assertEqual(self.client.get(self.action).status_code, 405)
            read.assert_not_called()
            data = self.form('pbs')
            self.assertEqual(self.client.post(self.action, data).status_code, 302)
            self.assertEqual(self.client.post(self.action, data).status_code, 302)
            read.assert_called_once()
            collect.assert_called_once_with('pbs', now=NOW)
        self.assertEqual(DiscoveryState.objects.get().history, [])
        for url in (self.url, '/recommendations/'):
            response = self.client.get(url)
            for text in ('SYNTHETIC music event', 'https://www.pbsfm.org.au/news/synthetic-event',
                         'PBS Events', 'Sun 15 Nov 2026', 'timezone unverified', 'live-automated'):
                self.assertContains(response, text)
        self.assertContains(self.client.get(self.url), 'Source notice: Free Event (not a current seller check)')
        sample = self.client.get(self.url, {'mode': 'sample'})
        self.assertNotContains(sample, 'SYNTHETIC music event')
        self.assertEqual(sample.context['more_source_events'], [])

    def test_invalid_fields_sample_nonce_and_grant_prevent_reads(self):
        data = self.form('pbs')
        with patch.object(transport, 'pbs') as read:
            for change in ({'mode': 'sample'}, {'key': 'official:venue:anything'},
                    {'radio_mode': 'medium'}, {'choice': 'interested'}, {'url': 'https://evil.invalid'},
                    {'operation': ['pbs', 'beat']}):
                self.assertEqual(self.client.post(self.action, dict(data, **change)).status_code, 400)
            self.assertEqual(self.client.post(self.action, dict(data, nonce='stale')).status_code, 409)
            self.assertEqual(Client().post(self.action, data).status_code, 302)
            self.grant.can_select = False
            self.grant.save()
            self.assertEqual(self.client.post(self.action, data).status_code, 404)
            for seed, mode in (('https://evil.invalid', ''), ('', 'medium')):
                self.assertEqual(sources.fetch_public('pbs', seed, mode, NOW).status, 'invalid')
            read.assert_not_called()

    def test_csrf_is_required_for_source_read(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        session = client.session
        session['selected_profile_id'] = 1
        session.save()
        with patch.object(transport, 'pbs') as read:
            self.assertEqual(client.post(self.action, self.form('pbs')).status_code, 403)
            read.assert_not_called()

    def test_revoked_grant_during_read_discards_result(self):
        data = self.form('pbs')
        def revoked():
            self.grant.can_select = False
            self.grant.save()
            return 200, listing()
        with patch.object(transport, 'pbs', side_effect=revoked) as read:
            self.assertEqual(self.client.post(self.action, data).status_code, 404)
            read.assert_called_once()
        self.assertEqual(views.PUBLIC_CACHE._cache, {})
        self.assertEqual(DiscoveryState.objects.get().history, [])

    def test_newer_choice_during_read_is_preserved_and_result_discarded(self):
        data = self.form('pbs')
        def newer():
            current = self.form('respond', key=sources.household_candidates()[0].key, choice='unknown')
            self.assertEqual(self.client.post(self.action, current).status_code, 302)
            return 200, listing()
        with patch.object(transport, 'pbs', side_effect=newer):
            self.assertEqual(self.client.post(self.action, data).status_code, 409)
        self.assertEqual(views.PUBLIC_CACHE._cache, {})
        self.assertEqual(DiscoveryState.objects.get().history[0]['choice'], 'unknown')

    def test_expiry_keeps_only_minimal_judged_identity_without_network(self):
        with patch.object(transport, 'pbs', return_value=(200, listing())):
            self.client.post(self.action, self.form('pbs'))
        page = self.client.get(self.url)
        event = next(row['card'] for row in page.context['events'] if row['card'].kind == 'event')
        self.assertEqual(self.client.post(self.action, self.form('save', key=event.key)).status_code, 302)
        history = json.dumps(DiscoveryState.objects.get().history)
        for excluded in ('SYNTHETIC music event', 'Sun 15 Nov 2026', 'Free Event', 'CANARY'):
            self.assertNotIn(excluded, history)
        with patch.object(views.timezone, 'now', return_value=NOW + timedelta(minutes=15)), \
                patch.object(transport, 'pbs') as read:
            expired = self.client.get(self.url)
            self.assertNotContains(expired, 'SYNTHETIC music event')
            self.assertContains(expired, 'Previously judged identity')
            self.assertEqual(expired.context['more_source_events'], [])
            self.assertNotContains(self.client.get('/recommendations/'), 'SYNTHETIC music event')
            read.assert_not_called()

    def test_escaped_notice_and_failed_refresh_replaces_old_evidence(self):
        raw = listing([card(title='SYNTHETIC <script>title</script>', notice='<b>Source wording</b>')])
        with patch.object(transport, 'pbs', return_value=(200, raw)):
            self.client.post(self.action, self.form('pbs'))
        response = self.client.get(self.url)
        self.assertContains(response, '&lt;script&gt;title&lt;/script&gt;')
        self.assertContains(response, '&lt;b&gt;Source wording&lt;/b&gt;')
        self.assertNotContains(response, '<script>title</script>')
        with patch.object(transport, 'pbs', side_effect=transport.PBSIntervalPending('CANARY')):
            self.client.post(self.action, self.form('pbs'))
        response = self.client.get(self.url)
        self.assertContains(response, 'ten seconds between shared requests')
        self.assertNotContains(response, 'SYNTHETIC')
        self.assertNotContains(response, 'CANARY')
        self.assertEqual(response.context['public_results'][0]['outcome'].status, 'rate-limited')
        self.assertEqual(DiscoveryState.objects.get().history, [])

    def test_overflow_covers_multiple_sources_without_duplicates_or_rejected_cards(self):
        past = [card('past-' + str(i), when='Tue 27 Oct 2026', machine='2026-10-27T00:00:00+11:00') for i in range(30)]
        current = [card('current-' + str(i), title='SYNTHETIC PBS ' + str(i)) for i in range(9)]
        with patch.object(transport, 'pbs', return_value=(200, listing(past + current))), \
                patch.object(transport, 'rrr', return_value=(200, rrr_listing(*(rrr_card(i) for i in range(1, 10))))), \
                patch.object(transport, 'events', return_value=(200, json.dumps(event_document()).encode())):
            for operation in ('pbs', 'rrr', 'events'):
                self.assertEqual(self.client.post(self.action, self.form(operation)).status_code, 302)
        page = self.client.get(self.url)
        cards = [row['card'] for row in page.context['events'] + page.context['more_source_events']
                 if row['card'].kind == 'event']
        self.assertEqual(len(cards), 19)
        self.assertEqual(len({card.key for card in cards}), 19)
        extra_sources = {row['card'].source for row in page.context['more_source_events']}
        self.assertIn('PBS Events', extra_sources)
        self.assertIn('Triple R events', extra_sources)
        self.assertContains(page, 'More from your source reads')
        reject = next(card for card in cards if getattr(card, 'source_id', '') == 'pbs')
        self.client.post(self.action, self.form('respond', key=reject.key, choice='not_for_me'))
        page = self.client.get(self.url)
        self.assertNotIn(reject.key, {row['card'].key for row in page.context['events'] + page.context['more_source_events']})
        self.assertEqual(self.client.get(self.url, {'category': 'gallery'}).context['more_source_events'], [])
