"""Gallery presentation through existing authenticated routes; no real source I/O."""
from datetime import datetime, timezone
from unittest.mock import patch

from django.test import TestCase, override_settings

from shelf import discovery_sources as sources, discovery_views
from shelf.gallery_sources import GalleryEvent, GalleryOutcome
from shelf.models import DiscoveryState, Grant, Profile, SavedItem
from shelf.tests import test_discovery_ui as existing


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class GalleryUITests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 31, 3, tzinfo=timezone.utc)
        self.enterContext(patch('django.utils.timezone.now', return_value=self.now))
        existing.DiscoveryUITests.setUp(self)
        discovery_views.PUBLIC_CACHE.clear()
        self.addCleanup(discovery_views.PUBLIC_CACHE.clear)
        self.event = GalleryEvent(
            key='official:event:synthetic-gallery', title='SYNTHETIC gallery <script>CANARY</script>',
            kind='event', source='Synthetic public gallery', provenance='live-automated', rank=1,
            retrieved_at=self.now.isoformat(), category='gallery',
            url='https://gertrude.org.au/exhibition/synthetic/',
            starts_at='2026-11-06T18:00:00+11:00', opening_date='2026-11-06',
            opening_wording='6pm; closing time not supplied', date_precision='opening time',
            exhibition_starts_on='2026-11-07', exhibition_ends_on='2026-12-12',
            timezone='Australia/Melbourne', checked_at=self.now.isoformat(),
            source_url='https://gertrude.org.au/exhibition/synthetic/')
        self.offer = {
            'offer_id': 'synthetic-offer', 'title': 'SYNTHETIC sale <script>CANARY</script>',
            'artist': 'Synthetic artist', 'seller': 'Synthetic seller', 'currency': 'AUD',
            'availability': 'unconfirmed', 'checked_at': self.now.isoformat(),
            'offer_url': 'https://www.australianprintworkshop.com/synthetic',
            'components': {name: {'status': 'unknown', 'source_basis': 'Not supplied'}
                           for name in ('artwork', 'shipping', 'framing', 'fees', 'tax', 'installation', 'fx')}}
        self.offer['components']['artwork'] = {
            'status': 'known', 'amount': '1100.00', 'source_basis': 'Listed item <script>CANARY</script>'}
        self.fetch = self.enterContext(patch.object(sources, 'fetch_public', return_value=GalleryOutcome(
            'ready', (self.event,), 'Synthetic bounded public read', offers=(self.offer,))))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))

    def page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response

    def fields(self):
        page = self.page()
        return {'mode': 'household', 'revision': page.context['revision'],
                'nonce': page.context['nonce'], 'operation': 'gallery'}

    def read(self):
        self.assertEqual(self.client.post(self.action, self.fields()).status_code, 302)
        return self.page()

    def test_get_is_local_and_explicit_read_replays_without_new_io(self):
        fields = self.fields()
        self.assertContains(self.page(), 'value="gallery"')
        self.fetch.assert_not_called()
        self.assertEqual(self.client.get(self.action).status_code, 405)
        self.assertEqual(self.client.post(self.action, fields).status_code, 302)
        self.assertEqual(self.client.post(self.action, fields).status_code, 302)
        self.fetch.assert_called_once()
        self.assertFalse(SavedItem.objects.exists())
        self.assertTrue(all(state.history == [] for state in DiscoveryState.objects.all()))

    def test_sample_mode_excludes_household_offers_sources_and_personal_brief(self):
        self.read()
        response = self.client.get(self.url, {'mode': 'sample', 'category': 'gallery'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['gallery_offers'], [])
        self.assertEqual(response.context['gallery_source_checks'], [])
        self.assertEqual(response.context['gallery_brief_note'], '')
        self.assertNotContains(response, self.offer['title'])
        self.assertNotContains(response, 'Your authorized art brief')

    def test_opening_and_run_have_distinct_precision_and_unknown_end(self):
        response = self.read()
        for text in ('Gallery opening:', '2026-11-06', 'Exhibition run:', '2026-11-07',
                     '2026-12-12', 'unknown; do not assume a closing time', 'Australia/Melbourne',
                     'Run dates are not daily opening hours', 'Source checked:'):
            self.assertContains(response, text)
        self.assertContains(response, '&lt;script&gt;CANARY&lt;/script&gt;')
        self.assertNotContains(response, '<script>CANARY</script>')

    def test_offer_is_separate_and_unknown_required_costs_block_complete_total(self):
        response = self.read()
        for text in ('Artwork sale leads', 'Listed item price', '1100.00', 'Framing',
                     'Unqualified purchase lead', 'Complete all-in total: unknown',
                     'No purchase is authorised', 'exactly AUD 2,500 fails'):
            self.assertContains(response, text)
        self.assertNotContains(response, 'Current cost and stock screening: strictly below')
        self.assertNotContains(response, 'value="purchase"')
        self.assertNotContains(response, 'value="synthetic-offer"')
        self.assertNotContains(response, '<script>CANARY</script>')

    def test_exact_ceiling_is_unqualified_even_with_confirmed_stock_and_costs(self):
        self.offer['availability'] = 'available'
        self.offer['components'] = {name: {'status': 'included', 'source_basis': 'Explicitly included'}
                                    for name in self.offer['components']}
        self.offer['components']['artwork'] = {
            'status': 'known', 'amount': '2500.00', 'source_basis': 'Exact observed item price'}
        response = self.read()
        self.assertContains(response, 'Unqualified purchase lead')
        self.assertContains(response, 'Observed complete total: AUD 2500.00')
        self.assertNotContains(response, 'Current cost and stock screening: strictly below')

    def test_recommendations_reuse_gallery_detail_and_offer_labels_without_fetch(self):
        self.read()
        self.fetch.reset_mock()
        response = self.client.get('/profiles/1/recommendations/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Gallery opening:')
        self.assertContains(response, 'Artwork sale leads')
        self.assertContains(response, 'Unqualified purchase lead')
        self.fetch.assert_not_called()

    def test_other_profile_cannot_see_owner_cache_or_personal_brief(self):
        self.read()
        partner = Profile.objects.get(pk=2)
        partner.active = True
        partner.save()
        Grant.objects.create(user=self.actor, profile=partner, can_view=True, can_select=True)
        session = self.client.session
        session['selected_profile_id'] = partner.pk
        session.save()
        for url in ('/profiles/2/discovery/', '/profiles/2/recommendations/'):
            response = self.client.get(url)
            # The current private provider demo is owner-only even if a spare
            # profile row and grant exist. Do not loosen that boundary to test UI.
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(b'SYNTHETIC gallery', response.content)
            self.assertNotIn(b'SYNTHETIC sale', response.content)
            self.assertNotIn(b'Your authorized art brief', response.content)
