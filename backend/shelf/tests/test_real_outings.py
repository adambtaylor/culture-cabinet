"""HTTP source-to-private-file journey; all account writes are forbidden."""
from copy import deepcopy
from datetime import timedelta
from email import message_from_bytes
from io import BytesIO
import json
from pathlib import Path
import importlib.util
from unittest.mock import patch

from django.test import TestCase, override_settings
from shelf import google_handoff, real_outings, event_plans
from shelf.acquisition_contract import Blocked
from shelf.calendar_handoff import CalendarHandoff
from shelf.discovery_state import Conflict
from shelf.models import EventPlan, Grant
from shelf.tests import test_date_night as evening

DEMO_OUTPUT = None


def readback(packet):
    args=packet['arguments']
    if packet['kind']=='calendar':
        record=dict(calendar_id='primary',id='observed_123',summary=args['title'],description=args['description'],
            start=args['start_time'],end=args['end_time'],timezone=args['timezone_str'],location=args['location'],
            visibility='private',attendees=[],status='confirmed')
    else:
        record=dict(id='observed_123',label_ids=['SENT'],to=['owner@example.com'],cc=[],bcc=[],
                    subject=args['subject'],html=args['payload']['body']['content'])
    return dict(account_email='owner@example.com',records=[record])


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class RealOutingTests(TestCase):
    setUp=evening.EveningUITests.setUp
    read_detail=evening.EveningUITests.read_detail
    form=evening.EveningUITests.form

    def save_hold(self, **values):
        self.read_detail()
        page=self.client.get(self.evening)
        token=page.context['form'].initial['hold_token']
        response=self.client.post(self.evening,dict(evening.VALUES,intent='save_hold',save_confirmed='on',
            hold_token=token,cycle_origin='PRIVATE ORIGIN CANARY',**values))
        self.assertEqual(response.status_code,302,response.content)
        plan=EventPlan.objects.get()
        return plan

    def action_hold(self,plan,action,**values):
        fields=dict(action=action,version=plan.version,nonce=plan.nonce,
                    preview=event_plans.preview_digest(plan),confirmed='on',**values)
        response=self.client.post(f'/profiles/1/event-plans/{plan.pk}/real-action/',fields)
        plan.refresh_from_db()
        return response,fields

    def test_real_source_save_exports_and_one_owner_handoff(self):
        plan=self.save_hold(companion_origin='richmond',companion_minutes=60)
        self.assertTrue(real_outings.is_hold(plan));self.assertFalse(plan.data['booked'])
        self.assertNotIn('PRIVATE ORIGIN CANARY',json.dumps(plan.data))
        calendar=CalendarHandoff.from_dict(plan.data['calendar'])
        self.assertEqual(calendar.event.start,'2026-10-30T19:00:00+11:00')
        page=self.client.get(f'/profiles/1/event-plans/{plan.pk}/')
        self.assertContains(page,'Real source · unbooked planning hold')
        self.assertContains(page,'Fri 30 Oct, 19:00–Fri 30 Oct, 23:55')
        self.assertNotContains(page,'Fri 30 Oct, 08:00')
        ics,fields=self.action_hold(plan,'ics')
        self.assertEqual(ics.status_code,200,ics.content)
        self.assertIn(b'STATUS:TENTATIVE',ics.content)
        self.assertNotIn(b'ATTENDEE',ics.content);self.assertNotIn(b'METHOD:REQUEST',ics.content)
        self.assertIn(b'UNBOOKED',ics.content)
        repeated=self.client.post(f'/profiles/1/event-plans/{plan.pk}/real-action/',fields)
        self.assertEqual(repeated.content,ics.content)
        eml,_=self.action_hold(plan,'eml')
        message=message_from_bytes(eml.content)
        self.assertIsNone(message['To']);self.assertEqual(message['X-Unsent'],'1')
        self.assertEqual(len(list(message.walk())),1)
        result,fields=self.action_hold(plan,'reserve_calendar')
        self.assertEqual(result.status_code,200,result.content)
        packet=result.json();self.assertEqual(packet['arguments']['attendees'],[])
        self.assertTrue(packet['requires_human_account_and_action_authorization'])
        self.assertEqual(plan.data['handoffs']['calendar']['state'],'reserved_unknown')
        retry=self.client.post(f'/profiles/1/event-plans/{plan.pk}/real-action/',fields)
        self.assertTrue(retry.json()['reconciliation_only']);self.assertNotIn('create',retry.json()['tool'])
        again,_=self.action_hold(plan,'reserve_calendar')
        self.assertTrue(again.json()['reconciliation_only'])
        self.assertEqual(plan.data['handoffs']['calendar']['attempts'],1)
        if DEMO_OUTPUT:
            import re
            DEMO_OUTPUT.mkdir(parents=True,exist_ok=True)
            for name,html in [('01-source-hold.html',page.content.decode()),
                ('02-reserved-handoff.html',self.client.get(f'/profiles/1/event-plans/{plan.pk}/').content.decode())]:
                (DEMO_OUTPUT/name).write_text(re.sub(r'<form\b.*?</form>','<p>[Interactive form omitted from static evidence]</p>',html,flags=re.S))
            (DEMO_OUTPUT/'planning-hold.ics').write_bytes(ics.content)
            (DEMO_OUTPUT/'owner-packet.eml').write_bytes(eml.content)
            (DEMO_OUTPUT/'supervised-command.json').write_text(json.dumps(packet,indent=2))
            (DEMO_OUTPUT/'evidence.json').write_text(json.dumps(dict(source='synthetic Beat markup through production parser/HTTP',
                external_writes=0,booking=False,private_origin_retained=False,calendar_file='tentative',recipients=[],attempts_reserved=1)))

    def test_explicit_save_stale_preview_unknown_source_and_invalid_routes(self):
        self.read_detail()
        token=self.client.get(self.evening).context['form'].initial['hold_token']
        for values in [dict(save_confirmed=''),dict(hold_token='invalid'),dict(return_mode='replacement_bus')]:
            fields=dict(evening.VALUES,intent='save_hold',save_confirmed='on',hold_token=token);fields.update(values)
            self.assertEqual(self.client.post(self.evening,fields).status_code,200)
            self.assertFalse(EventPlan.objects.exists())
        with patch.object(real_outings.timezone,'now',return_value=evening.NOW+timedelta(minutes=15)):
            self.assertEqual(self.client.post(self.evening,dict(evening.VALUES,intent='save_hold',save_confirmed='on',hold_token=token)).status_code,410)
        self.assertFalse(EventPlan.objects.exists())

    def test_old_save_replay_is_single_plan_and_updated_preview_invalidates_action(self):
        plan=self.save_hold()
        export,export_fields=self.action_hold(plan,'ics')
        old_fields=dict(action='reserve_email',version=plan.version,nonce=plan.nonce,preview=event_plans.preview_digest(plan),confirmed='on')
        token=self.client.get(self.evening).context['form'].initial['hold_token']
        fields=dict(evening.VALUES,intent='save_hold',save_confirmed='on',hold_token=token,outward_minutes=60)
        response=self.client.post(self.evening,fields);self.assertEqual(response.status_code,302,response.content)
        self.assertEqual(self.client.post(self.evening,fields).status_code,302)
        self.assertEqual(EventPlan.objects.count(),1)
        stale=self.client.post(f'/profiles/1/event-plans/{plan.pk}/real-action/',old_fields)
        self.assertEqual(stale.status_code,409)
        self.assertEqual(self.client.post(f'/profiles/1/event-plans/{plan.pk}/real-action/',export_fields).status_code,409)
        plan.refresh_from_db();self.assertEqual(plan.data['handoffs'],{})

    def test_readback_exact_account_record_and_idempotence(self):
        plan=self.save_hold();response,_=self.action_hold(plan,'reserve_calendar');packet=response.json()
        evidence=readback(packet)
        for field,value in [('attendees',['friend@example.com']),('start','2026-10-30T00:00:00+00:00'),('visibility','public')]:
            invalid=deepcopy(evidence);invalid['records'][0][field]=value
            with self.assertRaises(Blocked):real_outings.record_operator(actor=self.actor,profile_id=1,plan_id=plan.pk,kind='calendar',key=packet['key'],evidence=invalid)
        for records in ([],evidence['records']*2):
            with self.assertRaises(Blocked):google_handoff.verify_readback(packet,dict(evidence,records=records))
        result=real_outings.record_operator(actor=self.actor,profile_id=1,plan_id=plan.pk,kind='calendar',key=packet['key'],evidence=evidence)
        version=result.version
        repeated=real_outings.record_operator(actor=self.actor,profile_id=1,plan_id=plan.pk,kind='calendar',key=packet['key'],evidence=evidence)
        self.assertEqual(repeated.version,version)
        self.assertEqual(repeated.data['handoffs']['calendar']['state'],'recorded_calendar')
        conflict=deepcopy(evidence);conflict['records'][0]['id']='different'
        with self.assertRaises(Conflict):real_outings.record_operator(actor=self.actor,profile_id=1,plan_id=plan.pk,kind='calendar',key=packet['key'],evidence=conflict)
        token=self.client.get(self.evening).context['form'].initial['hold_token']
        page=self.client.post(self.evening,dict(evening.VALUES,intent='save_hold',save_confirmed='on',hold_token=token,outward_minutes=55))
        self.assertContains(page,'connected-tool handoff was reserved')
        plan.refresh_from_db();self.assertEqual(plan.version,version)

    def test_email_readback_is_self_only_no_extra_recipients(self):
        plan=self.save_hold();response,_=self.action_hold(plan,'reserve_email');packet=response.json()
        evidence=readback(packet)
        self.assertEqual(packet['arguments']['to'],'me')
        self.assertEqual(google_handoff.verify_readback(packet,evidence)['state'],'recorded_sent')
        for field,value in [('to',['other@example.com']),('cc',['friend@example.com']),('bcc',['friend@example.com']),('label_ids',[]),('html','wrong')]:
            invalid=deepcopy(evidence);invalid['records'][0][field]=value
            with self.assertRaises(Blocked):google_handoff.verify_readback(packet,invalid)
        self.assertIn('in:sent',google_handoff.reconciliation(packet)['arguments']['query'])

    def test_revoked_grant_cannot_record_or_export(self):
        plan=self.save_hold();response,_=self.action_hold(plan,'reserve_calendar');packet=response.json()
        Grant.objects.filter(user=self.actor,profile_id=1).delete()
        with self.assertRaises(Blocked):real_outings.record_operator(actor=self.actor,profile_id=1,plan_id=plan.pk,kind='calendar',key=packet['key'],evidence=readback(packet))
        response,_=self.action_hold(plan,'eml');self.assertEqual(response.status_code,404)
        self.assertEqual(plan.data['handoffs']['calendar']['state'],'reserved_unknown')

    def test_operator_input_is_bounded_and_candidate_cannot_touch_runtime(self):
        file=Path(__file__).resolve().parents[3]/'tools/record_google_handoff.py'
        spec=importlib.util.spec_from_file_location('google_receipt_operator',file)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with self.assertRaises(ValueError):module.read_evidence(BytesIO(b'x'*(module.LIMIT+1)))
        for raw in (b'[]',b'{"records":[],"records":[]}',b'{"value":NaN}'):
            with self.assertRaises(ValueError):module.read_evidence(BytesIO(raw))
        with self.assertRaisesRegex(ValueError,'Deployed operator path required'):module.run(None)

    def test_expiry_and_revoked_authority_during_readback_do_not_commit(self):
        plan=self.save_hold();response,_=self.action_hold(plan,'reserve_calendar');packet=response.json()
        def revoke(packet,evidence):
            Grant.objects.filter(user=self.actor).delete()
            return {'state':'recorded_calendar'}
        with patch.object(google_handoff,'verify_readback',side_effect=revoke):
            with self.assertRaises(Blocked):real_outings.record_operator(actor=self.actor,profile_id=1,
                plan_id=plan.pk,kind='calendar',key=packet['key'],evidence=readback(packet))
        guard=__import__('unittest.mock',fromlist=['Mock']).Mock(side_effect=[None,ValueError('expired')])
        with self.assertRaises(ValueError):real_outings.record_operator(actor=self.actor,profile_id=1,
            plan_id=plan.pk,kind='calendar',key=packet['key'],evidence=readback(packet),session_guard=guard)
        plan.refresh_from_db();self.assertEqual(plan.data['handoffs']['calendar']['state'],'reserved_unknown')
