"""Forum show cards and explicitly scoped sessions, without detail crawling."""
from datetime import date, timedelta
from hashlib import sha256
from urllib.parse import urljoin

from shelf.discovery_sources import SourceError, label, timestamp
from shelf.event_sources import (SOURCES, ListingEvent, MELBOURNE, _official_event,
                                  occurrence_event, reconcile_events)
from shelf.rrr_events import _Listing, _one
from shelf.event_listing_transport import MAX_BYTES

LISTING_URL = 'https://forummelbourne.com.au/shows'


class _Cards(_Listing):
    def captures_root(self, tag, attributes):
        return tag in ('article', 'div') and 'show-card' in attributes.get('class', '').split()


def parse_listing(raw, *, retrieved_at, provenance):
    now = timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw) > MAX_BYTES or now is None
            or provenance not in ('fixture', 'live-automated')):
        raise SourceError('Invalid bounded Forum observation.')
    parser = _Cards()
    parser.feed(raw.decode('utf-8'))
    parser.close()
    if parser.stack or not parser.columns or len(parser.columns) > 48:
        raise SourceError('Missing, changed or oversized Forum listing; coverage unknown.')
    digest = sha256(raw).hexdigest()
    events = []
    for card in parser.columns:
        anchor = _one(card.find('a', 'show-title'))
        notice_nodes = card.find(css='show-status')
        notice = label(_one(notice_nodes).text()) if notice_nodes else ''
        # Enumerate only explicit session containers, never a list of arbitrary
        # time nodes (which might instead describe a range or doors/headline).
        sessions = card.find('li', 'show-session') or [card]
        for session in sessions:
            if len(events) >= 100:
                raise SourceError('Forum observation budget exceeded.')
            dates = session.find(css='show-date')
            node = _one(dates)
            start = node.attrs.get('datetime') if node.tag == 'time' else None
            ends = session.find('time', 'show-end-date')
            end = _one(ends).attrs.get('datetime') if ends else None
            # If a nested session has its own notice, it scopes only that one.
            scoped = session.find(css='session-status') if session is not card else []
            source_notice = label(_one(scoped).text()) if scoped else notice
            row = {'kind': 'event', 'name': label(anchor.text()),
                   'url': urljoin(LISTING_URL, anchor.attrs.get('href', '')),
                   'startDate': start, 'endDate': end,
                   'status': {'cancelled': 'cancelled', 'postponed': 'postponed'}.get(source_notice.casefold())}
            event = _official_event(row, 'forum', SOURCES['forum'], retrieved_at,
                                    provenance, digest, len(events)+1, False)
            events.append(occurrence_event(ListingEvent(**event.__dict__, listed_date=label(node.text()),
                                                        source_notice=source_notice)))
    # Day-only repeats do not prove whether the page means one or two sessions.
    counts = {}
    for event in events:
        if event.occurrence_basis in ('explicit_date', 'explicit_range'):
            counts[event.occurrence_ref] = counts.get(event.occurrence_ref, 0) + 1
    events = [occurrence_event(e, unresolved=True) if counts.get(e.occurrence_ref, 0) > 1 else e for e in events]
    today = now.astimezone(MELBOURNE).date()
    end_window = today + timedelta(weeks=8)
    result = []
    for event in reconcile_events(events):
        if event.status in ('cancelled', 'postponed', 'ended'):
            continue
        if event.date_precision == 'time':
            if not now < timestamp(event.starts_at) <= now + timedelta(weeks=8):
                continue
        elif event.source_start:
            first = date.fromisoformat(event.source_start[:10])
            last = date.fromisoformat((event.source_end or event.source_start)[:10])
            if last < today or first > end_window:
                continue
        result.append(event)
    return tuple(result[:9])
