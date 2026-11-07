"""Real Django paths, synthetic facts and fixed public origins; no external calls."""
from dataclasses import replace
from datetime import timedelta
import json
from unittest.mock import patch

from django.test import Client, TestCase, SimpleTestCase, override_settings
from shelf import discovery_views as views, discovery_transport as transport, transport_planning as planning
from shelf.models import DiscoveryState, EventPlan
from shelf.tests import test_discovery_public_ui as public, test_discovery_ui as existing
from shelf.tests.test_beat_events import NOW, detail, event, listing

DEMO_OUTPUT = None
VALUES = dict(origin='flinders', arrival='brunswick', companion_origin='', bike='full_size', carry_bike=True,
    outward_mode='metro_train', return_mode='metro_train', outward_minutes=45, companion_minutes=30,
    return_minutes=45, lock_minutes=10, arrival_buffer=20, assumed_show_time='', leave_time='23:00', return_day=0)


class PlanningTests(SimpleTestCase):
    def test_mode_rules_and_stale_guidance_never_imply_readiness(self):
        for bike, mode, carry, expected in [('full_size','metro_train',True,'conditional'), ('factory_ebike','metro_train',True,'conditional'),
                ('full_size','tram',True,'blocked'), ('full_size','train_tram',True,'blocked'), ('full_size','bus',True,'blocked'), ('full_size','replacement_bus',True,'blocked'),
                ('converted_ebike','metro_train',True,'blocked'), ('folded','replacement_bus',True,'conditional'), ('full_size','bus',False,'conditional')]:
            with self.subTest(bike=bike, mode=mode):
                self.assertEqual(planning.bicycle_rule(bike,mode,carry,NOW)['status'], expected)
        for now in (NOW-timedelta(days=1), NOW+timedelta(days=31)):
            self.assertEqual(planning.bicycle_rule('full_size','metro_train',True,now)['status'], 'unverified')
        result=planning.proposal(event(True),VALUES,NOW)
        self.assertFalse(result['ready']); self.assertFalse(result['timed_journey_verified'])

    def test_show_not_doors_used_for_departure_and_separate_origin(self):
        result=planning.proposal(event(True),dict(VALUES,companion_origin='richmond'),NOW)
        self.assertEqual(result['start'].isoformat(), '2026-10-30T20:30:00+11:00')
        self.assertEqual(result['depart'].isoformat(), '2026-10-30T19:15:00+11:00')
        self.assertEqual(result['companion_depart'].isoformat(), '2026-10-30T19:30:00+11:00')
        self.assertEqual(result['return_arrival'].isoformat(), '2026-10-30T23:55:00+11:00')

    def test_unknown_start_requires_explicit_assumption_and_midnight_day(self):
        self.assertIsNone(planning.proposal(event(),VALUES,NOW)['depart'])
        result=planning.proposal(event(),dict(VALUES,assumed_show_time='20:00',leave_time='00:15',return_day=1),NOW)
        self.assertEqual(result['start_basis'],'your planning assumption')
        self.assertEqual(result['leave'].isoformat(),'2026-10-31T00:15:00+11:00')
        with self.assertRaises(ValueError): planning.proposal(event(True),dict(VALUES,leave_time='00:15'),NOW)

    def test_station_comparison_partial_tie_and_parked_bike_return(self):
        values=dict(VALUES,origin='batman',carry_bike=False,return_mode='train_tram',batman_cycle=8,batman_onward=40,coburg_cycle=12,coburg_onward=30)
        result=planning.proposal(event(True),values,NOW)
        comparison=result['station_comparison']
        self.assertEqual(comparison['winners'],['Coburg Station'])
        self.assertFalse(comparison['complete'])
        self.assertEqual(result['parked_bike_station'],'Batman Station')
        self.assertEqual(comparison['rows'][0]['return_station'],'Batman Station')
        self.assertEqual(comparison['rows'][1]['total'],72)
        self.assertFalse(result['blocked'])
        tied=planning.station_comparison(dict(values,merlynston_cycle=7,merlynston_onward=35))
        self.assertEqual(tied['winners'],['Coburg Station','Merlynston Station'])
        self.assertTrue(tied['complete'])

    def test_supported_cycle_links_have_explicit_origins_and_same_station_return(self):
        from urllib.parse import parse_qs, urlsplit
        self.assertEqual(planning.cycle_links(''),[])
        links=planning.cycle_links('Federation Square, Melbourne')
        self.assertEqual(len(links),3)
        for link in links:
            query=parse_qs(urlsplit(link['outward']).query)
            reverse=parse_qs(urlsplit(link['returning']).query)
            self.assertEqual(urlsplit(link['outward']).netloc,'www.google.com')
            self.assertEqual(query['api'],['1'])
            self.assertEqual(query['travelmode'],['bicycling'])
            self.assertEqual(query['origin'],reverse['destination'])
            self.assertEqual(query['destination'],reverse['origin'])

    def test_dst_gap_and_overlap_do_not_make_an_instant(self):
        for date, clock in [('2026-10-04','02:30'),('2027-04-04','02:30')]:
            with self.assertRaises(ValueError):
                planning.proposal(replace(event(),starts_at=date),dict(VALUES,assumed_show_time=clock),NOW)


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class EveningUITests(TestCase):
    form=public.PublicDiscoveryUITests.form

    def setUp(self):
        existing.DiscoveryUITests.setUp(self)
        views.PUBLIC_CACHE.clear(); self.addCleanup(views.PUBLIC_CACHE.clear)
        clock=patch.object(views.timezone,'now',return_value=NOW); clock.start(); self.addCleanup(clock.stop)
        network=patch.object(transport,'_send',side_effect=AssertionError('External I/O forbidden')); network.start(); self.addCleanup(network.stop)
        with patch.object(transport,'beat',return_value=(200,listing())):
            self.assertEqual(self.client.post(self.action,self.form('beat')).status_code,302)
        self.evening='/profiles/1/date-night/?event='+event().key

    def read_detail(self):
        with patch.object(transport,'beat_detail',return_value=(200,detail())) as send:
            data=self.form('beat_detail',key=event().key)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            send.assert_called_once()

    def test_review_demo_source_to_separate_origin_and_blocked_return(self):
        self.read_detail()
        first=self.client.get(self.evening)
        self.assertContains(first,'explicit show 20:30')
        self.assertContains(first,'not an all-in quote')
        response=self.client.post(self.evening,dict(VALUES,companion_origin='richmond'))
        self.assertContains(response,'Fri 30 Oct 19:15')
        self.assertContains(response,'Fri 30 Oct 19:30')
        self.assertContains(response,'Fri 30 Oct 23:55')
        self.assertContains(response,'no timed journey verified')
        self.assertContains(response,'https://transport.vic.gov.au/journey')
        blocked=self.client.post(self.evening,dict(VALUES,return_mode='replacement_bus'))
        self.assertContains(blocked,'constraint blocks this proposal')
        parked=self.client.post(self.evening,dict(VALUES,origin='batman',carry_bike='',outward_mode='train_tram',batman_cycle=8,batman_onward=40,coburg_cycle=12,coburg_onward=30))
        self.assertContains(parked,'return to Batman Station')
        self.assertContains(parked,'Lowest supplied estimate: Coburg Station')
        self.assertEqual(DiscoveryState.objects.get().history,[])
        self.assertFalse(EventPlan.objects.exists())
        if DEMO_OUTPUT:
            from pathlib import Path
            import shutil
            for name,page in [('01-source.html',first),('02-proposed-evening.html',response),('03-blocked-return.html',blocked)]:
                html=page.content.decode()
                # Static review evidence: remove forms and their live CSRF values.
                import re
                html=re.sub(r'<form\b.*?</form>', '<p>[Interactive form omitted from static review]</p>', html, flags=re.S)
                (DEMO_OUTPUT/name).write_text(html)
            static=Path(__file__).resolve().parents[3]/'static'
            shutil.copytree(static,DEMO_OUTPUT/'static',dirs_exist_ok=True)
            (DEMO_OUTPUT/'journey.json').write_text(json.dumps({'synthetic_source':True,'steps':3,'departure_local':'2026-10-30 19:15 Australia/Melbourne','separate_origin':True,'replacement_bus_full_bike_blocked':True,'external_calls':0,'bookings':0,'calendar_writes':0,'messages':0},indent=2))

    def test_date_night_badge_click_is_separate_from_save_and_unknown_is_unlabelled(self):
        self.assertNotContains(self.client.get(self.url), 'class="date-night-badge"')
        self.read_detail()
        page=self.client.get(self.url)
        self.assertContains(page,'class="date-night-badge"')
        self.assertContains(page,'Inferred evening option for two')
        self.assertEqual(self.client.get(self.evening).status_code,200)
        self.assertEqual(DiscoveryState.objects.get().history,[])
        self.client.post(self.action,self.form('save',key=event().key))
        self.assertContains(self.client.get(self.url),'class="date-night-badge"')

    def test_low_rank_source_results_remain_reachable_without_duplicates_or_rejected_cards(self):
        from shelf.tests.test_beat_events import card
        raw=(''.join(card(slug='old-'+str(i),date='Thu 29 Oct 2026') for i in range(30))+
            card()+''.join(card(slug='late-'+str(i),title='Late lead '+str(i)) for i in range(8))).encode()
        with patch.object(transport,'beat',return_value=(200,raw)):
            self.client.post(self.action,self.form('beat'))
        page=self.client.get(self.url)
        visible=[r['card'].key for r in page.context['events']]+[r['card'].key for r in page.context['more_source_events']]
        self.assertEqual(len(visible),len(set(visible)))
        self.assertContains(page,'Synthetic event')
        self.assertContains(page,'More from your source reads')
        self.assertEqual(len([key for key in visible if key.startswith('official:event:')]),9)
        self.assertEqual(self.client.get(self.evening).status_code,200)
        self.read_detail()
        self.assertContains(self.client.get(self.url),'class="date-night-badge"')
        self.client.post(self.action,self.form('respond',key=event().key,choice='not_for_me'))
        page=self.client.get(self.url)
        self.assertFalse(any(r['card'].key==event().key for r in page.context['more_source_events']))
        self.assertFalse(any(r['card'].key==event().key for r in page.context['events']))
        self.assertEqual(self.client.get(self.url,{'category':'gallery'}).context['more_source_events'],[])

    def test_gets_and_proposals_do_not_fetch_or_write_or_extend_freshness(self):
        before=DiscoveryState.objects.get().revision
        with patch.object(transport,'beat') as fetch, patch.object(transport,'beat_detail') as details:
            self.client.get(self.evening); self.client.post(self.evening,VALUES)
            self.assertEqual(DiscoveryState.objects.get().revision,before)
            fetch.assert_not_called(); details.assert_not_called()
            with patch.object(views.timezone,'now',return_value=NOW+timedelta(minutes=15)):
                self.assertEqual(self.client.get(self.evening).status_code,410)
                self.assertEqual(self.client.post(self.evening,VALUES).status_code,410)

    def test_detail_read_does_not_renew_listing_and_failure_clears_old_details(self):
        with patch.object(views.timezone,'now',return_value=NOW+timedelta(minutes=14)):
            self.read_detail()
        with patch.object(views.timezone,'now',return_value=NOW+timedelta(minutes=15)):
            self.assertEqual(self.client.get(self.evening).status_code,410)
        with patch.object(transport,'beat_detail',return_value=(403,b'CANARY')):
            self.client.post(self.action,self.form('beat_detail',key=event().key))
        page=self.client.get(self.evening)
        self.assertNotContains(page,'123 Example Street'); self.assertNotContains(page,'CANARY')

    def test_disruption_preview_exposes_two_independent_windows_without_activation(self):
        self.read_detail()
        response=self.client.post(self.evening,VALUES)
        self.assertContains(response,'No watch has been scheduled')
        self.assertContains(response,'Fri 30 Oct 17:15')
        self.assertContains(response,'Fri 30 Oct 21:00')
        self.assertContains(response,'https://transport.vic.gov.au/disruptions/disruptions-information')
        self.assertFalse(EventPlan.objects.exists())
        self.assertEqual(DiscoveryState.objects.get().history,[])

    def test_cycle_origin_is_transient_private_response_not_saved(self):
        response=self.client.post(self.evening,dict(VALUES,cycle_origin='Federation Square, Melbourne',occasion='other'))
        self.assertContains(response,'https://www.google.com/maps/dir/?api=1')
        self.assertIn('no-store',response.headers['Cache-Control'])
        self.assertEqual(DiscoveryState.objects.get().history,[])
        self.assertNotContains(self.client.get(self.evening),'Federation Square, Melbourne')

    def test_session_grant_csrf_and_unobserved_key(self):
        other=Client(); other.force_login(self.actor)
        session=other.session; session['selected_profile_id']=1; session.save()
        self.assertEqual(other.get(self.evening).status_code,410)
        self.assertEqual(self.client.get(self.evening+'x').status_code,410)
        self.assertEqual(self.client.get(self.evening+'&event=x').status_code,404)
        self.assertEqual(Client().get(self.evening).status_code,302)
        csrf=Client(enforce_csrf_checks=True); csrf.force_login(self.actor)
        self.assertEqual(csrf.post(self.evening,VALUES).status_code,403)
        self.grant.can_select=False; self.grant.save()
        self.assertEqual(self.client.get(self.evening).status_code,404)

    def test_bounded_forms_no_arbitrary_location_and_html_escaping(self):
        for extra in ({'origin':['flinders','richmond']},{'address':'PRIVATE ADDRESS'},{'padding':'x'*5000}):
            self.assertEqual(self.client.post(self.evening,dict(VALUES,**extra)).status_code,400)
        page=self.client.post(self.evening,dict(VALUES,origin='PRIVATE ADDRESS'))
        self.assertIsNone(page.context['proposal'])
        self.assertContains(page,'Select a valid choice')
        with patch.object(transport,'beat_detail',return_value=(200,detail(address='&lt;img src=x onerror=alert(1)&gt;'))):
            self.client.post(self.action,self.form('beat_detail',key=event().key))
        self.assertContains(self.client.get(self.evening),'&lt;img src=x onerror=alert(1)&gt;')
        self.assertNotContains(self.client.get(self.evening),'<img src=x onerror=alert(1)>')

    def test_detail_unobserved_and_revoked_result_discarded(self):
        with patch.object(transport,'beat_detail') as send:
            self.assertEqual(self.client.post(self.action,self.form('beat_detail',key='official:event:unknown')).status_code,409)
            send.assert_not_called()
        def revoked(url):
            self.grant.can_select=False; self.grant.save()
            return 200,detail()
        data=self.form('beat_detail',key=event().key)
        with patch.object(transport,'beat_detail',side_effect=revoked):
            self.assertEqual(self.client.post(self.action,data).status_code,404)
        cached=views.PUBLIC_CACHE.get(f'{self.actor.pk}:1:{self.client.session.session_key}')
        self.assertIsNone(cached[('beat','','')]['outcome'].candidates[0].details)
