"""The generic event source action uses real authority/cache/history handlers."""
from unittest.mock import patch
from django.test import Client, TestCase, override_settings
from shelf import discovery_transport as transport, discovery_views as views, event_scraping
from shelf.discovery_reducer import replay
from shelf.models import DiscoveryState
from shelf.tests import test_discovery_public_ui as existing
from shelf.tests.test_pbs_events import listing
from shelf.tests.test_rrr_events import listing as rrr_listing
from shelf.tests.test_discovery_transport import NOW


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class EventSourceUITests(TestCase):
    setUp = existing.PublicDiscoveryUITests.setUp
    form = existing.PublicDiscoveryUITests.form

    def source_form(self, source='pbs', **extra):
        return self.form('event_source', source_id=source, **extra)

    def test_pbs_normal_action_consumes_nonce_once_and_gets_do_not_acquire(self):
        with patch.object(transport,'pbs',return_value=(200,listing())) as read, \
             patch.object(event_scraping,'collect',wraps=event_scraping.collect) as collect:
            page=self.client.get(self.url)
            self.assertEqual(len(page.context['event_sources']),35)
            data=self.source_form()
            self.client.get('/recommendations/')
            self.assertEqual(self.client.get(self.action).status_code,405)
            read.assert_not_called()
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertContains(page,'SYNTHETIC music event')
            self.assertContains(page,'PBS')
            self.assertContains(page,'ticket availability unverified')
            collect.assert_called_once_with('pbs',now=NOW)
            read.assert_called_once_with()
        self.assertEqual(DiscoveryState.objects.get().history,[])

    def test_source_id_is_closed_binds_nonce_and_other_operations_reject_it(self):
        data=self.source_form()
        with patch.object(transport,'pbs',return_value=(200,listing())) as read:
            for change in ({'source_id':'https://evil.invalid'}, {'source_id':''},
                           {'source_id':['pbs','rrr']}, {'key':'external'}, {'radio_mode':'medium'},
                           {'mode':'sample'}, {'choice':'interested'}):
                self.assertEqual(self.client.post(self.action,dict(data,**change)).status_code,400)
            self.assertEqual(self.client.post(self.action,dict(data,operation='pbs')).status_code,400)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,dict(data,source_id='rrr')).status_code,409)
            read.assert_called_once_with()

    def test_generic_and_legacy_failure_replace_same_source_but_keep_another(self):
        with patch.object(transport,'rrr',return_value=(200,rrr_listing())):
            self.client.post(self.action,self.form('rrr'))
        for success, failure in ((self.source_form, lambda:self.form('pbs')),
                                 (lambda:self.form('pbs'), self.source_form)):
            with patch.object(transport,'pbs',return_value=(200,listing())):
                self.client.post(self.action,success())
            with patch.object(transport,'pbs',side_effect=TimeoutError('CANARY')):
                self.client.post(self.action,failure())
            page=self.client.get(self.url)
            self.assertNotContains(page,'SYNTHETIC music event')
            self.assertContains(page,'SYNTHETIC event')
            self.assertNotContains(page,'CANARY')
            self.assertContains(page,'timeout')
            self.assertEqual(len(page.context['public_results']),2)

    def test_current_profile_csrf_and_revocation_still_guard_generic_acquisition(self):
        data=self.source_form()
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.actor)
        with patch.object(transport,'pbs') as read:
            self.assertEqual(csrf.post(self.action,data).status_code,403)
            self.assertEqual(Client().post(self.action,data).status_code,302)
            self.assertEqual(self.client.post('/profiles/2/discovery/action/',data).status_code,404)
            read.assert_not_called()
        def revoked():
            self.grant.can_select=False;self.grant.save()
            return 200, listing()
        with patch.object(transport,'pbs',side_effect=revoked) as read:
            self.assertEqual(self.client.post(self.action,data).status_code,404)
            read.assert_called_once_with()
        self.assertEqual(views.PUBLIC_CACHE._cache,{})

    def test_reaction_save_undo_and_identity_survive_fresh_session_without_io(self):
        with patch.object(transport,'pbs',return_value=(200,listing())):
            self.client.post(self.action,self.source_form())
        key=self.client.get(self.url).context['public_results'][0]['outcome'].candidates[0].key
        self.client.post(self.action,self.form('respond',key=key,choice='interested'))
        self.client.post(self.action,self.form('save',key=key))
        self.assertIn(key,replay(DiscoveryState.objects.get().history)['saved'])
        views.PUBLIC_CACHE.clear()
        fresh=Client();fresh.force_login(self.actor)
        session=fresh.session;session['selected_profile_id']=1;session.save()
        with patch.object(transport,'pbs') as read:
            page=fresh.get(self.url)
            rows=page.context['responses']
            self.assertTrue(any(row['card'].key==key and row['saved'] for row in rows))
            self.assertContains(page,'Previously judged identity')
            data=dict(mode='household',revision=page.context['revision'],nonce=page.context['nonce'],
                      operation='undo',key='',choice='',radio_mode='')
            self.assertEqual(fresh.post(self.action,data).status_code,302)
            self.assertNotIn(key,replay(DiscoveryState.objects.get().history)['saved'])
            self.assertEqual(replay(DiscoveryState.objects.get().history)['choices'][key],'interested')
            read.assert_not_called()
        # Exact source URL restoration is a separate Architecture persistence
        # decision; an opaque saved identity alone is not goal completion.
