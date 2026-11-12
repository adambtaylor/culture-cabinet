"""West Space's source programme ranges, never invented opening sessions."""
from dataclasses import dataclass, replace
from datetime import date
from hashlib import sha256
import re
from urllib.parse import urljoin

from shelf.discovery_sources import SourceError, label, timestamp
from shelf.event_sources import (SOURCES, ListingEvent, MELBOURNE, _official_event,
                                 occurrence_event, reconcile_events)
from shelf.event_listing_transport import MAX_BYTES
from shelf.gallery_sources import MONTHS, _eligible
from shelf.rrr_events import _Listing, _one

LISTING_URL = 'https://westspace.org.au/whats-on/'
_MONTHS = {name:index for index,name in enumerate(MONTHS,1)}
_MONTHS.update({name[:3]:index for index,name in enumerate(MONTHS,1)})


@dataclass(frozen=True)
class ProgrammeRun(ListingEvent):
    # Use the existing pure gallery overlap rules, while keeping the generic
    # performance/opening starts_at/ends_at unset. No exhibition opening, daily
    # hours or timed session can be reconstructed from these range fields.
    exhibition_starts_on: str | None = None
    exhibition_ends_on: str | None = None
    opening_date: str | None = None

    def date_status(self, now):
        today=now.astimezone(MELBOURNE).date().isoformat()
        phase=('Current programme run' if self.exhibition_starts_on<=today<=self.exhibition_ends_on
               else 'Future programme run' if today<self.exhibition_starts_on else 'Programme run ended')
        return f'{phase}: {self.listed_date}. Calendar dates only; opening, hours and individual sessions unverified.'


class _Programme(_Listing):
    def captures_root(self, tag, attributes):
        return tag=='main'


def _range(value):
    match=re.fullmatch(r'(\d{1,2}) ([A-Z][a-z]+)(?: (\d{4}))? → '
        r'(\d{1,2}) ([A-Z][a-z]+) (\d{4})',value)
    if not match:
        raise SourceError('Unrecognized explicit programme range.')
    first,first_month,first_year,last,last_month,last_year=match.groups()
    try:
        m1,m2=_MONTHS[first_month],_MONTHS[last_month]
        # The trailing publisher year scopes this abbreviated calendar range.
        # A Nov->Jan arrow wraps into the preceding year, not the retrieval year.
        # Explicit years on both ends stay explicit, including longer runs.
        y1=int(first_year) if first_year else int(last_year)-int((m1,int(first))>(m2,int(last)))
        start=date(y1,m1,int(first));end=date(int(last_year),m2,int(last))
    except (KeyError,ValueError):
        raise SourceError('Invalid calendar range.') from None
    if end<start:
        raise SourceError('Source range ends before its start.')
    return start.isoformat(),end.isoformat()


def parse_listing(raw, *, retrieved_at, provenance='live-automated'):
    now=timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw)>MAX_BYTES or now is None
            or provenance not in ('fixture','live-automated')):
        raise SourceError('Invalid bounded West Space listing.')
    parser=_Programme();parser.feed(raw.decode('utf-8'));parser.close()
    if parser.stack or len(parser.columns)!=1:
        raise SourceError('Missing, changed or truncated programme region.')
    main=parser.columns[0]
    cards=[node for node in main.find('a') if node.attrs.get('href','').startswith('/program/')]
    if not 1<=len(cards)<=48:
        raise SourceError('Missing or oversized source programme cards.')
    digest=sha256(raw).hexdigest();events=[]
    for position,node in enumerate(cards,1):
        heading=_one(node.find('h2','type--50'))
        parts=[' '.join(child.split()) for child in heading.children if isinstance(child,str) and child.strip()]
        # Gatsby's rendered fields are separate text nodes bounded by title
        # quotation marks. Never adopt the combined title/artist/range as a name.
        if len(parts)!=5 or parts[0]!='“' or parts[2]!='”':
            raise SourceError('Unrecognized programme name/artist/range fields.')
        title,artist,wording=label(parts[1]),label(parts[3]),label(parts[4])
        if len(artist)>200:
            raise SourceError('Oversized source artist label.')
        start,end=_range(wording)
        url=urljoin(LISTING_URL,node.attrs['href'])
        row={'kind':'event','name':title,'url':url,'startDate':start,'endDate':end}
        base=_official_event(row,'west_space',SOURCES['west_space'],retrieved_at,
                             provenance,digest,position,False)
        if not re.fullmatch(r'https://westspace\.org\.au/program/[a-z0-9][a-z0-9-]*',base.official_url):
            raise SourceError('Exact public programme URL required.')
        run=ProgrammeRun(**base.__dict__, exhibition_starts_on=start,exhibition_ends_on=end,
                         listed_date=wording,source_notice=f'Source lists artist: {artist}; not a verified performance lineup.')
        run=replace(run,starts_at=None,ends_at=None)
        events.append(occurrence_event(run))
    eligible=[event for event in reconcile_events(events) if _eligible(event,now)]
    if not eligible:
        from shelf.event_scraping import ProgrammeUnavailable
        raise ProgrammeUnavailable
    return tuple(eligible[:9])
