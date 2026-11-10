"""Fixed existing source operations behind an explicitly supplied backend port.

There is deliberately no default network or credential-store implementation.
Legacy machine credentials, source URLs and browser dictionaries cannot select a
backend. Integration must supply current purpose/config/credential authorization
and an actually bounded single-request transport; incompatible sources stay off.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from threading import RLock
from urllib.parse import urlsplit

from .disclosure import canonical_bytes, read_json
from .enhance_contract import EnhanceBindings, EnhanceFault, digest, require, text, utc
from .source_evidence import SourceCard, FieldPolicy


@dataclass(frozen=True)
class SourceSpec:
    key: str
    origin: str
    categories: tuple[str, ...]
    operations: tuple[str, ...]
    parser: str
    credential_required: bool = False
    interval_seconds: int = 1
    ttl_seconds: int = 900
    response_bytes: int = 131072
    search: bool = False
    kind: str = 'page'
    starter_operation: str | None = None
    starter_label: str = ''

    def requires_terms(self, operation):
        require(operation in self.operations)
        return self.search and operation != self.starter_operation


SOURCES = (
    SourceSpec('musicbrainz_artist', 'https://musicbrainz.org', ('artist',),
               ('artists_by_name',), 'musicbrainz', False,
               ttl_seconds=3600, response_bytes=131072, search=True, kind='api'),
    SourceSpec('musicbrainz_recording', 'https://musicbrainz.org', ('recording',),
               ('recordings_by_artist_name',), 'musicbrainz', False,
               ttl_seconds=3600, response_bytes=131072, search=True, kind='api'),
    SourceSpec('tmdb_movie', 'https://api.themoviedb.org', ('movie',),
               ('search_movie', 'starter_movie_science'), 'tmdb', True, ttl_seconds=3600, response_bytes=262144,
               search=True, kind='api', starter_operation='starter_movie_science',
               starter_label='Exploratory: science fiction films'),
    SourceSpec('tmdb_tv', 'https://api.themoviedb.org', ('tv',),
               ('search_tv', 'starter_tv_fantasy'), 'tmdb', True, ttl_seconds=3600, response_bytes=262144,
               search=True, kind='api', starter_operation='starter_tv_fantasy',
               starter_label='Exploratory: science fiction / fantasy series'),
    SourceSpec('hardcover', 'https://api.hardcover.app', ('book',),
               ('book_search', 'book_details', 'starter_books'), 'hardcover', True,
               ttl_seconds=3600, response_bytes=262144, search=True, kind='api', starter_operation='starter_books',
               starter_label='Exploratory: popular books from Hardcover'),
    SourceSpec('moshtix', 'https://api.moshtix.com', ('event',), ('events',), 'moshtix', kind='api'),
    SourceSpec('rrr', 'https://www.rrr.org.au', ('event', 'community'), ('rrr',), 'rrr'),
    SourceSpec('beat', 'https://beat.com.au', ('event',), ('beat',), 'beat', response_bytes=262144),
    SourceSpec('pbs', 'https://www.pbsfm.org.au', ('event', 'community'), ('pbs',), 'pbs',
               interval_seconds=10),
    SourceSpec('tribe', 'https://www.tribesoundsystem.com', ('event',), ('tribe',), 'tribe'),
    SourceSpec('soundsystems', 'https://soundsystemsofmelbourne.com.au', ('event',),
               ('soundsystems',), 'soundsystems'),
    SourceSpec('gertrude', 'https://gertrude.org.au', ('gallery',), ('gertrude',), 'gertrude',
               interval_seconds=10, response_bytes=262144),
    SourceSpec('apw', 'https://www.australianprintworkshop.com', ('gallery',), ('apw',), 'apw',
               interval_seconds=10, response_bytes=262144),
    SourceSpec('apple_podcast', 'https://itunes.apple.com', ('podcast_episode',),
               ('podcast_search',), 'apple', interval_seconds=3, ttl_seconds=900, search=True, kind='api'),
)


def registered_source(key):
    require(type(key) is str)
    result = next((source for source in SOURCES if source.key == key), None)
    require(result is not None, 'CAPABILITY_UNAVAILABLE')
    return result


@dataclass(frozen=True, repr=False)
class SourceRequest:
    source: str
    category: str
    operation: str
    terms: tuple[str, ...] = ()
    ids: tuple[int, ...] = ()

    def __post_init__(self):
        spec = registered_source(self.source)
        require(self.category in spec.categories and self.operation in spec.operations)
        require(type(self.terms) is tuple and len(self.terms) <= 1)
        for term in self.terms:
            text(term)
        require(type(self.ids) is tuple and len(self.ids) <= 9)
        require(all(type(value) is int and 0 < value <= 2147483647 for value in self.ids))
        require(len(set(self.ids)) == len(self.ids))
        require((self.operation == 'book_details') == bool(self.ids))
        require(not spec.requires_terms(self.operation) or bool(self.terms))
        require(self.operation != spec.starter_operation or not self.terms)

    @property
    def request_digest(self):
        return digest(canonical_bytes(self.material))

    @property
    def material(self):
        return {'source': self.source, 'category': self.category, 'operation': self.operation,
                'terms': list(self.terms), 'ids': list(self.ids)}


def initial_source_request(source, category, terms):
    """Select normal search, declared empty-plan starter, or an explicit denial.

    ``terms`` are already purpose/category-filtered by the sole disclosure codec.
    No denied node or personal note is consulted here; a search failure cannot
    change this decision and cause an unpreviewed fallback.
    """
    spec = registered_source(source)
    require(type(terms) is tuple)
    for term in terms:
        text(term)
    if spec.search and not terms:
        require(spec.starter_operation is not None, 'NO_GROUNDED_CANDIDATES')
        return SourceRequest(source, category, spec.starter_operation)
    return SourceRequest(source, category, spec.operations[0], terms[:1] if spec.search else ())


@dataclass(frozen=True, repr=False)
class SourcePermit:
    """Server-issued port result; the collector still calls its issuer to revalidate."""
    source: str
    origin: str
    purpose: str
    plan_ref: str
    bindings: EnhanceBindings
    request_digest: str
    source_config_revision: int
    source_revision: int
    policy_ref: str
    policy_revision: int
    expires_at: datetime
    parent_ids: tuple[str, ...] = ()
    credential_generation: int | None = None

    def verify(self, request, plan, now):
        spec = registered_source(request.source)
        require(self.source == spec.key and self.origin == spec.origin
                and self.purpose == 'catalogue_lookup', 'AUTHORITY_DENIED')
        require(self.plan_ref == plan.plan_ref and self.bindings == plan.bindings
                and self.request_digest == request.request_digest, 'PREVIEW_STALE')
        require(utc(now) < min(utc(self.expires_at), plan.expires_at), 'PREVIEW_STALE')
        require(request.category in plan.categories, 'AUTHORITY_DENIED')
        approved = next(query['terms'] for query in read_json(plan.body)['queries']
                        if query['category'] == request.category)
        if request.operation == spec.starter_operation:
            # Editorial is the declared EMPTY-plan operation, never an escape
            # after denied/failed search or a way to hide an available term.
            require(not approved and not request.terms, 'AUTHORITY_DENIED')
        else:
            require(not spec.search or list(request.terms) == approved[:1], 'AUTHORITY_DENIED')
        for value in (self.source_config_revision, self.source_revision, self.policy_revision):
            require(type(value) is int and 0 <= value < 2**53)
        text(self.policy_ref)
        require(type(self.parent_ids) is tuple and len(self.parent_ids) <= 500
                and len(set(self.parent_ids)) == len(self.parent_ids))
        for parent in self.parent_ids:
            text(parent)
        # The issuer separately authenticates live owner intents. This check
        # binds their one explicit query root; it cannot confer source rights.
        body = read_json(plan.body)
        if body.get('schema') == 'catalogue-owner-query/v1':
            require(set(body) == {'schema', 'intent_ref', 'intent_digest', 'categories', 'queries'}
                    and body['intent_ref'] == plan.plan_ref and body['categories'] == ['tv']
                    and request.source == 'tmdb_tv' and request.operation == 'search_tv'
                    and body['queries'] == [{'category': 'tv', 'terms': list(request.terms)}]
                    and len(request.terms) == 1
                    and type(body['intent_digest']) is str and len(body['intent_digest']) == 64
                    and all(c in '0123456789abcdef' for c in body['intent_digest'])
                    and self.parent_ids == ('owner-query:' + plan.plan_ref + ':' + body['intent_digest'],),
                    'AUTHORITY_DENIED')
        else:
            require(body.get('schema') == 'catalogue-query/v1'
                    and not any(parent.startswith('owner-query:') for parent in self.parent_ids),
                    'AUTHORITY_DENIED')
        # Profile queries retain their original membership/order ancestry.
        require(not request.terms or bool(self.parent_ids), 'AUTHORITY_DENIED')
        require(not spec.credential_required or type(self.credential_generation) is int
                and self.credential_generation >= 0, 'CREDENTIAL_REQUIRED')


@dataclass(frozen=True, repr=False)
class SourceReply:
    request_digest: str
    status: int
    body: bytes = field(repr=False)
    observed_at: datetime
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        text(self.request_digest)
        require(type(self.status) is int and 100 <= self.status <= 599)
        require(type(self.body) is bytes)
        utc(self.observed_at)
        require(type(self.headers) is tuple and len(self.headers) <= 16)
        names = []
        for name, value in self.headers:
            require(name in ('retry-after', 'cache-control', 'content-type', 'date', 'etag', 'age', 'expires'))
            require(type(value) is str and len(value) <= 1024
                    and not any(ord(c) < 32 or ord(c) == 127 for c in value))
            names.append(name)
        require(len(set(names)) == len(names))


class SourceBackend(ABC):
    """Trusted integration port, not a transport chosen by request JSON.

    request must perform ONE fixed registered read under the supplied byte/time
    limits, with no redirects/retries or machine-key fallback. Its production
    implementation also owns cross-process/provider pacing and destination/TLS
    enforcement. None is supplied or activated by this source-only increment.
    """
    isolated_fixture = False

    @abstractmethod
    def authorize(self, request, plan, *, now): ...

    @abstractmethod
    def revalidate(self, permit, request, plan, *, now): ...

    @abstractmethod
    def policy(self, permit, *, now): ...

    @abstractmethod
    def request(self, request, permit, *, response_bytes, request_seconds, parent_seconds): ...


@dataclass(frozen=True)
class ParsedReply:
    cards: tuple[SourceCard, ...]
    enrichment_ids: tuple[int, ...] = ()


def normalize(spec, request, reply, *, now):
    """Call only existing fixed-source parsers, never their live fetch functions."""
    from .provider_catalogue import CatalogueError
    from .podcast_sources import PodcastSourceError
    try:
        return _normalize(spec, request, reply, now=now)
    except (CatalogueError, PodcastSourceError):
        raise EnhanceFault('SOURCE_UNAVAILABLE') from None


def _native_tv_first_air_dates(rows):
    """Optional facts from the SAME already validated first-nine TV rows.

    Legacy projection deduplicates IDs, so positional zipping could attach a
    different series' date. Any repeated ID is ambiguous here; ignore its date
    even when a duplicate supplies a seemingly usable name/version. Rows beyond
    the existing considered bound cannot supply or repair a missing fact.
    """
    values, duplicates = {}, set()
    for row in rows[:9]:
        source_id = row['id']  # parse_candidates has validated these exact rows.
        if source_id in values:
            duplicates.add(source_id)
        values[source_id] = row.get('first_air_date')
    dates = {}
    for source_id, value in values.items():
        if (source_id in duplicates or type(value) is not str or len(value) != 10
                or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value, re.ASCII)):
            continue
        try:
            datetime.fromisoformat(value)  # Calendar validity, not a new clock.
        except ValueError:
            continue
        dates[source_id] = value  # Preserve the original date, without trimming.
    return dates


def _normalize(spec, request, reply, *, now):
    from . import discovery_sources as source
    require(type(spec) is SourceSpec and spec == registered_source(request.source))
    require(type(reply) is SourceReply and reply.request_digest == request.request_digest)
    require(reply.status == 200 and len(reply.body) <= spec.response_bytes)
    if spec.parser == 'moshtix':
        return _moshtix_facts(request, reply, now=now)
    stamp = reply.observed_at.isoformat()
    cards = []
    if spec.parser in ('tmdb', 'hardcover'):
        from .provider_catalogue import parse_candidates
        document = source.strict_json(reply.body)
        require(type(document) is dict and 'errors' not in document)
        if spec.parser == 'hardcover':
            document = document.get('data')
            require(type(document) is dict and 'errors' not in document)
            if request.operation == 'book_search':
                search = document.get('search')
                require(type(search) is dict and not search.get('error'))
                ids = search.get('ids')
                require(type(ids) is list and len(ids) <= 9
                        and all(type(i) is int and 0 < i <= 2147483647 for i in ids))
                return ParsedReply((), tuple(dict.fromkeys(ids)))
            rows, provider, medium = document.get('books'), 'hardcover', 'book'
            starter = request.operation == spec.starter_operation
            require(type(rows) is list and len(rows) <= (6 if starter else 9), 'SOURCE_UNAVAILABLE')
            seen = set()
            for index, row in enumerate(rows):
                # Native details deliberately do NOT enter legacy metadata
                # parsing: accepting extra content would widen the reviewed
                # fetch/projection boundary despite a narrow final SourceCard.
                require(type(row) is dict and set(row) == {'id', 'title', 'slug'}, 'SOURCE_UNAVAILABLE')
                source_id, title, slug = row['id'], row['title'], row['slug']
                require(type(source_id) is int and 0 < source_id <= 2147483647
                        and (starter or source_id in request.ids) and source_id not in seen, 'SOURCE_UNAVAILABLE')
                require(type(title) is str and 0 < len(title) <= 300 and title.strip(), 'SOURCE_UNAVAILABLE')
                require(type(slug) is str and len(slug) <= 200
                        and re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug, re.ASCII), 'SOURCE_UNAVAILABLE')
                seen.add(source_id)
                cards.append(SourceCard(f'hardcover:book:{source_id}', title, 'book', 'hardcover',
                    str(source_id), 'https://hardcover.app/books/' + slug, f'normalized-source-row:{index}'))
            # A disappeared requested ID is an incomplete enrichment, never
            # evidence that the earlier successful search found no inventory.
            require(starter or seen == set(request.ids), 'SOURCE_UNAVAILABLE')
            return ParsedReply(tuple(cards))
        else:
            rows, provider, medium = document.get('results'), 'tmdb', request.category
        projected = parse_candidates(provider, medium, rows,
            lane='source-collection', lane_label='Bounded source collection',
            limit=6 if request.operation == spec.starter_operation else 9)
        dates = (_native_tv_first_air_dates(rows)
                 if spec.key == 'tmdb_tv' and request.operation == 'search_tv' else {})
        # The old helper stamps parsing time. Evidence uses the actual supplied
        # observation time below, so parsing/reuse never renews source freshness.
        for index, card in enumerate(projected):
            # An untrusted optional fact is not permission: existing evidence
            # filtering omits it unless its exact field uses are admitted.
            facts = (('tv.first_air_date', dates[card.source_id]),) if card.source_id in dates else ()
            cards.append(SourceCard(card.key, card.title, medium, provider,
                str(card.source_id), card.url, f'normalized-source-row:{index}', facts))
    elif spec.parser == 'musicbrainz':
        category = request.category
        require((request.source, category, request.operation) in (
            ('musicbrainz_recording', 'recording', 'recordings_by_artist_name'),
            ('musicbrainz_artist', 'artist', 'artists_by_name')))
        document = source.strict_json(reply.body)
        rows = document.get('artists' if category == 'artist' else 'recordings') if type(document) is dict else None
        require(type(rows) is list and len(rows) <= 9)
        projected = source.parse_musicbrainz(document, category, stamp)
        # The existing parser's rank is upstream response order, not CC0 core
        # identity data. Discard it, along with scores/tags/statistics. A stable
        # title tie-break makes duplicate-ID projection independent of response
        # order too; these local ordinals do not claim a provider ranking.
        unique = {}
        for row in sorted(projected, key=lambda row: (row.key, row.title)):
            unique.setdefault(row.key, row)
        for index, row in enumerate(unique.values()):
            cards.append(SourceCard(row.key, row.title, category, 'musicbrainz',
                row.key.rsplit(':', 1)[-1], row.url, f'normalized-core-{category}:{index}'))
    elif spec.parser == 'apple':
        from .podcast_sources import parse_results
        for index, row in enumerate(parse_results(source.strict_json(reply.body), stamp)):
            cards.append(SourceCard(row['key'], row['title'], 'podcast_episode', 'apple',
                row['apple_episode_id'], row['source_url'], f'normalized-source-row:{index}'))
    else:
        if spec.parser == 'moshtix':
            from .event_sources import parse_moshtix
            projected = parse_moshtix(reply.body, now=now, retrieved_at=stamp, provenance='live-automated')
        elif spec.parser in ('gertrude', 'apw'):
            from .gallery_sources import parse_gertrude, parse_apw
            projected = ((parse_gertrude if spec.parser == 'gertrude' else parse_apw)(
                reply.body, retrieved_at=stamp),)
        else:
            from . import rrr_events, beat_events, pbs_events, tribe_events, soundsystems_events
            parsers = {'rrr': rrr_events.parse_listing, 'beat': beat_events.parse_listing,
                       'pbs': pbs_events.parse_listing, 'tribe': tribe_events.parse_listing,
                       'soundsystems': soundsystems_events.parse_listing}
            projected = parsers[spec.parser](reply.body, retrieved_at=stamp)
        for row in projected:
            start = source.timestamp(row.starts_at)
            if start is not None and start <= now:
                continue
            # Date-only cards use the existing source's explicit local precision,
            # not an invented midnight instant or a year inferred from retrieval.
            if getattr(row, 'date_precision', None) == 'date':
                from .event_sources import event_timing
                if event_timing(row, now) in ('past-start', 'ended', 'cancelled', 'postponed'):
                    continue
            facts = tuple((name, getattr(row, name)) for name in
                ('starts_at', 'date_precision', 'event_timezone', 'published_at', 'feed_built_at', 'location_status')
                if type(getattr(row, name, None)) is str and getattr(row, name))
            cards.append(SourceCard(row.key, row.title, 'event', spec.key,
                row.key.rsplit(':', 1)[-1], row.url, f'normalized-source-position:{row.rank}', facts))
    require(len(cards) <= 20)
    return ParsedReply(tuple(cards))


def _moshtix_facts(request, reply, *, now):
    """Only the fixed selection's actual facts survive; missing stays missing.

    The existing event parser owns date/identity normalization. This narrower
    projection rejects added prose/media fields before they can become evidence.
    """
    from .discovery_sources import strict_json, timestamp
    from .event_sources import parse_moshtix
    try:
        document = strict_json(reply.body)
        require(type(document) is dict and not document.get('errors'), 'SOURCE_UNAVAILABLE')
        result = document['data']['viewer']['getEvents']
        rows = result['items']
        require(type(result) is dict and set(result) <= {'items', 'totalCount'}
                and type(rows) is list and len(rows) <= 9, 'SOURCE_UNAVAILABLE')
        by_id = {}
        for index, row in enumerate(rows):
            require(type(row) is dict and set(row) == {'id', 'name', 'startDate', 'venue'},
                    'SOURCE_UNAVAILABLE')
            venue, address = row['venue'], row['venue']['address']
            require(type(venue) is dict and set(venue) == {'id', 'name', 'address'}
                    and type(address) is dict and set(address) == {'locality', 'region'},
                    'SOURCE_UNAVAILABLE')
            require(type(row['name']) is str, 'SOURCE_UNAVAILABLE')
            for value, limit in ((row['name'], 160), (venue['name'], 160), (address['locality'], 80)):
                if value is None:
                    continue  # Nullable venue labels have no observation to bind.
                require(type(value) is str and 0 < len(value.strip()) <= limit
                        and not any(ord(c) < 32 or ord(c) == 127 or c in '<>' for c in value),
                        'SOURCE_UNAVAILABLE')
            require(address['region'] == 'VIC' and type(venue['id']) is int
                    and 0 < venue['id'] < 2**63, 'SOURCE_UNAVAILABLE')
            event_id = row['id']
            if type(event_id) is str and event_id.isascii() and event_id.isdigit():
                require(str(int(event_id)) == event_id, 'SOURCE_UNAVAILABLE')
                event_id = int(event_id)
            require(type(event_id) is int and 0 < event_id < 2**63
                    and event_id not in by_id, 'SOURCE_UNAVAILABLE')
            require(type(row['startDate']) is str and len(row['startDate']) <= 50
                    and timestamp(row['startDate']) is not None, 'SOURCE_UNAVAILABLE')
            by_id[event_id] = (index, row)
        parsed = parse_moshtix(reply.body, now=now,
            retrieved_at=reply.observed_at.isoformat(), provenance='live-automated')
        cards = []
        for event in parsed:
            event_id = int(event.source_event_id)
            index, row = by_id[event_id]
            venue = row['venue']
            facts = (('event.id', event_id), ('event.startDate', row['startDate']),
                ('venue.id', venue['id']), ('venue.address.region', venue['address']['region']))
            facts += tuple((name, value) for name, value in
                (('venue.name', venue['name']), ('venue.address.locality', venue['address']['locality']))
                if value is not None)
            cards.append(SourceCard(event.key, event.title, 'event', 'moshtix',
                str(event_id), event.official_url, f'data.viewer.getEvents.items[{index}]', facts))
        return ParsedReply(tuple(cards))
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError, RecursionError) as error:
        if isinstance(error, EnhanceFault):
            raise
        raise EnhanceFault('SOURCE_UNAVAILABLE') from None


class SourceAuthorityIssuer(ABC):
    """Taste-owned server dependency; no request JSON can implement this port.

    operation_for returns the operation retained by the exact source preview.
    authorize/revalidate must bind its digest AND actual bytes, plan/body and
    profile/session/installation/grant/recovery/configuration/policy revisions.
    No OpenAI authority may issue catalogue_lookup or authorize this origin.
    """
    @abstractmethod
    def operation_for(self, request, plan, *, now): ...

    @abstractmethod
    def authorize(self, request, plan, operation, *, now): ...

    @abstractmethod
    def revalidate(self, permit, request, plan, operation, *, now): ...

    @abstractmethod
    def policy(self, permit, operation, *, now): ...

    @abstractmethod
    def model_context_current(self, permit, operation, *, now): ...

    def policy_context(self, permit, operation, *, now):
        """Compatibility path: a generic issuer still proves processor rights.

        Concrete issuers may validate both results together. The default retains
        independent checks and never infers processor permission from a policy.
        """
        policy = self.policy(permit, operation, now=now)
        require(type(policy) is FieldPolicy, 'AUTHORITY_DENIED')
        model_current = (self.model_context_current(permit, operation, now=now) is True
                         if any('model_context' in uses for _, uses in policy.permissions) else False)
        return policy, model_current


@dataclass
class _MoshtixContext:
    permit: SourcePermit
    request: SourceRequest
    plan: object
    operation: object
    sent: bool = False


class MoshtixBackend(SourceBackend):
    """Real fixed transport composition; construction does not register a source.

    The issuer must close the incorporated-terms, source consent, VIC relevance,
    truthful identity/contact and current policy gates. No issuer or shared host
    limiter is supplied by default, and no denied model right is manufactured.
    """
    def __init__(self, issuer, limiter, *, clock):
        from .discovery_transport import MoshtixHostLimiter
        require(isinstance(issuer, SourceAuthorityIssuer)
                and type(limiter) is MoshtixHostLimiter, 'CAPABILITY_UNAVAILABLE')
        require(callable(getattr(clock, 'now', None)), 'CAPABILITY_UNAVAILABLE')
        self.issuer, self.limiter, self.clock = issuer, limiter, clock
        self._contexts, self._lock = {}, RLock()

    def authorize(self, request, plan, *, now):
        from .discovery_transport import MoshtixOperation
        require(type(request) is SourceRequest and request.source == 'moshtix'
                and request.category == 'event' and request.operation == 'events'
                and not request.terms and not request.ids, 'AUTHORITY_DENIED')
        operation = self.issuer.operation_for(request, plan, now=now)
        require(type(operation) is MoshtixOperation and operation.start <= utc(now)
                < operation.start + timedelta(minutes=15), 'PREVIEW_STALE')
        permit = self.issuer.authorize(request, plan, operation, now=now)
        require(type(permit) is SourcePermit and permit.credential_generation is None
                and not permit.parent_ids, 'AUTHORITY_DENIED')
        permit.verify(request, plan, now)
        context = _MoshtixContext(permit, request, plan, operation)
        with self._lock:
            for key in [key for key, value in self._contexts.items()
                        if utc(now) >= value.permit.expires_at]:
                del self._contexts[key]
            require(len(self._contexts) < 128, 'CAPABILITY_UNAVAILABLE')
            self._contexts[id(permit)] = context
        require(self.revalidate(permit, request, plan, now=now) is True, 'AUTHORITY_DENIED')
        return permit

    def _context(self, permit):
        with self._lock:
            context = self._contexts.get(id(permit))
        require(context is not None and context.permit is permit, 'AUTHORITY_DENIED')
        return context

    def revalidate(self, permit, request, plan, *, now):
        context = self._context(permit)
        require(context.request == request and context.plan == plan, 'AUTHORITY_DENIED')
        permit.verify(request, plan, now)
        require(context.operation.start <= utc(now) < context.operation.start + timedelta(minutes=15),
                'PREVIEW_STALE')
        return self.issuer.revalidate(permit, request, plan, context.operation, now=now) is True

    def policy(self, permit, *, now):
        from .source_evidence import validate_moshtix_policy
        context = self._context(permit)
        require(self.revalidate(permit, context.request, context.plan, now=now), 'PREVIEW_STALE')
        policy = self.issuer.policy(permit, context.operation, now=now)
        model_current = self.issuer.model_context_current(permit, context.operation, now=now) is True
        validate_moshtix_policy(policy, permit=permit, now=now, model_context_current=model_current)
        return policy

    def request(self, request, permit, *, response_bytes, request_seconds, parent_seconds):
        from .discovery_transport import (events_authorized, MoshtixRateLimited)
        from .discovery_sources import SourceError
        context = self._context(permit)
        require(context.request == request, 'AUTHORITY_DENIED')
        with self._lock:
            require(not context.sent, 'AUTHORITY_DENIED')
            context.sent = True  # Reserve once, including denied/failed attempts.
        status, headers, slot = None, (), None
        try:
            self.policy(permit, now=utc(self.clock.now()))
            slot = self.limiter.reserve()

            def current():
                now = utc(self.clock.now())
                require(slot.current() and self.revalidate(permit, request, context.plan, now=now),
                        'PREVIEW_STALE')
                self.policy(permit, now=now)
                return True

            status, body, headers = events_authorized(context.operation, revalidate=current,
                response_bytes=response_bytes, request_seconds=request_seconds,
                parent_seconds=parent_seconds)
            reply = SourceReply(request.request_digest, status, body, utc(self.clock.now()), headers)
            if status == 200:
                # Reject facts outside the authorized original date window,
                # even if a late parser clock would otherwise admit them.
                parsed = _moshtix_facts(request, reply, now=context.operation.start)
                document = read_json(body)
                require(len(parsed.cards) == len(document['data']['viewer']['getEvents']['items']),
                        'SOURCE_UNAVAILABLE')
            return reply
        except MoshtixRateLimited:
            raise EnhanceFault('RATE_LIMITED') from None
        except SourceError:
            raise EnhanceFault('SOURCE_UNAVAILABLE') from None
        finally:
            if slot is not None:
                slot.finish(status, headers, now=utc(self.clock.now()))


class MediaBackend(SourceBackend):
    """Concrete accepted-wire composition with current issuer and vault only.

    Never discovers credentials or synthesizes source rights. Exact search and
    detail operations each need their own issuer preview and confirmation.
    """
    def __init__(self, issuer, vault, limiter, *, clock):
        from .source_authority import SourceAuthority
        from .secret_leases import MemoryCredentials
        from .discovery_transport import MediaHostLimiter
        require(type(issuer) is SourceAuthority and type(vault) is MemoryCredentials
                and vault.authority is issuer.authority
                and getattr(issuer.authority, '_source_vault', None) is vault, 'CAPABILITY_UNAVAILABLE')
        self.source = issuer.source_key()
        require(self.source in ('tmdb_movie', 'tmdb_tv', 'hardcover'), 'CAPABILITY_UNAVAILABLE')
        require(type(limiter) is MediaHostLimiter and limiter.provider ==
                ('hardcover' if self.source == 'hardcover' else 'tmdb'), 'CAPABILITY_UNAVAILABLE')
        self.issuer, self.vault, self.limiter, self.clock = issuer, vault, limiter, clock
        self._contexts, self._lock = {}, RLock()

    def authorize(self, request, plan, *, now):
        require(type(request) is SourceRequest and request.source == self.source, 'AUTHORITY_DENIED')
        operation = self.issuer.operation_for(request, plan, now=now)
        from .provider_catalogue import media_operation
        require(operation == media_operation(request), 'PREVIEW_STALE')
        permit = self.issuer.authorize(request, plan, operation, now=now)
        require(type(permit) is SourcePermit and type(permit.credential_generation) is int,
                'CREDENTIAL_REQUIRED')
        permit.verify(request, plan, now)
        with self._lock:
            self._contexts = {key: value for key, value in self._contexts.items()
                              if utc(now) < value.permit.expires_at}
            require(len(self._contexts) < 128, 'CAPABILITY_UNAVAILABLE')
            self._contexts[id(permit)] = _MoshtixContext(permit, request, plan, operation)
        self.policy(permit, now=now)
        return permit

    def _context(self, permit):
        context = self._contexts.get(id(permit))
        require(context is not None and context.permit is permit, 'AUTHORITY_DENIED')
        return context

    def revalidate(self, permit, request, plan, *, now):
        context = self._context(permit)
        require(context.request == request and context.plan == plan, 'AUTHORITY_DENIED')
        permit.verify(request, plan, now)
        return self.issuer.revalidate(permit, request, plan, context.operation, now=now) is True

    def policy(self, permit, *, now):
        context = self._context(permit)
        require(self.revalidate(permit, context.request, context.plan, now=now), 'PREVIEW_STALE')
        policy, model_current = self.issuer.policy_context(permit, context.operation, now=now)
        require(type(policy) is FieldPolicy and policy.current(now)
                and policy.ref == permit.policy_ref and policy.revision == permit.policy_revision
                and policy.allows('*', 'fetch'), 'AUTHORITY_DENIED')
        require(not any('model_context' in uses for _, uses in policy.permissions)
                or model_current is True,
                'AUTHORITY_DENIED')
        return policy

    def request(self, request, permit, *, response_bytes, request_seconds, parent_seconds):
        from . import grant_authority as ga
        from .discovery_transport import media_authorized, MoshtixRateLimited
        from .discovery_sources import SourceError
        context = self._context(permit)
        require(context.request == request, 'AUTHORITY_DENIED')
        with self._lock:
            require(not context.sent, 'AUTHORITY_DENIED')
            context.sent = True
        def current():
            self.policy(permit, now=utc(self.clock.now()))
            return True
        try:
            with ga.recovery_guard(self.issuer.authority, self.issuer.session_ref,
                                   profile_ref=self.issuer.profile_ref):
                current()
                with self.vault.ephemeral(self.issuer.session_ref,
                    ga._uuid(self.issuer.connection_ref), permit.source_config_revision,
                    credential_generation=permit.credential_generation) as secret:
                    status, body, headers = media_authorized(context.operation, secret=secret,
                        revalidate=current, limiter=self.limiter, response_bytes=response_bytes,
                        request_seconds=min(5, request_seconds), parent_seconds=min(6, parent_seconds))
                current()
                reply = SourceReply(request.request_digest, status, body, utc(self.clock.now()), headers)
                if status == 200 and request.operation == 'book_search':
                    self.issuer.remember_details(permit, reply)
                return reply
        except MoshtixRateLimited:
            raise EnhanceFault('RATE_LIMITED') from None
        except SourceError:
            raise EnhanceFault('SOURCE_UNAVAILABLE') from None
