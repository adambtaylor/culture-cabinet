"""Single-card detail enrichment; source labels never establish ticket inventory."""
from dataclasses import replace
from datetime import date
from hashlib import sha256
import re
from urllib.parse import urlsplit

from shelf.calendar_handoff import https_url
from shelf.discovery_sources import CACHE_TTL, MAX_BYTES, SourceError, label, strict_json, timestamp
from shelf.event_sources import SOURCES, official_url
from shelf.rrr_events import RRREvent, RRRDetails, _Listing, _Node, _one, event_path

MONTHS = 'January February March April May June July August September October November December'.split()
WEEKDAYS = 'Monday Tuesday Wednesday Thursday Friday Saturday Sunday'.split()
CLOCK = r'([0-9]{1,2}):([0-9]{2})\s*(am|pm)'


def detail_url(value):
    url = official_url(value, SOURCES['rrr'])
    if url != value:
        raise SourceError('Invalid Triple R detail destination.')
    event_path(urlsplit(url).path)
    return url


class _Detail(_Listing):
    def __init__(self):
        super().__init__()
        self.canonicals = []
        self.records = []

    def captures_root(self, tag, attributes):
        return tag == 'section' and 'event' in attributes.get('class', '').split()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        canonical = tag == 'link' and 'canonical' in attributes.get('rel', '').split()
        if canonical or 'data-content' in attributes:
            if len(attributes) != len(attrs):
                raise SourceError('Ambiguous detail identity attributes.')
            if canonical:
                self.canonicals.append(attributes.get('href'))
            if 'data-content' in attributes:
                self.records.append(strict_json(attributes['data-content'].encode('utf-8')))
        super().handle_starttag(tag, attrs)


def _clock(value):
    match = re.fullmatch(CLOCK, value)
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2])
    if not 1 <= hour <= 12 or minute >= 60:
        return None
    return f'{hour % 12 + (12 if match[3] == "pm" else 0):02d}:{minute:02d}'


def _date(value):
    match = re.fullmatch(r'(?:(\w+), )?([0-9]{1,2}) (\w+) ([0-9]{4})', value)
    if not match or match[3] not in MONTHS:
        return None
    parsed = date(int(match[4]), MONTHS.index(match[3]) + 1, int(match[2]))
    if match[1] and match[1] != WEEKDAYS[parsed.weekday()]:
        raise SourceError('Explicit weekday/date disagreement.')
    return parsed.isoformat()


def _dated_clock(value):
    # This source uses "4:15 pm, 25 October 2026" for explicit range endpoints.
    match = re.fullmatch(r'(' + CLOCK + r'), (.+)', value)
    if match:
        return _date(match[5]), _clock(match[1])
    return _date(value), None


def _when(node):
    children = [c for c in node.children if not isinstance(c, str) or c.strip()]
    raw = label(' '.join(c.text() if isinstance(c, _Node) else c for c in children))
    start_date = end_date = start_time = end_time = None
    if (len(children) == 2 and all(isinstance(c, _Node) and c.matches('span', 'd-block') for c in children)):
        clocks = re.split(r'\s*[-–]\s*', children[0].text())
        if len(clocks) in (1, 2):
            start_time = _clock(clocks[0])
            end_time = _clock(clocks[1]) if len(clocks) == 2 else None
        start_date = _date(children[1].text())
    elif (len(children) == 4 and isinstance(children[0], _Node) and isinstance(children[2], _Node)
            and children[0].matches('span', 'event__details-starts') and children[0].text() == 'Starts'
            and children[2].matches('span', 'event__details-ends') and children[2].text() == 'Ends'
            and isinstance(children[1], str) and isinstance(children[3], str)):
        start_date, start_time = _dated_clock(children[1].strip())
        end_date, end_time = _dated_clock(children[3].strip())
    if start_date and end_date and end_date < start_date:
        raise SourceError('Explicit event range runs backwards.')
    return raw, start_date, end_date, start_time, end_time


def _more_info(node):
    links = [link for link in node.find('a', 'action-button')
             if link.attrs.get('ga-event-action') == 'more-info-click']
    if len(links) != 1:
        return None
    value = links[0].attrs.get('href')
    try:
        https_url(value)
        host = urlsplit(value).hostname
        # Display only; no destination request, DNS lookup or seller verification.
        if (not re.fullmatch(r'(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}', host)
                or host.lower().endswith(('.localhost', '.local', '.internal', '.test', '.invalid'))):
            return None
    except (TypeError, ValueError):
        return None
    return value


def parse_detail(raw, *, event, retrieved_at):
    if type(event) is not RRREvent or not isinstance(raw, bytes) or len(raw) > MAX_BYTES or timestamp(retrieved_at) is None:
        raise SourceError('Invalid bounded event detail.')
    parser = _Detail()
    parser.feed(raw.decode('utf-8'))
    parser.close()
    if parser.stack or detail_url(_one(parser.canonicals)) != detail_url(event.url):
        raise SourceError('Missing, changed or mismatched detail page.')
    record = _one(parser.records)
    if (type(record) is not dict or record.get('type') != 'events' or type(record.get('id')) is not int
            or str(record['id']) != event.source_event_id or record.get('name') != event.title):
        raise SourceError('Detail identity disagrees with listing.')
    section = _one(parser.columns)
    if label(_one(section.find('h1', 'event__title')).text()) != event.title:
        raise SourceError('Detail title disagrees with listing.')
    groups = {}
    for group in section.find('div', 'event__details'):
        heading = label(_one(group.find('h2', 'event__details-heading')).text())
        if heading in groups:
            raise SourceError('Ambiguous event detail fields.')
        groups[heading] = group
    when = _one(groups['When'].find('p', 'event__details-copy'))
    raw_when, start_date, end_date, start_time, end_time = _when(when)
    venue = address = ''
    if 'Where' in groups:
        location = _one(groups['Where'].find('span', 'event__details-copy'))
        parts = location.find('p')
        if not 1 <= len(parts) <= 2:
            raise SourceError('Ambiguous source venue/address.')
        venue = label(parts[0].text())
        address = label(parts[1].text()) if len(parts) == 2 else ''
    if event.venue and venue and venue != event.venue:
        raise SourceError('Detail venue disagrees with listing; refresh listing.')
    return RRRDetails(raw_when, venue, address, retrieved_at, sha256(raw).hexdigest(),
                      start_date, end_date, start_time, end_time, _more_info(section))


def fetch_detail(event, now):
    """The caller must resolve the event from its current authorized listing."""
    from shelf.discovery_transport import rrr_detail
    fetched = timestamp(event.retrieved_at) if type(event) is RRREvent else None
    if fetched is None or not 0 <= (now - fetched).total_seconds() < CACHE_TTL.total_seconds():
        raise SourceError('A fresh Triple R listing is required.')
    detail_url(event.url)
    status, message, details = 'invalid', 'Detail response invalid; no enrichment retained.', None
    try:
        code, raw = rrr_detail(event.url)
        if code == 429:
            status, message = 'rate-limited', 'Source rate limited; no retry attempted.'
        elif code != 200:
            status, message = 'error', 'Detail request failed; no retry attempted.'
        else:
            details = parse_detail(raw, event=event, retrieved_at=now.isoformat())
            status, message = 'ready', 'Source detail retrieved; timezone and ticket availability unverified.'
    except TimeoutError:
        status, message = 'timeout', 'Detail source timed out; no retry attempted.'
    except (SourceError, KeyError, TypeError, ValueError, RecursionError, AttributeError, OSError):
        pass
    return replace(event, details=details, detail_status=status, detail_message=message,
                   detail_checked_at=now.isoformat())
