"""Small computer-use contract for selected cultural pages; no browser driver.

A future driver must use ordinary visible browser UI and stop on any access
challenge. Cookies, credentials, browser storage and raw page bodies never enter
this contract. Source text is data, never instructions or authority to browse.
"""
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import re
from typing import Protocol
from urllib.parse import urlsplit

from shelf.discovery_sources import Candidate, SourceError, timestamp
from shelf.event_sources import SOURCES, official_url

MAX_SOURCES = 12
MAX_TASKS = 30
MAX_ITEMS = 5
MAX_INBOX = 60
REVIEW_TTL = timedelta(hours=24)
TASK_TTL = timedelta(minutes=10)
STATUSES = {
    'queued': 'Queued for one explicit read',
    'reading': 'Read interrupted or in progress; results not accepted yet',
    'ready': 'Bounded read complete; review the captured leads',
    'empty': 'No cultural leads in this bounded read; coverage remains partial',
    'not_connected': 'Browser adapter not connected',
    'login_needed': 'Login needed in your browser; this read stopped',
    'challenge': 'Browser challenge; this read stopped',
    'policy_block': 'Access or policy block; this read stopped',
    'unavailable': 'Read unavailable; no results accepted',
    'cancelled': 'Cancelled locally',
    'expired': 'Queued request expired; choose a new read',
}
OUTCOMES = frozenset(STATUSES) - {'queued', 'reading', 'cancelled', 'expired'}
SCENARIOS = ('ready', 'empty', 'login_needed', 'challenge', 'policy_block', 'not_connected', 'unavailable')
RESERVED = frozenset(('.', '..', 'accounts', 'account', 'login', 'logout', 'checkpoint', 'challenge', 'direct',
    'messages', 'messenger', 'friends', 'groups', 'home', 'feed', 'reels', 'explore', 'stories',
    'settings', 'notifications', 'marketplace', 'share', 'sharer', 'watch', 'search', 'oauth',
    'privacy', 'help', 'events', 'p', 'reel', 'profile.php', 'me', 'gaming', 'pages'))
HOSTS = {'facebook': ('facebook.com', 'www.facebook.com'),
         'instagram': ('instagram.com', 'www.instagram.com')}


def short_text(value, maximum=160, *, optional=False):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise SourceError('Invalid short cultural-event text.')
    value = value.strip()
    if not value and not optional:
        raise SourceError('A short cultural-event label is required.')
    return value


def social_url(platform, value, *, source=False):
    if platform not in HOSTS or not isinstance(value, str) or len(value) > 512:
        raise SourceError('Choose a supported Facebook or Instagram page URL.')
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or parts.netloc not in HOSTS[platform] or parts.query
                or parts.fragment or parts.port or '%' in value or '\\' in value
                or any(ord(c) < 33 or ord(c) == 127 for c in value)):
            raise ValueError
    except ValueError:
        raise SourceError('Use a clean HTTPS page link without tracking parameters.') from None
    path = parts.path.strip('/')
    slug = r'[A-Za-z0-9_.-]{1,100}'
    page = bool(re.fullmatch(slug, path)) and path.lower() not in RESERVED
    event = platform == 'facebook' and bool(re.fullmatch(r'events/[1-9][0-9]{0,24}', path))
    if source:
        valid = page or event
    elif platform == 'facebook':
        valid = event or bool(re.fullmatch(slug + r'/posts/[A-Za-z0-9_-]{1,100}', path))
        if '/posts/' in path and path.split('/')[0].lower() in RESERVED:
            valid = False
    else:
        valid = bool(re.fullmatch(r'(p|reel)/[A-Za-z0-9_-]{1,100}', path))
    if not valid:
        raise SourceError('Use a selected cultural page or an exact event/post permalink, not a personal feed.')
    return f'https://www.{platform}.com/{path}'


def event_url(value):
    """Optional cross-feed identity: exact known public event URL, never title matching."""
    if not value:
        return ''
    if not isinstance(value, str) or len(value) > 2048:
        raise SourceError('Invalid or oversized event link.')
    try:
        hostname = urlsplit(value).hostname
    except ValueError:
        raise SourceError('Invalid event link.') from None
    if hostname in HOSTS['facebook']:
        result = social_url('facebook', value)
        if '/events/' not in result:
            raise SourceError('The event link must identify an event.')
        return result
    for source in SOURCES.values():
        if hostname == urlsplit(source.listing_url).hostname:
            result = official_url(value, source)
            if result == official_url(source.listing_url, source):
                raise SourceError('Use an event detail link, not the listing page.')
            return result
    # The existing Moshtix projection uses this public hostname, not its API host.
    if re.fullmatch(r'https://(?:www\.)?moshtix\.com\.au/v2/event/[a-z0-9-]+/[1-9][0-9]{0,18}/?', value):
        return value.rstrip('/').replace('https://www.moshtix.com.au/', 'https://moshtix.com.au/')
    raise SourceError('Use a supported official event link, or leave it blank and retain the social permalink.')


def source_record(platform, url, label, kind, scope):
    url = social_url(platform, url, source=True)
    if kind not in ('venue', 'artist', 'page', 'event') or scope not in ('followed', 'public_event'):
        raise SourceError('Choose the cultural source type and scope.')
    is_event = platform == 'facebook' and '/events/' in url
    if (scope == 'public_event') != is_event or (kind == 'event') != is_event:
        raise SourceError('Public-event scope requires an exact Facebook event URL; other sources must be selected followed pages.')
    return dict(id=sha256(url.encode()).hexdigest(), platform=platform, url=url,
                label=short_text(label, 100), kind=kind, scope=scope)


@dataclass(frozen=True)
class BrowserRead:
    source_url: str
    platform: str
    source_kind: str
    started_at: str
    max_items: int = MAX_ITEMS
    max_pages: int = 1
    max_ui_actions: int = 8
    timeout_seconds: int = 60


@dataclass(frozen=True)
class BrowserOutcome:
    status: str
    source_url: str
    items: tuple = ()


class BrowserAdapter(Protocol):
    def read(self, request: BrowserRead) -> BrowserOutcome:
        """Read only the selected page's cultural announcements within the budget.

        Stop on login, challenge, policy block, timeout or changed scope. No DMs,
        friends' private posts, engagement, account changes or linked-page crawl.
        The adapter must enforce the UI/time budget before returning bounded data.
        """
        ...


class DisconnectedBrowser:
    def read(self, request):
        return BrowserOutcome('not_connected', request.source_url)


class SampleBrowser:
    """Fictional outcomes for local UI tests; never accesses a social account."""
    def __init__(self, scenario):
        if scenario not in SCENARIOS:
            raise SourceError('Unknown synthetic outcome.')
        self.scenario = scenario

    def read(self, request):
        items = ()
        if self.scenario == 'ready':
            post = (request.source_url + '/posts/fixture-announcement' if request.platform == 'facebook'
                    else 'https://www.instagram.com/p/fixture-announcement')
            if '/events/' in request.source_url:
                post = request.source_url
            items = ({'title': 'SAMPLE: Light, piano & moving image', 'post_url': post,
                'event_url': '', 'listed_when': 'Date to be announced',
                'captured_at': request.started_at, 'published_at': ''},)
        return BrowserOutcome(self.scenario, request.source_url, items)


def adapter(mode, scenario='ready'):
    if mode == 'household':
        return DisconnectedBrowser()
    if mode == 'sample':
        return SampleBrowser(scenario)
    raise SourceError('Invalid social discovery mode.')


def normalize_outcome(outcome, source, started_at, now, *, provenance):
    if (type(outcome) is not BrowserOutcome or outcome.status not in OUTCOMES
            or outcome.source_url != source['url'] or type(outcome.items) is not tuple
            or len(outcome.items) > MAX_ITEMS or (outcome.status != 'ready' and outcome.items)
            or (outcome.status == 'ready' and not outcome.items)):
        raise SourceError('Invalid bounded browser result.')
    start = timestamp(started_at)
    if start is None or not 0 <= (now - start).total_seconds() <= 60:
        raise SourceError('Browser read exceeded its time budget.')
    results = []
    for item in outcome.items:
        if (type(item) is not dict or set(item) != {'title', 'post_url', 'event_url', 'listed_when', 'captured_at', 'published_at'}
                or any(not isinstance(value, str) for value in item.values())):
            raise SourceError('Only the bounded cultural projection is accepted.')
        captured, published = timestamp(item['captured_at']), timestamp(item['published_at'])
        if (captured is None or not start <= captured <= now or
                (item['published_at'] and (published is None or published > captured))):
            raise SourceError('Invalid capture or source publication time.')
        post = social_url(source['platform'], item['post_url'])
        if source['platform'] == 'facebook' and post != source['url'] and not post.startswith(source['url'] + '/posts/'):
            raise SourceError('Post is outside the selected page. Select the related public event separately.')
        canonical = event_url(item['event_url'])
        results.append(dict(id=sha256((source['id'] + '\0' + post).encode()).hexdigest(),
            source_id=source['id'], title=short_text(item['title']), post_url=post,
            event_url=canonical, listed_when=short_text(item['listed_when'], optional=True),
            captured_at=captured.isoformat(), published_at=published.isoformat() if published else '',
            provenance=provenance, review='pending'))
    if len({row['id'] for row in results}) != len(results):
        raise SourceError('Duplicate browser observations.')
    return results


def fresh(row, now):
    captured = timestamp(row['captured_at'])
    return captured is not None and timedelta(0) <= now - captured < REVIEW_TTL


@dataclass(frozen=True)
class SocialCandidate(Candidate):
    social_evidence: tuple = ()

    def date_status(self, now):
        return 'Announcement only; exact session, timezone, cancellation and ticket availability unconfirmed.'

    def freshness(self, now):
        return 'Captured within 24 hours; publication freshness and current availability remain unverified.'
