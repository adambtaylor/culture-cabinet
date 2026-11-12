"""Normal guarded My Aeon source action: current versus archive fixture states."""
from unittest.mock import patch
from django.test import TestCase,override_settings
from shelf import event_listing_transport as transport,discovery_views as views
from shelf.models import DiscoveryState
from shelf.tests.test_my_aeon_events import NOW,listing,card
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class MyAeonUITests(TestCase):
    setUp=PublicDiscoveryUITests.setUp
    form=PublicDiscoveryUITests.form

    def test_current_card_then_archive_refresh_normal_action_replay_no_taste(self):
        with patch.object(views.timezone,'now',return_value=NOW), \
             patch.object(transport,'my_aeon',return_value=(200,listing())) as read:
            data=self.form('event_source',source_id='my_aeon')
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertContains(page,'SYNTHETIC event')
            self.assertContains(page,'2026-11-13')
            self.assertEqual(page.context['public_results'][0]['outcome'].status,'ready')
            read.assert_called_once_with()
        with patch.object(transport,'my_aeon',return_value=(200,listing([card(when='January 17, 2026')]))):
            self.client.post(self.action,self.form('event_source',source_id='my_aeon'))
            page=self.client.get(self.url)
            self.assertEqual(page.context['public_results'][0]['outcome'].status,'unavailable')
            self.assertContains(page,'Old years are not rolled forward')
            self.assertNotContains(page,'SYNTHETIC event')
        self.assertEqual(DiscoveryState.objects.get().history,[])
