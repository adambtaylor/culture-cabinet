"""Shared discovery preserves exhibition runs and actor-scoped art hypotheses."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from pathlib import Path

from django.test import TestCase, override_settings

from shelf import discovery_views
from shelf.gallery_sources import GalleryEvent, GalleryOutcome, parse_gertrude
from shelf.models import DiscoveryState
from shelf.tests import test_discovery_ui as existing


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class GalleryIntegrationTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 3, tzinfo=timezone.utc)
        self.enterContext(patch('django.utils.timezone.now', return_value=self.now))
        existing.DiscoveryUITests.setUp(self)
        discovery_views.PUBLIC_CACHE.clear()
        self.addCleanup(discovery_views.PUBLIC_CACHE.clear)
        self.event = GalleryEvent(key='official:event:synthetic-current-gallery',
            title='SYNTHETIC continuing exhibition', kind='event', category='gallery',
            source='SYNTHETIC public gallery', provenance='live-automated', rank=1,
            retrieved_at=self.now.isoformat(), url='https://gertrude.org.au/exhibition/synthetic/',
            source_url='https://gertrude.org.au/exhibition/synthetic/',
            starts_at='2026-11-06T18:00:00+11:00', opening_date='2026-11-06',
            opening_ends_at='2026-11-06T20:00:00+11:00',
            exhibition_starts_on='2026-11-07', exhibition_ends_on='2026-12-12',
            checked_at=self.now.isoformat())

    def cache(self, event):
        discovery_views.PUBLIC_CACHE.set(discovery_views.cache_key(self.request(), 1), {
            ('gallery', '', ''): {'at': self.now, 'source': 'SYNTHETIC gallery read',
                'outcome': GalleryOutcome('ready', (event,), 'Synthetic primary result')}}, timeout=900)

    def request(self):
        from django.test import RequestFactory
        request = RequestFactory().get('/profiles/1/discovery/')
        request.user = self.actor
        request.session = self.client.session
        return request

    def test_opening_passed_but_verified_exhibition_run_remains_a_recommendation(self):
        self.cache(self.event)
        page = self.client.get('/profiles/1/recommendations/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, self.event.title)
        self.assertContains(page, '2026-11-06')
        self.assertContains(page, '2026-12-12')
        self.assertContains(page, 'Curatorial hypothesis')
        self.assertFalse(DiscoveryState.objects.exists())

    def test_unknown_run_end_does_not_keep_a_past_opening_current(self):
        self.cache(replace(self.event, exhibition_starts_on=None, exhibition_ends_on=None))
        page = self.client.get('/profiles/1/recommendations/')
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, self.event.title)

    def test_expired_public_result_does_not_survive_as_current_gallery_offer(self):
        self.cache(self.event)
        with patch('django.utils.timezone.now', return_value=self.now + timedelta(minutes=16)):
            page = self.client.get('/profiles/1/recommendations/')
        self.assertNotContains(page, self.event.title)

    def test_real_parser_identity_remains_correctable_after_source_expiry(self):
        raw = (Path(__file__).resolve().parents[1] / 'fixtures/gallery_gertrude.html').read_bytes()
        event = parse_gertrude(raw, retrieved_at=self.now.isoformat())
        self.assertEqual(event.provenance, 'live-automated')
        self.cache(event)

        def respond(choice):
            page = self.client.get(self.url)
            data = {'mode': 'household', 'revision': page.context['revision'],
                'nonce': page.context['nonce'], 'operation': 'respond',
                'key': event.key, 'choice': choice}
            self.assertEqual(self.client.post(self.action, data).status_code, 302)

        respond('interested')
        with patch('django.utils.timezone.now', return_value=self.now + timedelta(minutes=16)):
            page = self.client.get(self.url)
            judged = [row['card'] for row in page.context['responses'] if row['card'].key == event.key]
            self.assertEqual(len(judged), 1)
            self.assertIn('expired', judged[0].title)
            respond('not_for_me')
        self.assertEqual(DiscoveryState.objects.get().history[-1]['choice'], 'not_for_me')
