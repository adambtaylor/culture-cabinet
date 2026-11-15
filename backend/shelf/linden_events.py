"""One Linden current-events region; archive, details and images stay outside."""
from datetime import timedelta
from hashlib import sha256
import re
from urllib.parse import urljoin

from shelf.discovery_sources import SourceError, label, timestamp
from shelf.event_listing_transport import MAX_BYTES
from shelf.event_sources import (SOURCES, ListingEvent, MELBOURNE, _official_event,
                                 occurrence_event, reconcile_events)
from shelf.gallery_sources import _date, WEEKDAYS, MONTHS
from shelf.rrr_events import _Listing, _one

LISTING_URL = 'https://www.lindenarts.org/public-programs/events/'
_DATE = re.compile(r'^('+'|'.join(WEEKDAYS)+r') (\d{1,2}) ('+'|'.join(MONTHS)+
    r'),?(?: (\d{4})(?!\d))?(?: (.*))?$')
_CLOCK = r'(1[0-2]|[1-9]):([0-5][0-9])\s*(am|pm)'
_TIME = re.compile(r'^'+_CLOCK+r'\s*[-–]\s*'+_CLOCK+r'(?: (.*))?$', re.I)


class _Current(_Listing):
    def captures_root(self, tag, attrs):
        return tag == 'div' and attrs.get('id') == 's8-datarepeater1_ajaxcontainer'

    def handle_starttag(self, tag, attrs):
        # Observed Oncord image tags contain bare width/height attributes.
        # Images supply none of our fields: omit them entirely rather than
        # weakening the shared tree's ambiguity checks for useful markup.
        if tag != 'img':
            super().handle_starttag(tag, attrs)


def _words(node):
    # Source paragraphs separate date, local time and audience with <br> and
    # nested formatting; preserve those boundaries instead of joining 2026/11am.
    return ' '.join(' '.join(child if isinstance(child,str) else _words(child)
                            for child in node.children).split())


def _timing(text):
    match = _DATE.fullmatch(text)
    if not match:
        raise SourceError('Unrecognized Linden explicit date field.')
    weekday, day, month, year, rest = match.groups();rest = rest or ''
    date_label = f'{weekday} {day} {month}'+(f' {year}' if year else '')
    start = _date(day,month,year,weekday).isoformat() if year else None
    end = None
    clock = _TIME.fullmatch(rest)
    notice = rest
    if clock:
        h1,m1,p1,h2,m2,p2,notice = clock.groups();notice = notice or ''
        first = (int(h1)%12 + (12 if p1.lower()=='pm' else 0),int(m1))
        last = (int(h2)%12 + (12 if p2.lower()=='pm' else 0),int(m2))
        date_label += f' {h1}:{m1}{p1} - {h2}:{m2}{p2}'
        if start:
            day_iso = start
            start += f'T{first[0]:02}:{first[1]:02}:00'
            # The source supplies no following day for an overnight clock.
            # Retain its end label without inventing an end date in that case.
            if last > first:
                end = day_iso+f'T{last[0]:02}:{last[1]:02}:00'
    return start, end, label(date_label), notice


def parse_listing(raw, *, retrieved_at, provenance='live-automated'):
    now = timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw)>MAX_BYTES or now is None
            or provenance not in ('fixture','live-automated')):
        raise SourceError('Invalid bounded Linden listing.')
    parser = _Current();parser.feed(raw.decode('utf-8'));parser.close()
    if parser.stack or len(parser.columns)!=1:
        raise SourceError('Missing or truncated Linden current region.')
    cards = parser.columns[0].find('div','boxed')
    if not 1<=len(cards)<=48:
        raise SourceError('Missing or oversized current Linden cards.')
    digest = sha256(raw).hexdigest();events = []
    for position,node in enumerate(cards,1):
        title = label(_words(_one(node.find('h3'))))
        wording = label(_words(_one(node.find('p'))))
        start,end,listed,notice = _timing(wording)
        links = {urljoin(LISTING_URL,a.attrs.get('href','')) for a in node.find('a')}
        if len(links)!=1:
            raise SourceError('Ambiguous Linden source event link.')
        url = links.pop()
        if not re.fullmatch(r'https://www\.lindenarts\.org/event-rsvp/[a-z0-9][a-z0-9-]*/',url):
            raise SourceError('Exact public Linden event link required.')
        status = next((word for word in ('cancelled','postponed')
                       if re.search(r'\b'+word+r'\b',notice,re.I)),None)
        row = dict(kind='event',name=title,url=url,startDate=start,endDate=end,status=status)
        base = _official_event(row,'linden',SOURCES['linden'],retrieved_at,provenance,digest,position,False)
        events.append(occurrence_event(ListingEvent(**base.__dict__,listed_date=listed,source_notice=notice)))
    today = now.astimezone(MELBOURNE).date();latest = today+timedelta(weeks=8)
    eligible = []
    for event in reconcile_events(events):
        if event.status in ('cancelled','postponed','ended'):
            continue
        if event.source_start and not today.isoformat()<=event.source_start[:10]<=latest.isoformat():
            continue
        eligible.append(event)
    if not eligible:
        from shelf.event_scraping import ProgrammeUnavailable
        raise ProgrammeUnavailable
    return tuple(eligible[:9])
