"""One fixed Fringe listing; explicit runs/points, no archive rollover or fan-out."""
from datetime import date,timedelta
from hashlib import sha256
import re
from urllib.parse import urljoin
from shelf.discovery_sources import SourceError,label,timestamp
from shelf.event_listing_transport import MAX_BYTES
from shelf.event_sources import SOURCES,ListingEvent,MELBOURNE,_official_event,occurrence_event,reconcile_events
from shelf.gallery_sources import MONTHS
from shelf.rrr_events import _Listing,_one

LISTING_URL='https://www.melbournefringe.com.au/whats-on/events'
_MONTHS={name:index for index,name in enumerate(MONTHS,1)}
_MONTHS.update({name[:3]:index for index,name in enumerate(MONTHS,1)})


class FringeListing(ListingEvent):
    def date_status(self,now):
        if self.source_end:
            return 'Source show run: '+self.listed_date+'; individual performance sessions and attendance unverified.'
        return 'Source lists '+self.listed_date+'; timezone, venue, audience eligibility and attendance unverified.'


class _Cards(_Listing):
    def captures_root(self,tag,attrs):
        return tag=='article' and 'whats-on-event-tile' in attrs.get('class','').split()

    def handle_starttag(self,tag,attrs):
        if tag!='img':super().handle_starttag(tag,attrs)


def _dates(wording):
    ranged=re.fullmatch(r'(\d{1,2})(?: ([A-Z][a-z]+))?\s*[-–]\s*(\d{1,2}) ([A-Z][a-z]+) (\d{4})',wording)
    single=re.fullmatch(r'(\d{1,2}) ([A-Z][a-z]+) (\d{4})(?: (1[0-2]|[1-9]):([0-5][0-9])(am|pm))?',wording,re.I)
    try:
        if ranged:
            d1,m1,d2,m2,year=ranged.groups()
            first=date(int(year),_MONTHS[m1 or m2],int(d1));last=date(int(year),_MONTHS[m2],int(d2))
            if last<first:raise ValueError
            return first.isoformat(),last.isoformat()
        if single:
            day,month,year,hour,minute,meridian=single.groups()
            first=date(int(year),_MONTHS[month],int(day)).isoformat()
            if hour:first+=f'T{int(hour)%12+(12 if meridian.lower()=="pm" else 0):02}:{minute}:00'
            return first,None
    except (KeyError,ValueError):raise SourceError('Invalid explicit Fringe dates.') from None
    raise SourceError('Explicit supported Fringe year/date required.')


def parse_listing(raw,*,retrieved_at,provenance='live-automated'):
    now=timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw)>MAX_BYTES or now is None
            or provenance not in ('fixture','live-automated')):
        raise SourceError('Invalid bounded Fringe listing.')
    parser=_Cards();parser.feed(raw.decode('utf-8'));parser.close()
    if parser.stack or not 1<=len(parser.columns)<=48:
        raise SourceError('Missing, changed or oversized Fringe listing.')
    events=[];digest=sha256(raw).hexdigest()
    for position,node in enumerate(parser.columns,1):
        title=label(_one(node.find('h3','event-card-heading')).text())
        # Hover panels reuse the subtitle class for artist/venue prose. Only
        # the observed plain subtitle field is the explicit date label.
        labels={label(part.text()) for part in node.find('div','event-card-subtitle')
                    if part.attrs.get('class','').split()==['event-card-subtitle']}
        # The actual first card repeats the plain date field. Permit repeated
        # labels only when their text agrees; conflicting dates still fail.
        if len(labels)!=1:raise SourceError('Missing or conflicting Fringe date labels.')
        wording=labels.pop();first,last=_dates(wording)
        urls={urljoin(LISTING_URL,anchor.attrs.get('href','')) for anchor in node.find('a')}
        urls={url for url in urls if re.fullmatch(r'https://www\.melbournefringe\.com\.au/whats-on/events/[a-z0-9][a-z0-9-]*/?',url)}
        if len(urls)!=1:raise SourceError('Exact unambiguous Fringe event URL required.')
        base=_official_event(dict(kind='event',name=title,url=urls.pop(),startDate=first,endDate=last),
            'fringe',SOURCES['fringe'],retrieved_at,provenance,digest,position,False)
        values=dict(base.__dict__,starts_at=None,ends_at=None)
        events.append(occurrence_event(FringeListing(**values,listed_date=wording)))
    today=now.astimezone(MELBOURNE).date();latest=today+timedelta(weeks=8)
    eligible=[event for event in reconcile_events(events)
        if today.isoformat()<=(event.source_end or event.source_start)[:10]
        and event.source_start[:10]<=latest.isoformat()]
    if not eligible:
        from shelf.event_scraping import ProgrammeUnavailable
        raise ProgrammeUnavailable
    return tuple(eligible[:9])
