"""Fixed public targets, sharing per-origin reservations across processes."""
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import time
from urllib.robotparser import RobotFileParser
from uuid import UUID
from types import MappingProxyType

from shelf.discovery_sources import SourceError, strict_json
from shelf.discovery_transport import _base, _send

MAX_BYTES = 512 * 1024
USER_AGENT = 'CultureCabinet/0.1 (private factual event listing reader)'
TARGETS = MappingProxyType({
    'venue303': ('https://303.net.au/gigs-events', 'https://303.net.au/robots.txt'),
    'forum': ('https://forummelbourne.com.au/shows', 'https://forummelbourne.com.au/robots.txt'),
    'bar_open': ('https://baropen.com.au/make-it-up-club/', 'https://baropen.com.au/robots.txt'),
    'west_space': ('https://westspace.org.au/whats-on/', 'https://westspace.org.au/robots.txt'),
    'moons24': ('https://24moons.com.au/events', 'https://24moons.com.au/robots.txt'),
    'affordable_art_fair': ('https://affordableartfair.com/fairs/Melbourne/', 'https://affordableartfair.com/robots.txt'),
    'improv_conspiracy': ('https://improvconspiracy.com/', 'https://improvconspiracy.com/robots.txt'),
    'triple_j': ('https://www.abc.net.au/triplej/touring', 'https://www.abc.net.au/robots.txt'),
    'double_j': ('https://www.abc.net.au/listen/doublej/touring', 'https://www.abc.net.au/robots.txt'),
    'my_aeon': ('https://www.myaeon.com.au/my-aeon-events', 'https://www.myaeon.com.au/robots.txt'),
})
_DELAYS = {'venue303': 10, 'forum': 10, 'bar_open': 10, 'west_space': 10, 'moons24': 10,
           'affordable_art_fair': 10, 'improv_conspiracy': 10,
           'triple_j': 10, 'double_j': 10, 'my_aeon': 10}
_ORIGIN_GROUPS = MappingProxyType({'triple_j': 'abc', 'double_j': 'abc'})
_RATE_DIRECTORY = '/tmp'
# Initial bounded observations: 303 robots allowed this path; Forum returned
# unrecognized content. The first exact 24 Moons robots read allowed /events
# with the existing ten-second minimum; Bar Open's MIUC path was also allowed.
# West Space's observed robots 404 retains the shared reader's existing 4xx rule.
# Keep Forum's live listing closed while its pure adapter
# and synthetic caller remain testable. This is not model/republication consent.
LISTING_ACCESS = MappingProxyType({'venue303': True, 'forum': False, 'bar_open': True,
                                 'west_space': True,
                                 'moons24': True, 'affordable_art_fair': False,
                                 'improv_conspiracy': True, 'triple_j': False,
                                 'double_j': False, 'my_aeon': True})


class ListingIntervalPending(SourceError):
    """No request was sent: another action owns the current origin interval."""


class ListingPolicyUnavailable(SourceError):
    """No listing request: exact source access conditions are unresolved."""


def _reserve(source_id, *, policy_delay=None):
    # One host receipt covers robots and listing, including failed sends. A boot
    # identity prevents restart/wall-clock changes from opening an early slot.
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if str(UUID(boot)) != boot:
        raise SourceError('Host clock identity unavailable.')
    # ABC's two fixed indexes share one origin/receipt. Existing source receipt
    # names and history stay unchanged; stricter delay lives in this same row.
    group = _ORIGIN_GROUPS.get(source_id, source_id)
    path = Path(_RATE_DIRECTORY) / f'culture-cabinet-events-{os.geteuid()}-{group}.rate'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise SourceError('Unsafe origin interval receipt.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ListingIntervalPending from None
        raw = os.read(fd, 257)
        now = time.monotonic()
        if raw:
            state = strict_json(raw)
            if (len(raw) > 256 or type(state) is not dict or set(state) != {'boot', 'at', 'delay'}
                    or type(state['boot']) is not str
                    or str(UUID(state['boot'])) != state['boot']
                    or any(type(state[k]) not in (int, float) or not math.isfinite(state[k])
                           or state[k] < 0 for k in ('at', 'delay')) or state['delay'] < 10):
                raise SourceError('Invalid origin interval receipt.')
            if policy_delay is None and state['boot'] == boot and now - state['at'] < max(state['delay'], _DELAYS[source_id]):
                raise ListingIntervalPending
        if policy_delay is not None:
            # Publish stricter robots pacing to other processes too, preserving
            # the reservation time. Updating policy is not another source send.
            if not raw or not math.isfinite(policy_delay) or policy_delay < 10:
                raise SourceError('Missing reservation for source policy.')
            record = dict(state, delay=max(state['delay'], policy_delay))
        else:
            record = {'boot': boot, 'at': now, 'delay': max(_DELAYS[source_id],
                       state['delay'] if raw and state['boot'] == boot else 10)}
        data = json.dumps(record).encode()
        os.lseek(fd, 0, os.SEEK_SET)
        if os.write(fd, data) != len(data):
            raise SourceError('Origin reservation failed.')
        os.ftruncate(fd, len(data))
    finally:
        os.close(fd)


def _read(source_id, *, robots=False):
    if type(source_id) is not str or source_id not in TARGETS:
        raise SourceError('Unapproved listing source.')
    if not robots and not LISTING_ACCESS[source_id]:
        raise ListingPolicyUnavailable
    _reserve(source_id)
    command = _base(MAX_BYTES)
    command[command.index('Accept: application/json')] = 'Accept: text/plain' if robots else 'Accept: text/html'
    return _send(command + ['--user-agent', USER_AGENT, '--request', 'GET',
                            '--url', TARGETS[source_id][bool(robots)]], max_bytes=MAX_BYTES)


def venue303():
    return _read('venue303')


def forum():
    return _read('forum')


def bar_open():
    return _read('bar_open')


def west_space():
    return _read('west_space')


def moons24():
    return _read('moons24')


def affordable_art_fair():
    return _read('affordable_art_fair')


def improv_conspiracy():
    return _read('improv_conspiracy')


def triple_j():
    return _read('triple_j')


def double_j():
    return _read('double_j')


def my_aeon():
    return _read('my_aeon')


def _robots_rules(source_id, code, raw):
    """Pure interpretation also permits checking exact retained source bytes."""
    if code == 404:
        return {'status': code, 'allowed': True, 'delay': 10}
    if code != 200:
        return {'status': code, 'allowed': False, 'delay': 10}
    # RFC9309 sections 2.2/2.2.1: zero groups is a valid empty robots file and
    # has no applicable rules. Only exact empty success is added here; nonempty
    # unknown text/HTML, denial and transport failure keep existing behaviour.
    if raw == b'':
        return {'status': code, 'allowed': True, 'delay': 10}
    text = raw.decode('utf-8')
    if (not any(line.lower().startswith('user-agent:') for line in text.splitlines())
            or '<html' in text.lower() or '<!doctype' in text.lower()):
        raise SourceError('Unrecognized robots response.')
    rules = RobotFileParser()
    rules.parse(text.splitlines())
    delay = max(10, rules.crawl_delay(USER_AGENT) or 0)
    rate = rules.request_rate(USER_AGENT)
    if rate:
        delay = max(delay, rate.seconds / rate.requests)
    return {'status': code, 'allowed': rules.can_fetch(USER_AGENT, TARGETS[source_id][0]), 'delay': delay}


def observe_robots(source_id):
    """Explicit readiness observation only; listing actions never crawl robots.

    The engineering caller records this receipt and honors allowed/delay before
    its first live listing. Robots permission is not a content/model licence.
    """
    code, raw = _read(source_id, robots=True)
    receipt = _robots_rules(source_id, code, raw)
    if code == 200:
        _DELAYS[source_id] = receipt['delay']
        _reserve(source_id, policy_delay=receipt['delay'])
    return receipt
