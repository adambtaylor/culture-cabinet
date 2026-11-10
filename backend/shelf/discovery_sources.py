"""Public identity projections, fixed public operations and isolated fixture seams.

Only MusicBrainz CC0 core identity fields are projected. Transport responses are
discarded after parsing; candidates never belong in durable human history.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 128 * 1024
CACHE_TTL = timedelta(minutes=15)
CATEGORIES = ('all', 'music', 'gallery', 'art_fair', 'community', 'regional_day_trip')


class SourceError(ValueError):
    pass


def mbid(value):
    if not isinstance(value, str):
        raise SourceError('Expected a string MBID.')
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError):
        raise SourceError('Invalid MBID.') from None
    if canonical != value:
        raise SourceError('Expected canonical MBID.')
    return value


def identity(source, kind, value):
    if source == 'musicbrainz' and kind in ('artist', 'recording'):
        value = mbid(value)
    elif source == 'moshtix' and kind == 'event':
        if type(value) is not int or not 0 < value < 2**63:
            raise SourceError('Expected positive event ID.')
        value = str(value)
    elif source in ('fixture', 'official') and kind in ('artist', 'recording', 'venue', 'event'):
        if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9-]{1,100}', value):
            raise SourceError('Invalid local identity.')
    else:
        raise SourceError('Unsupported identity namespace.')
    return f'{source}:{kind}:{value}'


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise SourceError('Missing or oversized identity label.')
    return value.strip()


def timestamp(value):
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Candidate:
    key: str
    title: str
    kind: str
    source: str
    provenance: str
    rank: int
    retrieved_at: str | None
    roots: tuple = ()
    url: str | None = None
    category: str = 'music'
    starts_at: str | None = None
    locality: str = ''
    venue: str = ''
    venue_key: str | None = None
    artist_key: str | None = None
    lineage: tuple = ()
    artwork_price: str | None = None
    artwork_currency: str | None = None
    price_per: str | None = None
    entry_fee: str | None = None

    def artwork_label(self):
        status = artwork_affordability(self.artwork_price, self.artwork_currency, per=self.price_per)
        if status == 'unknown':
            return 'Artwork price unknown; check official source'
        return (f'Artwork item price AUD {self.artwork_price} per individual artwork — {status} '
                '(item price only); extras and availability unknown; all-in cost not qualified')

    def freshness(self, now):
        fetched = timestamp(self.retrieved_at)
        if fetched is None:
            return 'Retrieval time unknown; unvalidated'
        if fetched > now or now - fetched > CACHE_TTL:
            return 'Stale — confirm on the official page'
        return 'Recently retrieved; availability unconfirmed'

    def date_status(self, now):
        start = timestamp(self.starts_at)
        if start is None:
            return 'Date/timezone unknown'
        return 'Past listing — confirm on official page' if start <= now else 'Future source date; status unverified'


def parse_musicbrainz(document, kind, retrieved_at):
    if kind not in ('artist', 'recording') or type(document) is not dict:
        raise SourceError('Invalid MusicBrainz shape.')
    rows = document.get('artists' if kind == 'artist' else 'recordings', [document])
    if not isinstance(rows, list) or len(rows) > 25:
        raise SourceError('MusicBrainz result budget exceeded.')
    return [Candidate(identity('musicbrainz', kind, row['id']),
                      label(row['name' if kind == 'artist' else 'title']), kind,
                      'MusicBrainz', 'live-automated', rank, retrieved_at,
                      url=f'https://musicbrainz.org/{kind}/{mbid(row["id"])}')
            for rank, row in enumerate(rows, 1)]


def parse_radio(document, seed, retrieved_at):
    """Documented artist-seeded radio dictionary, never a population co-like claim."""
    root = identity('musicbrainz', 'artist', seed)
    # The documented seed group is additional to max_similar_artists.
    if type(document) is not dict or len(document) - (seed in document) > 5:
        raise SourceError('Invalid radio dictionary.')
    output = []
    for artist_id, recordings in document.items():
        mbid(artist_id)
        if not isinstance(recordings, list) or len(recordings) > 3:
            raise SourceError('Radio recording budget exceeded.')
        for row in recordings:
            if type(row) is not dict or row.get('similar_artist_mbid') != artist_id:
                raise SourceError('Radio artist identity mismatch.')
            artist = label(row['similar_artist_name'])
            count = row.get('total_listen_count')
            if type(count) is not int or count < 0:
                raise SourceError('Invalid aggregate count.')
            recording = mbid(row['recording_mbid'])
            output.append(Candidate(identity('musicbrainz', 'recording', recording),
                f'Recording by {artist} (title unavailable)', 'recording', 'ListenBrainz artist radio',
                'live-automated', len(output) + 1, retrieved_at, (root,),
                f'https://musicbrainz.org/recording/{recording}',
                artist_key=identity('musicbrainz', 'artist', artist_id),
                lineage=((root, len(output) + 1),)))
    return output


def event_url(name, event_id):
    identity('moshtix', 'event', event_id)
    slug = re.sub(r'[^a-z0-9]+', '-', label(name).lower()).strip('-')
    if not slug:
        return None
    return f'https://moshtix.com.au/v2/event/{slug}/{event_id}'


def parse_events(document, retrieved_at):
    if type(document) is not dict or document.get('errors'):
        raise SourceError('GraphQL source error.')
    result = document['data']['viewer']['getEvents']
    if (type(result.get('totalCount')) is not int or result['totalCount'] < 0
            or not isinstance(result.get('items'), list) or len(result['items']) > 9
            or len(result['items']) > result['totalCount']):
        raise SourceError('Invalid event results.')
    output = []
    for rank, row in enumerate(result['items'], 1):
        event_id = row['id']
        # GraphQL IDs may be decimal strings; unlike MBIDs these remain event IDs.
        if isinstance(event_id, str) and re.fullmatch(r'[1-9][0-9]{0,18}', event_id):
            event_id = int(event_id)
        key = identity('moshtix', 'event', event_id)
        name = label(row['name'])
        venue = row.get('venue') or {}
        address = venue.get('address') or {}
        start = row.get('startDate')
        output.append(Candidate(key, name, 'event', 'Moshtix', 'live-automated', rank,
            retrieved_at, url=event_url(name, event_id), category='all',
            starts_at=start if timestamp(start) else None,
            locality=label(address['locality']) if address.get('locality') else '',
            venue=label(venue['name']) if venue.get('name') else 'Unknown venue',
            venue_key=('official:venue:thornbury-theatre' if type(venue.get('id')) is int
                       and venue['id'] == 2362 else None)))
    return output


@dataclass(frozen=True)
class Outcome:
    status: str
    candidates: tuple = ()
    message: str = ''


@dataclass(frozen=True)
class Request:
    url: str
    params: tuple
    timeout_seconds: int = 5
    max_bytes: int = MAX_BYTES


def radio_request(seed, mode='medium'):
    mbid(seed)
    if mode not in ('easy', 'medium', 'hard'):
        raise SourceError('Invalid radio mode.')
    return Request(f'https://api.listenbrainz.org/1/lb-radio/artist/{seed}',
                   (('mode', mode), ('max_similar_artists', 5),
                    ('max_recordings_per_artist', 3), ('pop_begin', 0), ('pop_end', 100)))


def fetch_radio_fixture(transport, seed, retrieved_at):
    """Injected synthetic transport only; no runtime route binds a live transport.

    A future transport must enforce timeout/stream byte bounds before returning.
    This seam never retries or takes arbitrary URLs, credentials or profile data.
    """
    try:
        status, raw = transport(radio_request(seed))
        if status == 429:
            return Outcome('rate-limited', message='Source rate limited; no retry attempted.')
        if status != 200:
            return Outcome('error', message='Source request failed.')
        if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
            raise SourceError('Response budget exceeded.')
        cards = parse_radio(strict_json(raw), seed, retrieved_at)
        # Fixtures cannot inherit a live label or a live identity/link.
        cards = [replace(c, key=c.key.replace('musicbrainz:', 'fixture:', 1),
                         roots=tuple(r.replace('musicbrainz:', 'fixture:', 1) for r in c.roots),
                         artist_key=c.artist_key.replace('musicbrainz:', 'fixture:', 1),
                         lineage=tuple((r.replace('musicbrainz:', 'fixture:', 1), p) for r, p in c.lineage),
                         provenance='fixture', url=None) for c in cards]
        return Outcome('ready' if cards else 'empty', tuple(cards))
    except TimeoutError:
        return Outcome('timeout', message='Source timed out; no retry attempted.')
    except (SourceError, KeyError, TypeError, ValueError, RecursionError):
        return Outcome('invalid', message='Source response was invalid; no candidates accepted.')


def source_statuses():
    return [
        {'name': 'Beat Magazine', 'status': 'explicit public read available', 'detail': 'One bounded public guide page; up to nine single-date leads within 14 days, from the first 48 cards. Optional exact detail read. Dates, source time labels and explicit doors/show times stay distinct; finish, status, fees and ticket availability unverified. Personal local use; no article or image republication.'},
        {'name': 'MusicBrainz', 'status': 'blocked', 'detail': 'Maintainer contact unavailable. A truthful operator-approved contact in User-Agent and aggregate ≤1 request/second are required before live calls. CC0 core identities only.'},
        {'name': 'ListenBrainz', 'status': 'explicit public read available; partial coverage', 'detail': 'Bounded artist-seeded radio, not collaborative likes. No listen history, tokens or submissions. Titles and audio destinations unavailable. Fixture tests do not establish recommendation quality.'},
        {'name': 'Moshtix', 'status': 'explicit POST available; live behavior unvalidated', 'detail': 'Verified public contract: eventStartDateFrom/eventStartDateTo use Date; region is [VIC]. Anonymous first page, one event over the next eight weeks. includePastEvents:false, publicOnly:true, searchableOnly:true. Partial coverage; Forum coverage unverified.'},
        {'name': 'Ticketmaster', 'status': 'blocked', 'detail': 'Requires an unapproved key; placeholder only.'},
        {'name': 'Triple R events', 'status': 'explicit public read available', 'detail': 'One public HTML listing page, at most nine cards in source order. Each cached card offers a separate one-page detail read for source dates, time labels, address and More info link. Missing years, timezone, admission and ticket availability stay unverified. No pagination or automatic crawling; details expire with the original 15-minute listing.'},
        {'name': 'Tribe Soundsystem', 'status': 'explicit public RSS read available', 'detail': 'Up to nine announcements. RSS publication and feed-build dates are not event times; older announcements may remain. Automated calendar enrichment is blocked by publisher robots rules. Open the original source to confirm sessions.'},
        {'name': 'Sound Systems of Melbourne', 'status': 'explicit public page read available', 'detail': 'Up to nine distinct underground/community dance and festival observations within eight weeks. Source dates and clock wording remain attributed; timezone, ending day, cancellation and tickets unverified. TBA locations remain undisclosed. No calendar or social-page fetching.'},
        {'name': 'Halftix Melbourne', 'status': 'official link only; publisher permission required', 'detail': 'Automatic listing storage is restricted by publisher terms. Open Halftix for cheaper options and confirm the exact offered price, face-value comparison, fees, stock and conditions. Discounts vary; not every offer is half price.'},
        {'name': 'PBS Events', 'status': 'explicit public read available', 'detail': 'One public upcoming-listing page; up to nine leads within eight weeks, with missing years labelled unknown and date ranges kept distinct from sessions. Archives excluded. Source notices are not seller availability checks. Ten seconds between shared PBS requests; results expire after 15 minutes. PBS Presents remains an unconnected reference.'},
    ]


def registry():
    """Packaged research handoff, never a runtime dependency on working requirements."""
    return json.loads((Path(__file__).parent / 'data/discovery_registry.json').read_text())


def artwork_affordability(price=None, currency=None, *, per=None):
    """Legacy item-price comparison only; never an all-in or availability gate.

    Admission is not artwork price. Use gallery_commerce.evaluate_offer for a
    complete sourced all-in check; even a pass grants no purchase authority.
    """
    if currency != 'AUD' or per != 'individual artwork' or isinstance(price, bool):
        return 'unknown'
    try:
        amount = Decimal(str(price))
    except (InvalidOperation, ValueError):
        return 'unknown'
    if not amount.is_finite() or amount < 0:
        return 'unknown'
    return 'within budget' if amount < Decimal('2500') else 'outside budget'


def household_candidates():
    seeds = registry()
    cards = [Candidate(identity('musicbrainz', 'artist', row['mbid']), row['name'],
        'artist', 'Reported artist seed', 'manual-attributed', rank, None,
        url=f'https://musicbrainz.org/artist/{row["mbid"]}')
        for rank, row in enumerate(seeds['artists'], 1)]
    cards.extend(Candidate(identity('official', 'artist', row['slug']), row['name'],
        'artist', row['source'], 'manual-attributed', rank, None, url=row['url'])
        for rank, row in enumerate(seeds['manual_artists'], len(cards) + 1))
    for slug, name in (('forum-melbourne', 'Forum Melbourne'), ('thornbury-theatre', 'Thornbury Theatre')):
        venue = next(row for row in seeds['sources'] if row['label'] == name)
        cards.append(Candidate(identity('official', 'venue', slug), name,
            'venue', 'Reported venue seed', 'manual-attributed', 1, None,
            url=venue['url'], category='all'))
    return cards


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SourceError('Duplicate JSON key.')
            result[key] = value
        return result

    def nonfinite(value):
        raise SourceError('Non-finite JSON number.')

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise SourceError('Non-finite JSON number.')
        return result

    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise SourceError('Response budget exceeded.')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=nonfinite,
                      parse_float=finite_float)


def fetch_public(operation, seed, mode, now):
    """Only the explicit authenticated POST binds this runtime transport."""
    from shelf import discovery_transport as transport
    try:
        if operation == 'gallery' and not seed and not mode:
            from shelf.gallery_sources import fetch
            return fetch(now)
        if operation == 'tribe' and not seed and not mode:
            from shelf.tribe_events import fetch_listing
            return fetch_listing(now)
        if operation == 'soundsystems' and not seed and not mode:
            from shelf.soundsystems_events import fetch_listing
            return fetch_listing(now)
        if operation == 'pbs' and not seed and not mode:
            from shelf.event_scraping import collect
            return collect('pbs', now=now)
        if operation == 'venue303' and not seed and not mode:
            from shelf.event_scraping import collect
            return collect('venue303', now=now)
        if operation == 'rrr' and not seed and not mode:
            from shelf.event_scraping import collect
            return collect('rrr', now=now)
        if operation == 'radio':
            status, raw = transport.radio(seed, mode)
        elif operation == 'events' and not seed and not mode:
            status, raw = transport.events(now)
        elif operation == 'beat' and not seed and not mode:
            status, raw = transport.beat()
        else:
            raise SourceError('Invalid public operation.')
        if status == 429:
            return Outcome('rate-limited', message='Source rate limited; no retry attempted.')
        if status != 200:
            return Outcome('error', message='Source request failed; no retry attempted.')
        if operation == 'beat':
            from shelf.beat_events import parse_listing
            cards = parse_listing(raw, retrieved_at=now.isoformat())
            return Outcome('ready' if cards else 'empty', cards, 'Partial coverage: up to nine single-date Beat leads within 14 days from the first 48 cards. Multi-day runs omitted; times, status and tickets need confirmation.')
        document = strict_json(raw)
        if operation == 'radio':
            cards = parse_radio(document, seed, now.isoformat())
        else:
            cards = parse_events(document, now.isoformat())
            rows = document['data']['viewer']['getEvents']['items']
            if len(rows) > 1:
                raise SourceError('Public page budget exceeded.')
            for row in rows:
                venue = row['venue']
                if (type(row['id']) is not int or type(venue['id']) is not int or venue['id'] <= 0
                        or venue['address']['region'] != 'VIC' or not label(venue['name'])
                        or not label(venue['address']['locality'])
                        or timestamp(row['startDate']) is None
                        or not now.replace(microsecond=0) <= timestamp(row['startDate']) <= now + timedelta(weeks=8)):
                    raise SourceError('Invalid public event fields.')
        return Outcome('ready' if cards else 'empty', tuple(cards),
                       'Partial source coverage; availability unverified.' if cards else
                       'Source returned no results in this bounded request; coverage remains unknown.')
    except TimeoutError:
        return Outcome('timeout', message='Source timed out; no retry attempted.')
    except (SourceError, KeyError, TypeError, ValueError, RecursionError, AttributeError, OSError):
        return Outcome('invalid', message='Source response or transport invalid; no candidates accepted.')


def sample_candidates():
    """Entirely synthetic JSON, stable dates and IDs; nothing pretends to be inventory."""
    rows = json.loads((ROOT / 'backend/shelf/fixtures/discovery_sample.json').read_text())
    return [Candidate(**dict(row, roots=tuple(row.get('roots', [])))) for row in rows]
