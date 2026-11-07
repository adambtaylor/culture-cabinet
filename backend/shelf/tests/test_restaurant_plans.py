"""Availability-first HTTP journeys; every restaurant and response is fictional."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import json
import re
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from shelf import event_plans, restaurant_plans
from shelf.acquisition_contract import Blocked, ContractError
from shelf.calendar_handoff import digest
from shelf.event_plan_provider import session
from shelf.models import EventPlan
from shelf.restaurant_provider import TableOffer, RehearsalRestaurantProvider, provider
from shelf.tests import test_event_plans as event_fixture

DEMO_OUTPUT = None


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class RestaurantPlanTests(TestCase):
    # Reuse the existing authenticated plan fixture, without rerunning its tests.
    setUp = event_fixture.EventPlanTests.setUp
    login = event_fixture.EventPlanTests.login
    start = event_fixture.EventPlanTests.start
    payload = event_fixture.EventPlanTests.payload
    act = event_fixture.EventPlanTests.act
    ticket = event_fixture.EventPlanTests.ticket
    calendar = event_fixture.EventPlanTests.calendar

    def restaurant_action(self, action, expected=302, **fields):
        response = self.client.post(self.url + 'restaurant/', self.payload(action, **fields))
        self.assertEqual(response.status_code, expected, response.content.decode()[:500])
        self.plan.refresh_from_db()
        return response

    def search(self, **changes):
        if not hasattr(self, 'plan'):
            self.assertEqual(self.start().status_code, 302)
        fields = dict(wish='  A relaxed dinner, then something surprising.  ', party_size=2,
            vegetarian='on', date_from='2027-01-15', date_to='2027-01-16', earliest='17:30',
            latest='18:30', preferred='18:30', meal_minutes=60, travel_minutes=20,
            buffer_minutes=10, scenario='confirmed')
        fields.update(changes)
        return self.restaurant_action('search', **fields)

    def choices(self):
        return restaurant_plans.candidates(self.plan.data['restaurant'], timezone.now())

    def choose(self, *, alternate=False):
        options = self.choices()
        option = next(r for r in options if (r['event'].session_id != self.calendar().event.session_id) == alternate)
        return self.restaurant_action('choose', offer_key=option['offer'].key)

    def test_finite_available_options_fit_buffers_and_preserve_exact_authored_wish(self):
        self.search(wish='  Dinner <script>fiction</script>  ')
        restaurant = self.plan.data['restaurant']
        self.assertEqual(restaurant['wish'], '  Dinner <script>fiction</script>  ')
        self.assertEqual(restaurant['wish_basis'], 'user_authored')
        choices = self.choices()
        self.assertLessEqual(len(choices), 3)
        self.assertEqual(choices[0]['offer'].start, '2027-01-16T18:30:00+11:00')
        self.assertTrue(any(r['availability'] == 'booked_out' for r in restaurant['observations']))
        self.assertTrue(all(r['offer'].availability == 'available' for r in choices))
        response = self.client.get(self.url)
        self.assertContains(response, '&lt;script&gt;fiction&lt;/script&gt;')
        self.assertNotContains(response, '<script>fiction</script>')
        self.assertContains(response, 'Live booking is not connected')
        self.assertContains(response, 'not verified routes')
        self.assertContains(response, 'meal total, drinks and transit fares unknown')

    def test_available_table_can_anchor_alternative_event_before_ticket_approval(self):
        self.search(); original_uid = self.calendar().uid
        self.choose(alternate=True)
        self.assertEqual(self.ticket().offer.event.session_id, 'fixture-session-16')
        self.assertEqual(self.calendar().event, self.ticket().offer.event)
        self.assertNotEqual(self.calendar().uid, original_uid)
        self.assertFalse(self.calendar().receipts)  # No earlier calendar event was dispatched.
        self.assertEqual(self.plan.data['selection']['session_id'], 'fixture-session-16')
        self.assertEqual(self.plan.active_key, event_plans.plan_key(self.actor.pk, self.calendar().event))
        self.assertEqual(EventPlan.objects.count(), 1)
        self.assertEqual(self.ticket().purchase_state, 'requested')

    def test_alternate_table_cannot_change_approved_event_or_collide_with_existing_plan(self):
        self.search(); self.act('approve_purchase')
        alternative = next(r for r in self.choices() if r['event'].session_id == 'fixture-session-16')
        self.restaurant_action('choose', expected=409, offer_key=alternative['offer'].key)
        self.assertEqual(self.calendar().event.session_id, 'fixture-session-15')
        self.act('refresh_quote')
        first, url = self.plan, self.url
        self.assertEqual(self.start(session_id='fixture-session-16').status_code, 302)
        self.plan, self.url = first, url
        self.restaurant_action('choose', expected=409, offer_key=alternative['offer'].key)
        self.assertEqual(self.calendar().event.session_id, 'fixture-session-15')
        self.assertEqual(EventPlan.objects.count(), 2)

    def test_expired_offer_cannot_be_approved_or_reserved(self):
        self.search(); self.choose()
        expiry = TableOffer(**self.plan.data['restaurant']['offer']).expires_at
        from shelf.calendar_handoff import instant
        with patch('shelf.restaurant_plans.timezone.now', return_value=instant(expiry)):
            self.restaurant_action('approve', expected=409)
        self.restaurant_action('approve')
        with patch('shelf.restaurant_plans.timezone.now', return_value=instant(expiry)):
            self.restaurant_action('reserve', expected=409)
        self.assertEqual(self.plan.data['restaurant']['state'], 'approved')
        self.assertNotIn('send_attempts', self.plan.data['restaurant'])

    def test_unknown_is_reconciled_with_same_key_and_never_resent_even_after_reload(self):
        self.search(scenario='unknown_then_confirmed'); self.choose(); self.restaurant_action('approve')
        payload = self.payload('reserve')
        original = RehearsalRestaurantProvider.reserve
        with patch.object(RehearsalRestaurantProvider, 'reserve', autospec=True, side_effect=original) as reserve:
            self.assertEqual(self.client.post(self.url + 'restaurant/', payload).status_code, 302)
            self.plan.refresh_from_db(); self.assertEqual(self.plan.data['restaurant']['state'], 'unknown')
            key = self.plan.data['restaurant']['request_key']
            self.assertEqual(self.client.post(self.url + 'restaurant/', payload).status_code, 302)
            self.restaurant_action('reserve', expected=409)
            self.login(); self.restaurant_action('reconcile')
            self.assertEqual(reserve.call_count, 1)
        restaurant = self.plan.data['restaurant']
        self.assertEqual((restaurant['state'], restaurant['send_attempts']), ('confirmed', 1))
        self.assertEqual(restaurant['evidence']['key'], key)
        self.assertEqual(self.ticket().purchase_state, 'requested')
        self.assertFalse(self.calendar().receipts)

    def test_crash_after_dispatch_stays_unknown_and_only_evidence_check_is_available(self):
        self.search(scenario='unknown_then_confirmed'); self.choose(); self.restaurant_action('approve')
        with patch.object(RehearsalRestaurantProvider, 'reserve', side_effect=OSError('fictional timeout')):
            self.restaurant_action('reserve', expected=503)
        self.assertEqual(self.plan.data['restaurant']['state'], 'unknown')
        self.search(expected=409)
        self.restaurant_action('reconcile')
        self.assertEqual(self.plan.data['restaurant']['state'], 'confirmed')

    def test_foreign_provider_receipt_does_not_confirm(self):
        self.search(); self.choose(); self.restaurant_action('approve')
        with patch.object(RehearsalRestaurantProvider, 'reserve', return_value={
                'fixture': True, 'state': 'confirmed', 'key': 'someone-else', 'offer_key': 'wrong'}):
            self.restaurant_action('reserve', expected=409)
        self.assertEqual(self.plan.data['restaurant']['state'], 'unknown')

    def test_failed_reservation_is_terminal_and_does_not_undo_tickets(self):
        self.search(scenario='failed'); self.choose(); self.restaurant_action('approve')
        self.restaurant_action('reserve')
        self.assertEqual(self.plan.data['restaurant']['state'], 'failed')
        self.restaurant_action('reconcile', expected=409)
        self.search(expected=409)
        self.assertEqual(self.ticket().purchase_state, 'requested')

    def test_search_refresh_discards_table_approval_and_does_not_change_exact_wish(self):
        self.search(); self.choose(); self.restaurant_action('approve')
        self.search(party_size=3, wish='Only my words')
        restaurant = self.plan.data['restaurant']
        self.assertEqual(restaurant['state'], 'draft')
        self.assertNotIn('approval', restaurant)
        self.assertNotIn('offer', restaurant)
        self.assertEqual(restaurant['wish'], 'Only my words')
        self.assertTrue(all(r['party_size'] == 3 for r in restaurant['observations']))

    def test_event_cancellation_flags_restaurant_without_claiming_cancellation_or_refund(self):
        self.search(); self.choose(); self.restaurant_action('approve'); self.restaurant_action('reserve')
        self.act('approve_purchase'); self.act('handoff'); self.act('reconcile_purchase')
        self.act('cancel_event')
        self.assertContains(self.client.get(self.url), 'no reservation has been updated, cancelled or refunded')
        self.assertEqual(self.plan.data['restaurant']['state'], 'confirmed')

    def test_unknown_hours_or_incompatible_timing_do_not_become_available_options(self):
        self.search(meal_minutes=150)
        self.assertEqual(self.choices(), [])
        self.assertContains(self.client.get(self.url), 'No current compatible tables')

    def test_diet_party_and_branch_identity_are_not_inferred(self):
        self.search(); restaurant = self.plan.data['restaurant']
        raw = deepcopy(restaurant['observations'][0]); raw['vegetarian'] = False
        raw['key'] = digest({k: v for k, v in raw.items() if k != 'key'})
        offer = TableOffer(**raw)
        self.assertFalse(offer.compatible(session(offer.session_id), restaurant['constraints']))
        self.assertFalse(offer.compatible(session(offer.session_id), dict(restaurant['constraints'], party_size=3)))
        with self.assertRaises(ContractError):
            replace(offer, branch_id='different-branch')
        with self.assertRaises(Blocked):
            provider('household').search(restaurant['constraints'], timezone.now())

    def test_csrf_grants_and_stale_preview_block_mutations(self):
        self.search(); payload = self.payload('choose', offer_key=self.choices()[0]['offer'].key)
        strict = Client(enforce_csrf_checks=True); strict.cookies = self.client.cookies
        self.assertEqual(strict.post(self.url + 'restaurant/', payload).status_code, 403)
        self.search()
        self.assertEqual(self.client.post(self.url + 'restaurant/', payload).status_code, 409)
        self.grant.can_select = False; self.grant.save()
        self.assertEqual(self.client.post(self.url + 'restaurant/', self.payload('approve')).status_code, 404)

    def test_malformed_form_and_changed_terms_are_rejected(self):
        self.search(); self.choose()
        self.restaurant_action('approve', expected=409, party_size=7)
        self.search(expected=409, date_from='2027-01-16', date_to='2027-01-15')
        data = deepcopy(self.plan.data); data['restaurant']['offer']['deposit'] = '0.00'
        self.plan.data = data; self.plan.save(update_fields=['data'])
        self.restaurant_action('approve', expected=409)

    def test_revoked_authority_leaves_unknown_and_receipt_cap_still_allows_reconciliation(self):
        self.search(scenario='unknown_then_confirmed'); self.choose(); self.restaurant_action('approve')
        original = RehearsalRestaurantProvider.reserve
        def revoke(adapter, *args):
            self.grant.can_select = False; self.grant.save()
            return original(adapter, *args)
        with patch.object(RehearsalRestaurantProvider, 'reserve', new=revoke):
            self.restaurant_action('reserve', expected=404)
        self.assertEqual(self.plan.data['restaurant']['state'], 'unknown')
        self.grant.can_select = True; self.grant.save()
        self.plan.receipts = {str(i): 'fixture' for i in range(200)}
        self.plan.save(update_fields=['receipts'])
        self.restaurant_action('reconcile')
        self.assertEqual(self.plan.data['restaurant']['state'], 'confirmed')
        self.assertEqual(len(self.plan.receipts), 200)

    def test_review_demo_availability_alternate_evening_and_uncertain_reservation(self):
        steps = []
        def capture(label):
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            steps.append({'step': label, 'restaurant_state': self.plan.data['restaurant']['state'],
                          'event_session': self.calendar().event.session_id})
            if DEMO_OUTPUT:
                html = re.sub(r'value="[^"]*"', 'value="redacted"', response.content.decode())
                html = html.replace('<button', '<button disabled').replace('<input', '<input disabled')
                (DEMO_OUTPUT / (label + '.html')).write_text(html)
        self.search(scenario='unknown_then_confirmed'); capture('01-availability')
        self.choose(alternate=True); capture('02-alternative-evening')
        self.restaurant_action('approve'); self.restaurant_action('reserve'); capture('03-uncertain-reservation')
        self.restaurant_action('reconcile'); capture('04-confirmed-rehearsal')
        if DEMO_OUTPUT:
            (DEMO_OUTPUT / 'journey.json').write_text(json.dumps({
                'mode': 'fictional rehearsal', 'external_actions': 0, 'steps': steps,
                'reservation': self.plan.data['restaurant']}, indent=2) + '\n')
