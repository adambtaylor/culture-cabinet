"""Fresh cached event authority and detail expiry through the actual POST flow."""
from datetime import timedelta
import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from shelf import discovery_transport as transport, discovery_views as views
from shelf.models import DiscoveryState
from shelf.tests import test_discovery_public_ui as existing
from shelf.tests.test_discovery_transport import NOW
from shelf.tests.test_rrr_details import detail, event, RANGE
from shelf.tests.test_rrr_events import listing


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class DetailUITests(TestCase):
    form = existing.PublicDiscoveryUITests.form

    def setUp(self):
        existing.PublicDiscoveryUITests.setUp(self)
        with patch.object(transport, 'rrr', return_value=(200, listing())):
            self.client.post(self.action, self.form('rrr'))

    def detail_form(self, **extra):
        return self.form('rrr_detail', key=event().key, **extra)

    def test_read_and_replay_are_one_request_and_no_human_preference(self):
        with patch.object(transport, 'rrr_detail', return_value=(200, detail(when=RANGE))) as send:
            data = self.detail_form()
            self.client.get(self.url)
            self.client.get('/recommendations/')
            send.assert_not_called()
            self.assertEqual(self.client.post(self.action, data).status_code, 302)
            self.assertEqual(self.client.post(self.action, data).status_code, 302)
            send.assert_called_once_with(event().url)
        self.assertEqual(DiscoveryState.objects.get().history, [])
        for url in (self.url, '/recommendations/'):
            page = self.client.get(url)
            self.assertContains(page, '2026-10-25')
            self.assertContains(page, '2026-11-08')
            self.assertContains(page, '16:15')
            self.assertContains(page, 'Timezone unverified')
            self.assertContains(page, '123 Synthetic Street')
            self.assertContains(page, 'https://tickets.example.org/event/123')
            self.assertContains(page, 'Destination and ticket availability unverified')
            self.assertContains(page, 'Original listing date: Friday, 30 October')

    def test_near_expiry_detail_read_does_not_extend_listing_or_persist_metadata(self):
        with patch.object(views.timezone, 'now', return_value=NOW+timedelta(minutes=14)), \
             patch.object(transport, 'rrr_detail', return_value=(200, detail())):
            self.assertEqual(self.client.post(self.action, self.detail_form()).status_code, 302)
            self.assertContains(self.client.get(self.url), '123 Synthetic Street')
            self.client.post(self.action, self.form('save', key=event().key))
        history = json.dumps(DiscoveryState.objects.get().history)
        for value in ('123 Synthetic Street', 'tickets.example.org', '6:00 pm'):
            self.assertNotIn(value, history)
        with patch.object(views.timezone, 'now', return_value=NOW+timedelta(minutes=15)), \
             patch.object(transport, 'rrr_detail') as send:
            for url in (self.url, '/recommendations/'):
                self.assertNotContains(self.client.get(url), '123 Synthetic Street')
                self.assertNotContains(self.client.get(url), 'tickets.example.org')
            self.assertEqual(self.client.post(self.action, self.detail_form()).status_code, 409)
            send.assert_not_called()

    def test_parent_listing_expiry_during_io_discards_enrichment(self):
        clock = [NOW+timedelta(minutes=14,seconds=59)]
        def slow(url):
            clock[0] = NOW+timedelta(minutes=15)
            return 200, detail()
        with patch.object(views.timezone, 'now', side_effect=lambda: clock[0]), \
             patch.object(transport, 'rrr_detail', side_effect=slow) as send:
            self.assertEqual(self.client.post(self.action, self.detail_form()).status_code, 409)
            send.assert_called_once()
            self.assertNotContains(self.client.get(self.url), '123 Synthetic Street')

    def test_refresh_listing_drops_previous_enrichment(self):
        with patch.object(transport, 'rrr_detail', return_value=(200, detail())):
            self.client.post(self.action, self.detail_form())
        with patch.object(views.timezone, 'now', return_value=NOW+timedelta(minutes=2)), \
             patch.object(transport, 'rrr', return_value=(200, listing())):
            self.client.post(self.action, self.form('rrr'))
            self.assertNotContains(self.client.get(self.url), '123 Synthetic Street')
            self.assertContains(self.client.get(self.url), 'have not been read')

    def test_failed_detail_refresh_clears_prior_link_and_shows_safe_error(self):
        with patch.object(transport, 'rrr_detail', return_value=(200, detail())):
            self.client.post(self.action, self.detail_form())
        with patch.object(transport, 'rrr_detail', return_value=(403, b'CANARY private response')) as send:
            self.client.post(self.action, self.detail_form())
            send.assert_called_once()
        for url in (self.url, '/recommendations/'):
            page = self.client.get(url)
            self.assertContains(page, 'Detail request failed')
            self.assertContains(page, 'SYNTHETIC event')
            self.assertNotContains(page, '123 Synthetic Street')
            self.assertNotContains(page, 'tickets.example.org')
            self.assertNotContains(page, 'CANARY')

    def test_unlisted_key_arbitrary_url_sample_and_invalid_fields_prevent_io(self):
        data = self.detail_form()
        with patch.object(transport, 'rrr_detail') as send:
            for changes, expected in [({'key':'official:event:unlisted'},409),
                    ({'key':'https://evil.example.org'},409), ({'mode':'sample'},400),
                    ({'choice':'interested'},400), ({'radio_mode':'medium'},400),
                    ({'url':'https://evil.example.org'},400), ({'nonce':'stale'},409)]:
                self.assertEqual(self.client.post(self.action, dict(data,**changes)).status_code, expected)
            self.assertEqual(Client().post(self.action,data).status_code,302)
            other = Client(enforce_csrf_checks=True); other.force_login(self.actor)
            self.assertEqual(other.post(self.action,data).status_code,403)
            self.grant.can_select=False; self.grant.save()
            self.assertEqual(self.client.post(self.action,data).status_code,404)
            send.assert_not_called()

    def test_separate_session_cannot_use_another_sessions_cached_card(self):
        other = Client(); other.force_login(self.actor)
        session = other.session; session['selected_profile_id']=1; session.save()
        with patch.object(transport,'rrr_detail') as send:
            self.assertEqual(other.post(self.action,self.detail_form()).status_code,409)
            send.assert_not_called()

    def test_post_fetch_revocation_and_newer_action_discard_details(self):
        for change in ('grant', 'selection', 'newer'):
            self.grant.can_select=True; self.grant.save()
            session=self.client.session; session['selected_profile_id']=1; session.save()
            data=self.detail_form()
            def changed(url):
                if change == 'grant':
                    self.grant.can_select=False; self.grant.save()
                elif change == 'selection':
                    session=self.client.session; session['selected_profile_id']=2; session.save()
                else:
                    self.client.post(self.action,self.form('save',key=event().key))
                return 200, detail()
            with patch.object(transport,'rrr_detail',side_effect=changed):
                self.assertEqual(self.client.post(self.action,data).status_code,409 if change=='newer' else 404)
            cached = views.PUBLIC_CACHE.get(f'{self.actor.pk}:1:{self.client.session.session_key}')
            self.assertTrue(all(c.details is None for c in cached[('rrr','','')]['outcome'].candidates))

    def test_markup_is_text_and_links_are_not_automatically_followed(self):
        raw=detail(address='<img src=x onerror=alert(1)>')
        with patch.object(transport,'rrr_detail',return_value=(200,raw)) as send:
            self.client.post(self.action,self.detail_form())
            for url in (self.url,'/recommendations/'):
                page=self.client.get(url)
                self.assertContains(page,'&lt;img src=x onerror=alert(1)&gt;')
                self.assertNotContains(page,'<img src=x onerror=alert(1)>')
            send.assert_called_once()
