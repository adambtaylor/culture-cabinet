"""Bounded event projections; official sites accept supplied snapshots only.

These candidates are transient source evidence, never attendee preferences.
No official website fetch, HTML scrape, JSON-LD context fetch or login exists.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import math
import re
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from shelf.discovery_sources import (
    Candidate, Outcome, SourceError, identity, label, parse_events, strict_json,
    timestamp, mbid,
)

if TYPE_CHECKING:
    from shelf.cross_media_feedback import FactualEdge

MAX_EVENTS = 9
MAX_RECONCILE = 100
MELBOURNE = ZoneInfo('Australia/Melbourne')
SOURCE_CATEGORIES = ('university', 'university_arts', 'venue', 'radio_editorial', 'editorial',
                     'council_arts', 'restaurant_events', 'ticket_retailer', 'public_api')


@dataclass(frozen=True)
class EventSource:
    name: str
    listing_url: str
    category: str
    candidate_category: str
    connection: str = 'supplied-snapshot-only'


# A registry entry is source navigation, not an event or a connected website API.
SOURCES = MappingProxyType({
    'venue303': EventSource('303 gigs and events', 'https://303.net.au/gigs-events',
        'venue', 'music', 'bounded-public-html'),
    'halftix': EventSource('Halftix Melbourne', 'https://halftixmelbourne.com/all-events/',
        'ticket_retailer', 'all', 'manual-link-only; permission-required'),
    'beat': EventSource('Beat Magazine', 'https://beat.com.au/gig-guide/',
        'editorial', 'all', 'bounded-public-html; personal-local-use'),
    'science-gallery': EventSource('Science Gallery Melbourne',
        'https://melbourne.sciencegallery.com/whats-on', 'university_arts', 'gallery'),
    'rmit': EventSource('RMIT events', 'https://www.rmit.edu.au/events', 'university', 'community'),
    'unimelb': EventSource('University of Melbourne events',
        'https://events.unimelb.edu.au/', 'university', 'community'),
    'forum': EventSource('Forum Melbourne', 'https://forummelbourne.com.au/shows', 'venue', 'music'),
    'rrr': EventSource('Triple R events', 'https://www.rrr.org.au/events', 'radio_editorial', 'music'),
    'pbs': EventSource('PBS events', 'https://www.pbsfm.org.au/news/pbs-events', 'radio_editorial', 'music'),
    'fringe': EventSource('Melbourne Fringe', 'https://www.melbournefringe.com.au/whats-on/events',
        'editorial','all','bounded-public-html'),
    'gertrude': EventSource('Gertrude exhibitions', 'https://gertrude.org.au/exhibitions/',
        'venue', 'gallery', 'bounded-public-html; factual exhibition runs only'),
    'arts_house': EventSource('Arts House', 'https://www.artshouse.com.au/whats-on/',
        'venue', 'all', 'bounded-public-html'),
    'darebin': EventSource('Darebin Arts',
        'https://arts.darebin.vic.gov.au/Whats-on/Event-calendar', 'council_arts', 'community'),
    'thornbury': EventSource('Thornbury Theatre',
        'https://www.moshtix.com.au/v2/venues/thornbury-theatre/2362', 'venue', 'music',
        'moshtix-venue-2362; website-not-an-api'),
    'moshtix': EventSource('Moshtix', 'https://api.moshtix.com/v1/graphql',
        'public_api', 'all', 'bounded-public-api; live-unvalidated'),
})
UNCONFIRMED_MAPPINGS = MappingProxyType({'deribin theater': 'darebin'})
# Access is unverified/artist-team scoped. This is NOT a registered event source.
UNCONNECTED_SERVICES = MappingProxyType({'bandsintown': 'unconnected; consumer API access unverified; artist-team scoped'})


@dataclass(frozen=True)
class EventEvidence:
    source_id: str
    event_id: str | None
    official_url: str | None
    retrieved_at: str
    provenance: str
    snapshot_sha256: str
    position: int
    occurrence_ref: str | None = None
    occurrence_basis: str | None = None


@dataclass(frozen=True)
class LineupReference:
    """Caller-verified artist identity on this exact source event record.

    A snapshot's performer name or MBID-shaped text alone is not verification.
    The authenticated integration supplies this attestation after checking evidence.
    """
    artist_mbid: str
    record: EventEvidence
    reference: str
    confidence: float
    verified: bool = False

    def __post_init__(self):
        mbid(self.artist_mbid)
        label(self.reference)
        if (type(self.record) is not EventEvidence or type(self.verified) is not bool
                or type(self.confidence) not in (int, float)
                or not math.isfinite(self.confidence) or not 0 < self.confidence <= 1):
            raise SourceError('Invalid verified lineup reference.')


@dataclass(frozen=True)
class EventCandidate(Candidate):
    source_id: str = ''
    source_event_id: str | None = None
    official_url: str | None = None
    source_start: str | None = None
    source_end: str | None = None
    ends_at: str | None = None
    event_timezone: str | None = None
    date_precision: str = 'unknown'
    end_precision: str = 'unknown'
    status: str = 'unknown'
    eligibility: str = 'unknown'
    searchable: bool | None = None
    lineup: tuple | None = None
    entry_currency: str | None = None
    evidence: tuple = ()
    lineup_references: tuple[LineupReference, ...] = ()
    occurrence_ref: str | None = None
    occurrence_basis: str | None = None

    def date_status(self, now):
        return event_timing(self, now)


OCCURRENCE_BASES = frozenset({'source_session_id', 'explicit_datetime', 'explicit_date',
                              'explicit_range', 'unresolved_observation'})


@dataclass(frozen=True)
class ListingEvent(EventCandidate):
    """Small attributed listing labels; no article or seller inventory."""
    listed_date: str = ''
    source_notice: str = ''

    def date_status(self, now):
        precision = ('date/time unverified' if self.date_precision == 'unknown' else
                     'timezone unverified' if self.event_timezone is None else event_timing(self, now))
        scope = ('unresolved observation; no stable session identity' if
                 self.occurrence_basis == 'unresolved_observation' else self.occurrence_basis)
        notice = f' Source notice: {self.source_notice} (not a seller check).' if self.source_notice else ''
        return f'Source lists {self.listed_date or "unknown date"}; {precision}; {scope}.{notice}'


def occurrence_event(event, *, session_id=None, unresolved=False):
    """Bind one explicitly observed occurrence, never a show URL alias.

    Local clocks may distinguish labelled sessions even if their timezone is
    unknown. Unresolved dates instead identify only this snapshot/position.
    """
    evidence, = event.evidence
    if unresolved:
        basis = 'unresolved_observation'
        discriminator = f'{evidence.snapshot_sha256}\0{evidence.position}'
    elif session_id is not None:
        basis, discriminator = 'source_session_id', label(session_id)
    elif event.source_start:
        basis = ('explicit_range' if event.source_end else
                 'explicit_datetime' if 'T' in event.source_start else 'explicit_date')
        discriminator = '\0'.join((event.official_url, event.source_start,
                                   event.source_end or '', event.date_precision, event.end_precision))
    else:
        basis = 'unresolved_observation'
        discriminator = f'{evidence.snapshot_sha256}\0{evidence.position}'
    ref = 'v1:' + sha256(f'{basis}\0{discriminator}'.encode()).hexdigest()
    key = sha256(f'occurrence-v1\0{event.source_id}\0{ref}'.encode()).hexdigest()
    bound = replace(evidence, occurrence_ref=ref, occurrence_basis=basis)
    return replace(event, key=identity('fixture' if event.provenance == 'fixture' else 'official', 'event', key),
                   occurrence_ref=ref, occurrence_basis=basis, evidence=(bound,))


def _aware(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise SourceError('An aware clock is required.')
    return now


def _snapshot(raw, retrieved_at, provenance):
    if provenance not in ('fixture', 'supplied-snapshot', 'live-automated'):
        raise SourceError('Invalid snapshot provenance.')
    if timestamp(retrieved_at) is None:
        raise SourceError('Snapshot retrieval time needs an offset.')
    try:
        return strict_json(raw), sha256(raw).hexdigest()
    except (ValueError, UnicodeError, RecursionError):
        raise SourceError('Invalid bounded JSON snapshot.') from None


def official_url(value, source):
    """Exact registered HTTPS origin, with no login or redirect destinations.

    Query URLs are outside this small snapshot contract. Do not guess canonical
    event identities from tracking parameters, titles, or a listing-page URL.
    """
    if not isinstance(value, str) or len(value) > 2048 or not value:
        raise SourceError('Missing or oversized official URL.')
    decoded = unquote(value)
    if any(ord(c) < 33 or ord(c) == 127 for c in decoded) or '\\' in decoded or '%' in decoded:
        raise SourceError('Unsafe official URL.')
    try:
        parts = urlsplit(value)
        allowed = urlsplit(source.listing_url)
        if (parts.scheme != 'https' or parts.hostname != allowed.hostname
                or parts.username is not None or parts.password is not None
                or parts.port is not None or parts.query or '%' in parts.netloc
                or parts.netloc.lower() != allowed.netloc.lower()):
            raise SourceError('URL is outside the official source.')
    except ValueError:
        raise SourceError('Invalid official URL.') from None
    segments = unquote(parts.path).lower().split('/')
    if any(p in ('.', '..', 'login', 'signin', 'sign-in', 'sso', 'auth', 'oauth',
                 'account', 'accounts', 'redirect') for p in segments):
        raise SourceError('Non-public official link.')
    return urlunsplit(('https', parts.netloc.lower(), parts.path.rstrip('/') or '/', '', ''))


def _date(value, zone):
    """Keep calendar dates distinct; never guess an ambiguous Melbourne clock."""
    if value is None:
        return None, 'unknown'
    if not isinstance(value, str) or len(value) > 50:
        raise SourceError('Invalid event date.')
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            date.fromisoformat(value)
            return value, 'date'
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?', value):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise SourceError('Invalid event date.') from None
    if parsed.tzinfo is not None:
        try:
            local = parsed.astimezone(timezone.utc).astimezone(MELBOURNE)
        except OverflowError:
            raise SourceError('Event date is outside supported range.') from None
        if zone == 'Australia/Melbourne' and parsed.utcoffset() != local.utcoffset():
            raise SourceError('Event offset disagrees with Melbourne timezone.')
        return parsed.isoformat(), 'time'
    if zone != 'Australia/Melbourne':
        return None, 'unknown'
    choices = set()
    for fold in (0, 1):
        try:
            utc = parsed.replace(tzinfo=MELBOURNE, fold=fold).astimezone(timezone.utc)
        except OverflowError:
            raise SourceError('Event date is outside supported range.') from None
        if utc.astimezone(MELBOURNE).replace(tzinfo=None) == parsed:
            choices.add(utc)
    if len(choices) != 1:
        return None, 'unknown'
    return choices.pop().astimezone(MELBOURNE).isoformat(), 'time'


def event_timing(event, now):
    _aware(now)
    if event.status in ('cancelled', 'postponed', 'ended'):
        return event.status
    if event.date_precision == 'unknown':
        return 'unknown-date'
    local_day = now.astimezone(MELBOURNE).date()
    if event.end_precision == 'time' and timestamp(event.ends_at) <= now:
        return 'ended'
    if event.end_precision == 'date':
        if event.event_timezone != 'Australia/Melbourne':
            return 'unknown-date'
        if date.fromisoformat(event.ends_at) < local_day:
            return 'ended'
    if event.date_precision == 'date':
        if event.event_timezone != 'Australia/Melbourne':
            return 'unknown-date'
        start = date.fromisoformat(event.starts_at)
        return 'future-date' if start > local_day else ('today-date' if start == local_day else 'past-start')
    return 'future' if timestamp(event.starts_at) > now else 'past-start'


def upcoming_events(candidates, now):
    """Public discovery only; a source's audience never establishes user eligibility."""
    _aware(now)
    return tuple(c for c in reconcile_events(candidates)
                 if c.eligibility == 'public' and c.searchable is not False
                 and event_timing(c, now) in ('future', 'future-date', 'today-date'))


@dataclass(frozen=True)
class ArtistGig:
    event: EventCandidate
    edges: tuple['FactualEdge', ...]


def artist_upcoming_gigs(artist_id, candidates, *, now, localities):
    """Exact MBID join to supplied local lineups, with the existing date/dedup policy.

    Localities are explicitly chosen by the caller; no radius/travel time is inferred.
    The latest event record must retain the verified reference. An old lineup is not
    silently carried into a newer record which omitted it. No provider I/O occurs.
    """
    from shelf.cross_media_feedback import FactualEdge

    artist_key = identity('musicbrainz', 'artist', artist_id)
    if type(localities) not in (list, tuple) or not 1 <= len(localities) <= 25:
        raise SourceError('Explicit bounded localities required.')
    places = {label(place).casefold() for place in localities}
    output = []
    for event in upcoming_events(candidates, now):
        if (type(event.lineup_references) is not tuple or len(event.lineup_references) > 25
                or any(type(ref) is not LineupReference for ref in event.lineup_references)):
            raise SourceError('Invalid or oversized verified lineup.')
        if event.category != 'music' or event.locality.casefold() not in places:
            continue
        edges = []
        for ref in event.lineup_references:
            if (not ref.verified or ref.artist_mbid != artist_id or ref.record not in event.evidence
                    or ref.record.retrieved_at != event.retrieved_at
                    or ref.record.source_id != event.source_id
                    or ref.record.event_id != event.source_event_id
                    or ref.record.official_url != event.official_url):
                continue
            edges.append(FactualEdge(event.key, artist_key, 'lineup', ref.record.source_id,
                                     ref.reference, ref.confidence))
        if edges:
            output.append(ArtistGig(event, tuple(dict.fromkeys(edges))))
    return tuple(sorted(output, key=lambda gig: (gig.event.starts_at, gig.event.key)))


def _money(value):
    if value is None:
        return None, None
    if type(value) is not dict:
        raise SourceError('Invalid price metadata.')
    amount, currency = value.get('price'), value.get('priceCurrency')
    if amount is None or currency is None:
        return None, None
    if isinstance(amount, bool) or not isinstance(amount, (str, int, float)):
        raise SourceError('Invalid price.')
    try:
        number = Decimal(str(amount))
    except InvalidOperation:
        raise SourceError('Invalid price.') from None
    if not number.is_finite() or number < 0 or not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
        raise SourceError('Invalid price or currency.')
    return str(amount), currency


def _enum(value, choices):
    if value is None:
        return 'unknown'
    if not isinstance(value, str) or value not in choices:
        raise SourceError('Invalid event classification.')
    return value


def _schema_term(value):
    if not isinstance(value, str):
        raise SourceError('Invalid schema term.')
    for prefix in ('https://schema.org/', 'http://schema.org/'):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _official_event(row, source_id, source, retrieved_at, provenance, digest, position, jsonld):
    if type(row) is not dict:
        raise SourceError('Invalid event row.')
    if jsonld:
        types = row.get('@type', [])
        types = [types] if isinstance(types, str) else types
        if not isinstance(types, list) or len(types) > MAX_EVENTS:
            raise SourceError('Invalid event type list.')
        if not set(map(_schema_term, types)) & {
                'Event', 'MusicEvent', 'ExhibitionEvent', 'TheaterEvent', 'FoodEvent', 'EducationEvent'}:
            # A Restaurant or Place is not a dated dining event.
            return None
    elif row.get('kind') != 'event':
        return None
    url = official_url(row.get('url') or (row.get('@id') if jsonld else None), source)
    if url == official_url(source.listing_url, source):
        raise SourceError('An event detail URL is required.')
    event_id = row.get('identifier', row.get('@id')) if jsonld else row.get('id')
    if event_id is not None:
        event_id = label(event_id)
    zone = row.get('timezone')
    if zone not in (None, 'Australia/Melbourne'):
        raise SourceError('Unsupported explicit event timezone.')
    raw_start, raw_end = row.get('startDate'), row.get('endDate')
    start, precision = _date(raw_start, zone)
    end, end_precision = _date(raw_end, zone)
    if start and end:
        if precision != end_precision:
            raise SourceError('Mixed start/end date precision.')
        if (timestamp(end) <= timestamp(start) if precision == 'time' else end < start):
            raise SourceError('Event ends before it starts.')
    status = row.get('eventStatus') if jsonld else row.get('status')
    if jsonld and status is not None:
        status = {'EventScheduled': 'scheduled', 'EventRescheduled': 'rescheduled',
                  'EventCancelled': 'cancelled', 'EventPostponed': 'postponed'}.get(
                      _schema_term(status), 'unknown')
    status = _enum(status, ('scheduled', 'rescheduled', 'cancelled', 'postponed', 'ended', 'unknown'))
    eligibility = _enum(row.get('eligibility'), ('public', 'student', 'invite', 'unknown'))
    searchable = row.get('searchable')
    if searchable is not None and type(searchable) is not bool:
        raise SourceError('Invalid searchable flag.')
    performers = row.get('performer') if jsonld else row.get('lineup')
    lineup = None
    if performers is not None:
        if type(performers) is dict and jsonld:
            performers = [performers]
        if not isinstance(performers, list) or len(performers) > 25:
            raise SourceError('Invalid or oversized lineup.')
        lineup = tuple(label(p.get('name') if jsonld and type(p) is dict else p) for p in performers) or None
    admission = row.get('offers') if jsonld else row.get('admission')
    if jsonld and isinstance(admission, list):
        if len(admission) > MAX_EVENTS:
            raise SourceError('Offer budget exceeded.')
        # Multiple offers may be different ticket classes, not a single price.
        admission = admission[0] if len(admission) == 1 else None
    entry_fee, entry_currency = _money(admission)
    artwork = row.get('artwork')
    art_price, art_currency = _money(artwork)
    per = artwork.get('per') if artwork else None
    location = row.get('location', {})
    if type(location) is not dict or type(location.get('address', {})) is not dict:
        raise SourceError('Invalid event location.')
    venue = label(location['name']) if location.get('name') else ''
    locality = location.get('address', {}).get('addressLocality')
    key_id = sha256(f'{source_id}\0{"id" if event_id else "url"}\0{event_id or url}'.encode()).hexdigest()
    evidence = EventEvidence(source_id, event_id, url, retrieved_at, provenance, digest, position)
    return EventCandidate(
        key=identity('fixture' if provenance == 'fixture' else 'official', 'event', key_id),
        title=label(row.get('name')), kind='event', source=source.name,
        provenance=provenance, rank=position, retrieved_at=retrieved_at,
        url=None if provenance == 'fixture' else url, category=source.candidate_category,
        starts_at=start, locality=label(locality) if locality else '', venue=venue,
        artwork_price=art_price, artwork_currency=art_currency, price_per=per,
        entry_fee=entry_fee, entry_currency=entry_currency, source_id=source_id,
        source_event_id=event_id, official_url=url, source_start=raw_start, source_end=raw_end,
        ends_at=end, event_timezone=zone, date_precision=precision, end_precision=end_precision,
        status=status, eligibility=eligibility, searchable=searchable, lineup=lineup, evidence=(evidence,))


def parse_official_snapshot(raw, *, source_id, retrieved_at, provenance, sources=SOURCES):
    """Parse metadata-v1 or JSON-LD supplied by a caller, never by fetching a URL.

    `sources` is trusted application configuration, not a field in the snapshot.
    Restaurant origins require explicit registry configuration before ingestion.
    """
    if (not isinstance(source_id, str) or source_id not in sources
            or sources[source_id].connection != 'supplied-snapshot-only'):
        raise SourceError('Source is not an allowed official snapshot source.')
    if provenance not in ('fixture', 'supplied-snapshot'):
        raise SourceError('Official websites are not connected APIs.')
    source = sources[source_id]
    if source.category not in SOURCE_CATEGORIES:
        raise SourceError('Unknown source category.')
    document, digest = _snapshot(raw, retrieved_at, provenance)
    if type(document) is dict and 'format' in document and document['format'] != 'event-metadata-v1':
        raise SourceError('Unsupported event metadata format.')
    jsonld = not (type(document) is dict and document.get('format') == 'event-metadata-v1')
    if not jsonld:
        rows = document.get('events')
    elif type(document) is dict:
        rows = document.get('@graph', [document])
    else:
        rows = document
    if not isinstance(rows, list) or len(rows) > MAX_EVENTS:
        raise SourceError('Official snapshot row budget exceeded.')
    output = []
    for position, row in enumerate(rows, 1):
        event = _official_event(row, source_id, source, retrieved_at, provenance, digest, position, jsonld)
        if event is not None:
            output.append(event)
    return reconcile_events(output)


def parse_moshtix(raw, *, now, retrieved_at, provenance):
    """Normalize a response to the fixed public/future/searchable VIC operation.

    The verified selection has no status, lineup or prices. These stay unknown;
    public/searchable describe the request filters, not a ticket availability claim.
    """
    _aware(now)
    document, digest = _snapshot(raw, retrieved_at, provenance)
    try:
        cards = parse_events(document, retrieved_at)
        rows = document['data']['viewer']['getEvents']['items']
        output = []
        for card, row in zip(cards, rows):
            venue = row['venue']
            if (type(venue.get('id')) is not int or venue['id'] <= 0
                    or venue['address']['region'] != 'VIC'):
                raise SourceError('Invalid Moshtix venue.')
            start, precision = _date(row.get('startDate'), None)
            if precision != 'time':
                # No zone or date-only assertion is available from this contract.
                continue
            if not now < timestamp(start) <= now + timedelta(weeks=8):
                continue
            event_id = card.key.rsplit(':', 1)[1]
            evidence = EventEvidence('moshtix', event_id, card.url, retrieved_at, provenance, digest, card.rank)
            output.append(EventCandidate(**{**vars(card),
                'key': card.key.replace('moshtix:', 'fixture:', 1) if provenance == 'fixture' else card.key,
                'url': None if provenance == 'fixture' else card.url,
                'provenance': provenance, 'starts_at': start},
                source_id='moshtix', source_event_id=event_id, official_url=card.url,
                source_start=row['startDate'], date_precision='time',
                eligibility='public', searchable=True, evidence=(evidence,)))
        return reconcile_events(output)
    except (KeyError, TypeError, AttributeError):
        raise SourceError('Invalid Moshtix snapshot shape.') from None


def fetch_moshtix(now):
    """Explicit callable only; no route, schedule, retries or pagination is added."""
    from shelf import discovery_transport
    try:
        _aware(now)
        status, raw = discovery_transport.events(now, page_size=MAX_EVENTS)
        if status == 429:
            return Outcome('rate-limited', message='Source rate limited; no retry attempted.')
        if status != 200:
            return Outcome('error', message='Source request failed; no retry attempted.')
        cards = parse_moshtix(raw, now=now, retrieved_at=now.isoformat(), provenance='live-automated')
        return Outcome('ready' if cards else 'empty', cards,
                       'Partial source coverage; status and availability unverified.')
    except TimeoutError:
        return Outcome('timeout', message='Source timed out; no retry attempted.')
    except (SourceError, OSError, ValueError, RecursionError):
        return Outcome('invalid', message='Invalid source response; no candidates accepted.')


def _aliases(event):
    # Fixture and supplied/live identities cannot reconcile with each other.
    mode = 'fixture' if event.provenance == 'fixture' else 'source'
    if event.occurrence_ref is not None or event.occurrence_basis is not None:
        label(event.occurrence_ref)
        if event.occurrence_basis not in OCCURRENCE_BASES or any(
                e.source_id != event.source_id or e.occurrence_ref != event.occurrence_ref
                or e.occurrence_basis != event.occurrence_basis
                or (e.provenance == 'fixture') != (mode == 'fixture') for e in event.evidence):
            raise SourceError('Evidence must address one occurrence and source mode.')
        # A legacy record with the same URL/show ID cannot bridge sessions.
        # The separate alias namespace also prevents transitive cross-mode joins.
        return {(mode, 'occurrence', event.source_id, event.occurrence_ref)}
    if any(e.occurrence_ref is not None or e.occurrence_basis is not None for e in event.evidence):
        raise SourceError('Legacy event cannot carry occurrence evidence.')
    return {(mode, 'id', e.source_id, e.event_id) for e in event.evidence if e.event_id} | {
        (mode, 'url', e.official_url) for e in event.evidence if e.official_url}


def reconcile_events(candidates):
    """Merge only source IDs/exact official URLs, preserving every observation.

    Latest observation wins; equal-time cancellations/postponements fail closed.
    Reconcile before suppressing listings so a cancellation can remove an older
    future event. Missing values in a newer observation remain unknown.
    """
    if not isinstance(candidates, (list, tuple)) or len(candidates) > MAX_RECONCILE:
        raise SourceError('Reconciliation budget exceeded.')
    if any(not isinstance(c, EventCandidate) or not c.evidence for c in candidates):
        raise SourceError('Expected event candidates with evidence.')
    if sum(len(c.evidence) for c in candidates) > MAX_RECONCILE:
        raise SourceError('Evidence budget exceeded.')
    groups = []
    for candidate in candidates:
        aliases = _aliases(candidate)
        members = [candidate]
        remaining = []
        for known, group in groups:
            if aliases & known:
                aliases |= known
                members.extend(group)
            else:
                remaining.append((known, group))
        # A new bridge can connect two previously separate identity groups.
        groups = remaining + [(aliases, members)]
    severity = {'scheduled': 0, 'rescheduled': 0, 'unknown': 1, 'ended': 2, 'postponed': 3, 'cancelled': 4}
    audience = {'public': 0, 'unknown': 1, 'student': 2, 'invite': 3}
    output = []
    for _, group in groups:
        winner = max(group, key=lambda c: (timestamp(c.retrieved_at),
            c.status in ('cancelled', 'postponed', 'ended'),
            c.eligibility != 'public' or c.searchable is False or c.date_precision == 'unknown'
            or (c.date_precision == 'date' and c.event_timezone is None),
            severity[c.status], audience[c.eligibility], c.key))
        blocked = [c for c in group if c.status in ('cancelled', 'postponed', 'ended')]
        if winner.status == 'unknown' and blocked:
            # Omission is not an explicit reinstatement of a withdrawn event.
            previous = max(blocked, key=lambda c: (timestamp(c.retrieved_at), severity[c.status]))
            winner = replace(winner, status=previous.status)
        evidence = tuple(dict.fromkeys(e for c in group for e in c.evidence))
        output.append(replace(winner, evidence=evidence, rank=min(c.rank for c in group)))
    return tuple(sorted(output, key=lambda c: (c.rank, c.key)))
