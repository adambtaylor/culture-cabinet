"""24 Moons' current-week and later programme, with no seller-page reads."""
from collections import Counter
from datetime import datetime, timedelta
from hashlib import sha256
import re

from shelf.discovery_sources import SourceError, label, timestamp
from shelf.event_sources import (EventEvidence, ListingEvent, MELBOURNE,
    _date, occurrence_event, reconcile_events)
from shelf.event_listing_transport import MAX_BYTES
from shelf.rrr_events import _Listing, _one

LISTING_URL = 'https://24moons.com.au/events'
_DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_MONTHS = {name: index for index, name in enumerate(
    ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'), 1)}


class _Programme(_Listing):
    def captures_root(self, tag, attributes):
        return tag == 'div' and attributes.get('id') in ('this-week', 'further-upcoming')


def _week(value):
    match = re.fullmatch(r'(\d{1,2}) ([A-Z][a-z]{2}) [–—-] (\d{1,2}) ([A-Z][a-z]{2}) (\d{4}) · Melbourne time', value)
    if not match:
        raise SourceError('Unrecognized current-week programme range.')
    first, month, last, last_month, year = match.groups()
    # A year boundary without two explicit years remains unsupported. No rolling
    # a programme label into the current year or converting it into a recurrence.
    start = datetime(int(year), _MONTHS[month], int(first)).date()
    end = datetime(int(year), _MONTHS[last_month], int(last)).date()
    if (end-start).days != 6:
        raise SourceError('Invalid bounded programme week.')
    return start, end


def _times(value):
    match = re.fullmatch(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{1,2}) ([A-Z][a-z]{2}) (\d{4}) · '
        r'(\d{1,2}):(\d{2})(am|pm) [—–-] (Mon|Tue|Wed|Thu|Fri|Sat|Sun) '
        r'(\d{1,2}):(\d{2})(am|pm) (AEDT|AEST)', value)
    if not match:
        return None, None  # Unknown wording is never repaired from the week label.
    day, date, month, year, hour, minute, meridian, end_day, end_hour, end_minute, end_meridian, zone = match.groups()
    def clock(h, m, period):
        if not 1 <= int(h) <= 12 or not 0 <= int(m) <= 59:
            raise SourceError('Invalid source clock.')
        return int(h) % 12 + (12 if period == 'pm' else 0), int(m)
    start = datetime(int(year), _MONTHS[month], int(date), *clock(hour, minute, meridian))
    if _DAYS[start.weekday()] != day:
        raise SourceError('Source weekday disagrees with explicit date.')
    next_day = (_DAYS.index(end_day)-start.weekday()) % 7
    if next_day > 1:
        return None, None  # Only an explicitly adjacent overnight day is supported.
    end_date = start.date() + timedelta(days=next_day)
    end = datetime.combine(end_date, datetime.min.time()).replace(
        hour=clock(end_hour, end_minute, end_meridian)[0], minute=int(end_minute))
    if end <= start:
        raise SourceError('Source range ends before its start.')
    offset = '+11:00' if zone == 'AEDT' else '+10:00'
    # The explicit publisher offset must agree with Melbourne on BOTH days,
    # including DST transitions. Do not guess ambiguous local wall-clock times.
    return (_date(start.isoformat()+offset, 'Australia/Melbourne')[0],
            _date(end.isoformat()+offset, 'Australia/Melbourne')[0])


def parse_listing(raw, *, retrieved_at, provenance='live-automated'):
    now = timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw) > MAX_BYTES or now is None
            or provenance not in ('fixture', 'live-automated')):
        raise SourceError('Invalid bounded 24 Moons observation.')
    parser = _Programme()
    parser.feed(raw.decode('utf-8')); parser.close()
    if parser.stack or len(parser.columns) != 2:
        raise SourceError('Missing or changed 24 Moons programme sections.')
    by_id = {node.attrs['id']:node for node in parser.columns}
    if set(by_id) != {'this-week','further-upcoming'}:
        raise SourceError('Ambiguous 24 Moons programme sections.')
    week, later = by_id['this-week'], by_id['further-upcoming']
    if not week.matches(css='weekly-programme') or not later.matches(css='later-programme'):
        raise SourceError('Unrecognized 24 Moons programme layout.')
    try:
        first, last = _week(label(_one(week.find(css='this-week-meta')).text()))
    except (KeyError, ValueError):
        raise SourceError('Unrecognized current-week programme range.') from None
    week_nodes = week.find(css='this-week-event')
    later_nodes = later.find('a','content-card')
    nodes = week_nodes + later_nodes
    if len(nodes) > 48:
        raise SourceError('Programme card budget exceeded.')
    if not later_nodes:
        notice = label(_one(later.find(css='programme-empty')).text())
        if not notice.startswith('No upcoming events are listed yet.'):
            raise SourceError('Unrecognized later-programme state.')
    digest, events = sha256(raw).hexdigest(), []
    for position, node in enumerate(nodes, 1):
        anchor = _one(node.find('a','content-card'))
        if not anchor.matches(css='events'):
            raise SourceError('Unrecognized event card.')
        # this-week-title labels the weekday wrapper, not the event. Event
        # identity/name comes from the card itself; never promote Friday/Saturday
        # headings into factual programme names.
        title = label(_one(anchor.find('h3')).text())
        if title in ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'):
            raise SourceError('Programme day heading is not an event title.')
        listed = label(_one(anchor.find('time','meta')).text())
        try:
            start, end = _times(listed)
        except (KeyError, ValueError):
            raise SourceError('Invalid explicit programme date or timezone.') from None
        if start and position <= len(week_nodes) and not first <= timestamp(start).astimezone(MELBOURNE).date() <= last:
            raise SourceError('Card date is outside its explicit programme week.')
        # Cards point at external sellers, not official event-detail pages.
        # Keep the official programme as the source locator; no outbound seller
        # is fetched or promoted into source authority/stock evidence.
        evidence = EventEvidence('moons24',None,LISTING_URL,retrieved_at,provenance,digest,position)
        notices = node.find(css='event-tags')
        notice = label(' · '.join(
            text for item in notices for child in item.children
            if (text := (child.text() if hasattr(child, 'text') else child).strip()))) if notices else ''
        event = ListingEvent(key='fixture:event:pending', title=title, kind='event', source='24 Moons',
            provenance=provenance, rank=position, retrieved_at=retrieved_at,
            url=None if provenance == 'fixture' else LISTING_URL,
            source_id='moons24', official_url=LISTING_URL, source_start=start, source_end=end,
            starts_at=start, ends_at=end, event_timezone='Australia/Melbourne' if start else None,
            date_precision='time' if start else 'unknown', end_precision='time' if end else 'unknown',
            category='music', venue='24 Moons', locality='Northcote', evidence=(evidence,),
            listed_date=listed, source_notice=notice)
        events.append(event)
    # Simultaneous cards on one programme URL cannot be joined as one session.
    # Duplicate ranges retain snapshot/position identities rather than titles.
    counts = Counter((event.source_start,event.source_end) for event in events)
    events = [occurrence_event(event, unresolved=not event.source_start or
        counts[(event.source_start,event.source_end)] > 1) for event in events]
    eligible = [event for event in events if not event.starts_at or
        now < timestamp(event.ends_at) and timestamp(event.starts_at) <= now+timedelta(weeks=8)]
    if not eligible:
        today = now.astimezone(MELBOURNE).date()
        if events or not first <= today <= last:
            raise SourceError('No current leads on this stale or out-of-window programme.')
        if not _one(week.find(css='programme-empty')).text().startswith('No events'):
            raise SourceError('Unrecognized current-week empty state.')
    return reconcile_events(eligible)[:9]
