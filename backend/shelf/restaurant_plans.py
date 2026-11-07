"""One restaurant component in EventPlan, using its ownership and CAS receipts."""
from copy import deepcopy
from dataclasses import asdict, replace

from django.db import transaction
from django.utils import timezone

from shelf import event_plans
from shelf.acquisition_contract import Blocked
from shelf.calendar_handoff import digest
from shelf.discovery_state import Conflict, public_authorized
from shelf.event_plan_provider import provider as event_provider, session
from shelf.restaurant_provider import TableOffer, provider


def candidates(restaurant, now):
    result = []
    for raw in restaurant.get('observations', []):
        offer = TableOffer(**raw)
        event = session(offer.session_id)
        if (offer.availability == 'available' and offer.current(now)
                and offer.compatible(event, restaurant['constraints'])):
            result.append({'offer': offer, 'event': event})
    # A finite comparison, ordered by requested preference then date/time.
    preferred = restaurant['constraints']['preferred']
    result.sort(key=lambda r: (r['offer'].start[11:16] != preferred, r['offer'].start))
    return result[:3]


def apply(request, profile_id, plan_id, fields):
    external = None
    with transaction.atomic():
        plan = event_plans.owned(request, profile_id, plan_id)
        if plan.mode != 'sample':
            raise Blocked('Live restaurant booking is not connected.')
        fingerprint, nonce = digest(fields), fields['nonce']
        if nonce in plan.receipts:
            if plan.receipts[nonce] != fingerprint:
                raise Conflict('This form was already used for another action.')
            return plan
        if (fields['version'] != plan.version or nonce != plan.nonce
                or fields['preview'] != event_plans.preview_digest(plan)):
            raise Conflict('The plan changed. Review it before approving.')
        action = fields['action']
        if len(plan.receipts) >= 200 and action != 'reconcile':
            raise Blocked('Action limit reached. Existing outcome checks remain available.')
        data = deepcopy(plan.data)
        restaurant = data.get('restaurant', {'state': 'draft'})
        ticket, calendar, quote = event_plans.contracts(plan)
        event_anchor = event_plans.session_event(plan)
        now = timezone.now().replace(microsecond=0)
        state = restaurant['state']
        active_key = plan.active_key
        if action == 'search':
            if state in ('unknown', 'confirmed', 'failed'):
                raise Blocked('Retain the reservation receipt; submitted reservations cannot be replaced.')
            constraints = fields['constraints']
            # Only the in-process fictional adapter is enabled. No provider I/O
            # occurs while this transaction prepares the availability preview.
            observations = provider(plan.mode).search(constraints, now)
            restaurant = {'state': 'draft', 'wish': fields['wish'], 'wish_basis': 'user_authored',
                'constraints': constraints, 'scenario': fields['scenario'],
                'observations': observations, 'searched_at': now.isoformat()}
        elif action == 'choose':
            if state not in ('draft', 'selected', 'approved'):
                raise Blocked('A submitted reservation cannot be replaced.')
            matches = [r for r in candidates(restaurant, now) if r['offer'].key == fields['offer_key']]
            if len(matches) != 1:
                raise Blocked('Choose a current, compatible displayed table.')
            offer, event = matches[0]['offer'], matches[0]['event']
            if event != event_anchor:
                if ticket.purchase_state != 'requested' or calendar.receipts:
                    raise Blocked('Keep your approved or submitted event. Alternative dates need a separate review.')
                quote = event_provider(plan.mode).quote(event, ticket.offer.quantity, now)
                ticket = replace(ticket, offer=quote.offer, revision=ticket.revision + 1)
                calendar = replace(calendar, event=event)
                if data.get('outing'):
                    data['outing']['anchor'] = asdict(event)
                data['selection']['session_id'] = event.session_id
                active_key = event_plans.plan_key(request.user.pk, event)
                data.update(ticket=ticket.to_dict(), calendar=calendar.to_dict(), quote=quote.to_dict())
            restaurant.update(state='selected', offer=asdict(offer), event=asdict(event))
            restaurant.pop('approval', None)
        elif action in ('approve', 'reserve'):
            expected = 'selected' if action == 'approve' else 'approved'
            if state != expected:
                raise Blocked('Review the current reservation details first; do not resubmit an uncertain booking.')
            offer = TableOffer(**restaurant['offer'])
            if not offer.current(now) or not offer.compatible(event_anchor, restaurant['constraints']):
                raise Blocked('Table availability expired or the event changed. Refresh and review again.')
            approval = digest([restaurant['offer'], restaurant['constraints'], restaurant['event']])
            if action == 'approve':
                restaurant.update(state='approved', approval=approval)
            else:
                if restaurant.get('approval') != approval:
                    raise Blocked('Reservation approval changed.')
                key = digest(['restaurant-v1', str(plan.pk), approval])
                restaurant.update(state='unknown', request_key=key, send_attempts=1,
                                  submitted_at=now.isoformat())
                external = 'reserve'
        elif action == 'reconcile':
            if state != 'unknown':
                raise Blocked('Only an uncertain reservation needs reconciliation.')
            external = 'reconcile'
        else:
            raise Blocked('Unknown restaurant action.')
        data['restaurant'] = restaurant
        public_authorized(request, profile_id)
        receipts = {**plan.receipts, nonce: fingerprint}
        if action == 'reconcile':
            receipts = dict(list(receipts.items())[-200:])
        event_plans._save(plan, data, receipts=receipts, active_key=active_key)
    return _resolve(request, profile_id, plan, external) if external else plan


def _resolve(request, profile_id, plan, operation):
    # The single dispatch is already durable. Exceptions or conflicting changes
    # leave unknown, and the next action can only query this same request key.
    public_authorized(request, profile_id)
    restaurant = plan.data['restaurant']
    offer = TableOffer(**restaurant['offer'])
    adapter = provider(plan.mode)
    evidence = getattr(adapter, operation)(offer, restaurant['request_key'], restaurant['scenario'])
    with transaction.atomic():
        current = event_plans.owned(request, profile_id, plan.pk)
        if current.version != plan.version:
            raise Conflict('Plan changed while checking the reservation. Reconcile its saved receipt.')
        if (evidence.get('fixture') is not True
                or evidence.get('state') not in ('confirmed', 'failed', 'unknown')
                or evidence.get('key') != restaurant['request_key']
                or evidence.get('offer_key') != offer.key
                or evidence.get('reference') != 'fixture-reservation-' + restaurant['request_key'][:24]):
            raise Blocked('Reservation evidence does not match the exact request; outcome remains unknown.')
        data = deepcopy(current.data)
        data['restaurant'].update(state=evidence['state'], evidence=evidence)
        return event_plans._save(current, data)


def context(plan):
    restaurant = plan.data.get('restaurant')
    if not restaurant:
        return {'restaurant': None, 'restaurant_editable': True}
    now = timezone.now()
    event = event_plans.session_event(plan)
    offer = TableOffer(**restaurant['offer']) if restaurant.get('offer') else None
    return {'restaurant': restaurant, 'restaurant_offer': offer,
        'restaurant_editable': restaurant['state'] not in ('confirmed', 'failed', 'unknown'),
        'restaurant_options': candidates(restaurant, now),
        'restaurant_current': offer.current(now) if offer else False,
        'restaurant_compatible': offer.compatible(event, restaurant['constraints']) if offer else False}
