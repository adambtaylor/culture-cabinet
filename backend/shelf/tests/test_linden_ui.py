"""Guarded handler integration with fictional Linden cards only."""
from unittest.mock import patch
from django.test import Client,TestCase,override_settings
from shelf import discovery_views as views,event_listing_transport as transport
from shelf.models import DiscoveryState
from shelf.discovery_reducer import replay
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests
from shelf.tests.test_linden_events import NOW,listing,card


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class LindenUITests(TestCase):
    setUp=PublicDiscoveryUITests.setUp
    form=PublicDiscoveryUITests.form

    def test_normal_action_render_replay_response_save_undo_and_fresh_identity(self):
        with patch.object(views.timezone,'now',return_value=NOW),\
             patch.object(transport,'linden',return_value=(200,listing())) as read:
            data=self.form('event_source',source_id='linden')
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertContains(page,'FICTIONAL workshop');self.assertContains(page,'Ages 2+')
            event=page.context['public_results'][0]['outcome'].candidates[0]
            self.client.post(self.action,self.form('save',key=event.key))
            self.client.post(self.action,self.form('respond',key=event.key,choice='intrigued'))
            self.client.post(self.action,self.form('undo'))
            views.PUBLIC_CACHE.clear()
            fresh=Client();fresh.force_login(self.actor)
            session=fresh.session;session['selected_profile_id']=1;session.save()
            row=next(row for row in fresh.get(self.url).context['responses'] if row['card'].key==event.key)
            self.assertTrue(row['saved']);self.assertIsNone(row['card'].url)
            self.assertNotIn(event.key,replay(DiscoveryState.objects.get().history)['choices'])
            read.assert_called_once_with()

    def test_current_authority_is_rechecked_after_acquisition(self):
        def revoke():
            self.grant.can_select=False;self.grant.save();return 200,listing()
        with patch.object(transport,'linden',side_effect=revoke):
            self.assertEqual(self.client.post(self.action,
                self.form('event_source',source_id='linden')).status_code,404)
        self.assertEqual(views.PUBLIC_CACHE._cache,{})

