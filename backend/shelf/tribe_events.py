"""Tribe's public RSS announcements; publication dates never become event starts.

Only fixed-feed metadata is projected. Description HTML, media and links inside
it are ignored. The publisher disallows format=ical, so no calendar reader exists.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
import re
from xml.etree import ElementTree

from shelf.discovery_sources import MAX_BYTES, Outcome, SourceError, identity, timestamp
from shelf.event_sources import EventCandidate, EventEvidence, MAX_EVENTS

ORIGIN = 'https://www.tribesoundsystem.com'
LISTING_URL = ORIGIN + '/events'
RSS_URL = LISTING_URL + '?format=rss'
MAX_ITEMS = 48
MAX_NODES = 2000
MAX_DEPTH = 16


def event_url(value):
    """Exact canonical event path; never a transport destination from source text."""
    if (not isinstance(value, str) or len(value) > 512
            or not re.fullmatch(re.escape(LISTING_URL) + r'/[a-z0-9][a-z0-9-]{0,180}', value)):
        raise SourceError('An exact canonical Tribe event URL is required.')
    return value


def _xml(raw):
    if type(raw) is not bytes or not raw or len(raw) > MAX_BYTES:
        raise SourceError('Bounded RSS bytes are required.')
    text = raw.decode('utf-8-sig')
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
        raise SourceError('RSS document/entity declarations are not supported.')
    parser = ElementTree.XMLPullParser(events=('start', 'end'))
    depth = nodes = 0
    root = None
    try:
        for offset in range(0, len(text), 4096):
            parser.feed(text[offset:offset + 4096])
            for action, node in parser.read_events():
                if action == 'start':
                    depth += 1
                    nodes += 1
                    if root is None:
                        root = node
                    if depth > MAX_DEPTH or nodes > MAX_NODES:
                        raise SourceError('RSS structure budget exceeded.')
                else:
                    depth -= 1
        parser.close()
    except ElementTree.ParseError:
        raise SourceError('Invalid or truncated RSS; coverage unknown.') from None
    if depth or root is None:
        raise SourceError('Incomplete RSS; coverage unknown.')
    return root


def _text(parent, tag, *, required=True, limit=300):
    elements = parent.findall(tag)
    if not elements and not required:
        return ''
    if len(elements) != 1 or len(elements[0]):
        raise SourceError('Missing or ambiguous RSS metadata.')
    value = (elements[0].text or '').strip()
    if (required and not value) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise SourceError('Invalid RSS metadata text.')
    return value


def _publication_time(value):
    """RSS timestamps describe publication/build only; absence stays unknown."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.isoformat() if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass(frozen=True)
class TribeEvent(EventCandidate):
    published_at: str | None = None
    publication_label: str = ''
    feed_built_at: str | None = None
    feed_build_label: str = ''

    def date_status(self, now):
        return ('Announcement only: event date, time and location unknown. RSS publication is not an event start. '
                'Cancellation and ticket availability unverified; check the original source.')

    def freshness(self, now):
        retrieval = super().freshness(now)
        built = timestamp(self.feed_built_at)
        if built is None:
            detail = 'Feed update time unknown.'
        elif built > now:
            detail = 'Feed build time is future dated; source freshness unverified.'
        elif now - built > timedelta(days=7):
            detail = 'Feed build is older than seven days; it may include past announcements.'
        else:
            detail = 'Recent feed build does not establish an upcoming event.'
        return retrieval + ' ' + detail


def parse_listing(raw, *, retrieved_at):
    if timestamp(retrieved_at) is None:
        raise SourceError('An aware retrieval timestamp is required.')
    root = _xml(raw)
    channels = root.findall('channel')
    if root.tag != 'rss' or root.get('version') != '2.0' or len(channels) != 1:
        raise SourceError('Expected the Tribe RSS 2.0 channel.')
    channel = channels[0]
    _text(channel, 'title')
    if _text(channel, 'link', limit=512) not in (LISTING_URL, LISTING_URL + '/'):
        raise SourceError('RSS channel is outside the selected Tribe source.')
    built_label = _text(channel, 'lastBuildDate', required=False, limit=100)
    built = _publication_time(built_label)
    items = channel.findall('item')
    if len(items) > MAX_ITEMS:
        raise SourceError('RSS item budget exceeded.')
    digest = sha256(raw).hexdigest()
    events, guid_urls = {}, {}
    for position, item in enumerate(items, 1):
        name = _text(item, 'title')
        url = event_url(_text(item, 'link', limit=512))
        guid = _text(item, 'guid', required=False, limit=200)
        if guid:
            permalink = item.find('guid').get('isPermaLink', 'true')
            if permalink not in ('true', 'false') or (permalink == 'true' and event_url(guid) != url):
                raise SourceError('RSS GUID disagrees with its event link.')
            if guid in guid_urls and guid_urls[guid] != url:
                raise SourceError('RSS GUID identifies conflicting event links.')
            guid_urls[guid] = url
        published_label = _text(item, 'pubDate', required=False, limit=100)
        evidence = EventEvidence('tribe', guid or None, url, retrieved_at, 'live-automated', digest, position)
        key_id = sha256(('tribe\0' + ('guid\0' + guid if guid else 'url\0' + url)).encode()).hexdigest()
        event = TribeEvent(key=identity('official', 'event', key_id), title=name, kind='event',
            source='Tribe Soundsystem RSS', provenance='live-automated', rank=position,
            retrieved_at=retrieved_at, url=url, category='music', source_id='tribe',
            source_event_id=guid or None, official_url=url, evidence=(evidence,),
            published_at=_publication_time(published_label), publication_label=published_label,
            feed_built_at=built, feed_build_label=built_label)
        previous = events.get(url)
        if previous and (previous.title, previous.source_event_id, previous.publication_label) != (name, guid or None, published_label):
            raise SourceError('RSS event link has conflicting identity or publication metadata.')
        events.setdefault(url, event)
    # Validate the whole bounded feed before selecting the first nine source
    # positions. Old publication dates cannot safely classify events as past.
    return tuple(events.values())[:MAX_EVENTS]


def fetch_listing(now):
    """One explicit anonymous RSS read. Caller owns authorization and cache TTL."""
    from shelf.discovery_transport import tribe
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise SourceError('An aware clock is required before a source request.')
    try:
        code, raw = tribe()
        if code == 429:
            return Outcome('rate-limited', message='Tribe rate limited this read; no retry attempted.')
        if code != 200:
            return Outcome('error', message='Tribe RSS unavailable; no retry or alternate route attempted.')
        cards = parse_listing(raw, retrieved_at=now.isoformat())
        message = ('Partial Tribe RSS coverage: up to nine announcements, not confirmed upcoming sessions. '
                   'Event dates, locations, cancellation and ticket availability unknown. '
                   'Calendar enrichment unavailable: the publisher disallows automated format=ical reads.')
        if not cards:
            message = 'No announcements in this valid bounded RSS response; this does not establish no events or tickets.'
        return Outcome('ready' if cards else 'empty', cards, message)
    except TimeoutError:
        return Outcome('timeout', message='Tribe RSS timed out; no retry attempted.')
    except (SourceError, UnicodeError, OSError):
        return Outcome('invalid', message='Tribe RSS could not be verified; source coverage unknown.')
