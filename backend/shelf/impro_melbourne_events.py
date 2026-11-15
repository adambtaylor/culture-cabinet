"""The explicit 2026 season's Now Playing show runs, never invented sessions."""
from dataclasses import dataclass
from datetime import date,timedelta
from hashlib import sha256
import re
from urllib.parse import urljoin

from shelf.discovery_sources import SourceError,label,timestamp
from shelf.event_listing_transport import MAX_BYTES
from shelf.event_sources import (SOURCES,ListingEvent,MELBOURNE,_official_event,
                                 occurrence_event,reconcile_events)
from shelf.gallery_sources import MONTHS
from shelf.rrr_events import _Listing,_one

LISTING_URL='https://www.impromelbourne.com.au/shows/2026-season/'
ORIGIN='https://www.impromelbourne.com.au/'
_MONTHS={name:index for index,name in enumerate(MONTHS,1)}
_MONTHS.update({name[:3]:index for index,name in enumerate(MONTHS,1)})


@dataclass(frozen=True)
class ShowRun(ListingEvent):
    programme_starts_on: str | None = None
    programme_ends_on: str | None = None

    def date_status(self,now):
        return ('Source show run: '+self.listed_date+' (2026 season). '
            'Individual performance dates, times, venue and audience eligibility are unverified; '
            'this range does not instantiate recurring sessions.')


class _Season(_Listing):
    def captures_root(self,tag,attrs):
        css=attrs.get('class','').split()
        return tag=='h2' and 'hero__title' in css or tag=='div' and 'cards' in css


def _run(wording):
    match=re.fullmatch(r'([A-Z][a-z]+) (\d{1,2})\s*[–-]\s*([A-Z][a-z]+) (\d{1,2})',wording)
    if not match:raise SourceError('Unrecognized show-run range.')
    m1,d1,m2,d2=match.groups()
    try:
        first=date(2026,_MONTHS[m1],int(d1));last=date(2026,_MONTHS[m2],int(d2))
    except (KeyError,ValueError):raise SourceError('Invalid explicit season dates.') from None
    if last<first:raise SourceError('No unobserved cross-year rollover is permitted.')
    return first.isoformat(),last.isoformat()


def parse_listing(raw,*,retrieved_at,provenance='live-automated'):
    now=timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw)>MAX_BYTES or now is None
            or provenance not in ('fixture','live-automated')):
        raise SourceError('Invalid bounded Impro Melbourne season.')
    parser=_Season();parser.feed(raw.decode('utf-8'));parser.close()
    if parser.stack:raise SourceError('Truncated Impro season.')
    title=_one([node for node in parser.columns if node.tag=='h2']).text()
    if title!='2026 Season':raise SourceError('Explicit 2026 season title required.')
    sections={}
    for node in (node for node in parser.columns if node.tag=='div'):
        heading=_one(node.find('h3','cards__title')).text()
        if heading in sections or heading not in ('Now Playing','Past Shows'):
            raise SourceError('Changed or ambiguous season regions.')
        sections[heading]=node
    if set(sections)!={'Now Playing','Past Shows'}:
        raise SourceError('Current and past season boundaries required.')
    if sum(len(node.find('li','cards__card--show')) for node in sections.values())>48:
        raise SourceError('Show card scan bound exceeded.')
    digest=sha256(raw).hexdigest();events=[]
    for position,node in enumerate(sections['Now Playing'].find('li','cards__card--show'),1):
        # cards__card-title is a date range, not a show name. The publisher's
        # actual image alt supplies the title; the image itself is never fetched.
        name=label(_one(node.find('img')).attrs.get('alt'))
        wording=label(_one(node.find('div','cards__card-title')).text())
        first,last=_run(wording)
        # The source's base-relative href begins shows/2026-season/... .
        # Resolve it against its official origin, not the season's own directory.
        url=urljoin(ORIGIN,_one(node.find('a')).attrs.get('href',''))
        if not re.fullmatch(r'https://www\.impromelbourne\.com\.au/shows/2026-season/[a-z0-9][a-z0-9-]*/?',url):
            raise SourceError('Exact official season show URL required.')
        row=dict(kind='event',name=name,url=url,startDate=first,endDate=last)
        base=_official_event(row,'impro_melbourne',SOURCES['impro_melbourne'],retrieved_at,
                             provenance,digest,position,False)
        values=dict(base.__dict__,starts_at=None,ends_at=None)
        events.append(occurrence_event(ShowRun(**values,listed_date=wording,
            programme_starts_on=first,programme_ends_on=last)))
    today=now.astimezone(MELBOURNE).date();latest=today+timedelta(weeks=8)
    eligible=[event for event in reconcile_events(events)
        if today.isoformat()<=event.programme_ends_on and event.programme_starts_on<=latest.isoformat()]
    if not eligible:
        from shelf.event_scraping import ProgrammeUnavailable
        raise ProgrammeUnavailable
    return tuple(eligible[:9])
