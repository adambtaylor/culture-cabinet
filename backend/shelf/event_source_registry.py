"""Closed event sources: descriptors are configuration, never connection proof.

Callables are fixed application code. Public controls receive metadata only;
source bytes and form values cannot install URLs, parsers or credentials.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable
from urllib.parse import urlsplit

from shelf.discovery_sources import SourceError

# Restriction identities outlive acquisition and bookmark-policy readiness.
# This is a closed map, not permission or an event-source:* prefix parser.
SOURCE_NAMESPACES = MappingProxyType({
    'pbs': 'event-source:pbs', 'rrr': 'event-source:rrr',
    'venue303': 'event-source:venue303', 'forum': 'event-source:forum',
    'moshtix': 'event-source:moshtix',
})
REFERENCE_POLICY = 'event-private-bookmark-reference/v1'


@dataclass(frozen=True)
class EventSourceDescriptor:
    source_id: str
    label: str
    targets: tuple[str, ...]
    acquire: Callable | None = None
    parse: Callable | None = None
    coverage: str = ''
    access_state: str = 'not-implemented'
    unavailable_message: str = 'This source is not connected yet.'
    max_bytes: int = 524288
    retention_seconds: int = 0
    policy_scope: str = 'Source access and retention remain unverified.'
    # Content-cache retention does not confer persistent bookmark permission.
    # Positive reviewed policies are supplied separately; absent means opaque.
    reference_policy_ref: str | None = None
    restriction_namespace: str = ''
    reference_url_validator: Callable | None = None

    @property
    def implemented(self):
        return self.acquire is not None and self.parse is not None

    @property
    def origin_groups(self):
        # Shared-host routes share one origin identity even across descriptors
        # (notably ABC/NGV). Acquisition keeps the existing host limiter history.
        return tuple(dict.fromkeys('https://' + urlsplit(url).netloc for url in self.targets))


def _rrr(now):
    from shelf.discovery_transport import rrr
    return rrr()


def _pbs(now):
    from shelf.discovery_transport import pbs
    return pbs()


def _venue303(now):
    from shelf.event_listing_transport import venue303
    return venue303()


def _forum(now):
    from shelf.event_listing_transport import forum
    return forum()


def _moshtix(now):
    from shelf.discovery_transport import events
    return events(now)  # Accepted one-row normal query, not the nine-row helper.


def _moons24(now):
    from shelf.event_listing_transport import moons24
    return moons24()


def _parse_rrr(raw, *, now, retrieved_at):
    from shelf.rrr_events import parse_listing
    return parse_listing(raw, retrieved_at=retrieved_at)


def _parse_pbs(raw, *, now, retrieved_at):
    from shelf.pbs_events import parse_listing
    return parse_listing(raw, retrieved_at=retrieved_at)


def _parse_venue303(raw, *, now, retrieved_at):
    from shelf.venue303_events import parse_listing
    return parse_listing(raw, retrieved_at=retrieved_at, provenance='live-automated')


def _parse_forum(raw, *, now, retrieved_at):
    from shelf.forum_events import parse_listing
    return parse_listing(raw, retrieved_at=retrieved_at, provenance='live-automated')


def _parse_moshtix(raw, *, now, retrieved_at):
    from shelf.discovery_sources import parse_public_event_response
    return parse_public_event_response(raw, now=now, retrieved_at=retrieved_at)


def _parse_moons24(raw, *, now, retrieved_at):
    from shelf.moons24_events import parse_listing
    return parse_listing(raw, retrieved_at=retrieved_at)


# Finite owner/Architecture catalogue, not an expandable client-controlled list.
# Unimplemented entries are explicit outcomes with zero acquisition, never seeds.
_PLANNED = (
    ('rrr', 'Triple R', ('https://www.rrr.org.au/events',), 'retained timeout'),
    ('pbs', 'PBS', ('https://www.pbsfm.org.au/news/pbs-events',), 'live unrun'),
    ('venue303', '303', ('https://303.net.au/gigs-events',), 'retained transport failure'),
    ('forum', 'Forum Melbourne', ('https://forummelbourne.com.au/shows',), 'access evidence unresolved'),
    ('moshtix', 'Moshtix', ('https://api.moshtix.com/v1/graphql',), 'reuse existing anonymous query and Unit Tester evidence'),
    ('bar_open', 'Bar Open', ('https://baropen.com.au/', 'https://baropen.com.au/make-it-up-club/'), 'listing shell/recurrence insufficient'),
    ('my_aeon', 'My Aeon', ('https://www.myaeon.com.au/my-aeon-events',), 'observed cards stale'),
    ('moons24', '24 Moons', ('https://24moons.com.au/events',), 'corrected identity, dated cards observed'),
    ('ngv', 'NGV', ('https://www.ngv.vic.gov.au/whats-on/programs-events/', 'https://www.ngv.vic.gov.au/whats-on/exhibitions/'), 'separate sessions/runs'),
    ('gertrude', 'Gertrude', ('https://gertrude.org.au/exhibitions/',), 'mixed archive/current index'),
    ('apw', 'Australian Print Workshop', ('https://www.australianprintworkshop.com/',), 'factual event scope, no artwork offers'),
    ('west_space', 'West Space', ('https://westspace.org.au/whats-on/',), 'exhibition ranges'),
    ('linden', 'Linden New Art', ('https://www.lindenarts.org/public-programs/events/',), 'dated programmes/audience limits'),
    ('affordable_art_fair', 'Affordable Art Fair Melbourne', ('https://affordableartfair.com/fairs/Melbourne/',), 'fair/run distinct from individual events'),
    ('triple_j', 'Triple J', ('https://www.abc.net.au/triplej/touring',), 'shell, current Melbourne guide unverified'),
    ('double_j', 'Double J', ('https://www.abc.net.au/listen/doublej/touring', 'https://www.abc.net.au/listen/doublej/music-news'), 'dated articles are not a complete gig guide'),
    ('miff', 'MIFF', ('https://miff.com.au/',), 'programme edition/session distinction'),
    ('comedy_festival', 'Melbourne International Comedy Festival', ('https://www.comedyfestival.com.au/',), 'active programme unverified'),
    ('fringe', 'Melbourne Fringe', ('https://www.melbournefringe.com.au/whats-on/events',), 'season/archive distinction'),
    ('butterfly_club', 'The Butterfly Club', ('https://thebutterflyclub.com/',), 'corrected identity, retained timeout'),
    ('impro_melbourne', 'Impro Melbourne', ('https://www.impromelbourne.com.au/shows/2026-season/',), 'exact dated season, no year rollover'),
    ('improv_conspiracy', 'Improv Conspiracy', ('https://improvconspiracy.com/',), 'actual performance-calendar target still needed'),
    ('la_sirene', 'La Sirene', ('https://www.instagram.com/lasirenebrewing/', 'https://lasirene.com.au/pages/events'), 'legitimate visual route; login/age wall'),
    ('halftix', 'Halftix', ('https://halftixmelbourne.com/all-events/',), 'no-fetch retention restriction'),
    ('hamer_hall', 'Hamer Hall', ('https://www.artscentremelbourne.com.au/whats-on',), 'Queue-it, exact venue filtering needed'),
    ('old_bar', 'The Old Bar', ('https://www.theoldbar.com.au/',), 'dated HTML/advertised ICS'),
    ('arts_house', 'Arts House', ('https://www.artshouse.com.au/whats-on/',), 'programme cards'),
    ('acmi', 'ACMI', ('https://www.acmi.net.au/whats-on/',), 'season/screening distinction'),
    ('mjc', 'Melbourne Jazz Co-op', ('https://mjc.org.au/upcoming-gigs/',), 'promoter across rooms'),
    ('jazzlab', 'The JazzLab', ('https://thejazzlab.com.au/events/',), 'venue/session evidence'),
    ('heide', 'Heide', ('https://www.heide.com.au/events/',), 'recurrence not a next occurrence'),
    ('recital_centre', 'Melbourne Recital Centre', ('https://www.melbournerecital.com.au/whats-on/',), 'listing shell'),
    ('substation', 'The Substation', ('https://thesubstation.org.au/whats-on/',), 'incomplete programme'),
    ('footscray_arts', 'Footscray Community Arts', ('https://footscrayarts.com/whats-on/',), 'incomplete programme'),
    ('sub_club', 'Sub Club', ('https://www.subclubmelbourne.com/events',), 'outbound Facebook link, no inventory'),
)

def _descriptor(row):
    source_id, label, targets, boundary = row
    label = {'rrr': 'Triple R events', 'pbs': 'PBS Events',
             'venue303': '303 gigs and events', 'forum': 'Forum Melbourne shows'}.get(source_id, label)
    common = dict(source_id=source_id, label=label, targets=targets,
                  restriction_namespace=SOURCE_NAMESPACES.get(source_id, ''))
    active = {
        'rrr': (_rrr, _parse_rrr, 131072,
                'Partial coverage: up to nine cards from one Triple R listing page. '
                'Source dates omit the year; upcoming status and ticket availability unverified.'),
        'pbs': (_pbs, _parse_pbs, 131072,
                'Partial PBS coverage: up to nine upcoming-section leads within eight weeks; '
                'unknown dates are labelled. Exact sessions and ticket availability unverified.'),
        'venue303': (_venue303, _parse_venue303, 524288,
                'Partial 303 coverage: one listing, up to nine source observations within eight weeks; '
                'unknown dates/session identity and ticket availability remain unverified.'),
        'forum': (_forum, _parse_forum, 524288,
                'Partial Forum coverage: one listing, up to nine source observations; '
                'dates and source notices are not verified ticket availability.'),
        'moshtix': (_moshtix, _parse_moshtix, 131072,
                'Partial Moshtix coverage: one public event in VIC within eight weeks; '
                'status and ticket availability unverified.'),
        'moons24': (_moons24, _parse_moons24, 524288,
                'Partial 24 Moons coverage: current-week plus later programme, up to nine leads '
                'within eight weeks. Unknown dates remain unverified; source links open the '
                'programme, not an exact session or a checked seller.'),
    }
    if source_id in active:
        acquire, parse, limit, coverage = active[source_id]
        return EventSourceDescriptor(**common, acquire=acquire, parse=parse, coverage=coverage,
            access_state='access-unverified' if source_id == 'forum' else 'implemented-unproved',
            max_bytes=limit, retention_seconds=900,
            policy_scope='Existing bounded factual-link scope only; no model or redistribution grant.')
    blocked = {
        'halftix': ('permission-required', 'Publisher permission for retrieval and storage is required; open the official link manually.'),
        'la_sirene': ('owner-action-required', 'A permitted, witnessed visual observation is needed; no public posts are available to this reader.'),
        'hamer_hall': ('access-unverified', 'The official programme reached a waiting room; no bypass or alternate route is attempted.'),
    }
    state, message = blocked.get(source_id, ('not-implemented', 'This source is not connected yet. Check the official source for current listings.'))
    return EventSourceDescriptor(**common, access_state=state, unavailable_message=message)


SOURCES = MappingProxyType({row[0]: _descriptor(row) for row in _PLANNED})
LEGACY_ALIASES = MappingProxyType({'rrr': 'rrr', 'pbs': 'pbs', 'venue303': 'venue303',
                                  'forum': 'forum', 'events': 'moshtix'})


def get_source(source_id):
    if type(source_id) is not str or source_id not in SOURCES:
        raise SourceError('Unknown fixed event source.')
    return SOURCES[source_id]


def source_cache_key(source_id):
    """Generic and legacy controls replace the same cached source result.

    Keeping legacy keys avoids both duplicate source cards and stale success
    surviving a failed refresh through the other control. No source I/O occurs.
    """
    get_source(source_id)
    legacy = next((alias for alias, source in LEGACY_ALIASES.items() if source == source_id), source_id)
    return legacy, '', ''


def source_controls():
    """Plain metadata only; Django templates must never receive callables."""
    return tuple({'source_id': source.source_id, 'label': source.label,
                  'implemented': source.implemented, 'access_state': source.access_state,
                  'official_urls': source.targets, 'origin_groups': source.origin_groups,
                  'coverage': source.coverage, 'retention_seconds': source.retention_seconds,
                  'unavailable_message': source.unavailable_message}
                 for source in SOURCES.values())
