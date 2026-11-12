"""303's single Squarespace listing; no calendar/detail/image requests."""
from datetime import date, timedelta
from hashlib import sha256
import re
from urllib.parse import urljoin

from shelf.discovery_sources import SourceError, label, timestamp
from shelf.event_sources import (SOURCES, ListingEvent, MELBOURNE, _official_event,
                                  occurrence_event, reconcile_events)
from shelf.rrr_events import _Listing, _one
from shelf.event_listing_transport import MAX_BYTES

LISTING_URL = 'https://303.net.au/gigs-events'
MAX_CARDS = 48


class _Cards(_Listing):
    def captures_root(self, tag, attributes):
        return tag == 'article' and 'eventlist-event' in attributes.get('class', '').split()


def parse_listing(raw, *, retrieved_at, provenance):
    now = timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw) > MAX_BYTES or now is None
            or provenance not in ('fixture', 'live-automated')):
        raise SourceError('Invalid bounded 303 observation.')
    parser = _Cards()
    parser.feed(raw.decode('utf-8'))
    parser.close()
    if parser.stack or not parser.columns or len(parser.columns) > MAX_CARDS:
        raise SourceError('Missing, truncated or oversized 303 listing; coverage unknown.')
    digest = sha256(raw).hexdigest()
    events = []
    for position, node in enumerate(parser.columns, 1):
        anchor = _one(node.find('a', 'eventlist-title-link'))
        start = _one(node.find('time', 'event-date')).attrs.get('datetime')
        # Squarespace date and time fields are independent. A date alone is not
        # midnight; clock text is retained but never assigned a venue-wide zone.
        end_nodes = node.find('time', 'event-end-date')
        if len(end_nodes) > 1:
            raise SourceError('Ambiguous end date.')
        end = end_nodes[0].attrs.get('datetime') if end_nodes else None
        date_label = label(' '.join(n.text() for n in node.find('li', 'eventlist-meta-date')))
        clocks = node.find('li', 'eventlist-meta-time')
        if clocks:
            date_label = label(date_label + ' ' + _one(clocks).text())
        local_start = node.find(css='event-time-localized-start')
        ambiguous_clock = False
        if local_start and start and re.fullmatch(r'\d{4}-\d{2}-\d{2}', start):
            clock = _one(local_start).text()
            match = re.fullmatch(r'(1[0-2]|[1-9]):([0-5][0-9])\s*([AP]M)', clock, re.I)
            if match:
                hour = int(match[1]) % 12 + (12 if match[3].upper() == 'PM' else 0)
                start = f'{start}T{hour:02}:{match[2]}:00'
            else:
                ambiguous_clock = True
        notices = node.find(css='eventlist-meta-status')
        notice = label(_one(notices).text()) if notices else ''
        url = urljoin(LISTING_URL, anchor.attrs.get('href', ''))
        # A missing year cannot be repaired from the slug, date label, retrieval
        # clock or recurrence. Unparseable explicit machine dates fail visibly.
        row = {'kind': 'event', 'name': label(anchor.text()), 'url': url,
               'startDate': start or None, 'endDate': end or None,
               'status': {'cancelled': 'cancelled', 'postponed': 'postponed'}.get(notice.casefold())}
        base = _official_event(row, 'venue303', SOURCES['venue303'], retrieved_at,
                               provenance, digest, position, False)
        event = occurrence_event(ListingEvent(**base.__dict__, listed_date=date_label, source_notice=notice),
                                 unresolved=ambiguous_clock)
        events.append(event)
    # Repeated day-only cards sharing a show URL cannot prove one session on
    # that day. Retain separate observations rather than merge or guess clocks.
    day_counts = {}
    for event in events:
        if event.occurrence_basis in ('explicit_date', 'explicit_range'):
            day_counts[event.occurrence_ref] = day_counts.get(event.occurrence_ref, 0) + 1
    events = [occurrence_event(event, unresolved=True) if day_counts.get(event.occurrence_ref, 0) > 1
              else event for event in events]
    # Reconcile cancellations/updates before the local date-window projection.
    today = now.astimezone(MELBOURNE).date()
    latest = today + timedelta(weeks=8)
    eligible = []
    for event in reconcile_events(events):
        if event.status in ('cancelled', 'postponed', 'ended'):
            continue
        if event.date_precision == 'date':
            if (event.ends_at or event.starts_at) < today.isoformat() or event.starts_at > latest.isoformat():
                continue
        elif event.date_precision == 'time':
            if timestamp(event.starts_at) <= now or timestamp(event.starts_at) > now + timedelta(weeks=8):
                continue
        elif event.source_start:
            # An explicit local clock can be unzoned yet still supply a day.
            # Filter that day, without converting its clock into an instant.
            day = date.fromisoformat(event.source_start[:10])
            if day < today or day > latest:
                continue
        eligible.append(event)
    return tuple(eligible[:9])
