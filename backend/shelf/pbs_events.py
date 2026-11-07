"""Bounded PBS Events facts from its public upcoming-card section.

Drupal's midnight date metadata is not a performance start time. Only explicit
displayed calendar dates are normalized; article prose and images are discarded.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
import re

from shelf.discovery_sources import MAX_BYTES, Outcome, SourceError, identity, label, timestamp
from shelf.event_sources import EventCandidate, EventEvidence, MELBOURNE, SOURCES, official_url
from shelf.rrr_events import _Listing, _one

LISTING_URL = 'https://www.pbsfm.org.au/news/pbs-events'
MAX_CARDS = 48
MAX_EVENTS = 9
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


@dataclass(frozen=True)
class PBSEvent(EventCandidate):
    listed_date: str = ''
    source_notice: str = ''

    def date_status(self, now):
        precision = 'Exact session time and timezone unverified.' if self.starts_at else 'Year or date unverified.'
        return f'Source lists {self.listed_date}. {precision} Ticket availability unverified.'


def event_url(value):
    if not isinstance(value, str):
        raise SourceError('Missing PBS event path.')
    if value.startswith('/news/'):
        value = 'https://www.pbsfm.org.au' + value
    url = official_url(value, SOURCES['pbs'])
    path = url.removeprefix('https://www.pbsfm.org.au')
    if (not re.fullmatch(r'/news/[a-z0-9][a-z0-9-]{0,180}', path)
            or path in ('/news/pbs-events', '/news/pbs-presents')):
        raise SourceError('Exact public PBS event URL required.')
    return url


class _Cards(_Listing):
    def __init__(self):
        super().__init__()
        self.canonicals = []

    def captures_root(self, tag, attrs):
        classes = attrs.get('class', '').split()
        return (tag == 'h1' and 'title' in classes or tag == 'div'
                and 'view-taxonomy-news' in classes and 'view-display-id-attachment_1' in classes)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'link' and attributes.get('rel') == 'canonical':
            if len(attributes) != len(attrs):
                raise SourceError('Ambiguous PBS canonical identity.')
            self.canonicals.append(attributes.get('href'))
        super().handle_starttag(tag, attrs)


def _date(node):
    text = label(node.text())
    match = re.fullmatch(r'([A-Za-z]{3}) (\d{1,2}) ([A-Za-z]{3}) (\d{4})', text)
    if not match:
        return None  # A machine date cannot fill a missing visible year.
    weekday, day, month, year = match.groups()
    try:
        value = date(int(year), MONTHS.index(month) + 1, int(day))
    except (ValueError, OverflowError):
        raise SourceError('Invalid explicit PBS date.') from None
    if WEEKDAYS[value.weekday()] != weekday:
        raise SourceError('Conflicting PBS weekday and date.')
    machine = node.attrs.get('content')
    if machine is not None:
        observed = timestamp(machine)
        if observed is None or observed.date() != value:
            raise SourceError('Conflicting visible and machine PBS dates.')
    return value.isoformat()


def _dates(field):
    single = field.find('span', 'date-display-single')
    starts, ends = field.find('span', 'date-display-start'), field.find('span', 'date-display-end')
    if single and not starts and not ends:
        return _date(_one(single)), None
    if not single and len(starts) == len(ends) == 1:
        start, end = _date(starts[0]), _date(ends[0])
        if start and end and end < start:
            raise SourceError('Reversed PBS date range.')
        return start, end
    raise SourceError('Missing or ambiguous PBS date fields.')


def parse_listing(raw, *, retrieved_at):
    now = timestamp(retrieved_at)
    if type(raw) is not bytes or len(raw) > MAX_BYTES or now is None:
        raise SourceError('Bounded PBS HTML and aware retrieval time required.')
    parser = _Cards()
    parser.feed(raw.decode('utf-8'))
    parser.close()
    if parser.stack or parser.canonicals != [LISTING_URL]:
        raise SourceError('Unrecognizable or truncated PBS listing.')
    title = _one([n for n in parser.columns if n.tag == 'h1'])
    upcoming = _one([n for n in parser.columns if n.tag == 'div'])
    if label(title.text()) != 'PBS Events':
        raise SourceError('PBS listing title changed.')
    cards = upcoming.find('div', 'view-mode-card')
    # A missing/empty attachment may be source drift, not proof of no events.
    if not cards or len(cards) > MAX_CARDS:
        raise SourceError('PBS upcoming-card coverage unknown or oversized.')
    digest = sha256(raw).hexdigest()
    today = now.astimezone(MELBOURNE).date()
    end_window = (today + timedelta(weeks=8)).isoformat()
    events = {}
    for position, card in enumerate(cards, 1):
        if not card.matches('div', 'node-news'):
            raise SourceError('Changed PBS event card.')
        anchor = _one(card.find('a', 'group-more-info'))
        url = event_url(anchor.attrs.get('href'))
        if event_url(card.attrs.get('about')) != url:
            raise SourceError('Conflicting PBS event identities.')
        if label(_one(anchor.find('div', 'field-name-field-news-category')).text()) != 'PBS Events':
            raise SourceError('Changed PBS event category.')
        name = label(_one(_one(anchor.find('div', 'field-title')).find('h3')).text())
        field = _one(anchor.find('div', 'field-name-field-news-date'))
        listed_date = label(field.text())
        start, end = _dates(field)
        alerts = card.find('div', 'field-name-field-news-alert')
        notice = label(_one(alerts).text()) if alerts else ''
        evidence = EventEvidence('pbs', None, url, retrieved_at, 'live-automated', digest, position)
        event = PBSEvent(key=identity('official', 'event', sha256(('pbs\0url\0' + url).encode()).hexdigest()),
            title=name, kind='event', source='PBS Events', provenance='live-automated', rank=position,
            retrieved_at=retrieved_at, url=url, category='music', source_id='pbs', official_url=url,
            starts_at=start, source_start=listed_date, ends_at=end,
            date_precision='date' if start else 'unknown', end_precision='date' if end else 'unknown',
            evidence=(evidence,), listed_date=listed_date, source_notice=notice)
        old = events.get(url)
        if old and (old.title, old.listed_date, old.starts_at, old.ends_at, old.source_notice) != (
                name, listed_date, start, end, notice):
            raise SourceError('Conflicting PBS event facts.')
        events.setdefault(url, event)
    # Dates are calendar evidence only. Missing years stay visible as unknown;
    # explicit past/out-of-window dates do not become fresh recommendations.
    return tuple(e for e in events.values() if not e.starts_at or (
        (e.ends_at or e.starts_at) >= today.isoformat() and e.starts_at <= end_window))[:MAX_EVENTS]


def fetch_listing(now):
    from shelf.discovery_transport import PBSIntervalPending, pbs
    try:
        code, raw = pbs()
        if code == 429:
            return Outcome('rate-limited', message='PBS rate limited this read; no retry attempted.')
        if code != 200:
            return Outcome('error', message='PBS listing unavailable; no retry or alternate route attempted.')
        cards = parse_listing(raw, retrieved_at=now.isoformat())
        message = ('Partial PBS coverage: up to nine upcoming-section leads within eight weeks; '
                   'unknown dates are labelled. Exact sessions and ticket availability unverified.')
        if not cards:
            message = 'No eligible dated PBS leads in this bounded page; this does not establish no events or tickets.'
        return Outcome('ready' if cards else 'empty', cards, message)
    except PBSIntervalPending:
        return Outcome('rate-limited', message='PBS requires ten seconds between shared requests; try again later.')
    except TimeoutError:
        return Outcome('timeout', message='PBS read timed out; no retry attempted.')
    except (SourceError, UnicodeError, ValueError, TypeError, OSError, RecursionError):
        return Outcome('invalid', message='PBS response or access interval could not be verified; coverage unknown.')
