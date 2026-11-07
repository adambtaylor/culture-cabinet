"""Saved real-source planning holds; no invented purchase, quote or attendance."""
from copy import deepcopy
import secrets
import uuid

from django.core import signing
from django.db import transaction
from django.utils import timezone

from shelf import event_plans, google_handoff, transport_planning
from shelf.acquisition_contract import Blocked
from shelf.calendar_handoff import CalendarHandoff, Event, digest, instant
from shelf.discovery_state import Conflict, public_authorized
from shelf.models import EventPlan
from shelf.beat_events import BeatEvent

KIND='source_planning_hold'


def is_hold(plan):return plan.mode=='household' and plan.data.get('kind')==KIND


def source_facts(event):
    if type(event) is not BeatEvent or event.provenance!='live-automated' or not event.evidence:
        raise Blocked('A current real-source listing is required; rehearsal cannot be promoted.')
    return {'key':event.key,'title':event.title,'date':event.starts_at,'venue':event.venue,
        'url':event.url,'retrieved_at':event.retrieved_at,'listing_digest':event.evidence[0].snapshot_sha256,
        'show':event.details.show_time if event.details else None,
        'doors':event.details.doors_time if event.details else None,
        'address':event.details.address if event.details else '',
        'detail_digest':event.details.snapshot_sha256 if event.details else None}


def active_key(request,profile_id,event):
    return digest(['source-hold-v1',request.user.pk,profile_id,event.key,event.starts_at])


def token(request,profile_id,event):
    key=active_key(request,profile_id,event)
    plan=EventPlan.objects.filter(active_key=key).first()
    data=[request.user.pk,profile_id,key,digest(source_facts(event)),plan.version if plan else None,plan.nonce if plan else None]
    return signing.dumps(data,salt='source-outing-hold'),plan


def save(request,profile_id,event,proposal,provided_token):
    public_authorized(request,profile_id)
    if proposal['blocked'] or not proposal['depart']:
        raise Blocked('Resolve the source time and bicycle constraints before saving this planning hold.')
    if proposal['depart']<=timezone.now():raise Blocked('Departure is already past; choose a future outing.')
    facts=source_facts(event);key=active_key(request,profile_id,event)
    try:bound=signing.loads(provided_token,salt='source-outing-hold',max_age=900)
    except signing.BadSignature:raise Conflict('Planning preview expired. Read the source again.') from None
    if bound[:4]!=[request.user.pk,profile_id,key,digest(facts)]:raise Conflict('Source or owner changed. Review again.')
    values={k:v for k,v in proposal['values'].items() if k not in ('cycle_origin','hold_token','save_confirmed')}
    fingerprint=digest([facts,values])
    with transaction.atomic():
        public_authorized(request,profile_id)
        plan=EventPlan.objects.filter(active_key=key).first()
        if plan and plan.data.get('selection_digest')==fingerprint:return plan
        if bound[4:]!=([plan.version,plan.nonce] if plan else [None,None]):raise Conflict('A newer outing edit exists.')
        if plan and plan.data['handoffs']:
            raise Blocked('A connected-tool handoff was reserved. Reconcile it before changing this saved plan; updates require the recorded provider identity.')
        now=timezone.now().replace(microsecond=0)
        identity=str(plan.pk) if plan else str(uuid.uuid4())
        event_id='source-'+digest(event.key)[:40]
        subject='Planning hold — '+facts['title']
        description='\n'.join([
            subject+' — UNBOOKED. Saving or importing this is not a reservation or ticket.',
            'Source: '+facts['url']+'; read '+facts['retrieved_at']+'. Archived planning reference; current cancellation/availability must be rechecked.',
            'Venue: '+(facts['venue'] or 'unknown')+'. Address: '+(facts['address'] or 'unknown')+'.',
            'Show: '+proposal['start'].strftime('%a %d %b %H:%M %Z')+' ('+proposal['start_basis']+'). Doors: '+(facts['doors'] or 'unknown')+'. Finish unknown.',
            'Your public station plan: '+proposal['origin']+' to '+proposal['arrival']+'; venue connection unverified.',
            'Leave by '+proposal['depart'].strftime('%a %d %b %H:%M %Z')+'; includes '+str(values['outward_minutes'])+' estimated minutes plus handling and arrival buffer.',
            'Preferred venue departure '+proposal['leave'].strftime('%a %d %b %H:%M %Z')+'; estimated return '+proposal['return_arrival'].strftime('%a %d %b %H:%M %Z')+'. This is not the event finish or last service.',
            'Bike: '+dict(transport_planning.BIKES)[values['bike']]+'; carried on PT: '+('yes' if values['carry_bike'] else 'no')+'. '+proposal['returning']['message'],
            ('Companion public origin: '+dict(transport_planning.ORIGINS)[values['companion_origin']]+'; estimated departure '+proposal['companion_depart'].strftime('%a %d %b %H:%M %Z')+'. No invitation or participation is confirmed.') if proposal['companion_depart'] else 'No separate companion journey selected.',
            'No table, ticket PDF, Wallet pass, invitation or live disruption alert exists for this hold.',
            'Check timed travel and disruptions: '+proposal['journey_url']])
        calendar_event=Event(event_id,event_id+'-'+facts['date'],subject,
            min(d for d in (proposal['depart'],proposal['companion_depart']) if d).replace(microsecond=0).isoformat(),proposal['return_arrival'].replace(microsecond=0).isoformat(),
            'Australia/Melbourne',facts['venue'] or 'Venue unknown',facts['url'],
            'Editorial listing plus explicit user travel estimates; not an issuer-confirmed session.',status='tentative',source_verified=False)
        calendar=CalendarHandoff.from_dict(plan.data['calendar']).revise(calendar_event,
            actor_id=str(request.user.pk),expected_revision=CalendarHandoff.from_dict(plan.data['calendar']).revision,
            modified_at=now.isoformat(),description=description) if plan else CalendarHandoff(
            str(request.user.pk),identity,calendar_event,now.isoformat(),description=description)
        data={'kind':KIND,'source':facts,'values':values,'selection_digest':fingerprint,'calendar':calendar.to_dict(),
            'handoffs':{},'booked':False,'precise_origin_retained':False}
        if plan:return event_plans._save(plan,data)
        return EventPlan.objects.create(id=identity,actor=request.user,profile_id=profile_id,mode='household',
            active_key=key,nonce=secrets.token_urlsafe(24),data=data)


def owned(request,profile_id,plan_id):
    plan=event_plans.owned(request,profile_id,plan_id)
    if not is_hold(plan):raise Blocked('Real source planning hold required.')
    return plan


def apply(request,profile_id,plan_id,fields):
    """Exports are local files; reserve returns one supervised tool write packet."""
    with transaction.atomic():
        plan=owned(request,profile_id,plan_id)
        fingerprint=digest(fields);nonce=fields['nonce'];action=fields['action']
        if nonce in plan.receipts:
            if plan.receipts[nonce]!=fingerprint:raise Conflict('Form already used for a different action.')
            if action in ('reserve_calendar','reserve_email'):
                kind=action.removeprefix('reserve_');receipt=plan.data['handoffs'][kind]
                return plan,{'reconciliation_only':True,**google_handoff.reconciliation(receipt['packet'])}
            exported=plan.data.get('exports',{}).get(nonce)
            if exported!={'kind':action,'revision':plan.data['calendar']['revision']}:
                raise Conflict('The outing changed after this export. Review the current packet.')
            return plan,None
        if fields['version']!=plan.version or nonce!=plan.nonce or fields['preview']!=event_plans.preview_digest(plan):
            raise Conflict('The saved hold changed. Review it again.')
        if len(plan.receipts)>=200:raise Blocked('Local action limit reached.')
        calendar=CalendarHandoff.from_dict(plan.data['calendar']);data=deepcopy(plan.data)
        result=None
        if action in ('ics','eml'):
            if action=='ics':
                view=calendar.preview()
                calendar,receipt=calendar.approve(actor_id=str(request.user.pk),expected_revision=calendar.revision,
                    preview_digest=view['digest'],key='hold-export-'+view['digest'][:32],user_confirmed=True)
                result=calendar.export_ics(receipt.key,actor_id=str(request.user.pk));data['calendar']=calendar.to_dict()
            else:result=google_handoff.email_file(calendar)
            data.setdefault('exports',{})[nonce]={'kind':action,'revision':calendar.revision}
        elif action in ('reserve_calendar','reserve_email'):
            kind=action.removeprefix('reserve_')
            if kind in data['handoffs']:
                return plan,{'reconciliation_only':True,**google_handoff.reconciliation(data['handoffs'][kind]['packet'])}
            if instant(calendar.event.start)<=timezone.now():raise Blocked('Departure passed; do not create a stale hold.')
            key=digest([str(plan.pk),calendar.revision,kind,plan.nonce])
            packet=google_handoff.command(calendar,kind,key)
            data['handoffs'][kind]={'state':'reserved_unknown','packet':packet,'attempts':1,
                'reserved_at':timezone.now().isoformat(),'observed_receipt':None}
            result=packet
        else:raise Blocked('Unknown planning-hold action.')
        public_authorized(request,profile_id)
        event_plans._save(plan,data,receipts={**plan.receipts,nonce:fingerprint})
        return plan,result


def record_operator(*,actor,profile_id,plan_id,kind,key,evidence,session_guard=None):
    """Trusted SSH operator after actual connected-tool read-back; no web ingress."""
    with transaction.atomic():
        from shelf.provider_views import _profiles
        if session_guard is not None:session_guard()
        if not _profiles(actor,'select').filter(pk=profile_id).exists():raise Blocked('Current owner grant required.')
        plan=EventPlan.objects.get(pk=plan_id,actor=actor,profile_id=profile_id,mode='household')
        if not is_hold(plan):raise Blocked('Real planning hold required.')
        receipt=plan.data['handoffs'].get(kind)
        if not receipt or receipt['packet']['key']!=key:raise Blocked('Exact reserved handoff required.')
        verified=google_handoff.verify_readback(receipt['packet'],evidence)
        if receipt['observed_receipt']:
            if receipt['observed_receipt']!=verified:raise Conflict('Conflicting provider evidence retained for operator reconciliation.')
            return plan
        if receipt['state']!='reserved_unknown':raise Blocked('No unknown handoff to reconcile.')
        if session_guard is not None:session_guard()
        if not _profiles(actor,'select').filter(pk=profile_id).exists():raise Blocked('Current owner grant required.')
        data=deepcopy(plan.data);data['handoffs'][kind].update(state=verified['state'],observed_receipt=verified,
            recorded_at=timezone.now().isoformat())
        return event_plans._save(plan,data)
