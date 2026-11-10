"""Five allowlisted HTTPS GET targets, shared host pacing and refusal backoff."""
import fcntl
import json
import math
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from shelf.discovery_sources import SourceError
from shelf.discovery_transport import _base
from dataclasses import dataclass
from hashlib import sha256
import selectors
import subprocess
from urllib.robotparser import RobotFileParser
from shelf.gallery_sources import APW_URL, GERTRUDE_URL, OFFER_URL, MAX_BYTES

_ROBOTS_CACHE = {}  # Two fixed hosts only; policy receipts expire after one hour.

ALLOWED_URLS = frozenset((GERTRUDE_URL, APW_URL, OFFER_URL,
                         'https://gertrude.org.au/robots.txt', APW_URL + 'robots.txt'))


def read(url, *, max_bytes=MAX_BYTES, deadline=None, revalidate=None, include_headers=False, observation=None, request_seconds=5):
    """No redirect, retry, cookie, credential, proxy or arbitrary URL interface.

    A private host lock covers pacing and I/O. A refusal suppresses all reads
    from that host for an hour, including robots and the other allowed paths.
    """
    if (url not in ALLOWED_URLS or type(max_bytes) is not int
            or not 0 < max_bytes <= MAX_BYTES):
        raise SourceError('Unapproved gallery target.')
    host = urlsplit(url).hostname
    path = f'/tmp/culture-cabinet-gallery-{os.geteuid()}-{host}.rate'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise SourceError('Unsafe gallery interval state.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GalleryRateLimited('rate-limited') from None
        raw = os.read(fd, 201)
        state = {'last': 0, 'blocked_until': 0}
        if raw:
            if len(raw) > 200:
                raise SourceError('Invalid gallery interval state.')
            state = json.loads(raw)
            if (type(state) is not dict or set(state) != {'last', 'blocked_until'} or any(
                    type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in state.values())):
                raise SourceError('Invalid gallery interval state.')
        now = time.time()
        if now < state['last'] or now < state['blocked_until']:
            raise GalleryRateLimited('rate-limited')
        cached = _ROBOTS_CACHE.get(url)
        if cached and 0 <= time.monotonic() - cached[0] < 3600:
            if len(cached[2]) > max_bytes:
                raise SourceError('Gallery response budget exceeded.')
            if observation is not None:
                if len(cached) < 4:
                    raise SourceError('Original robots clock unavailable.')
                observation.update(actual_sends=0, actual_response_bytes=0,
                    observed_at=cached[3], body_sha256=sha256(cached[2]).hexdigest(),
                    host_not_before=datetime.fromtimestamp(state['last']+10, timezone.utc))
            return cached[1], cached[2]
        wait = max(0, 10 - (now - state['last']))
        minimum_io = 6 if revalidate is None else 0.1
        if deadline is not None and time.monotonic() + wait + minimum_io > deadline:
            raise GalleryRateLimited('rate-limited')
        if wait and revalidate is not None:
            raise GalleryRateLimited('Source interval not elapsed.')
        if wait:
            time.sleep(wait)  # At most ten seconds, never an HTTP retry.
        state['last'] = time.time()

        def save():
            receipt = json.dumps(state).encode('ascii')
            os.lseek(fd, 0, os.SEEK_SET)
            if os.write(fd, receipt) != len(receipt):
                raise SourceError('Gallery interval reservation failed.')
            os.ftruncate(fd, len(receipt))
        save()
        if observation is not None:
            observation['host_not_before'] = datetime.fromtimestamp(state['last']+10, timezone.utc)
        command = _base(max_bytes)
        command[command.index('Accept: application/json')] = 'Accept: text/html,text/plain'
        if include_headers:
            command += ['--include']
        result = _send(command + ['--user-agent', 'CultureCabinet/0.1 (private informational metadata reader)',
                                     '--request', 'GET', '--url', url], max_bytes=max_bytes,
                           deadline=deadline, revalidate=revalidate, include_headers=include_headers, observation=observation, request_seconds=request_seconds)
        code, body = result[:2]
        if code in (403, 429):
            state['blocked_until'] = time.time() + 3600
            save()
        if url.endswith('/robots.txt') and code in (200, 404):
            _ROBOTS_CACHE[url] = (time.monotonic(), code, body,
                observation['observed_at'] if observation is not None else datetime.now(timezone.utc))
        return result
    finally:
        os.close(fd)


def _send(command, *, max_bytes, deadline=None, revalidate=None, include_headers=False, observation=None, request_seconds=5):
    """Gallery's existing read path with an enclosing deadline/currentness gate.

    Native collection cannot reuse the legacy fixed six-second child window
    after pacing has spent part of its parent allowance. Bound both I/O and
    reaping inside the remaining deadline, without another network client.
    """
    end = min(time.monotonic() + 6, deadline) if deadline is not None else time.monotonic() + 6
    io_end = end - 0.1
    child = None
    output = bytearray()
    cap = max_bytes + (8192 + 8 if include_headers else 4)
    command = list(command)
    def current():
        if time.monotonic() >= io_end or revalidate is not None and revalidate() is not True:
            raise SourceError('Gallery authority or deadline unavailable.')
    try:
        current()
        command[command.index('--max-time') + 1] = str(min(5, request_seconds, io_end - time.monotonic()))
        if observation is not None:
            observation['actual_sends'] = 1
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, env={'LANG': 'C', 'LC_ALL': 'C'})
        with selectors.DefaultSelector() as ready:
            os.set_blocking(child.stdout.fileno(), False)
            ready.register(child.stdout, selectors.EVENT_READ)
            while ready.get_map():
                remaining = io_end - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise TimeoutError
                try:
                    block = os.read(child.stdout.fileno(), min(8192, cap + 1 - len(output)))
                except BlockingIOError:
                    continue
                if not block:
                    ready.unregister(child.stdout)
                else:
                    output.extend(block)
                    if observation is not None:
                        observation.update(actual_response_bytes=len(output), observed_at=datetime.now(timezone.utc),
                                           body_sha256=sha256(output).hexdigest())
                    if len(output) > cap:
                        raise SourceError('Gallery response budget exceeded.')
            child.wait(timeout=max(0.001, io_end - time.monotonic()))
        if child.returncode == 28:
            raise TimeoutError
        if child.returncode:
            raise SourceError('Gallery request failed.')
        if include_headers:
            from .discovery_transport import _moshtix_envelope
            result = _moshtix_envelope(bytes(output), response_bytes=max_bytes)
            if observation is not None:
                observation.update(actual_response_bytes=len(result[1]), observed_at=datetime.now(timezone.utc),
                                   body_sha256=sha256(result[1]).hexdigest())
            current()
            return result
        body, marker, code = output.rpartition(b'\n')
        if not marker or len(code) != 3 or not code.isdigit() or len(body) > max_bytes:
            raise SourceError('Invalid gallery response envelope.')
        current()
        if observation is not None:
            observation.update(actual_response_bytes=len(body), observed_at=datetime.now(timezone.utc),
                               body_sha256=sha256(body).hexdigest())
        return int(code), bytes(body)
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    except (OSError, subprocess.SubprocessError):
        raise SourceError('Gallery request failed.') from None
    finally:
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=max(0.001, end - time.monotonic()))
            except (OSError, subprocess.SubprocessError):
                raise SourceError('Gallery child cleanup failed.') from None
            finally:
                child.stdout.close()


@dataclass(frozen=True)
class GalleryOperation:
    source: str

    def __post_init__(self):
        if type(self.source) is not str or self.source not in ('apw', 'gertrude'):
            raise SourceError('Unregistered gallery source.')

    @property
    def url(self):
        return APW_URL if self.source == 'apw' else GERTRUDE_URL

    @property
    def robots_url(self):
        split = urlsplit(self.url)
        return split.scheme + '://' + split.netloc + '/robots.txt'

    @property
    def material(self):
        split = urlsplit(self.url)
        return {'source': self.source, 'method': 'GET', 'origin': split.scheme + '://' + split.netloc,
                'path': split.path, 'query': '', 'body_digest': sha256(b'').hexdigest(),
                'build': 'gallery-fixed-phases-v1',
                'phases': [{'method': 'GET', 'url': self.robots_url, 'conditional_fresh_cache': True},
                           {'method': 'GET', 'url': self.url, 'conditional_fresh_cache': False}]}

    @property
    def operation_digest(self):
        from .disclosure import canonical_bytes
        return sha256(canonical_bytes(self.material)).hexdigest()


def _robots(operation):
    cached = _ROBOTS_CACHE.get(operation.robots_url)
    if cached is None or not 0 <= time.monotonic() - cached[0] < 3600:
        raise SourceError('Gallery robots preparation required.')
    _, status, body = cached[:3]
    if status == 404:
        return
    if status != 200 or b'<html' in body.lower() or b'user-agent:' not in body.lower():
        raise SourceError('Gallery robots unavailable.')
    rules = RobotFileParser()
    rules.parse(body.decode('utf-8').splitlines())
    if not rules.can_fetch('CultureCabinet', operation.url):
        raise GalleryRobotsRefused('Gallery robots refused.')
    delay = rules.crawl_delay('CultureCabinet') or rules.crawl_delay('*') or 0
    if delay > 10:
        raise SourceError('Gallery robots delay unsupported.')


class GalleryRobotsRefused(SourceError):
    """Recognized robots rules refuse this exact fixed page."""


class GalleryRateLimited(SourceError):
    """Another context consumed the shared slot; collector never spins/retries."""


@dataclass(frozen=True)
class RobotsPreparation:
    decision: str
    observed_at: datetime
    valid_until: datetime
    body_sha256: str
    actual_sends: int
    actual_response_bytes: int
    host_not_before: datetime


def prepare_robots(operation, *, revalidate, response_bytes=262144,
                   request_seconds=5, parent_seconds=6, deadline=None):
    """One declared robots phase or authentic cached receipt, with safe accounting."""
    if (type(operation) is not GalleryOperation or not callable(revalidate)
            or type(response_bytes) is not int or not 0 < response_bytes <= 262144
            or type(request_seconds) not in (int, float) or not 0 < request_seconds <= 5
            or type(parent_seconds) not in (int, float) or not 0 < parent_seconds <= 6):
        raise SourceError('Gallery preparation unavailable.')
    observed = datetime.now(timezone.utc)
    info = dict(actual_sends=0, actual_response_bytes=0, observed_at=observed,
                body_sha256=sha256(b'').hexdigest(), host_not_before=observed)
    decision = 'unavailable'
    try:
        if revalidate() is not True:
            raise SourceError('Gallery authority unavailable.')
        status, body = read(operation.robots_url, max_bytes=response_bytes,
            deadline=min(time.monotonic()+min(parent_seconds, request_seconds+1),
                         deadline if deadline is not None else float('inf')),
            revalidate=revalidate, observation=info, request_seconds=request_seconds)
        if status in (401, 403):
            decision = 'refused'
        elif status == 429:
            decision = 'rate_limited'
        else:
            _robots(operation)
            if revalidate() is True:
                decision = 'ready'
    except GalleryRateLimited:
        decision = 'rate_limited'
    except GalleryRobotsRefused:
        decision = 'refused'
    except (SourceError, TimeoutError, UnicodeError):
        pass
    return RobotsPreparation(decision, info['observed_at'],
        info['observed_at']+timedelta(hours=1), info['body_sha256'],
        info['actual_sends'], info['actual_response_bytes'], info['host_not_before'])


def preparation_current(operation, body_sha256):
    """Read-only shared refusal/cache check; never fetches to repair an old receipt."""
    _robots(operation)
    cached = _ROBOTS_CACHE[operation.robots_url]
    if sha256(cached[2]).hexdigest() != body_sha256:
        return False
    host = urlsplit(operation.url).hostname
    path = f'/tmp/culture-cabinet-gallery-{os.geteuid()}-{host}.rate'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            return False
        raw = os.read(fd, 201)
        if len(raw) > 200:
            return False
        state = json.loads(raw)
        return (type(state) is dict and set(state) == {'last', 'blocked_until'}
                and all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in state.values())
                and time.time() >= max(state['last'], state['blocked_until']))
    finally:
        os.close(fd)


def gallery_authorized(operation, *, revalidate, response_bytes=262144,
                       request_seconds=5, parent_seconds=6, observation=None, deadline=None):
    """One fixed exhibition-page read; no offers, crawl or private query."""
    if (type(operation) is not GalleryOperation or not callable(revalidate)
            or type(response_bytes) is not int or not 0 < response_bytes <= 262144
            or type(request_seconds) not in (int, float) or not 0 < request_seconds <= 5
            or type(parent_seconds) not in (int, float) or not 0 < parent_seconds <= 6
            or revalidate() is not True):
        raise SourceError('Invalid native gallery operation.')
    _robots(operation)
    end = min(time.monotonic() + min(parent_seconds, request_seconds + 1),
              deadline if deadline is not None else float('inf'))
    def current():
        _robots(operation)
        return time.monotonic() < end and revalidate() is True
    result = read(operation.url, max_bytes=response_bytes, deadline=end, revalidate=current, include_headers=True, request_seconds=request_seconds, observation=observation)
    if not current():
        raise SourceError('Gallery authority changed.')
    return result
