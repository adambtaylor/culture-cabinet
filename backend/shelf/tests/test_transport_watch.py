"""Adapter contract tests: no network, worker registration or real delivery."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import Mock

from shelf import transport_watch as w

NOW=datetime(2026,11,20,7,tzinfo=timezone.utc)
OUT=w.Leg('outward',NOW+timedelta(hours=2),NOW+timedelta(hours=3),('ptv:route:15',),('ptv:stop:1014',),('ptv:trip:1',),True)
BACK=w.Leg('return',NOW+timedelta(hours=6),NOW+timedelta(hours=7),('ptv:route:15',),('ptv:stop:1014',),('ptv:trip:2',),True)
JOURNEY=w.Journey('synthetic-journey',1,'synthetic-owner','saved',(OUT,BACK))
NOTICE=w.Notice('notice-1','replacement_bus',True,NOW,NOW+timedelta(hours=8),('ptv:route:15',),(),(),
    'Synthetic replacement buses','Train service is replaced.','','https://transport.vic.gov.au/disruptions/disruptions-information')


def feed(now=NOW,notices=(NOTICE,),complete=True):return w.Feed(now,complete,notices)


class WatchTests(TestCase):
    def test_connection_gates_and_independent_exact_windows(self):
        scheduler=Mock(connected=True)
        self.assertEqual(w.schedule(JOURNEY,NOW,scheduler=scheduler)['state'],'blocked')
        scheduler.replace.assert_not_called()
        result=w.schedule(JOURNEY,NOW,feed_connected=True,delivery_connected=True,scheduler=scheduler)
        self.assertEqual(len(result['jobs']),2)
        self.assertEqual(result['jobs'][0]['start'],NOW.isoformat())
        self.assertEqual(result['jobs'][1]['start'],(NOW+timedelta(hours=4)).isoformat())
        self.assertEqual(result['jobs'][0]['interval_seconds'],300)
        scheduler.replace.assert_called_once_with(JOURNEY.id,1,result['jobs'])

    def test_late_saved_immediate_and_expired_legs_omitted(self):
        result=w.schedule(JOURNEY,NOW+timedelta(hours=1),feed_connected=True,delivery_connected=True,scheduler=Mock(connected=True))
        self.assertEqual(result['jobs'][0]['start'],(NOW+timedelta(hours=1)).isoformat())
        later=w.schedule(JOURNEY,NOW+timedelta(hours=3),feed_connected=True,delivery_connected=True,scheduler=Mock(connected=True))
        self.assertEqual([j['leg'] for j in later['jobs']],['return'])

    def test_draft_cancel_and_reschedule_replace_only_owned_manifest(self):
        scheduler=Mock(connected=True)
        for status in ('draft','cancelled'):
            result=w.schedule(replace(JOURNEY,status=status),NOW,feed_connected=True,delivery_connected=True,scheduler=scheduler)
            self.assertEqual(result['jobs'],[])
        shifted=replace(JOURNEY,revision=2,legs=(replace(OUT,departure=OUT.departure+timedelta(minutes=10)),BACK))
        w.schedule(shifted,NOW,feed_connected=True,delivery_connected=True,scheduler=scheduler)
        self.assertEqual(scheduler.replace.call_args.args[:2],(JOURNEY.id,2))

    def test_before_window_and_at_departure_are_quiet_return_is_independent(self):
        self.assertEqual(w.evaluate(JOURNEY,{},NOW-timedelta(seconds=1),feed())['receipts'],{})
        state=w.evaluate(JOURNEY,{},OUT.departure,feed(OUT.departure))
        self.assertEqual(state['receipts'],{})
        returned=w.evaluate(JOURNEY,{},NOW+timedelta(hours=4),feed(NOW+timedelta(hours=4)))
        self.assertEqual([x['payload']['leg'] for x in returned['receipts'].values()],['return'])

    def test_first_check_and_material_change_dedup_ignores_retrieval_time(self):
        state=w.evaluate(JOURNEY,{},NOW,feed())
        self.assertEqual(len(state['receipts']),1)
        again=w.evaluate(JOURNEY,state,NOW+timedelta(minutes=5),feed(NOW+timedelta(minutes=5)))
        self.assertEqual(len(again['receipts']),1)
        changed=w.evaluate(JOURNEY,again,NOW+timedelta(minutes=10),feed(NOW+timedelta(minutes=10),(replace(NOTICE,consequence='Station also closed.'),)))
        self.assertEqual(len(changed['receipts']),2)
        self.assertEqual({r['payload']['planned'] for r in changed['receipts'].values()},{True})

    def test_interval_bounds_no_unchanged_alert_spam(self):
        state=w.evaluate(JOURNEY,{},NOW,feed())
        result=w.evaluate(JOURNEY,state,NOW+timedelta(minutes=4),feed(NOW+timedelta(minutes=4),(replace(NOTICE,title='Change'),)))
        self.assertEqual(result,state)

    def test_recurring_material_condition_is_a_new_occurrence_not_a_resend(self):
        state={};keys=[]
        adapter=Mock(connected=True)
        adapter.send.return_value={'state':'sent','evidence':'synthetic-receipt'}
        for minute,notice in ((0,NOTICE),(5,replace(NOTICE,title='Changed impact')),(10,NOTICE)):
            now=NOW+timedelta(minutes=minute)
            state=w.evaluate(JOURNEY,state,now,feed(now,(notice,)))
            pending=[key for key,r in state['receipts'].items() if r['state']=='pending']
            self.assertEqual(len(pending),1)
            keys.extend(pending)
            state=w.deliver(state,pending[0],current_journey=JOURNEY,now=now,adapter=adapter,persist=Mock())
        self.assertEqual(len(set(keys)),3)
        self.assertEqual(adapter.send.call_count,3)
        replay=w.evaluate(JOURNEY,state,NOW+timedelta(minutes=15),feed(NOW+timedelta(minutes=15)))
        self.assertEqual(len(replay['receipts']),3)
        self.assertFalse(any(r['state']=='pending' for r in replay['receipts'].values()))

    def test_withdrawn_or_superseded_pending_notice_cannot_send(self):
        for next_notices in ((),(replace(NOTICE,title='New impact'),)):
            state=w.evaluate(JOURNEY,{},NOW,feed());old_key=next(iter(state['receipts']))
            later=NOW+timedelta(minutes=5)
            state=w.evaluate(JOURNEY,state,later,feed(later,next_notices))
            self.assertEqual(state['receipts'][old_key]['state'],'superseded')
            adapter=Mock(connected=True);persist=Mock()
            w.deliver(state,old_key,current_journey=JOURNEY,now=later,adapter=adapter,persist=persist)
            adapter.send.assert_not_called();persist.assert_not_called()
            self.assertEqual(len(state['active']),len(next_notices))

    def test_reappearance_after_withdrawal_and_feed_recovery_gets_new_identity(self):
        for first_feed in (feed(),feed(complete=False)):
            state=w.evaluate(JOURNEY,{},NOW,first_feed);original=next(iter(state['receipts']))
            later=NOW+timedelta(minutes=5)
            state=w.evaluate(JOURNEY,state,later,feed(later,()))
            self.assertEqual(state['active'],{})
            latest=NOW+timedelta(minutes=10)
            state=w.evaluate(JOURNEY,state,latest,replace(first_feed,observed_at=latest))
            self.assertEqual(len(state['receipts']),2)
            self.assertNotIn(original,state['active'].values())
            self.assertEqual(state['receipts'][original]['state'],'superseded')

    def test_unknown_delivery_is_retained_after_withdrawal_and_lookup_only(self):
        state=w.evaluate(JOURNEY,{},NOW,feed());key=next(iter(state['receipts']))
        adapter=Mock(connected=True);adapter.send.side_effect=TimeoutError
        state=w.deliver(state,key,current_journey=JOURNEY,now=NOW,adapter=adapter,persist=Mock())
        later=NOW+timedelta(minutes=5)
        state=w.evaluate(JOURNEY,state,later,feed(later,()))
        self.assertEqual(state['receipts'][key]['state'],'unknown')
        w.deliver(state,key,current_journey=JOURNEY,now=later,adapter=adapter,persist=Mock())
        adapter.lookup.return_value={'state':'sent','evidence':'late-synthetic-proof'}
        state=w.reconcile(state,key,current_journey=JOURNEY,adapter=adapter,persist=Mock())
        adapter.send.assert_called_once();adapter.lookup.assert_called_once_with(key=key)
        self.assertEqual(state['receipts'][key]['state'],'sent')

    def test_delayed_delivery_requires_fresh_evaluation_and_refreshes_pending_evidence(self):
        state=w.evaluate(JOURNEY,{},NOW,feed());key=next(iter(state['receipts']))
        later=NOW+timedelta(minutes=6);adapter=Mock(connected=True);persist=Mock()
        w.deliver(state,key,current_journey=JOURNEY,now=later,adapter=adapter,persist=persist)
        adapter.send.assert_not_called();persist.assert_not_called()
        state=w.evaluate(JOURNEY,state,later,feed(later))
        self.assertEqual(len(state['receipts']),1)
        self.assertEqual(state['receipts'][key]['payload']['observed_at'],later.isoformat())
        adapter.send.return_value={'state':'sent','evidence':'fresh-synthetic-proof'}
        w.deliver(state,key,current_journey=JOURNEY,now=later,adapter=adapter,persist=persist)
        adapter.send.assert_called_once()

    def test_relevance_routes_stops_trips_and_effective_time(self):
        for notice in (replace(NOTICE,route_ids=('other',)),replace(NOTICE,end=OUT.departure),
                replace(NOTICE,start=OUT.arrival),replace(NOTICE,trip_ids=('ptv:trip:other',))):
            self.assertFalse(notice.relevant(OUT))
        for notice in (NOTICE,replace(NOTICE,route_ids=(),stop_ids=('ptv:stop:1014',)),
                replace(NOTICE,route_ids=(),trip_ids=('ptv:trip:1',)),replace(NOTICE,route_ids=(),network_wide=True)):
            self.assertTrue(notice.relevant(OUT))

    def test_all_supported_consequences_and_bike_replacement_guidance(self):
        for kind in w.KINDS:
            state=w.evaluate(JOURNEY,{},NOW,feed(notices=(replace(NOTICE,kind=kind,planned=False),)))
            receipt=next(iter(state['receipts'].values()))
            self.assertEqual(receipt['payload']['kind'],kind)
            self.assertFalse(receipt['payload']['planned'])
            if kind=='replacement_bus':
                self.assertIn('full-size bike cannot',receipt['payload']['consequence'])
                self.assertIn('return to that station',receipt['payload']['alternative'])

    def test_unavailable_stale_future_and_incomplete_are_not_all_clear(self):
        for current in (feed(complete=False),feed(NOW-timedelta(minutes=6)),feed(NOW+timedelta(seconds=1))):
            state=w.evaluate(JOURNEY,{},NOW,current)
            self.assertEqual(next(iter(state['receipts'].values()))['payload']['kind'],'unavailable')
            again=w.evaluate(JOURNEY,state,NOW+timedelta(minutes=5),feed(complete=False))
            self.assertEqual(len(again['receipts']),1)
        self.assertEqual(w.evaluate(JOURNEY,{},NOW,feed(notices=()))['receipts'],{})

    def test_invalid_time_source_duplicate_and_binding_rejected(self):
        for bad in (replace(NOTICE,source_url='https://evil.invalid/'),replace(NOTICE,source_url='https://transport.vic.gov.au/a?key=x'),replace(NOTICE,title='x'*601)):
            with self.assertRaises(ValueError):w.evaluate(JOURNEY,{},NOW,feed(notices=(bad,)))
        with self.assertRaises(ValueError):w.evaluate(JOURNEY,{},NOW,feed(notices=(NOTICE,NOTICE)))
        with self.assertRaises(ValueError):w.evaluate(JOURNEY,{},NOW.replace(tzinfo=None),feed())
        state=w.evaluate(JOURNEY,{},NOW,feed())
        for changed in (replace(JOURNEY,revision=2),replace(JOURNEY,owner_id='another')):
            with self.assertRaises(ValueError):w.evaluate(changed,state,NOW,feed())

    def test_single_dispatch_persists_unknown_before_send_and_reconciles_only(self):
        state=w.evaluate(JOURNEY,{},NOW,feed());key=next(iter(state['receipts']))
        saved=[]
        def send(**kwargs):
            self.assertEqual(saved[-1]['receipts'][key]['state'],'unknown')
            raise TimeoutError
        adapter=Mock(connected=True);adapter.send.side_effect=send
        unknown=w.deliver(state,key,current_journey=JOURNEY,now=NOW,adapter=adapter,persist=saved.append)
        self.assertEqual(unknown['receipts'][key]['state'],'unknown')
        w.deliver(unknown,key,current_journey=JOURNEY,now=NOW,adapter=adapter,persist=saved.append)
        adapter.send.assert_called_once()
        adapter.lookup.return_value={'state':'sent','evidence':'synthetic-delivery-1'}
        done=w.reconcile(unknown,key,current_journey=JOURNEY,adapter=adapter,persist=saved.append)
        self.assertEqual(done['receipts'][key]['state'],'sent')
        adapter.send.assert_called_once()

    def test_failed_persistence_disconnected_cancelled_late_never_send(self):
        state=w.evaluate(JOURNEY,{},NOW,feed());key=next(iter(state['receipts']))
        adapter=Mock(connected=True)
        with self.assertRaises(OSError):w.deliver(state,key,current_journey=JOURNEY,now=NOW,adapter=adapter,persist=Mock(side_effect=OSError))
        with self.assertRaises(ValueError):w.deliver(state,key,current_journey=replace(JOURNEY,status='cancelled'),now=NOW,adapter=adapter,persist=Mock())
        self.assertEqual(w.deliver(state,key,current_journey=JOURNEY,now=OUT.departure,adapter=adapter,persist=Mock()),state)
        adapter.connected=False
        with self.assertRaises(ValueError):w.deliver(state,key,current_journey=JOURNEY,now=NOW,adapter=adapter,persist=Mock())
        adapter.send.assert_not_called()

    def test_sent_requires_evidence_and_reconciliation_owner_binding(self):
        state=w.evaluate(JOURNEY,{},NOW,feed());key=next(iter(state['receipts']))
        adapter=Mock(connected=True);adapter.send.return_value={'state':'sent'}
        result=w.deliver(state,key,current_journey=JOURNEY,now=NOW,adapter=adapter,persist=Mock())
        self.assertEqual(result['receipts'][key]['state'],'unknown')
        with self.assertRaises(ValueError):w.reconcile(result,key,current_journey=replace(JOURNEY,owner_id='other'),adapter=adapter,persist=Mock())

    def test_ui_preview_cannot_schedule_and_uses_each_leg_time(self):
        rows=w.preview_windows(OUT.departure,BACK.departure)
        self.assertEqual(rows[0]['start'],NOW)
        self.assertEqual(rows[1]['start'],NOW+timedelta(hours=4))
        self.assertEqual(len(w.preview_windows(None,BACK.departure)),1)
