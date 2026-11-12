"""Current Gertrude exhibition runs from one fixed, mixed-history listing."""
from datetime import date, datetime, timedelta
from hashlib import sha256
import re
from urllib.parse import urljoin, urlsplit

from shelf.discovery_sources import SourceError, label, timestamp
from shelf.event_listing_transport import MAX_BYTES
from shelf.event_sources import (SOURCES, ListingEvent, MELBOURNE, _official_event,
                                  occurrence_event, reconcile_events)
from shelf.rrr_events import _Listing, _one

LISTING_URL = 'https://gertrude.org.au/exhibitions/'
MAX_CARDS = 48
_MONTHS = {name: index for index, name in enumerate(
    'January February March April May June July August September October November December'.split(), 1)}
_MONTHS.update({name[:3]: index for name, index in tuple(_MONTHS.items()) if len(name) > 3})
_RUN = re.compile(r'([A-Z][a-z]{2,8}) (\d{1,2}), (\d{4})\s*[-–]\s*'
                  r'([A-Z][a-z]{2,8}) (\d{1,2}), (\d{4})')
_DETAIL_PATH = re.compile(r'/exhibition/[a-z0-9][a-z0-9-]*/?')


class GertrudeEvent(ListingEvent):
    def date_status(self, now):
        return ('Source exhibition run: ' + (self.listed_date or 'unknown date') +
                '; opening sessions, admission and attendance availability unverified.')


class _Cards(_Listing):
    """Capture only visible Current/Upcoming cards; stop before the archive."""
    def __init__(self):
        super().__init__()
        self.section = 'before'
        self.in_page_heading = False
        self.ignored_depth = 0
        self.saw_archive = False

    def handle_starttag(self, tag, attrs):
        if self.ignored_depth:
            if tag not in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                           'link', 'meta', 'param', 'source', 'track', 'wbr'):
                self.ignored_depth += 1
            return
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.ignored_depth = 1
            return
        if tag == 'img':
            return
        if not self.stack and tag == 'h1' and self.section == 'before':
            self.in_page_heading = True
        if (not self.stack and tag == 'div'
                and 'exhibition-tile' in dict(attrs).get('class', '').split()
                and self.section in ('current', 'upcoming')):
            if len(self.columns) >= MAX_CARDS:
                raise SourceError('Gertrude current/upcoming card limit exceeded.')
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.ignored_depth:
            self.ignored_depth -= 1
            return
        if not self.stack and tag == 'h1' and self.in_page_heading:
            self.in_page_heading = False
        super().handle_endtag(tag)

    def handle_data(self, data):
        if self.ignored_depth:
            return
        if self.stack:
            super().handle_data(data)
            return
        text = ' '.join(data.split())
        if self.section == 'before' and self.in_page_heading:
            if text.casefold() == 'exhibitions':
                self.section = 'seeking-current'
        elif self.section == 'seeking-current' and text.casefold() == 'current':
            self.section = 'current'
        elif self.section in ('current', 'upcoming') and text.casefold() == 'upcoming':
            self.section = 'upcoming'
        elif self.section in ('current', 'upcoming') and text.casefold() == 'past exhibitions':
            self.section = 'past'
            self.saw_archive = True

    def captures_root(self, tag, attributes):
        return (self.section in ('current', 'upcoming') and tag == 'div'
                and 'exhibition-tile' in attributes.get('class', '').split())


def _date(value):
    match = re.fullmatch(r'([A-Z][a-z]{2,8}) (\d{1,2}), (\d{4})', value)
    if not match:
        raise SourceError('Explicit Gertrude exhibition dates required.')
    month_name, day, year = match.groups()
    month = _MONTHS.get(month_name)
    if month is None:
        raise SourceError('Unsupported Gertrude exhibition month.')
    try:
        return date(int(year), month, int(day))
    except ValueError:
        raise SourceError('Invalid Gertrude exhibition date.') from None


def parse_listing(raw, *, now, retrieved_at, provenance='live-automated'):
    observed = timestamp(retrieved_at)
    if (type(raw) is not bytes or len(raw) > MAX_BYTES or observed is None
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or provenance not in ('fixture', 'live-automated')):
        raise SourceError('Invalid bounded Gertrude listing.')
    parser = _Cards()
    parser.feed(raw.decode('utf-8'))
    parser.close()
    if parser.stack or not parser.saw_archive or not parser.columns:
        raise SourceError('Changed, incomplete or ambiguous Gertrude current/upcoming listing.')

    digest = sha256(raw).hexdigest()
    events = []
    for position, node in enumerate(parser.columns, 1):
        title = label(_one(node.find('h3', 'exhibition-title')).text())
        run_wording = label(_one(node.find(css='exhibition-dates')).text())
        run = _RUN.fullmatch(run_wording)
        if not run:
            raise SourceError('Gertrude exhibition dates are missing or changed.')
        start = _date(f'{run[1]} {run[2]}, {run[3]}')
        end = _date(f'{run[4]} {run[5]}, {run[6]}')
        if end < start:
            raise SourceError('Gertrude exhibition ends before it starts.')

        locations = node.find(css='exhibition-location')
        if len(locations) > 1:
            raise SourceError('Ambiguous Gertrude exhibition venue.')
        venue = label(locations[0].text()) if locations else ''
        paths = set()
        for anchor in node.find('a'):
            href = anchor.attrs.get('href')
            if not href:
                continue
            parts = urlsplit(urljoin(LISTING_URL, href))
            if (parts.scheme == 'https' and parts.netloc.lower() == 'gertrude.org.au'
                    and not parts.query and not parts.fragment and _DETAIL_PATH.fullmatch(parts.path)):
                paths.add('https://gertrude.org.au' + parts.path)
        if len(paths) != 1:
            raise SourceError('One exact official Gertrude exhibition link required.')
        url = paths.pop()
        row = {'kind': 'event', 'name': title, 'url': url,
               'startDate': start.isoformat(), 'endDate': end.isoformat(),
               'location': {'name': venue}}
        base = _official_event(row, 'gertrude', SOURCES['gertrude'], retrieved_at,
                               provenance, digest, position, False)
        if base is None:
            raise SourceError('Gertrude listing did not identify an exhibition.')
        events.append(occurrence_event(GertrudeEvent(**base.__dict__, listed_date=run_wording)))

    if len({event.occurrence_ref for event in events}) != len(events):
        raise SourceError('Duplicate Gertrude exhibition run.')
    today = now.astimezone(MELBOURNE).date()
    latest = today + timedelta(weeks=8)
    eligible = [event for event in reconcile_events(events)
                if today <= date.fromisoformat(event.source_end)
                and date.fromisoformat(event.source_start) <= latest]
    return tuple(eligible[:9])
