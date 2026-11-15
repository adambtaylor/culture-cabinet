"""Unavailable current calendar is explicit; no guessed path or fake show."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from django.test import TestCase as DjangoCase,override_settings
from shelf import event_listing_transport as transport,event_scraping
from shelf.models import DiscoveryState
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests
from shelf.tests.test_discovery_transport import NOW


class ConspiracyReadinessTests(TestCase):
    def test_fixed_homepage_and_robots_share_one_origin_reservation(self):
        with TemporaryDirectory() as directory,patch.object(transport,'_RATE_DIRECTORY',directory),\
             patch.object(transport,'_send',return_value=(404,b'Not found')) as send:
            self.assertTrue(transport.observe_robots('improv_conspiracy')['allowed'])
            with self.assertRaises(transport.ListingIntervalPending):transport.improv_conspiracy()
            self.assertEqual(len(list(Path(directory).iterdir())),1);send.assert_called_once()
        with patch.object(transport,'_reserve'),patch.object(transport,'_send',return_value=(200,b'HTML')) as send:
            transport.improv_conspiracy()
            self.assertEqual(send.call_args.args[0][-1],'https://improvconspiracy.com/')

    def test_missing_calendar_descriptor_never_acquires_a_guessed_url(self):
        with patch.object(transport,'_send') as send:
            outcome=event_scraping.collect('improv_conspiracy',now=NOW)
            self.assertEqual(outcome.status,'unavailable');self.assertEqual(outcome.candidates,())
            self.assertIn('Performance Calendar target',outcome.message);send.assert_not_called()


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class ConspiracyReadinessUITests(DjangoCase):
    setUp=PublicDiscoveryUITests.setUp
    form=PublicDiscoveryUITests.form

    def test_actual_action_replay_render_is_truthful_without_source_io(self):
        with patch.object(transport,'_send') as send,\
             patch.object(event_scraping,'collect',wraps=event_scraping.collect) as collect:
            data=self.form('event_source',source_id='improv_conspiracy')
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertContains(page,'No shows or sessions are confirmed')
            self.assertEqual(page.context['public_results'][0]['outcome'].status,'unavailable')
            self.assertEqual(DiscoveryState.objects.get().history,[])
            collect.assert_called_once_with('improv_conspiracy',now=NOW);send.assert_not_called()
