"""One Triple R listing page, treated as attributed leads rather than inventory.

The public HTML omits event years. Retain its date label without converting it
to an instant or asserting that an event is upcoming. Detail reads are explicit.
"""
from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
import re

from shelf.discovery_sources import MAX_BYTES, SourceError, identity, label, strict_json, timestamp
from shelf.event_sources import EventCandidate, EventEvidence, MAX_EVENTS, SOURCES, official_url

LISTING_URL = 'https://www.rrr.org.au/events'
VOID_TAGS = frozenset('area base br col embed hr img input link meta param source track wbr'.split())


@dataclass(frozen=True)
class RRRDetails:
    when: str
    venue: str
    address: str
    retrieved_at: str
    snapshot_sha256: str
    start_date: str | None = None
    end_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    more_info_url: str | None = None


@dataclass(frozen=True)
class RRREvent(EventCandidate):
    listed_date: str = ''
    details: RRRDetails | None = None
    detail_status: str = ''
    detail_message: str = ''
    detail_checked_at: str | None = None

    def date_status(self, now):
        if self.details:
            unknown = ['timezone unverified']
            if not self.details.start_date:
                unknown.insert(0, 'year unverified')
            if not self.details.start_time:
                unknown.append('start time unverified')
            return f'Source detail lists {self.details.when}; {", ".join(unknown)}.'
        return f'Source lists {self.listed_date}; year and start time unverified.'


@dataclass
class _Node:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)

    def matches(self, tag=None, css=None):
        return (tag is None or self.tag == tag) and (css is None or css in self.attrs.get('class', '').split())

    def find(self, tag=None, css=None):
        result = [self] if self.matches(tag, css) else []
        for child in self.children:
            if isinstance(child, _Node):
                result.extend(child.find(tag, css))
        return result

    def text(self):
        return ' '.join(''.join(c.text() if isinstance(c, _Node) else c for c in self.children).split())


class _Listing(HTMLParser):
    """Capture only grid columns, with explicit depth/node limits and no I/O."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.columns = []
        self.nodes = 0

    def captures_root(self, tag, attributes):
        return tag == 'div' and 'grid-col' in attributes.get('class', '').split()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if not self.stack and not self.captures_root(tag, attributes):
            return
        if len(attributes) != len(attrs) or any(value is None for value in attributes.values()):
            raise SourceError('Ambiguous listing attributes.')
        self.nodes += 1
        if self.nodes > 4000 or len(self.stack) >= 48:
            raise SourceError('Listing structure budget exceeded.')
        node = _Node(tag, attributes)
        if self.stack:
            self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.stack or tag in VOID_TAGS:
            return
        if self.stack[-1].tag != tag:
            raise SourceError('Changed or malformed listing structure.')
        node = self.stack.pop()
        if not self.stack:
            self.columns.append(node)

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].children.append(data)


def _one(nodes):
    if len(nodes) != 1:
        raise SourceError('Missing or ambiguous event field.')
    return nodes[0]


def event_path(value):
    # Flat character classes keep rejection linear, including long malformed
    # source slugs. Triple R legitimately uses repeated hyphens in event URLs.
    if not isinstance(value, str) or not re.fullmatch(r'/events/[a-z0-9][a-z0-9-]*', value):
        raise SourceError('Invalid source event detail path.')
    return value


def _event(column, position, retrieved_at, digest):
    card = _one(column.find('div', 'card'))
    marker = _one([node for node in column.find() if 'data-content' in node.attrs])
    record = strict_json(marker.attrs['data-content'].encode('utf-8'))
    if (type(record) is not dict or record.get('type') != 'events'
            or type(record.get('id')) is not int or not 0 < record['id'] < 2**63):
        raise SourceError('Invalid source event identity.')
    title = _one(card.find('h1', 'card__title'))
    anchor = _one(title.find('a', 'card__anchor'))
    name = label(anchor.text())
    if name != label(record.get('name')):
        raise SourceError('Conflicting source event title.')
    path = event_path(anchor.attrs.get('href', ''))
    url = official_url('https://www.rrr.org.au' + path, SOURCES['rrr'])
    details = _one(card.find('span', 'card__meta'))
    lines = details.find('div', 'd-block')
    # Cards without a venue use a plain date span instead of block children.
    if not lines and all(isinstance(child, str) for child in details.children):
        lines = [details]
    if not 1 <= len(lines) <= 2:
        raise SourceError('Changed source date/venue fields.')
    listed_date = label(lines[0].text())
    venue = label(lines[1].text()) if len(lines) == 2 else ''
    calendar = label(_one(card.find('div', 'card__meta')).text())
    # Calendar labels are source classifications, not proof of admission or genre.
    category = {'Gig Guide': 'music', 'Community Events': 'community'}.get(calendar, 'all')
    event_id = str(record['id'])
    key_id = sha256(f'rrr\0id\0{event_id}'.encode()).hexdigest()
    evidence = EventEvidence('rrr', event_id, url, retrieved_at, 'live-automated', digest, position)
    return RRREvent(key=identity('official', 'event', key_id), title=name, kind='event',
        source='Triple R events', provenance='live-automated', rank=position,
        retrieved_at=retrieved_at, url=url, category=category, venue=venue,
        source_id='rrr', source_event_id=event_id, official_url=url,
        evidence=(evidence,), listed_date=listed_date)


def parse_listing(raw, *, retrieved_at):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES or timestamp(retrieved_at) is None:
        raise SourceError('Invalid bounded listing or retrieval time.')
    parser = _Listing()
    parser.feed(raw.decode('utf-8'))
    parser.close()
    if parser.stack:
        raise SourceError('Truncated source listing.')
    columns = [column for column in parser.columns if column.find('div', 'card')]
    # An empty/challenge/redesigned HTML page is not evidence of zero events.
    if not columns:
        raise SourceError('No recognizable event cards; coverage unknown.')
    digest = sha256(raw).hexdigest()
    events = [_event(column, position, retrieved_at, digest)
              for position, column in enumerate(columns[:MAX_EVENTS], 1)]
    if len({e.key for e in events}) != len(events) or len({e.url for e in events}) != len(events):
        raise SourceError('Duplicate or conflicting event identities.')
    return tuple(events)
