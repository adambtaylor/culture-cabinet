"""Generic guarded action with fictional 24 Moons programme fragments."""
from unittest.mock import patch
from django.test import TestCase, override_settings
from shelf import discovery_views as views, event_listing_transport as transport
from shelf.models import DiscoveryState
from shelf.discovery_reducer import replay
from shelf.tests.test_moons24_events import NOW, listing
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class Moons24UITests(TestCase):
    setUp=PublicDiscoveryUITests.setUp
    form=PublicDiscoveryUITests.form

    def test_normal_source_action_render_replay_reaction_save_and_undo(self):
        with patch.object(views.timezone,'now',return_value=NOW), \
             patch.object(transport,'moons24',return_value=(200,listing())) as read:
            data=self.form('event_source',source_id='moons24')
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertContains(page,'SYNTHETIC overnight event')
            self.assertContains(page,'24 Moons')
            self.assertContains(page,'not an exact session')
            self.assertContains(page,'18+')
            self.assertEqual(DiscoveryState.objects.get().history,[])
            event=page.context['public_results'][0]['outcome'].candidates[0]
            self.assertEqual(event.starts_at,'2026-11-13T22:00:00+11:00')
            self.client.post(self.action,self.form('respond',key=event.key,choice='interested'))
            self.client.post(self.action,self.form('save',key=event.key))
            self.assertIn(event.key,replay(DiscoveryState.objects.get().history)['saved'])
            self.client.post(self.action,self.form('undo'))
            self.assertNotIn(event.key,replay(DiscoveryState.objects.get().history)['saved'])
            self.assertEqual(replay(DiscoveryState.objects.get().history)['choices'][event.key],'interested')
            read.assert_called_once_with()

    def test_failed_refresh_isolated_and_stale_programme_is_not_empty(self):
        with patch.object(views.timezone,'now',return_value=NOW), \
             patch.object(transport,'moons24',return_value=(200,listing())):
            self.client.post(self.action,self.form('event_source',source_id='moons24'))
        for response in ((200,b'<html>challenge</html>'),(403,b'CANARY')):
            with patch.object(transport,'moons24',return_value=response) as read:
                self.client.post(self.action,self.form('event_source',source_id='moons24'))
                page=self.client.get(self.url)
                outcome=page.context['public_results'][0]['outcome']
                self.assertIn(outcome.status,('invalid','error'))
                self.assertNotEqual(outcome.status,'empty')
                self.assertNotContains(page,'SYNTHETIC overnight event')
                self.assertNotContains(page,'CANARY')
                read.assert_called_once_with()
