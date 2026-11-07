"""Bounded disruption-watch adapter contract; no connected runtime or credentials.

Callers must load the current owned saved journey and persist returned state
atomically before delivering. This module never schedules or sends by itself.
Route/stop/trip identifiers are provider-scoped; no street address is required.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Protocol
from urllib.parse import urlsplit

WINDOW = timedelta(hours=2)
INTERVAL = timedelta(minutes=5)
FRESHNESS = timedelta(minutes=5)
SOURCE_HOSTS = {'transport.vic.gov.au', 'www.ptv.vic.gov.au'}
KINDS = {'cancellation', 'replacement_bus', 'station_closure', 'timetable_change', 'last_service', 'delay', 'other'}


def aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('An explicit aware time is required.')
    return value.astimezone(timezone.utc)


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class Leg:
    id: str  # outward or return
    departure: datetime
    arrival: datetime
    route_ids: tuple[str, ...]
    stop_ids: tuple[str, ...]
    trip_ids: tuple[str, ...] = ()
    carry_full_size_bike: bool = False

    def validate(self):
        if self.id not in ('outward', 'return') or not aware(self.departure) < aware(self.arrival) <= aware(self.departure) + timedelta(hours=12):
            raise ValueError('Bounded explicit leg required.')
        if not self.route_ids or len(self.route_ids) > 12 or len(self.stop_ids) > 40 or len(self.trip_ids) > 12:
            raise ValueError('Observed route and stop identities required.')
        if any(not isinstance(x, str) or not 0 < len(x) <= 100 for x in (*self.route_ids,*self.stop_ids,*self.trip_ids)):
            raise ValueError('Invalid provider identity.')
        return self


@dataclass(frozen=True)
class Journey:
    id: str
    revision: int
    owner_id: str
    status: str  # saved, confirmed, cancelled, or draft
    legs: tuple[Leg, ...]

    def validate(self):
        if not self.id or not self.owner_id or type(self.revision) is not int or self.revision < 0:
            raise ValueError('Owned versioned journey required.')
        if self.status not in ('saved', 'confirmed', 'cancelled', 'draft'):
            raise ValueError('Unknown journey state.')
        if not 1 <= len(self.legs) <= 2 or len({leg.id for leg in self.legs}) != len(self.legs):
            raise ValueError('One independent outward/return leg each required.')
        for leg in self.legs: leg.validate()
        return self


@dataclass(frozen=True)
class Notice:
    id: str
    kind: str
    planned: bool | None
    start: datetime
    end: datetime
    route_ids: tuple[str, ...]
    stop_ids: tuple[str, ...]
    trip_ids: tuple[str, ...]
    title: str
    consequence: str
    alternative: str  # source-backed guidance, or empty for a generic replan action
    source_url: str
    network_wide: bool = False

    def validate(self):
        url = urlsplit(self.source_url)
        if (not self.id or self.kind not in KINDS or type(self.planned) not in (bool,type(None))
                or not aware(self.start) < aware(self.end) or url.scheme != 'https'
                or url.hostname not in SOURCE_HOSTS or url.username or url.password or url.port
                or url.query or url.fragment or type(self.network_wide) is not bool):
            raise ValueError('Invalid source-backed notice.')
        if any(not isinstance(x,str) or len(x)>600 or any(ord(c)<32 for c in x)
               for x in (self.id,self.title,self.consequence,self.alternative)):
            raise ValueError('Invalid bounded notice text.')
        if not self.title or not self.consequence or len(self.route_ids)>40 or len(self.stop_ids)>100 or len(self.trip_ids)>40:
            raise ValueError('Notice lacks bounded actionable evidence.')
        return self

    def relevant(self, leg):
        if not aware(self.start) < aware(leg.arrival) or not aware(self.end) > aware(leg.departure):
            return False
        # Explicit trip selectors narrow a notice; route overlap alone cannot
        # turn another service's cancellation into this journey's cancellation.
        if self.trip_ids:
            return bool(set(self.trip_ids) & set(leg.trip_ids))
        return self.network_wide or bool(set(self.route_ids)&set(leg.route_ids) or set(self.stop_ids)&set(leg.stop_ids))


@dataclass(frozen=True)
class Feed:
    observed_at: datetime
    complete: bool  # False includes unavailable, truncated or unsupported coverage
    notices: tuple[Notice, ...]


class Scheduler(Protocol):
    connected: bool
    def replace(self, journey_id: str, revision: int, jobs: list[dict]):
        """Atomically replace this journey's jobs; idempotent on identical inputs."""


class Notifications(Protocol):
    connected: bool
    def send(self, *, key: str, owner_id: str, payload: dict): ...
    def lookup(self, *, key: str): ...  # read-only reconciliation, never resend


def schedule(journey, now, *, feed_connected=False, delivery_connected=False, scheduler=None):
    journey.validate(); now=aware(now)
    if not (feed_connected and delivery_connected and scheduler and scheduler.connected):
        return {'state':'blocked','jobs':[], 'reason':'Live feed, durable scheduler and owner delivery channel must all be connected and tested.'}
    jobs=[]
    if journey.status in ('saved','confirmed'):
        for leg in journey.legs:
            if now>=aware(leg.departure):continue
            jobs.append({'key':fingerprint([journey.id,journey.revision,leg.id]),'leg':leg.id,
                'start':max(now,aware(leg.departure)-WINDOW).isoformat(),'stop':aware(leg.departure).isoformat(),
                'interval_seconds':int(INTERVAL.total_seconds())})
    scheduler.replace(journey.id,journey.revision,jobs)
    return {'state':'scheduled' if jobs else 'inactive','jobs':jobs}


def evaluate(journey, state, now, feed):
    """Pure check result + pending receipts. Persist before calling deliver().

    The caller loads CURRENT journey revision before every tick; old scheduled
    jobs are ignored by their caller if its expected revision no longer matches.
    """
    journey.validate();now=aware(now)
    stamp=[journey.id,journey.revision,journey.owner_id]
    if state and state.get('binding')!=stamp:
        raise ValueError('Stale or another owner journey state.')
    result=deepcopy(state) if state else {'binding':stamp,'checked':{},'receipts':{},'latest':{},'active':{}}
    if journey.status not in ('saved','confirmed'):
        return result
    if len(result['receipts'])>200:
        raise ValueError('Receipt budget exceeded; retain and reconcile existing receipts.')
    usable=(type(feed.complete) is bool and feed.complete and
        timedelta(0)<=now-aware(feed.observed_at)<=FRESHNESS and len(feed.notices)<=100)
    if usable:
        for notice in feed.notices:notice.validate()
        if len({n.id for n in feed.notices})!=len(feed.notices):
            raise ValueError('Ambiguous notice identities.')
    for leg in journey.legs:
        if not aware(leg.departure)-WINDOW<=now<aware(leg.departure):continue
        previous=result['checked'].get(leg.id)
        if previous and now-datetime.fromisoformat(previous)<INTERVAL:continue
        result['checked'][leg.id]=now.isoformat()
        notices=[]
        if not usable:
            notices=[{'id':'feed-unavailable','kind':'unavailable','planned':None,
                'title':'Disruption information unavailable','consequence':'This leg cannot be checked reliably.',
                'alternative':'Open the official Journey Planner and check this exact leg before leaving.',
                'source_url':'https://transport.vic.gov.au/journey'}]
        else:
            for notice in feed.notices:
                if not notice.relevant(leg):continue
                material=asdict(notice);material['start']=aware(notice.start).isoformat();material['end']=aware(notice.end).isoformat()
                if notice.kind=='replacement_bus' and leg.carry_full_size_bike:
                    material['consequence']+=' A carried full-size bike cannot use a metropolitan replacement bus.'
                    material['alternative']='Revise the route or leave the bike parked and preserve the return to that station. Check the official planner; no alternative is verified.'
                elif not material['alternative']:
                    material['alternative']='Check the official planner for this leg; no alternative has been verified.'
                notices.append(material)
        current=set()
        for notice in notices:
            logical=leg.id+':'+notice['id'];version=fingerprint(notice)
            current.add(logical)
            payload=dict(notice,leg=leg.id,observed_at=aware(feed.observed_at).isoformat(),checked_at=now.isoformat())
            if result['latest'].get(logical)==version:
                receipt=result['receipts'][result['active'][logical]]
                if receipt['state']=='pending':receipt['payload']=payload
                continue
            if len(result['receipts'])>=200:raise ValueError('Notification receipt budget reached.')
            # An occurrence is a transition, not all historical appearances of
            # the same text: A -> B -> A must create a third delivery identity.
            key=fingerprint([stamp,logical,version,len(result['receipts'])+1])
            result['receipts'][key]={'state':'pending','payload':payload,'evidence':''}
            result['latest'][logical]=version
            result['active'][logical]=key
        for logical in list(result['active']):
            if logical.startswith(leg.id+':') and logical not in current:
                del result['active'][logical]
                del result['latest'][logical]
        eligible=set(result['active'].values())
        for key,receipt in result['receipts'].items():
            if receipt['payload']['leg']==leg.id and receipt['state']=='pending' and key not in eligible:
                receipt['state']='superseded'
    return result


def deliver(state, key, *, current_journey, now, adapter, persist):
    """Durable single dispatch; errors/unknown are reconciled by lookup only.

    persist must perform an owned CAS transaction and return before any send.
    The runtime adapter is intentionally absent until real delivery is selected.
    """
    current_journey.validate()
    if state['binding']!=[current_journey.id,current_journey.revision,current_journey.owner_id] or current_journey.status not in ('saved','confirmed'):
        raise ValueError('Changed or cancelled journey; do not notify.')
    receipt=state['receipts'][key]
    leg=next(leg for leg in current_journey.legs if leg.id==receipt['payload']['leg'])
    if not aware(leg.departure)-WINDOW<=aware(now)<aware(leg.departure):return state
    if not adapter.connected:raise ValueError('Notification delivery is not connected.')
    if receipt['state']!='pending':return state
    checked=state['checked'].get(leg.id)
    if (key not in state['active'].values() or not checked
            or not timedelta(0)<=aware(now)-datetime.fromisoformat(checked)<=FRESHNESS):
        return state
    reserved=deepcopy(state);reserved['receipts'][key]['state']='unknown'
    persist(reserved)  # Any conflict or storage failure prevents transmission.
    try:outcome=adapter.send(key=key,owner_id=current_journey.owner_id,payload=receipt['payload'])
    except (OSError,TimeoutError):return reserved
    if outcome.get('state')=='sent' and isinstance(outcome.get('evidence'),str) and 0<len(outcome['evidence'])<=200:
        done=deepcopy(reserved);done['receipts'][key].update(state='sent',evidence=outcome['evidence']);persist(done);return done
    return reserved


def reconcile(state,key,*,current_journey,adapter,persist):
    if state['binding'][0]!=current_journey.id or state['binding'][2]!=current_journey.owner_id:
        raise ValueError('Another owner journey receipt.')
    if not adapter.connected or state['receipts'][key]['state']!='unknown':return state
    outcome=adapter.lookup(key=key)
    if outcome.get('state')=='sent' and isinstance(outcome.get('evidence'),str) and 0<len(outcome['evidence'])<=200:
        done=deepcopy(state);done['receipts'][key].update(state='sent',evidence=outcome['evidence']);persist(done);return done
    return state


def preview_windows(departure, returning):
    """Local UI proposal only; cannot register a scheduler or send an alert."""
    return [{'leg':name,'start':aware(value)-WINDOW,'stop':aware(value)} for name,value in
            (('outward',departure),('return',returning)) if value is not None]
