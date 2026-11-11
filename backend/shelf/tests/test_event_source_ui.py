"""The generic event source action uses real authority/cache/history handlers."""
from unittest.mock import patch
from shelf.tests.test_event_saved_references import policies
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


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class SavedEventReferenceUITests(TestCase):
    setUp = existing.PublicDiscoveryUITests.setUp
    form = existing.PublicDiscoveryUITests.form

    def test_same_session_saved_reference_leaves_retained_recommendation_slots(self):
        descriptors=policies()
        with patch('shelf.event_source_registry.SOURCES',descriptors), \
             patch.object(views,'EVENT_SOURCES',descriptors), \
             patch.object(views.sources,'household_candidates',return_value=[]), \
             patch.object(transport,'pbs',return_value=(200,listing())) as read:
            self.client.post(self.action,self.form('event_source',source_id='pbs'))
            page=self.client.get(self.url)
            key=page.context['events'][0]['card'].key
            self.client.post(self.action,self.form('save',key=key))
            # Follow the real redirect before cache loss: this same session now
            # has a current unjudged recommendation deck containing the key.
            self.assertIn(key,[row['card'].key for row in self.client.get(self.url).context['events']])
            before=DiscoveryState.objects.get().history
            views.PUBLIC_CACHE.clear()
            page=self.client.get(self.url)
            saved=next(row for row in page.context['responses'] if row['card'].key==key)
            self.assertTrue(saved['saved'] and saved['reference_only'])
            self.assertIsNone(saved['card'].url)
            self.assertIn('/news/',saved['saved_reference']['official_url'])
            for field in ('events','more_source_events'):
                self.assertNotIn(key,[row['card'].key for row in page.context[field]])
            self.assertEqual(DiscoveryState.objects.get().history,before)
            read.assert_called_once_with()

    def test_another_response_drops_stale_selected_and_overflow_pins(self):
        from shelf.tests.test_pbs_events import card
        from shelf.discovery_sources import Candidate
        descriptors=policies()
        seeds=[Candidate('official:artist:synthetic-'+str(i),'SYNTHETIC seed',
            'artist','Synthetic seed','manual-attributed',i+1,None) for i in range(12)]
        raw=listing([card('synthetic-event-'+str(i)) for i in range(9)])
        with patch('shelf.event_source_registry.SOURCES',descriptors), \
             patch.object(views,'EVENT_SOURCES',descriptors), \
             patch.object(views.sources,'household_candidates',return_value=seeds), \
             patch.object(transport,'pbs',return_value=(200,raw)) as read:
            self.client.post(self.action,self.form('event_source',source_id='pbs'))
            page=self.client.get(self.url)
            first=page.context['events'][0]['card'].key
            overflow=page.context['more_source_events'][0]['card'].key
            for key in (first,overflow):
                self.client.post(self.action,self.form('save',key=key))
            page=self.client.get(self.url)
            other=page.context['music'][0]['card'].key
            data=self.form('respond',key=other,choice='interested')
            views.PUBLIC_CACHE.clear()
            # Respond without an intervening GET, exercising _replace_key's
            # preserved slots independently of _context's retained-deck filter.
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            decks=self.client.session[views.DECK_KEY]
            deck=next(iter(decks.values()))
            self.assertTrue({first,overflow}.isdisjoint(deck['keys']+deck.get('source_keys',[])))
            page=self.client.get(self.url)
            saved={row['card'].key:row for row in page.context['responses'] if row['saved']}
            self.assertEqual(set(saved),{first,overflow})
            for row in saved.values():
                self.assertTrue(row['reference_only'])
                self.assertIsNone(row['card'].url)
            for field in ('events','more_source_events'):
                self.assertTrue({first,overflow}.isdisjoint(row['card'].key for row in page.context[field]))
            read.assert_called_once_with()

    def test_save_only_link_survives_cache_loss_fresh_session_without_new_taste(self):
        descriptors=policies()
        with patch('shelf.event_source_registry.SOURCES',descriptors), \
             patch.object(views,'EVENT_SOURCES',descriptors), \
             patch.object(transport,'pbs',return_value=(200,listing())) as read:
            self.client.post(self.action,self.form('event_source',source_id='pbs'))
            key=self.client.get(self.url).context['public_results'][0]['outcome'].candidates[0].key
            data=self.form('save',key=key)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            stored=DiscoveryState.objects.get()
            self.assertEqual(len(stored.history),1)
            event=stored.history[0]
            self.assertNotIn('card',event)
            from shelf.profile_snapshot import _history
            # The real taste-history consumer must keep save-only references
            # out of preference evidence, even though they are durable JSON.
            self.assertEqual(_history(None,stored),[])
            reference=event['saved_reference']
            self.assertIn('/news/',reference['official_url'])
            views.PUBLIC_CACHE.clear()
            fresh=Client();fresh.force_login(self.actor)
            session=fresh.session;session['selected_profile_id']=1;session.save()
            page=fresh.get(self.url)
            row=next(row for row in page.context['responses'] if row['card'].key==key)
            self.assertTrue(row['saved'])
            self.assertTrue(row['reference_only'])
            self.assertIsNone(row['card'].url)
            self.assertEqual(row['saved_reference'],reference)
            self.assertIn('Stale',row['freshness'])
            self.assertNotIn(key,[r['card'].key for r in page.context['events']])
            self.assertEqual(DiscoveryState.objects.get().history,[event])
            read.assert_called_once_with()
            # A stale card cannot refill a withdrawn/missing bookmark on Save.
            extra=dict(mode='household',revision=page.context['revision'],nonce=page.context['nonce'],
                       operation='save',key=key,choice='',radio_mode='')
            self.assertEqual(fresh.post(self.action,extra).status_code,302)
            self.assertNotIn('saved_reference',DiscoveryState.objects.get().history[-1])
            page=fresh.get(self.url)
            undo=dict(extra,operation='undo',key='',revision=page.context['revision'],nonce=page.context['nonce'])
            self.assertEqual(fresh.post(self.action,undo).status_code,302)
            self.assertIn(key,replay(DiscoveryState.objects.get().history)['saved'])
            read.assert_called_once_with()

    def test_post_cannot_forge_reference_metadata(self):
        with patch.object(transport,'pbs') as read:
            for field in ('saved_reference','official_url','occurrence_ref','occurrence_basis',
                          'retention_policy_ref','provenance'):
                data=self.form('save',key='official:event:fake',**{field:'FORGED'})
                self.assertEqual(self.client.post(self.action,data).status_code,400)
            self.assertFalse(DiscoveryState.objects.exists())
            read.assert_not_called()

    def test_two_sessions_unsave_undo_restart_remain_distinct_after_cache_loss(self):
        from shelf.tests.test_venue303_events import listing as venue_listing, card
        from shelf import event_listing_transport
        descriptors=policies()
        raw=venue_listing(card(start='2026-11-12',clock='7:30 PM'),
                          card(start='2026-11-12',clock='9:30 PM'))
        with patch('shelf.event_source_registry.SOURCES',descriptors), \
             patch.object(views,'EVENT_SOURCES',descriptors), \
             patch.object(event_listing_transport,'venue303',return_value=(200,raw)) as read:
            self.client.post(self.action,self.form('event_source',source_id='venue303'))
            cards=self.client.get(self.url).context['public_results'][0]['outcome'].candidates
            first,second=cards
            for event in cards:
                self.assertEqual(self.client.post(self.action,self.form('save',key=event.key)).status_code,302)
            self.client.post(self.action,self.form('respond',key=first.key,choice='interested'))
            views.PUBLIC_CACHE.clear()
            page=self.client.get(self.url)
            rows={row['card'].key:row for row in page.context['responses']}
            self.assertEqual(set(rows),{first.key,second.key})
            self.assertEqual(rows[first.key]['saved_reference']['official_url'],
                             rows[second.key]['saved_reference']['official_url'])
            self.assertNotEqual(rows[first.key]['saved_reference']['occurrence_ref'],
                                rows[second.key]['saved_reference']['occurrence_ref'])
            self.client.post(self.action,self.form('unsave',key=first.key))
            self.assertEqual(replay(DiscoveryState.objects.get().history)['saved'],[second.key])
            self.client.post(self.action,self.form('undo'))
            self.assertEqual(set(replay(DiscoveryState.objects.get().history)['saved']),{first.key,second.key})
            self.client.post(self.action,self.form('restart'))
            self.assertEqual(replay(DiscoveryState.objects.get().history)['saved'],[])
            self.client.post(self.action,self.form('undo'))
            self.assertEqual(set(replay(DiscoveryState.objects.get().history)['saved']),{first.key,second.key})
            for event in DiscoveryState.objects.get().history[3:]:
                self.assertNotIn('saved_reference',event)
            read.assert_called_once_with()

    def test_current_restriction_hides_reference_without_purging_on_get(self):
        from contextlib import contextmanager
        descriptors=policies()
        with patch('shelf.event_source_registry.SOURCES',descriptors), \
             patch.object(views,'EVENT_SOURCES',descriptors), \
             patch.object(transport,'pbs',return_value=(200,listing())):
            self.client.post(self.action,self.form('event_source',source_id='pbs'))
            key=self.client.get(self.url).context['public_results'][0]['outcome'].candidates[0].key
            self.client.post(self.action,self.form('save',key=key))
            before=DiscoveryState.objects.get().history
            views.PUBLIC_CACHE.clear()
            @contextmanager
            def denied(*args):
                yield frozenset({'event-source:pbs'})
            # Actual restriction transaction/replay is Taste's separate tail.
            with patch.object(views.storage,'reference_window',denied):
                page=self.client.get(self.url)
            row=next(row for row in page.context['responses'] if row['card'].key==key)
            self.assertTrue(row['saved'])
            self.assertIsNone(row['saved_reference'])
            self.assertIsNone(row['card'].url)
            self.assertEqual(DiscoveryState.objects.get().history,before)
