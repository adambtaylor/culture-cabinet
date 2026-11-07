"""Whole HTTP journeys with fictional providers; no external effects or credentials."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from shelf import event_plans
from shelf.acquisition_contract import Blocked, ContractError
from shelf.authority import SELECTION_KEY
from shelf.event_plan_provider import CONTACTS, Quote, RehearsalProvider, provider, resolve_contacts
from shelf.models import EventPlan, Grant, Profile
from shelf.ticket_handoff import TicketHandoff

DEMO_OUTPUT = None


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class EventPlanTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-event-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='SYNTHETIC owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.login()
        self.base = '/profiles/1/event-plans/'
        self.steps = []

    def login(self):
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = 1
        session.save()

    def start(self, **changes):
        fields = dict(session_id='fixture-session-15', quantity=2,
            contacts=[CONTACTS[0].recipient_id], purchase_scenario='confirmed', calendar_scenario='confirmed')
        fields.update(changes)
        response = self.client.post(self.base + 'start/', fields)
        if response.status_code == 302:
            self.plan = EventPlan.objects.get(pk=response.url.strip('/').split('/')[-1])
            self.url = response.url
        return response

    def payload(self, action, **changes):
        self.plan.refresh_from_db()
        return dict(action=action, version=self.plan.version, nonce=self.plan.nonce,
                    preview=event_plans.preview_digest(self.plan), confirmed='on', **changes)

    def act(self, action, *, expected=302, **changes):
        result = self.client.post(self.url + 'action/', self.payload(action, **changes))
        self.assertEqual(result.status_code, expected, result.content.decode()[:500])
        self.plan.refresh_from_db()
        return result

    def ticket(self):
        self.plan.refresh_from_db()
        return event_plans.contracts(self.plan)[0]

    def calendar(self):
        self.plan.refresh_from_db()
        return event_plans.contracts(self.plan)[1]

    def book(self, **changes):
        self.assertEqual(self.start(**changes).status_code, 302)
        self.act('approve_purchase')
        self.act('handoff')
        self.act('reconcile_purchase')

    def snapshot(self, label):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.steps.append({'step': label, 'purchase': self.ticket().purchase_state,
            'calendar_uid': self.calendar().uid, 'calendar_revision': self.calendar().revision,
            'calendar_receipts': [{'operation': r.operation, 'state': r.state,
                'recipient': r.recipient.address if r.recipient else None} for r in self.calendar().receipts]})
        if DEMO_OUTPUT is not None:
            html = response.content.decode()
            html = re.sub(r'value="[^"]*"(?=[^>]*>)', 'value="redacted"', html)
            html = html.replace('<button', '<button disabled').replace('<input', '<input disabled')
            (DEMO_OUTPUT / (label + '.html')).write_text(html)

    def test_review_demo_booking_owner_invites_reschedule_and_cancellation(self):
        self.assertEqual(self.start().status_code, 302)
        response = self.client.get(self.url)
        for text in ('84.50 total', '4.50 fees', 'non-refundable', CONTACTS[0].address,
                     'Wallet pass: unavailable', 'No payment, booking, calendar change or invitation has occurred'):
            self.assertContains(response, text)
        self.snapshot('01-review')
        self.act('approve_purchase')
        self.act('handoff')
        self.assertEqual(self.ticket().purchase_state, 'submitted')
        self.assertEqual(self.calendar().receipts, ())
        self.snapshot('02-unknown-handoff')
        self.act('reconcile_purchase')
        self.snapshot('03-booking-evidence')
        self.act('calendar')
        self.act('calendar', recipient=CONTACTS[0].recipient_id)
        self.snapshot('04-calendar-and-invitation')
        original_uid = self.calendar().uid
        original_order = self.ticket().order_reference
        original_start = self.ticket().offer.event.start
        first_event_ids = {v['evidence']['event_id'] for v in self.plan.data['operations'].values()}
        self.act('reschedule')
        self.assertEqual(self.calendar().uid, original_uid)
        self.assertEqual(self.calendar().revision, 1)
        self.act('calendar')
        self.act('calendar', recipient=CONTACTS[0].recipient_id)
        self.snapshot('05-reschedule-synchronized')
        self.act('cancel_event')
        self.act('calendar')
        self.act('calendar', recipient=CONTACTS[0].recipient_id)
        self.snapshot('06-cancellation-synchronized')
        self.assertEqual(self.calendar().uid, original_uid)
        self.assertEqual(self.calendar().revision, 2)
        self.assertEqual(self.calendar().event.status, 'cancelled')
        self.assertEqual(self.ticket().order_reference, original_order)
        self.assertEqual(self.ticket().offer.event.start, original_start)
        self.assertEqual({v['evidence']['event_id'] for v in self.plan.data['operations'].values()}, first_event_ids)
        self.assertTrue(all(v['send_attempts'] == 1 for v in self.plan.data['operations'].values()))
        self.assertTrue(all(v['evidence']['rsvp'] == 'unknown' for v in self.plan.data['operations'].values()))
        self.assertIsNone(self.ticket().wallet_artifact)
        if DEMO_OUTPUT is not None:
            (DEMO_OUTPUT / 'journey.json').write_text(json.dumps({
                'mode': 'fictional rehearsal', 'real_external_actions': 0, 'steps': self.steps,
                'operations': self.plan.data['operations']}, indent=2) + '\n')

    def test_start_replay_preserves_one_plan_and_conflicting_selection_refused(self):
        self.start()
        original = self.plan.pk
        self.start()
        self.assertEqual(self.plan.pk, original)
        self.assertEqual(EventPlan.objects.count(), 1)
        self.assertEqual(self.start(quantity=3).status_code, 409)
        self.assertEqual(EventPlan.objects.count(), 1)

    def test_attendee_resolution_requires_exact_unique_mailbox_choices_and_enough_tickets(self):
        for values in ({'contacts': ['Alex']}, {'contacts': ['person@example.org']},
                       {'contacts': [CONTACTS[0].recipient_id] * 2}, {'quantity': 1}):
            with self.subTest(values=values):
                self.assertEqual(self.start(**values).status_code, 409)
        self.assertEqual(EventPlan.objects.count(), 0)
        self.start(quantity=3, contacts=[CONTACTS[0].recipient_id, CONTACTS[1].recipient_id])
        response = self.client.get(self.url)
        self.assertContains(response, CONTACTS[0].address)
        self.assertContains(response, CONTACTS[1].address)
        with self.assertRaises(Blocked):
            resolve_contacts(['Alex'])

    def test_nonce_replay_does_not_repeat_handoff_or_calendar_dispatch(self):
        self.book()
        payload = self.payload('calendar')
        with patch.object(RehearsalProvider, 'calendar_evidence', autospec=True,
                          side_effect=RehearsalProvider.calendar_evidence) as call:
            for _ in range(2):
                self.assertEqual(self.client.post(self.url + 'action/', payload).status_code, 302)
            self.assertEqual(call.call_count, 1)
        self.assertEqual(len(self.calendar().receipts), 1)
        self.act('calendar')  # Fresh form, identical command still has no new dispatch.
        self.assertEqual(len(self.calendar().receipts), 1)
        payload['action'] = 'cancel_event'
        self.assertEqual(self.client.post(self.url + 'action/', payload).status_code, 409)

    def test_stale_preview_and_changed_terms_fail_exact_approval(self):
        self.start()
        payload = self.payload('approve_purchase')
        changed = deepcopy(self.plan.data)
        changed['quote']['refund_terms'] = 'Changed fictional restrictions'
        EventPlan.objects.filter(pk=self.plan.pk).update(data=changed)
        self.assertEqual(self.client.post(self.url + 'action/', payload).status_code, 409)
        self.assertEqual(self.ticket().purchase_state, 'requested')
        payload = self.payload('approve_purchase')
        self.act('refresh_quote')
        self.assertEqual(self.client.post(self.url + 'action/', payload).status_code, 409)

    def test_quote_expiry_blocks_approval_and_handoff_refresh_invalidates_consent(self):
        self.start()
        self.act('approve_purchase')
        future = timezone.now() + timedelta(minutes=11)
        with patch('shelf.event_plans.timezone.now', return_value=future):
            self.act('handoff', expected=409)
            self.act('refresh_quote')
            self.assertEqual(self.ticket().purchase_state, 'requested')
            self.act('handoff', expected=409)
            self.act('approve_purchase')
            self.act('handoff')

    def test_unknown_purchase_survives_logout_and_only_reconciles(self):
        self.book(purchase_scenario='unknown_then_confirmed')
        self.assertEqual(self.ticket().purchase_state, 'submitted')
        self.client.logout()
        self.login()
        self.act('refresh_quote', expected=409)
        self.act('calendar', expected=409)
        self.act('handoff')  # Contract replay returns no new instructions.
        self.act('reconcile_purchase')
        self.assertEqual(self.ticket().purchase_state, 'confirmed')
        self.assertEqual(EventPlan.objects.count(), 1)

    def test_definite_purchase_failure_keeps_evidence_and_blocks_calendar_and_new_intent(self):
        self.book(purchase_scenario='failed')
        self.assertEqual(self.ticket().purchase_state, 'failed')
        self.assertTrue(self.ticket().failure_reference)
        for action in ('calendar', 'refresh_quote', 'reconcile_purchase'):
            self.act(action, expected=409)
        self.assertEqual(self.start(quantity=3).status_code, 409)
        restored = TicketHandoff.from_dict(self.ticket().to_dict())
        self.assertEqual(restored, self.ticket())

    def test_failed_ticket_backward_compatibility_and_unverified_failure_refused(self):
        self.start()
        old = self.ticket().to_dict()
        old.pop('failure_reference')
        old['schema'] = 1
        self.assertEqual(TicketHandoff.from_dict(old), self.ticket())
        self.act('approve_purchase'); self.act('handoff')
        ticket = self.ticket()
        with self.assertRaises(Blocked):
            ticket.confirm_failure(actor_id='1', offer_digest=ticket.approval_digest,
                                   failure_reference='fixture-failure')

    def test_owner_calendar_before_each_selected_invitation_and_purchase_is_not_invite_approval(self):
        self.book()
        self.assertEqual(self.calendar().receipts, ())
        self.act('calendar', recipient=CONTACTS[0].recipient_id, expected=409)
        self.act('calendar')
        self.act('calendar', recipient=CONTACTS[1].recipient_id, expected=409)
        self.act('calendar', recipient=CONTACTS[0].recipient_id)
        self.assertEqual(len(self.calendar().receipts), 2)

    def test_calendar_unknown_dedupes_and_reconciliation_does_not_send_again(self):
        self.book(calendar_scenario='unknown_then_confirmed')
        self.act('calendar')
        receipt = self.calendar().receipts[0]
        self.assertEqual(receipt.state, 'submitted_unknown')
        self.act('calendar')
        self.act('reschedule', expected=409)
        self.act('reconcile_calendar', receipt=receipt.key)
        self.assertEqual(self.calendar().receipts[0].state, 'confirmed')
        self.assertEqual(self.plan.data['operations'][receipt.key]['send_attempts'], 1)
        self.act('calendar', recipient=CONTACTS[0].recipient_id)
        invite = self.calendar().receipts[-1]
        self.act('reconcile_calendar', receipt=invite.key)
        self.assertEqual(self.calendar().receipts[-1].state, 'confirmed')
        self.assertEqual(self.plan.data['operations'][invite.key]['send_attempts'], 1)

    def test_dispatch_exception_retains_reservation_for_reconciliation_without_resend(self):
        self.book()
        with patch.object(RehearsalProvider, 'calendar_evidence', side_effect=OSError('fixture outage')):
            self.act('calendar', expected=503)
        receipt = self.calendar().receipts[0]
        self.assertEqual(receipt.state, 'submitted_unknown')
        self.act('calendar')
        self.act('reconcile_calendar', receipt=receipt.key)
        self.assertEqual(self.calendar().receipts[0].state, 'confirmed')
        self.assertEqual(self.plan.data['operations'][receipt.key]['send_attempts'], 1)

    def test_calendar_definite_failure_is_terminal_for_same_command(self):
        self.book(calendar_scenario='failed')
        self.act('calendar')
        self.assertEqual(self.calendar().receipts[0].state, 'failed')
        self.act('calendar')
        self.assertEqual(len(self.calendar().receipts), 1)
        self.act('reconcile_calendar', receipt=self.calendar().receipts[0].key, expected=409)
        self.act('calendar', recipient=CONTACTS[0].recipient_id, expected=409)

    def test_cancellation_never_sends_to_a_friend_who_was_not_invited(self):
        self.book()
        self.act('calendar')
        self.act('cancel_event')
        self.act('calendar')
        self.act('calendar', recipient=CONTACTS[0].recipient_id, expected=409)
        self.assertContains(self.client.get(self.url), 'No confirmed prior invitation to cancel')

    def test_current_grant_and_persisted_selection_rechecked_after_provider_response(self):
        self.book()
        original = RehearsalProvider.calendar_evidence
        def revoke(adapter, *args, **kwargs):
            result = original(adapter, *args, **kwargs)
            Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
            return result
        with patch.object(RehearsalProvider, 'calendar_evidence', revoke):
            self.act('calendar', expected=404)
        self.assertEqual(self.calendar().receipts[0].state, 'submitted_unknown')
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_logout_during_evidence_read_cannot_publish_confirmation(self):
        self.book()
        original = RehearsalProvider.calendar_evidence
        def logout(adapter, *args, **kwargs):
            result = original(adapter, *args, **kwargs)
            self.client.session.flush()
            return result
        with patch.object(RehearsalProvider, 'calendar_evidence', logout):
            self.act('calendar', expected=404)
        self.assertEqual(self.calendar().receipts[0].state, 'submitted_unknown')

    def test_csrf_foreign_profile_and_unselected_session_rejected(self):
        self.start()
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.actor)
        self.assertEqual(csrf.post(self.base + 'start/', {}).status_code, 403)
        self.assertEqual(self.client.get(self.url.replace('/profiles/1/', '/profiles/2/')).status_code, 404)
        session = self.client.session
        session.pop(SELECTION_KEY)
        session.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url + 'action/', self.payload('approve_purchase')).status_code, 404)

    def test_read_only_get_and_live_adapter_unavailable(self):
        self.start()
        before = deepcopy(self.plan.data)
        with patch.object(RehearsalProvider, 'quote', side_effect=AssertionError('No quote on GET')), \
             patch.object(RehearsalProvider, 'calendar_evidence', side_effect=AssertionError('No write on GET')), \
             patch.object(RehearsalProvider, 'purchase_evidence', side_effect=AssertionError('No evidence on GET')):
            self.assertContains(self.client.get(self.base), 'Live booking awaits connection')
            self.assertEqual(self.client.get(self.url).status_code, 200)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.data, before)
        with self.assertRaises(Blocked):
            provider('household').quote(None, 1, timezone.now())
        EventPlan.objects.filter(pk=self.plan.pk).update(mode='household')
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.act('approve_purchase', expected=409)

    def test_unsigned_extra_fields_cannot_assert_seller_or_contact_authority(self):
        self.assertEqual(self.start(issuer_verified='true').status_code, 409)
        self.start()
        payload = self.payload('approve_purchase')
        payload['quote'] = '{}'
        self.assertEqual(self.client.post(self.url + 'action/', payload).status_code, 409)
        payload.pop('quote'); payload.pop('confirmed')
        self.assertEqual(self.client.post(self.url + 'action/', payload).status_code, 409)
        self.assertEqual(self.ticket().purchase_state, 'requested')

    def test_quote_rejects_invalid_fees_and_inconsistent_total(self):
        self.start()
        quote = Quote.from_dict(self.plan.data['quote'])
        for fees in ('-1', 'NaN', '1e2', '1.0.0', '99999999999999999', '5.00'):
            with self.subTest(fees=fees), self.assertRaises(ContractError):
                replace(quote, fees=fees)

    def test_unmatched_seller_evidence_does_not_confirm_booking(self):
        self.start(); self.act('approve_purchase'); self.act('handoff')
        with patch.object(RehearsalProvider, 'purchase_evidence', return_value={
                'fixture': True, 'status': 'confirmed', 'offer_digest': 'wrong', 'reference': 'fixture-order'}):
            self.act('reconcile_purchase', expected=409)
        self.assertEqual(self.ticket().purchase_state, 'submitted')

    def test_storage_unavailable_shows_read_only_boundary(self):
        with patch('shelf.event_plans.available', return_value=False):
            self.assertContains(self.client.get(self.base), 'cannot save event plans')
            self.assertEqual(self.start().status_code, 409)

    def test_purchase_unknown_remains_reconcilable_at_receipt_cap(self):
        self.book(purchase_scenario='unknown_then_confirmed')
        original = self.ticket()
        EventPlan.objects.filter(pk=self.plan.pk).update(
            receipts={f'old-{n}': '0' * 64 for n in range(200)})
        self.act('refresh_quote', expected=409)
        payload = self.payload('reconcile_purchase')
        with patch.object(RehearsalProvider, 'purchase_evidence', autospec=True,
                          side_effect=RehearsalProvider.purchase_evidence) as checks:
            for _ in range(2):
                self.assertEqual(self.client.post(self.url + 'action/', payload).status_code, 302)
            self.assertEqual(checks.call_count, 1)
        self.assertEqual(self.ticket().purchase_state, 'confirmed')
        self.assertEqual(self.ticket().approval_digest, original.approval_digest)
        self.assertEqual(self.ticket().request_id, original.request_id)
        self.assertEqual(len(self.plan.receipts), 200)
        self.assertNotIn('old-0', self.plan.receipts)
        old = dict(payload, version=0, nonce='old-0')
        self.assertEqual(self.client.post(self.url + 'action/', old).status_code, 409)

    def test_calendar_unknown_remains_reconcilable_at_receipt_cap_without_resend(self):
        self.book(calendar_scenario='unknown_then_confirmed')
        self.act('calendar')
        receipt = self.calendar().receipts[0]
        EventPlan.objects.filter(pk=self.plan.pk).update(
            receipts={f'old-{n}': '0' * 64 for n in range(200)})
        self.act('calendar', expected=409)
        self.act('reconcile_calendar', receipt=receipt.key)
        self.assertEqual(self.calendar().receipts[0].state, 'confirmed')
        self.assertEqual(self.plan.data['operations'][receipt.key]['send_attempts'], 1)
        self.assertEqual(len(self.plan.receipts), 200)
