"""Bounded personal-use Beat facts: source links/dates, never article republication."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
import re

from shelf.discovery_sources import SourceError, identity, label, timestamp
from shelf.event_sources import EventCandidate, EventEvidence, MELBOURNE, SOURCES, official_url
from shelf.rrr_events import _Listing, _one

LISTING_URL = 'https://beat.com.au/gig-guide/'
MAX_BYTES = 1024 * 1024
MAX_CARDS = 48
MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')
WEEKDAYS = ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')


def event_url(value):
    url = official_url(value, SOURCES['beat'])
    path = url.removeprefix('https://beat.com.au')
    if not re.fullmatch(r'/[a-z0-9][a-z0-9-]{0,180}', path) or path in (
            '/gig-guide', '/login', '/terms-conditions', '/feed', '/wp-admin'):
        raise SourceError('Exact public Beat event URL required.')
    return url + '/'


def source_date(value):
    match = re.fullmatch(r'([A-Za-z]{3}) (\d{1,2}) ([A-Za-z]{3}) (\d{4})', value.strip())
    if not match:
        return None  # Ranges and ambiguous wording do not become single sessions.
    day, number, month, year = match.groups()
    try:
        date = datetime(int(year), MONTHS.index(month.upper()) + 1, int(number)).date()
    except (ValueError, OverflowError):
        return None
    return date.isoformat() if WEEKDAYS[date.weekday()] == day.upper() else None


@dataclass(frozen=True)
class BeatDetails:
    source_date: str
    time_label: str
    address: str
    price_label: str
    doors_time: str | None
    show_time: str | None
    retrieved_at: str
    snapshot_sha256: str


@dataclass(frozen=True)
class BeatEvent(EventCandidate):
    listed_date: str = ''
    price_label: str = ''
    details: BeatDetails | None = None
    detail_status: str = ''
    detail_message: str = ''

    @property
    def date_night_reason(self):
        clock = self.details.show_time if self.details else None
        if self.venue and clock and '17:00' <= clock < '23:00':
            return 'Inferred evening option for two: explicit evening show time and a named venue provide an anchor for dinner and travel. Taste fit and Lisa’s interest remain unconfirmed.'
        return ''

    def date_status(self, now):
        suffix = 'Session time, doors, finish and ticket availability unverified.'
        if self.details:
            suffix = f'Source time label: {self.details.time_label or "unknown"}; check doors versus show. Finish unknown.'
        return f'Source date: {self.listed_date}. {suffix}'


class _Cards(_Listing):
    def captures_root(self, tag, attrs):
        return tag == 'div' and 'gig-card' in attrs.get('class', '').split() and len(self.columns) < MAX_CARDS


def parse_listing(raw, *, retrieved_at):
    now = timestamp(retrieved_at)
    if type(raw) is not bytes or len(raw) > MAX_BYTES or now is None:
        raise SourceError('Bounded Beat HTML and aware retrieval time required.')
    parser = _Cards(); parser.feed(raw.decode('utf-8')); parser.close()
    if parser.stack or not parser.columns:
        raise SourceError('Unrecognizable or truncated Beat listing; coverage unknown.')
    digest = sha256(raw).hexdigest()
    today = now.astimezone(MELBOURNE).date()
    events = {}
    for position, card in enumerate(parser.columns, 1):
        date_label = label(_one(card.find('div', 'gig-date')).text())
        date = source_date(date_label)
        # Only single-date examples in the next 14 days. A festival run is not
        # an exact evening, and a past listing cannot establish fresh inventory.
        if card.find('div', 'gig-end-date') or date is None or not today.isoformat() <= date <= (today + timedelta(days=14)).isoformat():
            continue
        title = _one(card.find('h3', 'gig-title'))
        anchor = _one(title.find('a'))
        url = event_url(anchor.attrs.get('href'))
        name = label(anchor.text())
        locations = card.find('div', 'gig-location')
        venue = label(_one(locations).text()) if locations else ''
        prices = card.find('div', 'gig-price')
        price = label(_one(prices).text()) if prices else ''
        key_id = sha256(('beat\0url\0' + url).encode()).hexdigest()
        evidence = EventEvidence('beat', None, url, retrieved_at, 'live-automated', digest, position)
        event = BeatEvent(key=identity('official', 'event', key_id), title=name, kind='event',
            source='Beat Magazine', provenance='live-automated', rank=position,
            retrieved_at=retrieved_at, url=url, category='all', venue=venue,
            source_id='beat', official_url=url, starts_at=date, source_start=date_label,
            date_precision='date', evidence=(evidence,), listed_date=date_label, price_label=price)
        old = events.get(url)
        if old and (old.title, old.starts_at, old.venue, old.price_label) != (name, date, venue, price):
            raise SourceError('Conflicting Beat event facts.')
        events.setdefault(url, event)
    return tuple(events.values())[:9]


class _Details(_Listing):
    def __init__(self):
        super().__init__(); self.canonicals = []

    def captures_root(self, tag, attrs):
        return tag == 'h1' or tag == 'div' and any(c in attrs.get('class', '').split()
            for c in ('article-meta', 'article-content__primary'))

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'link' and attributes.get('rel') == 'canonical':
            self.canonicals.append(attributes.get('href'))
        super().handle_starttag(tag, attrs)


def _clock_matches(text, prefix):
    values = re.findall(prefix + r'\s*:?\s*(\d{1,2})(?::([0-5]\d))?\s*(AM|PM)\b', text, re.I)
    clocks = set()
    for hour, minute, period in values:
        if not 1 <= int(hour) <= 12:
            return None
        clocks.add(f'{int(hour) % 12 + (12 if period.upper() == "PM" else 0):02}:{minute or "00"}')
    return clocks.pop() if len(clocks) == 1 else None


def parse_detail(raw, *, event, retrieved_at):
    if type(event) is not BeatEvent or type(raw) is not bytes or len(raw) > MAX_BYTES or timestamp(retrieved_at) is None:
        raise SourceError('Bounded exact Beat detail required.')
    parser = _Details(); parser.feed(raw.decode('utf-8')); parser.close()
    if parser.stack or not 1 <= len(parser.canonicals) <= 2 or set(parser.canonicals) != {event.url}:
        raise SourceError('Beat detail canonical identity changed.')
    title = _one([n for n in parser.columns if n.tag == 'h1'])
    if label(title.text()) != event.title:
        raise SourceError('Beat detail title changed.')
    meta = _one([n for n in parser.columns if n.matches('div', 'article-meta')])
    fields = {}
    for item in meta.find('div', 'article-meta-item'):
        key = _one(item.find('div', 'article-meta-headline')).text().casefold()
        if key in fields:
            raise SourceError('Ambiguous Beat detail field.')
        fields[key] = _one(item.find('div', 'article-meta-content'))
    when = label(fields['date'].text())
    if source_date(when) != event.starts_at:
        raise SourceError('Beat date changed; refresh listing first.')
    location = fields['location']
    venue = label(_one(location.find('a')).text())
    if venue != event.venue:
        raise SourceError('Beat venue changed; refresh listing first.')
    parts = [n.text().strip(' ,') for n in location.children if not isinstance(n, str)]
    if len(parts) != 2 or parts[0] != venue:
        raise SourceError('Ambiguous Beat venue/address.')
    primary_node = _one([n for n in parser.columns if n.matches('div', 'article-content__primary')])
    # Block boundaries must not glue PM to the next paragraph and hide a clock.
    def block_text(node):
        return ' '.join(c if isinstance(c, str) else block_text(c) for c in node.children)
    primary = block_text(primary_node)
    details = BeatDetails(when, label(fields['time'].text()) if 'time' in fields else '',
        label(parts[1]), label(fields['price'].text()) if 'price' in fields else '',
        _clock_matches(primary, r'\bDOORS(?: OPEN)?'),
        _clock_matches(primary, r'\bSHOW STARTS'), retrieved_at, sha256(raw).hexdigest())
    return replace(event, details=details, detail_status='ready', detail_message='Source details read; seller availability and finish remain unverified.')


def fetch_detail(event, now):
    from shelf.discovery_transport import beat_detail
    try:
        code, raw = beat_detail(event.url)
        if code != 200:
            return replace(event, details=None, detail_status='unavailable', detail_message='Detail unavailable; no retry or alternate route attempted.')
        return parse_detail(raw, event=event, retrieved_at=now.isoformat())
    except (SourceError, ValueError, KeyError, TypeError, OSError, TimeoutError, RecursionError):
        return replace(event, details=None, detail_status='unavailable', detail_message='Detail could not be verified; refresh or use the source page.')
