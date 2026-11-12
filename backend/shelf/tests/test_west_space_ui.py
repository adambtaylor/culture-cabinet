"""Normal authenticated source actions, fictional programme fields only."""
from unittest.mock import patch
from django.test import Client,TestCase,override_settings
from shelf import discovery_views as views,event_listing_transport as transport
from shelf.models import DiscoveryState
from shelf.discovery_reducer import replay
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests
from shelf.tests.test_west_space_events import NOW,card,listing


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class WestSpaceUITests(TestCase):
    setUp=PublicDiscoveryUITests.setUp
    form=PublicDiscoveryUITests.form

    def test_actual_action_run_render_replay_save_response_undo_and_fresh_session(self):
        with patch.object(views.timezone,'now',return_value=NOW),\
             patch.object(transport,'west_space',return_value=(200,listing())) as read:
            data=self.form('event_source',source_id='west_space')
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertContains(page,'FICTIONAL programme')
            self.assertContains(page,'Current programme run')
            self.assertContains(page,'opening, hours and individual sessions unverified')
            key=page.context['public_results'][0]['outcome'].candidates[0].key
            self.client.post(self.action,self.form('save',key=key))
            self.client.post(self.action,self.form('respond',key=key,choice='intrigued'))
            self.client.post(self.action,self.form('undo'))
            state=DiscoveryState.objects.get()
            self.assertIn(key,replay(state.history)['saved'])
            self.assertNotIn(key,replay(state.history)['choices'])
            views.PUBLIC_CACHE.clear()
            fresh=Client();fresh.force_login(self.actor)
            session=fresh.session;session['selected_profile_id']=1;session.save()
            row=next(row for row in fresh.get(self.url).context['responses'] if row['card'].key==key)
            self.assertTrue(row['saved']);self.assertIsNone(row['card'].url)
            read.assert_called_once_with()

    def test_unrecognized_page_and_recognized_old_runs_are_distinct(self):
        for raw,status in ((b'<html>challenge</html>','invalid'),
            (listing(card(dates='1 Jan → 1 Feb 2026')),'unavailable')):
            with patch.object(transport,'west_space',return_value=(200,raw)) as read:
                self.client.post(self.action,self.form('event_source',source_id='west_space'))
                page=self.client.get(self.url)
                self.assertEqual(page.context['public_results'][0]['outcome'].status,status)
                self.assertNotContains(page,'FICTIONAL programme')
                read.assert_called_once_with()
        self.assertEqual(DiscoveryState.objects.get().history,[])
