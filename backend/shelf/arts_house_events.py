"""One bounded Arts House programme listing; dates are not session times."""
from datetime import date, datetime, timedelta
from hashlib import sha256
import re
from urllib.parse import urljoin

from shelf.discovery_sources import SourceError, label, timestamp
from shelf.event_listing_transport import MAX_BYTES
from shelf.event_sources import (
    MELBOURNE, SOURCES, ListingEvent, _official_event, occurrence_event, reconcile_events,
)
from shelf.gallery_sources import MONTHS
from shelf.rrr_events import _Listing, _Node, _one

LISTING_URL = 'https://www.artshouse.com.au/whats-on/'
_MONTHS = {name: index for index, name in enumerate(MONTHS, 1)}
_MONTHS.update({name[:3]: index for index, name in enumerate(MONTHS, 1)})


class _Cards(_Listing):
    def captures_root(self, tag, attrs):
        return tag == 'a' and 'event_anchor' in attrs.get('class', '').split()


class ArtsHouseListing(ListingEvent):
    def date_status(self, now):
        if self.source_end:
            return 'Source programme run: '+self.listed_date+'; individual performance sessions and attendance unverified.'
        return 'Source lists '+self.listed_date+'; session time, timezone and attendance unverified.'


def _children(node, tag, classes=None):
    return [child for child in node.children if isinstance(child, _Node)
            and child.tag == tag
            and (classes is None or child.attrs.get('class', '').split() == classes)]


def _date_label(text):
    # The observed page exposes a visible date range or day, not a performance
    # clock. Do not infer a year, time, timezone, or session from a show card.
    ranged = re.fullmatch(
        r'(\d{1,2})(?: ([A-Z][a-z]+))?\s*[–—-]\s*(\d{1,2}) ([A-Z][a-z]+) (\d{4})', text)
    single = re.fullmatch(r'(\d{1,2}) ([A-Z][a-z]+) (\d{4})', text)
    try:
        if ranged:
            first_day, first_month, last_day, last_month, year = ranged.groups()
            first = date(int(year), _MONTHS[(first_month or last_month).title()], int(first_day))
            last = date(int(year), _MONTHS[last_month.title()], int(last_day))
            if last < first:
                raise ValueError
            return first.isoformat(), last.isoformat()
        if single:
            day, month, year = single.groups()
            return date(int(year), _MONTHS[month.title()], int(day)).isoformat(), None
    except (KeyError, ValueError):
        raise SourceError('Invalid explicit Arts House date.') from None
    raise SourceError('Explicit Arts House year and supported date required.')


def parse_listing(raw, *, now, retrieved_at, provenance='live-automated'):
    clock = now if (isinstance(now, datetime) and now.tzinfo is not None
                    and now.utcoffset() is not None) else None
    observed = timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw) > MAX_BYTES or clock is None or observed is None
            or provenance not in ('fixture', 'live-automated')):
        raise SourceError('Invalid bounded Arts House listing.')
    parser = _Cards()
    parser.feed(raw.decode('utf-8'))
    parser.close()
    if parser.stack or not 1 <= len(parser.columns) <= 48:
        raise SourceError('Missing, changed or oversized Arts House listing.')

    digest = sha256(raw).hexdigest()
    events = []
    for position, card in enumerate(parser.columns, 1):
        wrapper = _one(_children(card, 'div', ['wrap']))
        heading = _one(_children(wrapper, 'h3'))
        date_box = _one(_children(wrapper, 'div', ['event-time']))
        time_node = _one(_children(date_box, 'time'))
        title = label(heading.text())
        wording = label(time_node.text())
        first, last = _date_label(wording)
        source_url = urljoin(LISTING_URL, card.attrs.get('href', ''))
        base = _official_event(
            {'kind': 'event', 'name': title, 'url': source_url,
             'startDate': first, 'endDate': last},
            'arts_house', SOURCES['arts_house'], retrieved_at, provenance, digest, position, False)
        # The rendered label is a date or programme run; it never proves a
        # timed performance, timezone, attendance, or ticket availability.
        values = dict(base.__dict__, starts_at=None, ends_at=None)
        events.append(occurrence_event(ArtsHouseListing(**values, listed_date=wording)))

    today = clock.astimezone(MELBOURNE).date()
    latest = today + timedelta(weeks=8)
    eligible = [event for event in reconcile_events(events)
        if today.isoformat() <= (event.source_end or event.source_start)[:10]
        and event.source_start[:10] <= latest.isoformat()]
    if not eligible:
        from shelf.event_scraping import ProgrammeUnavailable
        raise ProgrammeUnavailable
    return tuple(eligible[:9])
