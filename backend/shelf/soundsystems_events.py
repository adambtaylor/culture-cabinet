"""Public Sound Systems of Melbourne cards; factual leads, not bookable sessions.

No calendar feed, social page, backend API, script, image or linked page is read.
The shared listing URL is not an event identifier and must not merge its cards.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
import re
from urllib.parse import parse_qsl, urlsplit

from shelf.discovery_sources import MAX_BYTES, Outcome, SourceError, identity, label, timestamp
from shelf.event_sources import EventCandidate, EventEvidence, MELBOURNE
from shelf.rrr_events import _Listing, _one

LISTING_URL = 'https://soundsystemsofmelbourne.com.au/shows/'
MAX_CARDS = 48
MAX_EVENTS = 9
MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')
WEEKDAYS = ('MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY')


def _class(attrs, name):
    return any(re.fullmatch(r'Calendar_' + re.escape(name) + r'__[A-Za-z0-9_]{1,50}', value)
               for value in attrs.get('class', '').split())


def _nodes(root, tag, name):
    return [node for node in root.find(tag) if _class(node.attrs, name)]


@dataclass(frozen=True)
class SoundSystemEvent(EventCandidate):
    listed_date: str = ''
    listed_time: str = ''
    start_clock: str | None = None
    end_clock: str | None = None
    source_location: str = ''
    location_status: str = 'unknown'
    referenced_post: str | None = None
    source_attributes: tuple = ()
    outdoor: bool | None = None
    community: bool | None = None
    day_party: bool | None = None

    def date_status(self, now):
        return (f'Source lists {self.listed_date}; clocks {self.listed_time or "unknown"}. '
                'Timezone and ending calendar day unverified; ticket availability unknown.')


class _Cards(_Listing):
    def __init__(self):
        super().__init__()
        self.page_urls = []

    def captures_root(self, tag, attrs):
        return tag == 'div' and _class(attrs, 'eventCard') or tag == 'h2' and _class(attrs, 'calendarTitle')

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'meta' and attributes.get('property') == 'og:url':
            if len(attributes) != len(attrs):
                raise SourceError('Ambiguous Sound Systems page identity.')
            self.page_urls.append(attributes.get('content'))
        super().handle_starttag(tag, attrs)


def _date(card):
    block = _one(_nodes(card, 'div', 'eventDate'))
    parts = [node for node in block.children if not isinstance(node, str)]
    if len(parts) != 2 or parts[0].tag != 'div' or not _class(parts[1].attrs, 'eventWeekday'):
        raise SourceError('Changed Sound Systems date structure.')
    raw, weekday = label(parts[0].text()), label(parts[1].text())
    match = re.fullmatch(r'([A-Z]{3}) (\d{1,2}) (\d{4})', raw)
    if not match:
        return raw, None
    month, day, year = match.groups()
    try:
        value = date(int(year), MONTHS.index(month) + 1, int(day))
    except (ValueError, OverflowError):
        raise SourceError('Invalid explicit Sound Systems date.') from None
    if weekday != WEEKDAYS[value.weekday()]:
        raise SourceError('Sound Systems weekday disagrees with date.')
    return raw, value.isoformat()


def _post_reference(value):
    """Normalize only the observed public post-link form; never fetch it."""
    if not isinstance(value, str) or len(value) > 1000 or any(ord(c) <= 32 for c in value):
        return None
    try:
        parts = urlsplit(value)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if (parts.scheme != 'https' or parts.netloc not in ('www.instagram.com', 'instagram.com')
                or parts.fragment or not re.fullmatch(r'/(?:p|reel)/[A-Za-z0-9_-]{1,80}/?', parts.path)
                or len(query) > 2 or len({key for key, _ in query}) != len(query)
                or any(key not in ('igsh', 'stkn') or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', item)
                       for key, item in query)):
            return None
    except ValueError:
        return None
    return 'https://www.instagram.com' + parts.path.rstrip('/') + '/'


def _attributes(card):
    captions = _nodes(card, 'p', 'moreInfoCaption')
    if len(captions) > 1:
        raise SourceError('Ambiguous Sound Systems source caption.')
    if not captions:
        return ()
    caption = captions[0].text()
    # This observed, affirmative clause is an attributed community description,
    # not public-admission eligibility. No category is inferred from venue/time.
    phrase = 'is bringing the whole community together'
    lower = caption.casefold()
    before, found, after = lower.partition(phrase)
    ending = re.split(r'([.!?—;])', after, maxsplit=1)
    clause = re.split(r'[.!?—;]', before)[-1] + phrase + ending[0]
    questioned = len(ending) > 1 and ending[1] == '?'
    if found and not questioned and not re.search(r"\b(?:not|never|no|if|maybe|might|false|denied|whether)\b|n['’]t\b", clause):
        return (('community', phrase),)
    return ()


def parse_listing(raw, *, retrieved_at):
    now = timestamp(retrieved_at)
    if type(raw) is not bytes or len(raw) > MAX_BYTES or now is None:
        raise SourceError('Bounded Sound Systems HTML and aware retrieval time required.')
    parser = _Cards()
    parser.feed(raw.decode('utf-8'))
    parser.close()
    if parser.stack or parser.page_urls not in ([LISTING_URL], [LISTING_URL.rstrip('/')]):
        raise SourceError('Changed or truncated Sound Systems page.')
    heading = _one([node for node in parser.columns if node.tag == 'h2'])
    cards = [node for node in parser.columns if node.tag == 'div']
    if heading.text() != 'UPCOMING DANCES' or not 1 <= len(cards) <= MAX_CARDS:
        raise SourceError('Sound Systems event coverage unknown or oversized.')
    digest = sha256(raw).hexdigest()
    today = now.astimezone(MELBOURNE).date()
    horizon = (today + timedelta(weeks=8)).isoformat()
    events, references = {}, {}
    for position, card in enumerate(cards, 1):
        title = label(_one(_nodes(card, 'h3', 'eventTitle')).text())
        raw_date, start = _date(card)
        times = _nodes(card, 'div', 'eventTime')
        raw_time = label(_one(times).text()) if times else ''
        match = re.fullmatch(r'([0-2]\d:[0-5]\d) — ([0-2]\d:[0-5]\d)', raw_time)
        clocks = match.groups() if match and all(int(clock[:2]) < 24 for clock in match.groups()) else (None, None)
        locations = _nodes(card, 'div', 'eventLocation')
        location = label(_one(locations).text()) if locations else ''
        withheld = bool(re.search(r'\b(?:tba|tbc|secret|undisclosed|embargoed)\b|to be announced|revealed later', location, re.I))
        status = 'undisclosed' if withheld else ('source-stated' if location else 'unknown')
        links = _nodes(card, 'a', 'moreInfoInstagramLink')
        reference = _post_reference(_one(links).attrs.get('href')) if links else None
        attributes = _attributes(card)
        identity_parts = (reference or '', raw_date, title)
        local_key = sha256(('soundsystems\0observation\0' + '\0'.join(identity_parts)).encode()).hexdigest()
        evidence = EventEvidence('soundsystems', None, None, retrieved_at, 'live-automated', digest, position)
        event = SoundSystemEvent(key=identity('official', 'event', local_key), title=title, kind='event',
            source='Sound Systems of Melbourne', provenance='live-automated', rank=position,
            retrieved_at=retrieved_at, url=LISTING_URL, category='music', source_id='soundsystems',
            starts_at=start, source_start=raw_date, date_precision='date' if start else 'unknown',
            venue='' if withheld else location, evidence=(evidence,), listed_date=raw_date,
            listed_time=raw_time, start_clock=clocks[0], end_clock=clocks[1], source_location=location,
            location_status=status, referenced_post=reference, source_attributes=attributes,
            community=True if attributes else None)
        binding = (reference or title, raw_date)
        old = references.get(binding)
        if old and (old.title, old.listed_time, old.source_location, old.source_attributes) != (
                title, raw_time, location, attributes):
            raise SourceError('Conflicting Sound Systems observations.')
        references[binding] = event
        events.setdefault(event.key, event)
    return tuple(event for event in events.values() if not event.starts_at
                 or today.isoformat() <= event.starts_at <= horizon)[:MAX_EVENTS]


def fetch_listing(now):
    from shelf.discovery_transport import soundsystems
    try:
        code, raw = soundsystems()
        if code == 429:
            return Outcome('rate-limited', message='Sound Systems page rate limited; no retry or feed fallback attempted.')
        if code != 200:
            return Outcome('error', message='Sound Systems page unavailable; no retry or alternate route attempted.')
        cards = parse_listing(raw, retrieved_at=now.isoformat())
        message = ('Partial coverage: up to nine Sound Systems source observations within eight weeks; '
                   'unknown dates stay labelled. Locations, clock labels and source claims require confirmation.')
        if not cards:
            message = 'No eligible Sound Systems leads in this bounded page; wider coverage and ticket stock remain unknown.'
        return Outcome('ready' if cards else 'empty', cards, message)
    except TimeoutError:
        return Outcome('timeout', message='Sound Systems read timed out; no retry attempted.')
    except (SourceError, ValueError, TypeError, OSError, RecursionError):
        return Outcome('invalid', message='Sound Systems response could not be verified; coverage unknown.')
