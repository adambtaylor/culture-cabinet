"""My Aeon's dated Webflow cards; archive-only pages are not current inventory."""
from datetime import date, timedelta
from hashlib import sha256
import re
from urllib.parse import urljoin, urlsplit

from shelf.discovery_sources import SourceError, label, timestamp
from shelf.event_sources import (SOURCES, ListingEvent, MELBOURNE, _official_event,
                                 occurrence_event, reconcile_events)
from shelf.event_listing_transport import MAX_BYTES
from shelf.rrr_events import _Listing, _one, event_path

LISTING_URL = 'https://www.myaeon.com.au/my-aeon-events'
_MONTHS = {name:index for index,name in enumerate(('January','February','March','April',
    'May','June','July','August','September','October','November','December'),1)}


class _Programme(_Listing):
    def captures_root(self, tag, attributes):
        classes = attributes.get('class','').split()
        return tag == 'div' and ('event-header' in classes or 'event-listing-collection-list' in classes)


def _date_label(value):
    match = re.fullmatch(r'([A-Z][a-z]+) (\d{1,2}), (\d{4})',value)
    if not match:
        if re.search(r'\b[12][0-9]{3}\b',value):
            raise SourceError('Unsupported explicit source date wording.')
        return None  # No year or different wording: retain an unresolved lead.
    month,day,year = match.groups()
    try:
        return date(int(year),_MONTHS[month],int(day)).isoformat()
    except (KeyError,ValueError):
        raise SourceError('Invalid explicit My Aeon date.') from None


def parse_listing(raw, *, retrieved_at, provenance='live-automated'):
    now = timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw)>MAX_BYTES or now is None
            or provenance not in ('fixture','live-automated')):
        raise SourceError('Invalid bounded My Aeon listing.')
    parser=_Programme();parser.feed(raw.decode('utf-8'));parser.close()
    if parser.stack or len(parser.columns)!=2:
        raise SourceError('Missing or changed My Aeon programme sections.')
    header=_one([node for node in parser.columns if node.matches(css='event-header')])
    listing=_one([node for node in parser.columns if node.matches(css='event-listing-collection-list')])
    if label(_one(header.find('h1')).text())!='EVENTS' or not listing.matches(css='w-dyn-items'):
        raise SourceError('Unrecognized My Aeon listing layout.')
    rows=listing.find('div','collection-item-11')
    if len(rows)>48:
        raise SourceError('My Aeon card budget exceeded.')
    digest=sha256(raw).hexdigest();events={}
    for position,node in enumerate(rows,1):
        if not node.matches(css='w-dyn-item'):
            raise SourceError('Unrecognized My Aeon event row.')
        anchor=_one(node.find('a','homepage-event-v2'))
        name=label(_one(anchor.find(css='event-listing-name')).text())
        listed=label(_one(anchor.find(css='event-listing-date-center')).text())
        start=_date_label(listed)
        row={'kind':'event','name':name,'url':urljoin(LISTING_URL,anchor.attrs.get('href','')),
             'startDate':start}
        # Calendar dates are not midnight/show-time evidence. The listing does
        # not state an offset/zone, price, admission, lineup or stock; leave those
        # unknown instead of adopting facts from slugs, posters or descriptions.
        base=_official_event(row,'my_aeon',SOURCES['my_aeon'],retrieved_at,
                             provenance,digest,position,False)
        if not base.official_url.startswith('https://www.myaeon.com.au/events/'):
            raise SourceError('Exact My Aeon event detail URL required.')
        event_path(urlsplit(base.official_url).path)
        event=occurrence_event(ListingEvent(**base.__dict__,listed_date=listed),unresolved=not start)
        old=events.get(event.key)
        if old and old.title!=event.title:
            raise SourceError('Conflicting names for the same source occurrence.')
        # Exact source URL/date observations may reconcile; similar names or
        # different detail URLs never establish that two source rows are one gig.
        events[event.key]=event if old is None else reconcile_events([old,event])[0]
    today=now.astimezone(MELBOURNE).date()
    last=(now+timedelta(weeks=8)).astimezone(MELBOURNE).date()
    eligible=[event for event in events.values() if not event.starts_at or
              today.isoformat()<=event.starts_at<=last.isoformat()]
    if not eligible:
        # A recognized archive or empty CMS wrapper has no confirmed current
        # empty/season statement. Do not roll old years forward or claim no gigs.
        from shelf.event_scraping import ProgrammeUnavailable
        raise ProgrammeUnavailable
    return tuple(eligible[:9])
