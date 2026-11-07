"""Five allowlisted HTTPS GET targets, shared host pacing and refusal backoff."""
import fcntl
import json
import math
import os
import stat
import time
from urllib.parse import urlsplit

from shelf.discovery_sources import SourceError
from shelf.discovery_transport import _base, _send
from shelf.gallery_sources import APW_URL, GERTRUDE_URL, OFFER_URL, MAX_BYTES

_ROBOTS_CACHE = {}  # Two fixed hosts only; policy receipts expire after one hour.

ALLOWED_URLS = frozenset((GERTRUDE_URL, APW_URL, OFFER_URL,
                         'https://gertrude.org.au/robots.txt', APW_URL + 'robots.txt'))


def read(url, *, max_bytes=MAX_BYTES, deadline=None):
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
            raise SourceError('rate-limited') from None
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
            raise SourceError('rate-limited')
        cached = _ROBOTS_CACHE.get(url)
        if cached and time.monotonic() - cached[0] < 3600:
            if len(cached[2]) > max_bytes:
                raise SourceError('Gallery response budget exceeded.')
            return cached[1], cached[2]
        wait = max(0, 10 - (now - state['last']))
        if deadline is not None and time.monotonic() + wait + 6 > deadline:
            raise SourceError('rate-limited')
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
        command = _base(max_bytes)
        command[command.index('Accept: application/json')] = 'Accept: text/html,text/plain'
        code, body = _send(command + ['--user-agent', 'CultureCabinet/0.1 (private informational metadata reader)',
                                     '--request', 'GET', '--url', url], max_bytes=max_bytes)
        if code in (403, 429):
            state['blocked_until'] = time.time() + 3600
            save()
        if url.endswith('/robots.txt') and code in (200, 404):
            _ROBOTS_CACHE[url] = (time.monotonic(), code, body)
        return code, body
    finally:
        os.close(fd)
