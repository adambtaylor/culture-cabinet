"""One outing packet inside EventPlan; no routing, calendar or mail network I/O.

The existing CalendarHandoff owns the single master busy block. Its description
is common to all invitees. Private travel/tickets belong only in addressed mail
packets, never in that shared description. Rehearsal is the only enabled mode.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.db import transaction
from django.utils import timezone

from shelf import event_plans
from shelf.acquisition_contract import Blocked, ContractError
from shelf.calendar_handoff import Event, digest, https_url, instant
from shelf.discovery_state import Conflict, public_authorized
from shelf.event_plan_provider import resolve_contacts
from shelf.restaurant_provider import TableOffer

MODES = {'cycle': 'Cycle the whole route', 'pt': 'Public transport', 'bike_train': 'Bike + train',
         'uber': 'Uber / rideshare', 'drive': 'Drive'}
ORIGINS = {'batman': 'Batman Station, Melbourne', 'coburg': 'Coburg Station, Melbourne',
           'merlynston': 'Merlynston Station, Melbourne', 'flinders': 'Flinders Street Station, Melbourne',
           'richmond': 'Richmond Station, Melbourne'}
OWNER = 'owner'
OWNER_MAILBOX = 'owner@example.invalid'
EXAMPLE_MEETING = 'Federation Square, Melbourne'


def participants(plan):
    return [{'id': OWNER, 'label': 'You (fictional owner)', 'address': OWNER_MAILBOX},
            *[{'id': r.recipient_id, 'label': r.label, 'address': r.address}
              for r in resolve_contacts(plan.data['selection']['contacts'])]]


def anchor(plan):
    return Event(**plan.data['outing']['anchor']) if plan.data.get('outing') else event_plans.contracts(plan)[1].event


def source_digest(plan):
    ticket, _, quote = event_plans.contracts(plan)
    return digest([asdict(anchor(plan)), plan.data.get('restaurant'), plan.data.get('ticket_documents', []),
        ticket.purchase_state, ticket.order_reference, quote.to_dict(), participants(plan)])


def current(plan):
    return bool(plan.data.get('outing') and plan.data['outing']['source_digest'] == source_digest(plan))


def require_current(plan):
    if not current(plan):
        raise Blocked('The outing packet is missing or stale. Rebuild and review its current sources first.')


def require_settled(plan):
    _, calendar, _ = event_plans.contracts(plan)
    if any(r.state == 'submitted_unknown' for r in calendar.receipts) or any(
            r.get('kind') == 'packet_email' and r['state'] == 'unknown' for r in plan.data['operations'].values()):
        raise Blocked('Reconcile unknown calendar or email outcomes before changing this outing.')


def map_link(origin, destination, mode):
    # Official Maps URL handoff only. Bike+train requires separately checking
    # cycling and transit legs; Maps does not promise a bike-carrying itinerary.
    return 'https://www.google.com/maps/dir/?' + urlencode({'api': '1', 'origin': origin,
        'destination': destination, 'travelmode': {'cycle':'bicycling','pt':'transit',
        'bike_train':'transit','uber':'driving','drive':'driving'}[mode]})


def validate_choices(plan, choices):
    if set(choices) != {'arrival_buffer', 'dinner_minutes', 'transfer_minutes', 'drink_minutes', 'return_buffer', 'people'}:
        raise Blocked('Complete bounded outing choices required.')
    for field in ('arrival_buffer','dinner_minutes','transfer_minutes','drink_minutes','return_buffer'):
        if type(choices[field]) is not int or not 0 <= choices[field] <= 180:
            raise Blocked('Invalid outing allowance.')
    if set(choices['people']) != {p['id'] for p in participants(plan)}:
        raise Blocked('Choose travel separately for each selected participant.')
    for row in choices['people'].values():
        if set(row) != {'origin','out_mode','return_mode','out_minutes','return_minutes','handling_minutes','park_bike','parking','fare'}:
            raise Blocked('Unexpected travel choices.')
        if row['origin'] not in ORIGINS or row['out_mode'] not in MODES or row['return_mode'] not in MODES:
            raise Blocked('Choose the displayed example origins and modes.')
        for name in ('out_minutes','return_minutes','handling_minutes'):
            if type(row[name]) is not int or not (0 if name=='handling_minutes' else 1) <= row[name] <= 240:
                raise Blocked('Bounded travel estimates required.')
        if type(row['park_bike']) is not bool or type(row['parking']) is not str or len(row['parking'])>200 or any(ord(c)<32 for c in row['parking']):
            raise Blocked('Invalid storage or parking assumption.')
        if row['fare'] != '':
            try: value=Decimal(row['fare'])
            except (InvalidOperation, TypeError, ValueError): raise Blocked('Use a finite AUD estimate.') from None
            if not value.is_finite() or not 0 <= value <= 10000 or value.as_tuple().exponent < -2:
                raise Blocked('Use a finite AUD estimate with cents.')
        out,back=row['out_mode'],row['return_mode']
        if (out=='drive') != (back=='drive'):
            raise Blocked('This preview requires return to the parked car; choose driving in both directions.')
        if row['park_bike']:
            if out!='bike_train' or back!='bike_train':
                raise Blocked('A parked-bike plan must return by bike + train to the same origin station.')
        elif (out in ('cycle','bike_train')) != (back in ('cycle','bike_train')):
            raise Blocked('Keep the bicycle with you both ways or use the parked-bike option.')


def ticket_documents(plan, audience, now):
    """Read trusted provider metadata only; never manufacture PDFs or fetch URLs.

    No browser form can write this collection. A future issuer adapter must
    authenticate the order, audience permission, MIME/hash/size and link expiry.
    """
    ticket=event_plans.contracts(plan)[0]
    result=[]
    for item in plan.data.get('ticket_documents', [])[:20]:
        if (item.get('audience') != audience or item.get('issuer_verified') is not True
                or item.get('order_reference') != ticket.order_reference or ticket.purchase_state!='confirmed'
                or item.get('status')!='available' or not item.get('receipt_reference')):
            continue
        if (item.get('mime')!='application/pdf' or type(item.get('bytes')) is not int or not 0<item['bytes']<=20_000_000
                or type(item.get('sha256')) is not str or len(item['sha256'])!=64
                or any(c not in '0123456789abcdef' for c in item['sha256'])):
            continue
        try:
            url=https_url(item['url'],private=True)
            if instant(item['expires_at'])<=now:continue
        except (KeyError,ContractError):continue
        name=item.get('filename')
        if not isinstance(name,str) or not name.endswith('.pdf') or len(name)>160 or any(ord(c)<32 for c in name):continue
        result.append({'filename':name,'mime':item['mime'],'bytes':item['bytes'],'sha256':item['sha256'],
            'url':url,'expires_at':item['expires_at'],'receipt_reference':item['receipt_reference']})
    return result


def build(plan, choices, now):
    if plan.mode!='sample':raise Blocked('Live outing source and delivery adapters are not connected.')
    validate_choices(plan,choices)
    event=anchor(plan)
    if not event.start or not event.end or not event.timezone:raise Blocked('Exact session times and timezone required.')
    start,end=instant(event.start),instant(event.end)
    restaurant=plan.data.get('restaurant',{})
    offer=TableOffer(**restaurant['offer']) if restaurant.get('offer') else None
    dinner_end=start-timedelta(minutes=choices['arrival_buffer']+choices['transfer_minutes'])
    dinner_start=dinner_end-timedelta(minutes=choices['dinner_minutes'])
    meal_label='Meal venue unselected; time allowance only'
    if offer:
        if offer.session_id!=event.session_id or instant(offer.end)+timedelta(minutes=choices['transfer_minutes']+choices['arrival_buffer'])>start:
            raise Blocked('Selected table conflicts with this session or travel buffers. Revise the meal first.')
        dinner_start,dinner_end=instant(offer.start),instant(offer.end)
        meal_label=f'{offer.branch} — {restaurant["state"]} (simulated); {offer.address}'
    leave=end+timedelta(minutes=choices['drink_minutes']+choices['return_buffer'])
    legs=[]
    for person in participants(plan):
        row=choices['people'][person['id']]
        depart=dinner_start-timedelta(minutes=row['out_minutes']+row['handling_minutes'])
        home=leave+timedelta(minutes=row['return_minutes']+row['handling_minutes'])
        origin=ORIGINS[row['origin']]
        legs.append({**person,**row,'origin_label':origin,'out_label':MODES[row['out_mode']],
            'return_label':MODES[row['return_mode']],'depart':depart.isoformat(),'home':home.isoformat(),
            'out_map':map_link(origin,EXAMPLE_MEETING,row['out_mode']),
            'return_map':map_link(EXAMPLE_MEETING,origin,row['return_mode'])})
    occupied_start=min(instant(r['depart']) for r in legs);occupied_end=max(instant(r['home']) for r in legs)
    if occupied_end-occupied_start>timedelta(hours=18):raise Blocked('Keep the outing within eighteen hours.')
    shared=[f'REHEARSAL ONLY — {event.title}',f'Occupied block: {occupied_start.isoformat()} to {occupied_end.isoformat()} ({event.timezone}).',
        f'Meal: {dinner_start.isoformat()} to {dinner_end.isoformat()}; {meal_label}.',
        f'Transfer allowance {choices["transfer_minutes"]} min; event arrival buffer {choices["arrival_buffer"]} min.',
        f'Event starts {event.start}; finishes {event.end}. Doors unknown. Venue: {event.location}.',
        f'Optional drink allowance {choices["drink_minutes"]} min; venue unselected. Return begins {leave.isoformat()} after {choices["return_buffer"]} min buffer.',
        f'Session status: {event.status}. Source: {event.official_source}',
        'All travel times are supplied estimates. Timed services, disruptions, weather, parking and fares are unverified. Alerts inactive.',
        'One master busy block covers the earliest departure to latest return; private origins, other mailboxes and tickets are excluded.',
        'Public example meeting point for map handoff: '+EXAMPLE_MEETING+'. This is not a real concert venue assertion.']
    if event.status=='cancelled':shared.insert(1,'CANCELLED — cancel the existing master event; no refund is implied.')
    packet={'anchor':asdict(event),'choices':deepcopy(choices),'people':legs,'shared':'\n'.join(shared),
        'occupied_start':occupied_start.isoformat(),'occupied_end':occupied_end.isoformat(),
        'dinner_start':dinner_start.isoformat(),'dinner_end':dinner_end.isoformat(),'meal_label':meal_label,
        'return_departure':leave.isoformat(),'source_digest':source_digest(plan)}
    # Bind private route/attachment/recipient changes without exposing their text
    # in the description shared with all invitees.
    packet['content_digest']=digest([packet,plan.data.get('ticket_documents',[])])
    description=packet['shared']+'\nPacket revision fingerprint: '+packet['content_digest']
    calendar=event_plans.contracts(plan)[1]
    master=replace(event,title=event.title+' — full outing',start=packet['occupied_start'],end=packet['occupied_end'])
    calendar=calendar.revise(master,actor_id=str(plan.actor_id),expected_revision=calendar.revision,
        modified_at=now.isoformat(),description=description)
    return packet,calendar


def apply(request,profile_id,plan_id,fields):
    with transaction.atomic():
        plan=event_plans.owned(request,profile_id,plan_id)
        if plan.mode!='sample':raise Blocked('Live outing adapters are not connected.')
        fingerprint=digest(fields);nonce=fields['nonce']
        if nonce in plan.receipts:
            if plan.receipts[nonce]!=fingerprint:raise Conflict('This form was used for another action.')
            return plan
        if (fields['version']!=plan.version or nonce!=plan.nonce or fields['preview']!=event_plans.preview_digest(plan)):
            raise Conflict('The outing changed. Review the current version.')
        if len(plan.receipts)>=200:raise Blocked('Plan action limit reached.')
        require_settled(plan)
        packet,calendar=build(plan,fields['choices'],timezone.now().replace(microsecond=0))
        data=deepcopy(plan.data);data.update(outing=packet,calendar=calendar.to_dict())
        public_authorized(request,profile_id)
        return event_plans._save(plan,data,receipts={**plan.receipts,nonce:fingerprint})


def email_preview(plan,audience):
    require_current(plan)
    packet=plan.data['outing'];person=next((p for p in packet['people'] if p['id']==audience),None)
    if not person:raise Blocked('Recipient not selected for this outing.')
    now=timezone.now().replace(microsecond=0)
    documents=ticket_documents(plan,audience,now)
    private=[packet['shared'],f'Your travel: {person["origin_label"]}; depart {person["depart"]}; return {person["home"]}.',
        f'Outward: {person["out_label"]}, {person["out_minutes"]} estimated minutes. Return: {person["return_label"]}, {person["return_minutes"]} estimated minutes.',
        f'Handling allowance each way: {person["handling_minutes"]} min. Parking/storage assumption: {person["parking"] or "not supplied"}.',
        ('Return to the same origin station to collect the bike.' if person['park_bike'] else 'Any carried bike needs permitted carriage and space; no tram or replacement-bus route is verified.'),
        f'Your transport estimate: AUD {person["fare"] or "unknown"}; not a rideshare quote or booking.',
        'Outward map handoff: '+person['out_map'],'Return map handoff: '+person['return_map']]
    if audience==OWNER:
        ticket,_,quote=event_plans.contracts(plan)
        private.extend([f'Owner booking reference: {ticket.order_reference or "unconfirmed"}.',
            f'Ticket total AUD {ticket.offer.total}; {quote.refund_terms}',
            'Whole-night cost is incomplete: meal, drinks and transport are estimates or unknown.'])
        restaurant=plan.data.get('restaurant',{})
        if restaurant.get('offer'):
            offer=restaurant['offer'];private.extend([offer['cancellation_terms'],
                'Reservation reference: '+restaurant.get('evidence',{}).get('reference','unconfirmed')])
    private.append('Ticket PDFs: '+('recipient-authorized issuer metadata below; delivery/attachment support not connected.' if documents else 'unavailable — no verified current PDF/link for this recipient.'))
    payload={'to':person['address'],'recipient_id':audience,'subject':'REHEARSAL outing: '+anchor(plan).title,
        'body':'\n'.join(private),'attachments':documents,'calendar_uid':event_plans.contracts(plan)[1].uid,
        'calendar_revision':event_plans.contracts(plan)[1].revision,'fixture':True}
    return {**payload,'digest':digest(payload)}


def context(plan):
    packet=plan.data.get('outing')
    if not packet:return {'outing':None,'outing_current':False,'packet_emails':[]}
    ready=current(plan)
    emails=[]
    if ready:
        for person in participants(plan):
            view=email_preview(plan,person['id'])
            receipt=next((r for r in plan.data['operations'].values() if r.get('kind')=='packet_email' and r['payload']['digest']==view['digest']),None)
            emails.append({'person':person,'preview':view,'receipt':receipt})
    return {'outing':packet,'outing_current':ready,'packet_emails':emails,
        'packet_email_unknown':[r for r in plan.data['operations'].values() if r.get('kind')=='packet_email' and r['state']=='unknown']}
