"""An unavailable source-policy response must not open fair acquisition."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from django.test import TestCase as DjangoCase,override_settings
from shelf import event_listing_transport as transport,event_scraping
from shelf.models import DiscoveryState
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests
from shelf.tests.test_discovery_transport import NOW


class ArtFairAccessTests(TestCase):
    def test_policy_refusal_is_not_permission_or_a_listing_send(self):
        with TemporaryDirectory() as directory,patch.object(transport,'_RATE_DIRECTORY',directory),\
             patch.object(transport,'_send',return_value=(403,b'Unusable access response')) as send:
            result=transport.observe_robots('affordable_art_fair')
            self.assertFalse(result['allowed']);self.assertEqual(result['status'],403)
            self.assertEqual(send.call_args.args[0][-1],'https://affordableartfair.com/robots.txt')
            with self.assertRaises(transport.ListingPolicyUnavailable):transport.affordable_art_fair()
            send.assert_called_once()
            self.assertEqual(len(list(Path(directory).iterdir())),1)

    def test_closed_descriptor_returns_truthful_outcome_before_io(self):
        with patch.object(transport,'_reserve') as reserve,patch.object(transport,'_send') as send:
            result=event_scraping.collect('affordable_art_fair',now=NOW)
            self.assertEqual(result.status,'permission-required')
            self.assertEqual(result.candidates,())
            self.assertIn('no fair listing was fetched',result.message)
            reserve.assert_not_called();send.assert_not_called()


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class ArtFairAccessUITests(DjangoCase):
    setUp=PublicDiscoveryUITests.setUp
    form=PublicDiscoveryUITests.form

    def test_authenticated_action_replay_and_get_keep_source_closed(self):
        with patch.object(transport,'_send') as send,\
             patch.object(event_scraping,'collect',wraps=event_scraping.collect) as collect:
            data=self.form('event_source',source_id='affordable_art_fair')
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertEqual(page.status_code,200)
            self.assertEqual(page.context['public_results'][0]['outcome'].status,'permission-required')
            self.assertContains(page,'no fair listing was fetched')
            self.assertFalse(next(row for row in page.context['event_sources']
                if row['source_id']=='affordable_art_fair')['implemented'])
            self.assertEqual(DiscoveryState.objects.get().history,[])
            collect.assert_called_once_with('affordable_art_fair',now=NOW)
            send.assert_not_called()
