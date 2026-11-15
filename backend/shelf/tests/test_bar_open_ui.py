"""Actual guarded action/replay around a fictional recognized source shell."""
from unittest.mock import patch
from django.test import TestCase,override_settings
from shelf import event_listing_transport as transport,discovery_views as views
from shelf.models import DiscoveryState
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests
from shelf.tests.test_bar_open_events import listing


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class BarOpenUITests(TestCase):
    setUp=PublicDiscoveryUITests.setUp
    form=PublicDiscoveryUITests.form

    def test_shell_outcome_normal_handler_replay_and_get_write_no_taste_or_inventory(self):
        with patch.object(transport,'bar_open',return_value=(200,listing())) as read:
            data=self.form('event_source',source_id='bar_open')
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertContains(page,'recurrence is not a confirmed next event')
            outcome=page.context['public_results'][0]['outcome']
            self.assertEqual(outcome.status,'unavailable')
            self.assertEqual(outcome.candidates,())
            self.assertEqual(DiscoveryState.objects.get().history,[])
            self.client.get(self.url)
            read.assert_called_once_with()
