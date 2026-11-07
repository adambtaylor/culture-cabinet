"""Canonical outing journeys with synthetic HTTP/provider boundaries only."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import re
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.utils import timezone
from shelf import event_plans, outing_plans as o
from shelf.calendar_handoff import CalendarHandoff, digest, instant
from shelf.event_plan_provider import CONTACTS, RehearsalProvider
from shelf.models import EventPlan
from shelf.tests import test_event_plans as existing

DEMO_OUTPUT=None


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class OutingTests(TestCase):
    setUp=existing.EventPlanTests.setUp
    login=existing.EventPlanTests.login
    start=existing.EventPlanTests.start
    payload=existing.EventPlanTests.payload
    act=existing.EventPlanTests.act
    ticket=existing.EventPlanTests.ticket
    calendar=existing.EventPlanTests.calendar
    book=existing.EventPlanTests.book

    def configure_fields(self,**updates):
        self.plan.refresh_from_db()
        data={k:v for k,v in self.payload('unused').items() if k!='action'}
        data.update(dinner_minutes=60,transfer_minutes=20,arrival_buffer=20,drink_minutes=30,return_buffer=10)
        for n,person in enumerate(o.participants(self.plan)):
            row=dict(origin='batman' if n==0 else 'richmond',out_mode='bike_train' if n==0 else 'pt',
                return_mode='bike_train' if n==0 else 'pt',out_minutes=40 if n==0 else 25,return_minutes=45,
                handling_minutes=10,park_bike='on' if n==0 else '',parking='Unverified example storage',fare='8.00')
            data.update({person['id']+'__'+k:v for k,v in row.items()})
        data.update(updates)
        return data

    def configure(self,expected=302,**updates):
        response=self.client.post(self.url+'outing/',self.configure_fields(**updates))
        self.assertEqual(response.status_code,expected,response.content.decode()[:500])
        self.plan.refresh_from_db()
        return response

    def email(self,audience='owner',expected=302):
        self.plan.refresh_from_db()
        view=o.email_preview(self.plan,audience)
        return self.act('packet_email',recipient=audience,packet_digest=view['digest'],expected=expected)

    def snapshot(self,name):
        response=self.client.get(self.url)
        self.assertEqual(response.status_code,200)
        if DEMO_OUTPUT:
            html=re.sub(r'<form\b.*?</form>','<p>[Interactive form omitted from static review]</p>',response.content.decode(),flags=re.S)
            (DEMO_OUTPUT/(name+'.html')).write_text(html)
        return response

    def test_review_demo_full_block_privacy_email_unknown_update_and_cancel(self):
        self.book(calendar_scenario='unknown_then_confirmed')
        uid=self.calendar().uid
        self.configure()
        self.assertEqual(self.calendar().uid,uid)
        self.assertEqual(o.anchor(self.plan).start,'2027-01-15T20:00:00+11:00')
        self.assertEqual(self.calendar().event.start,'2027-01-15T17:30:00+11:00')
        self.assertEqual(self.calendar().event.end,'2027-01-15T23:35:00+11:00')
        page=self.snapshot('01-whole-outing')
        self.assertContains(page,'Ticket PDFs unavailable')
        self.assertContains(page,'No routing request, rideshare quote or booking')
        self.act('calendar');self.act('reconcile_calendar',receipt=self.calendar().receipts[-1].key)
        self.act('calendar',recipient=CONTACTS[0].recipient_id)
        self.act('reconcile_calendar',receipt=self.calendar().receipts[-1].key)
        self.email();self.snapshot('02-unknown-email')
        receipt=next(v for v in self.plan.data['operations'].values() if v.get('kind')=='packet_email')
        self.act('reconcile_email',receipt=receipt['key'])
        self.snapshot('03-reconciled-email')
        self.configure(owner__out_minutes=50)
        self.assertFalse(event_plans.context(self.plan)['owner_current'])
        self.assertEqual(self.calendar().uid,uid)
        self.snapshot('04-route-update-needs-approval')
        self.act('cancel_event')
        self.assertEqual(o.anchor(self.plan).status,'cancelled')
        self.assertEqual(self.calendar().event.status,'cancelled')
        self.assertEqual(self.calendar().uid,uid)
        self.snapshot('05-cancel-existing-block')
        if DEMO_OUTPUT:
            (DEMO_OUTPUT/'journey.json').write_text(json.dumps({'mode':'synthetic rehearsal','calendar_uid_stable':True,
                'calendar_blocks':1,'outward_and_return':True,'individual_origins':True,'email_send_attempts':1,
                'real_calendar_writes':0,'real_emails':0,'real_bookings':0,'actual_ticket_files':0},indent=2))

    def test_modes_are_selectable_without_provider_quotes_or_queries(self):
        self.book()
        for mode in o.MODES:
            self.configure(owner__out_mode=mode,owner__return_mode=mode,owner__park_bike='on' if mode=='bike_train' else '')
            self.assertEqual(self.plan.data['outing']['people'][0]['out_mode'],mode)
            self.assertContains(self.client.get(self.url),'estimate, not quote')
        self.assertEqual(len(self.calendar().receipts),0)

    def test_incompatible_vehicle_or_bike_return_is_blocked(self):
        self.book()
        for change in ({'owner__return_mode':'uber'},{'owner__out_mode':'drive','owner__return_mode':'pt','owner__park_bike':''},
                       {'owner__out_mode':'cycle','owner__return_mode':'pt','owner__park_bike':''}):
            self.configure(expected=409,**change)
        self.assertNotIn('outing',self.plan.data)

    def test_calendar_common_description_never_contains_private_origins_or_order(self):
        self.book();self.configure()
        calendar=self.calendar()
        for secret in ('Batman Station','Richmond Station',CONTACTS[0].address,self.ticket().order_reference):
            self.assertNotIn(secret,calendar.description)
        owner=o.email_preview(self.plan,'owner');friend=o.email_preview(self.plan,CONTACTS[0].recipient_id)
        self.assertIn('Batman Station',owner['body']);self.assertNotIn('Richmond Station',owner['body'])
        self.assertIn('Richmond Station',friend['body']);self.assertNotIn('Batman Station',friend['body'])
        self.assertNotIn(self.ticket().order_reference,friend['body'])
        self.assertNotIn(CONTACTS[0].address,owner['body'])
        self.assertIn('no-store',self.client.get(self.url)['Cache-Control'])

    def document(self,audience='owner',**changes):
        return dict(filename='synthetic-test-ticket.pdf',mime='application/pdf',bytes=100,sha256='a'*64,
            audience=audience,issuer_verified=True,order_reference=self.ticket().order_reference,
            receipt_reference='fixture-issuer-proof',status='available',
            url='https://issuer.example.invalid/private-ticket?opaque=fixture',
            expires_at=(timezone.now()+timedelta(hours=1)).replace(microsecond=0).isoformat(),**changes)

    def save_documents(self,documents):
        self.plan.refresh_from_db();data=deepcopy(self.plan.data);data['ticket_documents']=documents
        EventPlan.objects.filter(pk=self.plan.pk).update(data=data);self.plan.refresh_from_db()

    def test_issuer_metadata_is_recipient_specific_never_a_fake_pdf(self):
        self.book();self.save_documents([self.document()]);self.configure()
        owner=o.email_preview(self.plan,'owner');friend=o.email_preview(self.plan,CONTACTS[0].recipient_id)
        self.assertEqual(len(owner['attachments']),1);self.assertEqual(friend['attachments'],[])
        self.assertNotIn('private-ticket',self.calendar().description)
        self.assertNotIn('fixture-issuer-proof',json.dumps(friend))
        self.assertContains(self.client.get(self.url),'No PDF bytes have been copied or attached')

    def test_unverified_expired_wrong_order_or_nonpdf_metadata_is_excluded(self):
        self.book()
        for changed in ({'issuer_verified':False},{'order_reference':'other-order'},{'mime':'text/html'},
                        {'expires_at':(timezone.now()-timedelta(seconds=1)).replace(microsecond=0).isoformat()},
                        {'url':'javascript:alert(1)'},{'sha256':'x'*64},{'audience':'not-selected'}):
            doc=self.document();doc.update(changed);self.save_documents([doc])
            self.assertEqual(o.ticket_documents(self.plan,'owner',timezone.now()),[])

    def test_route_edit_invalidates_approval_and_preserves_single_calendar_identity(self):
        self.book();self.configure();self.act('calendar');uid=self.calendar().uid
        old=self.payload('calendar',recipient=CONTACTS[0].recipient_id)
        self.configure(owner__out_minutes=55)
        self.assertEqual(self.client.post(self.url+'action/',old).status_code,409)
        self.email(expected=409)
        self.act('calendar');self.email()
        self.assertEqual(self.calendar().uid,uid)
        self.assertEqual(len({r['event_uid'] for r in self.plan.data['operations'].values() if 'event_uid' in r}),1)

    def test_replay_and_changed_nonce_payload_cannot_duplicate_packet_or_email(self):
        self.book();fields=self.configure_fields()
        self.assertEqual(self.client.post(self.url+'outing/',fields).status_code,302)
        self.plan.refresh_from_db();version=self.plan.version
        self.assertEqual(self.client.post(self.url+'outing/',fields).status_code,302)
        self.plan.refresh_from_db();self.assertEqual(self.plan.version,version)
        self.assertEqual(self.client.post(self.url+'outing/',dict(fields,owner__out_minutes=99)).status_code,409)
        self.act('calendar')
        with patch.object(RehearsalProvider,'packet_email_evidence',autospec=True,side_effect=RehearsalProvider.packet_email_evidence) as call:
            self.email();self.email()
            self.assertEqual(call.call_count,1)

    def test_unknown_email_persists_before_send_blocks_edits_and_reconciles_only(self):
        self.book();self.configure();self.act('calendar')
        def outage(adapter,receipt,*args,**kwargs):
            saved=EventPlan.objects.get(pk=self.plan.pk).data['operations'][receipt['key']]
            self.assertEqual(saved['state'],'unknown');self.assertEqual(saved['send_attempts'],1)
            raise OSError('synthetic timeout')
        with patch.object(RehearsalProvider,'packet_email_evidence',autospec=True,side_effect=outage):self.email(expected=503)
        self.configure(expected=409);self.act('reschedule',expected=409)
        receipt=next(v for v in self.plan.data['operations'].values() if v.get('kind')=='packet_email')
        with patch.object(RehearsalProvider,'packet_email_evidence',autospec=True,side_effect=RehearsalProvider.packet_email_evidence) as call:
            self.email();call.assert_not_called()
            self.act('reconcile_email',receipt=receipt['key'])
            self.assertTrue(call.call_args.kwargs['reconcile'])
        self.configure(owner__out_minutes=50)

    def test_wrong_delivery_evidence_remains_unknown(self):
        self.book();self.configure();self.act('calendar')
        with patch.object(RehearsalProvider,'packet_email_evidence',return_value={'fixture':True,'state':'confirmed','key':'wrong'}):
            self.email(expected=409)
        receipt=next(v for v in self.plan.data['operations'].values() if v.get('kind')=='packet_email')
        self.assertEqual(receipt['state'],'unknown')

    def test_packet_source_changes_require_rebuild_before_calendar_actions(self):
        self.book();self.configure()
        self.save_documents([self.document()])
        self.assertFalse(o.current(self.plan));self.act('calendar',expected=409)
        self.assertContains(self.client.get(self.url),'Packet is stale')
        self.configure();self.assertTrue(o.current(self.plan));self.act('calendar')

    def test_expired_attachment_invalidates_addressed_email_preview(self):
        self.book();document=self.document()
        document['expires_at']=(timezone.now()+timedelta(seconds=5)).replace(microsecond=0).isoformat()
        self.save_documents([document]);self.configure();self.act('calendar')
        payload=self.payload('packet_email',recipient='owner',packet_digest=o.email_preview(self.plan,'owner')['digest'])
        with patch('shelf.outing_plans.timezone.now',return_value=timezone.now()+timedelta(seconds=10)):
            self.assertEqual(self.client.post(self.url+'action/',payload).status_code,409)
        self.assertFalse(any(v.get('kind')=='packet_email' for v in self.plan.data['operations'].values()))

    def test_organizer_update_preserves_anchor_order_and_rebuilds_master(self):
        self.book();self.configure();original_order=self.ticket().order_reference;uid=self.calendar().uid
        self.act('calendar');self.act('reschedule')
        self.assertEqual(o.anchor(self.plan).start,'2027-01-15T21:00:00+11:00')
        self.assertEqual(self.calendar().event.start,'2027-01-15T18:30:00+11:00')
        self.assertTrue(o.current(self.plan));self.assertEqual(self.calendar().uid,uid)
        self.assertEqual(self.ticket().order_reference,original_order)
        self.assertEqual(self.ticket().offer.event.start,'2027-01-15T20:00:00+11:00')

    def test_calendar_description_roundtrip_digest_and_noninviting_export(self):
        self.book();self.configure();state=self.calendar()
        self.assertEqual(CalendarHandoff.from_dict(state.to_dict()),state)
        preview=state.preview()
        state,receipt=state.approve(actor_id=str(self.actor.pk),expected_revision=state.revision,
            preview_digest=preview['digest'],key='fixture-export',user_confirmed=True)
        raw=state.export_ics(receipt.key,actor_id=str(self.actor.pk)).decode().replace('\r\n ','')
        self.assertIn('Occupied block',raw);self.assertNotIn('ATTENDEE:',raw)
        self.assertNotIn('Batman Station',raw)
        old=replace(state,description='',receipts=()).to_dict()
        self.assertEqual(old['schema'],1);self.assertNotIn('description',old)
        self.assertEqual(CalendarHandoff.from_dict(old).description,'')

    def test_http_rejects_unknown_recipients_and_browser_ticket_metadata(self):
        self.book();self.configure();self.act('calendar')
        self.act('packet_email',recipient='not-selected',packet_digest='a'*64,expected=409)
        self.assertEqual(self.client.post(self.url+'outing/',dict(self.configure_fields(),ticket_documents='forged')).status_code,409)
        self.client.logout()
        self.assertEqual(self.client.post(self.url+'outing/',{}).status_code,302)

    def test_real_mode_cannot_rehearse_email_or_configure(self):
        self.book();EventPlan.objects.filter(pk=self.plan.pk).update(mode='household');self.plan.refresh_from_db()
        self.configure(expected=409);self.act('packet_email',recipient='owner',packet_digest='a'*64,expected=409)
