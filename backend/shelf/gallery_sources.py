"""Current, minimal facts from two fixed primary gallery pages.

Opening sessions, exhibition runs and sale offers remain separate. This module
has no preference writes, image retrieval, JSON-LD expansion or purchase action.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from hashlib import sha256
import re
import time
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

from shelf.discovery_sources import Candidate, Outcome, SourceError, timestamp
from shelf.rrr_events import _Listing, _one

MAX_BYTES = 512 * 1024
MELBOURNE = ZoneInfo('Australia/Melbourne')
GERTRUDE_URL = 'https://gertrude.org.au/exhibition/et-cetera/'
APW_URL = 'https://www.australianprintworkshop.com/'
OFFER_URL = APW_URL + 'megan-cope/goompi'
MONTHS = ('January February March April May June July August September October November December').split()
WEEKDAYS = ('Monday Tuesday Wednesday Thursday Friday Saturday Sunday').split()


@dataclass(frozen=True)
class GalleryEvent(Candidate):
    opening_ends_at: str | None = None
    opening_date: str | None = None
    opening_wording: str = ''
    exhibition_starts_on: str | None = None
    exhibition_ends_on: str | None = None
    run_wording: str = ''
    date_precision: str = 'unknown'
    timezone: str = 'Australia/Melbourne'
    checked_at: str | None = None
    source_url: str = ''
    artists: tuple = ()
    media: tuple = ()
    source_topics: tuple = ()
    public_facts: tuple = ()

    @property
    def gallery_data(self):
        names = ('opening_ends_at', 'opening_date', 'opening_wording',
                 'exhibition_starts_on', 'exhibition_ends_on', 'run_wording',
                 'date_precision', 'timezone', 'checked_at', 'source_url',
                 'artists', 'media', 'source_topics')
        return dict({name: getattr(self, name) for name in names}, opening_starts_at=self.starts_at)

    def date_status(self, now):
        if timestamp(self.starts_at) and timestamp(self.starts_at) < now:
            return 'Opening has passed; exhibition run is separate. Confirm current access on the source.'
        return 'Source opening time in Melbourne; admission and attendance availability unverified.'


@dataclass(frozen=True)
class GalleryOutcome(Outcome):
    offers: tuple = ()
    source_checks: tuple = ()


class _Metadata(_Listing):
    def __init__(self, *, include_shipping=False):
        super().__init__()
        self.include_shipping = include_shipping

    def captures_root(self, tag, attrs):
        css = attrs.get('class', '').split()
        return (tag == 'h1' or tag == 'h4' and 'main-title' in css
                or tag == 'div' and 'location-dates' in css
                or tag == 'div' and 'sidebar__hero' in css
                or tag in ('h2', 'p') and any(c.startswith('print__') for c in css)
                or tag == 'address' or 'exhibition-artists' in css
                or self.include_shipping and tag == 'p' and not css)


def _document(raw, retrieved_at, *, include_shipping=False):
    if type(raw) is not bytes or len(raw) > MAX_BYTES or timestamp(retrieved_at) is None:
        raise SourceError('Bounded HTML and aware retrieval time required.')
    parser = _Metadata(include_shipping=include_shipping)
    parser.feed(raw.decode('utf-8'))
    parser.close()
    if parser.stack:
        raise SourceError('Truncated gallery metadata.')
    return parser.columns


def _date(day, month, year, weekday=None):
    result = date(int(year), MONTHS.index(month) + 1, int(day))
    if weekday and WEEKDAYS[result.weekday()] != weekday:
        raise SourceError('Conflicting opening weekday and explicit date.')
    return result


def _instant(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MELBOURNE).isoformat()


def _event(title, source, url, retrieved_at, opening, start, end=None, run_start=None,
           run_end=None, run_wording=''):
    # Homepages are reused for later exhibitions. Bind identity to the named
    # event and explicit calendar dates, independently of retrieval time.
    identity = '\0'.join((url, title, start[:10], run_start.isoformat() if run_start else ''))
    return GalleryEvent(key='official:event:' + sha256(identity.encode()).hexdigest(),
        title=title, kind='event', source=source, provenance='live-automated',
        rank=1, retrieved_at=retrieved_at, url=url, category='gallery', starts_at=start,
        locality='Melbourne', venue=source, opening_ends_at=end,
        opening_date=start[:10], opening_wording=opening,
        exhibition_starts_on=run_start.isoformat() if run_start else None,
        exhibition_ends_on=run_end.isoformat() if run_end else None,
        run_wording=run_wording, date_precision='datetime', checked_at=retrieved_at, source_url=url)


def parse_gertrude(raw, *, retrieved_at):
    nodes = _document(raw, retrieved_at)
    title = _one([n for n in nodes if n.tag == 'h1']).text()
    run = _one([n for n in nodes if n.tag == 'h4']).text()
    opening_nodes = [n for n in nodes if n.matches('div', 'location-dates')]
    opening = _one(opening_nodes).text() if opening_nodes else ''
    # The opening's omitted year is bound only to this same exhibition's explicit
    # run year; never to today's year or a next-year rollover.
    match = re.fullmatch(r'(\d{1,2}) ([A-Z][a-z]+)\s*[-–]\s*(\d{1,2}) ([A-Z][a-z]+) (\d{4})', run)
    session = re.fullmatch(r'Opening event:\s*([A-Z][a-z]+) (\d{1,2}) ([A-Z][a-z]+), (\d{1,2})-(\d{1,2})pm', opening)
    if not 1 <= len(title) <= 300 or not match or opening and not session:
        raise SourceError('Gertrude metadata changed; opening not verified.')
    d1, m1, d2, m2, year = match.groups()
    first, last = _date(d1, m1, year), _date(d2, m2, year)
    if first > last:
        raise SourceError('Conflicting Gertrude run.')
    if session is None:
        identity = '\0'.join((GERTRUDE_URL, title, first.isoformat(), last.isoformat()))
        return GalleryEvent(key='official:event:' + sha256(identity.encode()).hexdigest(),
            title=title, kind='event', source='Gertrude Contemporary', provenance='live-automated',
            rank=1, retrieved_at=retrieved_at, url=GERTRUDE_URL, category='gallery',
            exhibition_starts_on=first.isoformat(), exhibition_ends_on=last.isoformat(),
            run_wording=run, date_precision='date', checked_at=retrieved_at,
            source_url=GERTRUDE_URL, public_facts=_listed_facts(nodes))
    weekday, day, month, start_hour, end_hour = session.groups()
    opening_day = _date(day, month, year, weekday)
    start_hour, end_hour = int(start_hour), int(end_hour)
    # A range with a shared pm suffix is accepted only within one afternoon.
    if not (1 <= start_hour < end_hour <= 11 and opening_day <= first <= last
            and (first - opening_day).days <= 7):
        raise SourceError('Ambiguous Gertrude opening or run.')
    return replace(_event(title, 'Gertrude Contemporary', GERTRUDE_URL, retrieved_at, opening,
                  _instant(opening_day, start_hour + 12), _instant(opening_day, end_hour + 12),
                  first, last, run), public_facts=_listed_facts(nodes))


def _listed_facts(nodes):
    """Only explicit artist/participant and venue fields, never general prose."""
    artists, facts = [], []
    for root in nodes:
        address_nodes = {id(child) for address in root.find('address') for child in address.find()}
        exhibition_scope = root.matches(css='sidebar__hero') or root.matches(css='exhibition-artists')
        for node in root.find():
            prop = node.attrs.get('itemprop', '')
            css = node.attrs.get('class', '').split()
            if exhibition_scope and (prop in ('artist', 'creator') or 'exhibition-artist' in css):
                name = node.text()
                if 0 < len(name) <= 200 and name not in [n for n, _ in artists]:
                    href = node.attrs.get('href')
                    from urllib.parse import urlsplit
                    link = urlsplit(href) if type(href) is str else None
                    safe = (href if link and link.scheme == 'https' and link.hostname in
                        ('gertrude.org.au', 'www.australianprintworkshop.com') and not link.username
                        and not link.password and not link.query and not link.fragment else None)
                    artists.append((name, safe))
            mapping = {'addressCountry': 'venue.address.country', 'postalCode': 'venue.address.postcode',
                'addressLocality': 'venue.address.locality', 'addressRegion': 'venue.address.region',
                'latitude': 'venue.latitude', 'longitude': 'venue.longitude'}
            if id(node) in address_nodes and prop in mapping:
                value = node.attrs.get('content', node.text())
                if type(value) is str and 0 < len(value) <= 200:
                    if prop in ('latitude', 'longitude'):
                        from .postal_locality import _coordinate
                        from decimal import Decimal
                        try:
                            _coordinate(value, -90 if prop == 'latitude' else -180,
                                        90 if prop == 'latitude' else 180)
                            value = format(Decimal(value).normalize(), 'f')
                        except ValueError:
                            continue
                    facts.append((mapping[prop], value))
    # Conflicting venue fields are unknown; do not pick the first location.
    unique = {}
    for name, value in facts:
        unique.setdefault(name, set()).add(value)
    facts = [(name, next(iter(values))) for name, values in sorted(unique.items()) if len(values) == 1]
    for i, (name, link) in enumerate(artists[:10]):
        facts.append((f'gallery.artist_name.{i}', name))
        if link:
            facts.append((f'gallery.artist_source_ref.{i}', link))
    return tuple(facts)


def parse_apw(raw, *, retrieved_at):
    nodes = _document(raw, retrieved_at)
    hero = _one([n for n in nodes if n.matches('div', 'sidebar__hero')])
    heading = _one(hero.find('p', 'sidebar__hero-text')).text()
    prefix = 'Australian Print Workshop '
    title = heading.removeprefix(prefix)
    if not heading.startswith(prefix) or not 1 <= len(title) <= 300:
        raise SourceError('APW exhibition title unavailable.')
    sessions = hero.find('a', 'sidebar__hero-action')
    opening = _one(sessions).text() if sessions else ''
    match = re.fullmatch(r'Opening (\d{1,2})(am|pm)\. ([A-Z][a-z]+) (\d{1,2}) ([A-Z][a-z]+) (\d{4})', opening)
    opening_day = start = None
    if opening.startswith('Opening'):
        if not match:
            raise SourceError('Ambiguous APW opening metadata.')
        hour, meridian, weekday, day, month, year = match.groups()
        if not 1 <= int(hour) <= 12:
            raise SourceError('Invalid opening clock.')
        opening_day = _date(day, month, year, weekday)
        start = _instant(opening_day, int(hour) % 12 + (12 if meridian == 'pm' else 0))
    run_nodes = hero.find(css='sidebar__hero-dates')
    run = _one(run_nodes).text() if run_nodes else ''
    run_start = run_end = None
    if run:
        dates = re.fullmatch(r'(\d{1,2}) ([A-Z][a-z]+)\s*[-–]\s*(\d{1,2}) ([A-Z][a-z]+) (\d{4})', run)
        if not dates:
            raise SourceError('Ambiguous APW exhibition run.')
        d1, m1, d2, m2, year = dates.groups()
        run_start, run_end = _date(d1, m1, year), _date(d2, m2, year)
        if run_end < run_start:
            raise SourceError('Conflicting APW exhibition run.')
    # Same homepage can host a later occurrence. Retrieval is never identity.
    identity = '\0'.join((APW_URL, title, start or '', str(run_start or ''), str(run_end or '')))
    return GalleryEvent(key='official:event:' + sha256(identity.encode()).hexdigest(),
        title=title, kind='event', source='Australian Print Workshop', provenance='live-automated',
        rank=1, retrieved_at=retrieved_at, url=APW_URL, category='gallery', starts_at=start,
        venue='Australian Print Workshop', opening_date=opening_day.isoformat() if opening_day else None,
        opening_wording=opening if start else '', exhibition_starts_on=str(run_start) if run_start else None,
        exhibition_ends_on=str(run_end) if run_end else None, run_wording=run,
        date_precision='datetime' if start else 'date' if run_start else 'unknown',
        checked_at=retrieved_at, source_url=APW_URL, public_facts=_listed_facts(nodes))


def parse_offer(raw, *, retrieved_at):
    nodes = _document(raw, retrieved_at, include_shipping=True)
    artist = _one([n for n in nodes if n.tag == 'h1' and n.matches(css='print__title')]).text()
    title = _one([n for n in nodes if n.tag == 'h2' and n.matches(css='print__title')]).text()
    price = _one([n for n in nodes if n.matches('p', 'print__price')]).text()
    match = re.fullmatch(r'\$([0-9,]+(?:\.\d{2})?) AUD \(GST inc\)', price)
    details = _one([n for n in nodes if n.matches('p', 'print__details')]).text()
    # APW uses this class for both its medium label and process prose. Keep
    # only a recognizable short medium; ambiguity leaves the medium unknown.
    descriptions = [n.text() for n in nodes if n.matches('p', 'print__description')]
    media = [text for text in descriptions if len(text) <= 160
             and re.fullmatch(r'Lithograph(?: \([a-z ,\-]{1,120}\))?', text)]
    medium = media[0] if len(media) == 1 else ''
    if len(details) > 300:
        raise SourceError('Oversized offer metadata.')
    if artist != 'Megan Cope' or title != 'Goompi' or not match:
        raise SourceError('APW offer identity or price changed.')
    components = {name: {'status': 'unknown', 'source_basis': ''} for name in
                  ('artwork', 'shipping', 'framing', 'fees', 'tax', 'installation', 'fx')}
    components['artwork'] = {'status': 'known', 'amount': match[1].replace(',', ''), 'source_basis': price}
    components['tax'] = {'status': 'included', 'source_basis': '(GST inc)'}
    shipping = [node.text() for node in nodes if node.tag == 'p'
                and re.fullmatch(r'Within Australia: \$[0-9]+(?:\.\d{2})? per tube \(including GST\)', node.text())]
    if len(shipping) == 1:
        components['shipping']['source_basis'] = shipping[0] + '; conditional tariff, exact delivery charge unverified'
    # The listed domestic tube tariff is informational, not a delivered quote.
    # No address, framing requirement or checkout total has been established.
    return {'offer_id': 'apw-megan-cope-goompi', 'title': title, 'artist': artist,
            'seller': 'Australian Print Workshop', 'offer_url': OFFER_URL, 'source_url': OFFER_URL,
            'currency': 'AUD', 'availability': 'unknown', 'checked_at': retrieved_at,
            'components': components, 'edition': details, 'medium': medium or 'Medium unverified',
            'purchase_authorized': False}


def _eligible(event, now):
    today = now.astimezone(MELBOURNE).date()
    if not (event.exhibition_ends_on or event.opening_date):
        return False
    last = date.fromisoformat(event.exhibition_ends_on or event.opening_date)
    first = date.fromisoformat(event.exhibition_starts_on or event.opening_date)
    return today <= last and first <= today + timedelta(weeks=8)


def fetch(now, *, transport=None):
    """Explicit refresh, at most five fixed reads and 512 KiB in total.

    A transport seam receives only allowlisted URLs and returns (status, bytes).
    Runtime transport owns cross-process host intervals and refusal backoff.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return GalleryOutcome('invalid', message='Aware current time required.')
    if transport is None:
        from shelf.gallery_transport import read
        deadline = time.monotonic() + 30
        transport = lambda target: read(target, max_bytes=remaining, deadline=deadline)
    checked_at = now.isoformat()
    candidates, offers, checks = [], [], []
    remaining = MAX_BYTES
    for name, base, url, parser in (
            ('Gertrude Contemporary', 'https://gertrude.org.au', GERTRUDE_URL, parse_gertrude),
            ('Australian Print Workshop', APW_URL.rstrip('/'), APW_URL, parse_apw)):
        stage = 'robots'
        checked_url = base + '/robots.txt'
        try:
            def get(target):
                nonlocal remaining, checked_url
                checked_url = target
                code, raw = transport(target)
                if type(raw) is not bytes or len(raw) > remaining:
                    raise SourceError('Gallery total response budget exceeded.')
                remaining -= len(raw)
                if code in (403, 429):
                    raise SourceError('refused' if code == 403 else 'rate-limited')
                return code, raw
            code, raw = get(base + '/robots.txt')
            rules = RobotFileParser()
            robots_missing = code == 404
            if code == 200:
                if b'<html' in raw.lower() or b'user-agent:' not in raw.lower():
                    raise SourceError('Unrecognized robots response.')
                rules.parse(raw.decode('utf-8').splitlines())
                if not rules.can_fetch('CultureCabinet', url):
                    raise SourceError('robots-refused')
                delay = rules.crawl_delay('CultureCabinet') or rules.crawl_delay('*') or 0
                if delay > 10:
                    raise SourceError('robots-delay-unsupported')
            elif code != 404:
                raise SourceError('robots-unavailable')
            stage = 'listing'
            code, raw = get(url)
            if code != 200:
                raise SourceError('unavailable')
            event = parser(raw, retrieved_at=checked_at)
            eligible = _eligible(event, now)
            if eligible:
                candidates.append(event)
            checks.append({'source': name, 'status': 'ready' if eligible else 'stale',
                           'source_url': url, 'checked_at': checked_at})
            if url == APW_URL:
                stage = 'offer'
                checked_url = OFFER_URL
                if not robots_missing and not rules.can_fetch('CultureCabinet', OFFER_URL):
                    raise SourceError('robots-refused')
                code, raw = get(OFFER_URL)
                if code != 200:
                    raise SourceError('unavailable')
                offers.append(parse_offer(raw, retrieved_at=checked_at))
        except TimeoutError:
            checks.append({'source': name, 'status': 'timeout', 'stage': stage,
                           'source_url': checked_url,
                           'checked_at': checked_at})
        except (SourceError, ValueError, TypeError, UnicodeError, OSError, RecursionError) as error:
            status = str(error) if str(error) in ('refused', 'rate-limited', 'robots-refused',
                'robots-unavailable', 'robots-delay-unsupported', 'unavailable') else 'parser-changed'
            checks.append({'source': name, 'status': status, 'stage': stage,
                           'source_url': checked_url,
                           'checked_at': checked_at})
    status = 'ready' if candidates or offers else ('empty' if checks and all(
        c['status'] == 'stale' for c in checks) else 'unavailable')
    return GalleryOutcome(status, tuple(candidates),
        'Partial primary-source check; opening sessions and exhibition runs differ. '
        'Unknown dates, stock and complete costs remain unverified.', tuple(offers), tuple(checks))


def native_gallery_cards(source, raw, *, observed_at, now):
    """Same-page native scalar facts only, separately rights-filtered by collector."""
    from .source_evidence import SourceCard
    parser = {'apw': parse_apw, 'gertrude': parse_gertrude}.get(source)
    if parser is None:
        raise SourceError('Unsupported native gallery source.')
    event = parser(raw, retrieved_at=observed_at.isoformat())
    if not _eligible(event, now):
        return ()
    fields = [('gallery.opening_starts_at', event.starts_at),
        ('gallery.opening_ends_at', event.opening_ends_at),
        ('gallery.exhibition_starts_on', event.exhibition_starts_on),
        ('gallery.exhibition_ends_on', event.exhibition_ends_on),
        ('gallery.date_precision', event.date_precision), ('gallery.timezone', event.timezone)]
    facts = tuple((key, value) for key, value in fields if value is not None) + event.public_facts
    if len(facts) > 40:
        raise SourceError('Gallery fact budget exceeded.')
    return (SourceCard(event.key, event.title, 'gallery', source, event.key.rsplit(':', 1)[-1],
        event.url, 'same-page-exhibition', facts),)
