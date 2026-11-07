"""Bounded anonymous public reads. No caller URL or credential interface."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import selectors
import subprocess
import time
from urllib.parse import urlencode

from shelf.provider_smoke import CURL, CA
from shelf.discovery_sources import MAX_BYTES, SourceError, radio_request, strict_json

# Verified Date scalar/filter contract; default one result, never pagination.
MOSHTIX_QUERY = 'query CultureCabinetBoundedContract($from:Date,$to:Date){viewer{getEvents(pageIndex:0,pageSize:1,sortBy:STARTDATE,sortByDirection:ASC,region:[VIC],eventStartDateFrom:$from,eventStartDateTo:$to,includePastEvents:false,publicOnly:true,searchableOnly:true){totalCount items{id name startDate venue{id name address{locality region}}}}}}'


def _base(max_bytes=MAX_BYTES):
    return [CURL, '-q', '--silent', '--proto', '=https', '--proto-redir', '=https',
            '--cacert', CA, '--proxy', '', '--noproxy', '*', '--no-netrc',
            '--max-redirs', '0', '--retry', '0', '--connect-timeout', '5',
            '--max-time', '5', '--max-filesize', str(max_bytes),
            '--header', 'Accept: application/json', '--header', 'Accept-Encoding: identity',
            '--write-out', '\n%{http_code}']


def radio_command(seed, mode):
    request = radio_request(seed, mode)
    return _base() + ['--request', 'GET', '--url', request.url + '?' + urlencode(request.params)]


def events_command(now, *, page_size=1):
    if now.tzinfo is None or now.utcoffset() is None:
        raise SourceError('Invalid public date window.')
    if type(page_size) is not int or not 1 <= page_size <= 9:
        raise SourceError('Invalid public page budget.')
    start = now.astimezone(timezone.utc).replace(microsecond=0)
    variables = {name: value.isoformat().replace('+00:00', 'Z') for name, value in
                 (('from', start), ('to', start + timedelta(weeks=8)))}
    return _base() + ['--request', 'POST', '--header', 'Content-Type: application/json',
                     '--data-binary', json.dumps({'query': MOSHTIX_QUERY.replace(
                         'pageSize:1,', f'pageSize:{page_size},'), 'variables': variables}),
                     '--url', 'https://api.moshtix.com/v1/graphql']


def _send(command, *, max_bytes=MAX_BYTES):
    """Bound stdout in parent memory too, including unknown-length responses."""
    child = None
    output = bytearray()
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, close_fds=True,
                                 env={'LANG': 'C', 'LC_ALL': 'C'})
        deadline = time.monotonic() + 6
        with selectors.DefaultSelector() as ready:
            os.set_blocking(child.stdout.fileno(), False)
            ready.register(child.stdout, selectors.EVENT_READ)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise TimeoutError
                block = os.read(child.stdout.fileno(), min(8192, max_bytes + 5 - len(output)))
                if not block:
                    ready.unregister(child.stdout)
                else:
                    output.extend(block)
                    if len(output) > max_bytes + 4:
                        raise SourceError('Response budget exceeded.')
            child.wait(timeout=max(0.001, deadline - time.monotonic()))
        if child.returncode == 28:
            raise TimeoutError
        if child.returncode:
            raise SourceError('Public transport failed.')
        body, separator, code = output.rpartition(b'\n')
        if not separator or len(code) != 3 or not code.isdigit():
            raise SourceError('Invalid HTTP envelope.')
        return int(code), bytes(body)
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    except TimeoutError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise SourceError('Public transport failed.') from None
    finally:
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
            finally:
                child.stdout.close()


def radio(seed, mode):
    return _send(radio_command(seed, mode))


def events(now, *, page_size=1):
    return _send(events_command(now, page_size=page_size))


def rrr():
    from shelf.rrr_events import LISTING_URL
    return _send(_rrr_command(LISTING_URL))


def _rrr_command(url):
    command = _base()
    command[command.index('Accept: application/json')] = 'Accept: text/html'
    return command + ['--user-agent', 'CultureCabinet/0.1 (public event listing reader)',
                      '--request', 'GET', '--url', url]


def rrr_detail(url):
    from shelf.rrr_details import detail_url
    return _send(_rrr_command(detail_url(url)))


def _beat_command(url):
    from shelf.beat_events import MAX_BYTES as limit
    command = _base(limit)
    command[command.index('Accept: application/json')] = 'Accept: text/html'
    return command + ['--user-agent', 'CultureCabinet/0.1 (public event listing reader)',
                      '--request', 'GET', '--url', url]


def beat():
    from shelf.beat_events import LISTING_URL, MAX_BYTES as limit
    return _send(_beat_command(LISTING_URL), max_bytes=limit)


def beat_detail(url):
    from shelf.beat_events import event_url, MAX_BYTES as limit
    return _send(_beat_command(event_url(url)), max_bytes=limit)


class PBSIntervalPending(SourceError):
    """A shared PBS request is still inside the publisher's ten-second interval."""


_PBS_RATE_FILE = f'/tmp/culture-cabinet-pbs-{os.geteuid()}.rate'


def _reserve_pbs_slot():
    """Reserve before I/O across threads/processes of this host's app owner.

    The tiny owned 0600 file is only an ephemeral rate-limit receipt. A boot ID
    makes the shared monotonic clock safe across restarts without wall-clock
    jumps opening a second request slot. Invalid state fails closed.
    """
    import fcntl
    import math
    from pathlib import Path
    import stat

    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    from uuid import UUID
    if str(UUID(boot)) != boot:
        raise SourceError('PBS host clock identity unavailable.')
    fd = os.open(_PBS_RATE_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise SourceError('PBS shared interval file is not private.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PBSIntervalPending('PBS read already reserved.') from None
        raw = os.read(fd, 201)
        now = time.monotonic()
        if not math.isfinite(now) or now < 0 or len(raw) > 200:
            raise SourceError('Invalid PBS interval state.')
        if raw:
            previous = strict_json(raw)
            if (type(previous) is not dict or set(previous) != {'boot', 'at'}
                    or type(previous['boot']) is not str or str(UUID(previous['boot'])) != previous['boot']
                    or type(previous['at']) not in (int, float) or not math.isfinite(previous['at'])
                    or previous['at'] < 0):
                raise SourceError('Invalid PBS interval state.')
            if previous['boot'] == boot and now - previous['at'] < 10:
                raise PBSIntervalPending('PBS shared request interval has not elapsed.')
        reservation = json.dumps({'boot': boot, 'at': now}).encode('ascii')
        os.lseek(fd, 0, os.SEEK_SET)
        if os.write(fd, reservation) != len(reservation):
            raise SourceError('PBS interval reservation failed.')
        os.ftruncate(fd, len(reservation))
    finally:
        os.close(fd)


def pbs():
    from shelf.pbs_events import LISTING_URL
    _reserve_pbs_slot()
    command = _base()
    command[command.index('Accept: application/json')] = 'Accept: text/html'
    return _send(command + ['--user-agent', 'CultureCabinet/0.1 (public event listing reader)',
                           '--request', 'GET', '--url', LISTING_URL])


def tribe():
    from shelf.tribe_events import RSS_URL
    command = _base()
    command[command.index('Accept: application/json')] = 'Accept: application/rss+xml'
    return _send(command + ['--user-agent', 'CultureCabinet/0.1 (public event metadata reader)',
                           '--request', 'GET', '--url', RSS_URL])


def soundsystems():
    from shelf.soundsystems_events import LISTING_URL
    command = _base()
    command[command.index('Accept: application/json')] = 'Accept: text/html'
    return _send(command + ['--user-agent', 'CultureCabinet/0.1 (public event listing reader)',
                           '--request', 'GET', '--url', LISTING_URL])


# The Enhance path is additive. The older listing callers above keep their
# original contracts, while this path binds the actual wire bytes to authority.
@dataclass(frozen=True, repr=False)
class MoshtixOperation:
    start: datetime
    page_size: int
    application_identity: str

    def __post_init__(self):
        if (type(self.start) is not datetime or self.start.tzinfo is None
                or self.start.utcoffset() != timedelta(0) or self.start.microsecond
                or type(self.page_size) is not int or not 1 <= self.page_size <= 9
                or type(self.application_identity) is not str
                or not self.application_identity.startswith('CultureCabinet/')
                or not 20 <= len(self.application_identity) <= 200
                or any(not 32 <= ord(c) < 127 for c in self.application_identity)):
            raise SourceError('Invalid fixed source operation.')

    @property
    def body(self):
        from shelf.disclosure import canonical_bytes
        variables = {name: value.isoformat().replace('+00:00', 'Z') for name, value in
            (('from', self.start), ('to', self.start + timedelta(weeks=8)))}
        return canonical_bytes({'query': MOSHTIX_QUERY.replace(
            'pageSize:1,', f'pageSize:{self.page_size},'), 'variables': variables})

    @property
    def material(self):
        # No profile, plan, query term, account identifier or credential belongs
        # in this public operation. Identity is supplied by the trusted issuer.
        return {'method': 'POST', 'origin': 'https://api.moshtix.com',
            'path': '/v1/graphql', 'query': '',
            'headers': [['accept', 'application/json'], ['accept-encoding', 'identity'],
                        ['content-type', 'application/json'],
                        ['user-agent', self.application_identity]],
            'body_digest': sha256(self.body).hexdigest()}

    @property
    def operation_digest(self):
        from shelf.disclosure import canonical_bytes
        return sha256(canonical_bytes(self.material)).hexdigest()


def _moshtix_envelope(raw, *, response_bytes):
    """Retain only small useful headers; cookies and raw headers never escape."""
    header, separator, payload = raw.partition(b'\r\n\r\n')
    if not separator or len(header) > 8192:
        raise SourceError('Invalid source header envelope.')
    body, marker, code = payload.rpartition(b'\n')
    lines = header.split(b'\r\n')
    if (not marker or len(code) != 3 or not code.isdigit() or len(body) > response_bytes
            or not lines or len(lines) > 64 or any(len(line) > 1100 for line in lines)):
        raise SourceError('Invalid bounded source response.')
    status = lines[0].split(b' ')
    if (len(status) < 2 or status[0] not in (b'HTTP/1.0', b'HTTP/1.1', b'HTTP/2', b'HTTP/3')
            or status[1] != code):
        # This also rejects intermediate/proxy/redirect header blocks. Curl is
        # never allowed to follow a redirect or negotiate another destination.
        raise SourceError('Unexpected source response envelope.')
    retained = {}
    allowed = {'retry-after', 'cache-control', 'content-type', 'date', 'etag', 'age', 'expires'}
    for line in lines[1:]:
        name, colon, value = line.partition(b':')
        if not colon or not name or any(c < 33 or c > 126 for c in name):
            raise SourceError('Invalid source header.')
        try:
            name = name.decode('ascii').lower()
            value = value.strip(b' \t').decode('ascii')
        except UnicodeError:
            raise SourceError('Invalid source header encoding.') from None
        if len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise SourceError('Invalid source header value.')
        if name == 'content-encoding' and value.lower() not in ('', 'identity'):
            raise SourceError('Compressed source response denied.')
        if name in allowed:
            if name in retained:
                raise SourceError('Ambiguous source header.')
            retained[name] = value
    return int(code), bytes(body), tuple(sorted(retained.items()))


def events_authorized(operation, *, revalidate, response_bytes=131072,
                      request_seconds=5, parent_seconds=6):
    """One fixed request; the backend supplies current issuer/limiter checks.

    The callback is a trusted server dependency, never deserialized authority.
    It runs immediately before spawning curl and again before returning bytes.
    """
    if (type(operation) is not MoshtixOperation or not callable(revalidate)
            or type(response_bytes) is not int or not 0 < response_bytes <= 131072
            or type(request_seconds) not in (int, float) or not 0 < request_seconds <= 5
            or type(parent_seconds) not in (int, float) or not 0 < parent_seconds <= 6):
        raise SourceError('Invalid authorized source bounds.')
    deadline, child, output = time.monotonic() + parent_seconds, None, bytearray()
    command = _base(response_bytes)
    command[command.index('--max-time') + 1] = str(min(request_seconds, parent_seconds))
    command += ['--include', '--request', 'POST', '--header', 'Content-Type: application/json',
                '--user-agent', operation.application_identity,
                '--data-binary', operation.body.decode('ascii'),
                '--url', 'https://api.moshtix.com/v1/graphql']
    try:
        if revalidate() is not True or time.monotonic() >= deadline:
            raise SourceError('Source authority unavailable.')
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, env={'LANG': 'C', 'LC_ALL': 'C'})
        cap = response_bytes + 8192 + 8
        with selectors.DefaultSelector() as ready:
            os.set_blocking(child.stdout.fileno(), False)
            ready.register(child.stdout, selectors.EVENT_READ)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise TimeoutError
                block = os.read(child.stdout.fileno(), min(8192, cap + 1 - len(output)))
                if not block:
                    ready.unregister(child.stdout)
                else:
                    output.extend(block)
                    if len(output) > cap:
                        raise SourceError('Source response budget exceeded.')
            child.wait(timeout=max(0.001, deadline - time.monotonic()))
        if child.returncode == 28:
            raise TimeoutError
        if child.returncode:
            raise SourceError('Authorized source transport failed.')
        result = _moshtix_envelope(bytes(output), response_bytes=response_bytes)
        if revalidate() is not True or time.monotonic() >= deadline:
            raise SourceError('Source authority changed during request.')
        return result
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    except (OSError, subprocess.SubprocessError):
        raise SourceError('Authorized source transport failed.') from None
    finally:
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=0.1)
            except (OSError, subprocess.SubprocessError):
                pass
            finally:
                child.stdout.close()


class MoshtixRateLimited(SourceError):
    """Another process owns the source slot, or its cooldown has not elapsed."""


_MOSHTIX_RATE_FILE = f'/tmp/culture-cabinet-moshtix-{os.geteuid()}.rate'


def moshtix_cooldown(status, headers, *, now):
    """Application floors are not publisher rate grants; Retry-After can extend."""
    from email.utils import parsedate_to_datetime
    delay = 60 if status in (403, 429, 503) or status is None else 1
    raw = dict(headers).get('retry-after', '')
    if status in (403, 429, 503) and raw:
        if raw.isascii() and raw.isdigit() and len(raw) <= 10:
            delay = max(delay, int(raw))
        else:
            try:
                stamp = parsedate_to_datetime(raw)
                if stamp.tzinfo is not None:
                    delay = max(delay, (stamp - now).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return delay


class MoshtixHostLimiter:
    """Cross-process pacing for this host's application OS owner; no auto-use.

    A nonblocking owned-file lock is held through I/O. Reserving the full parent
    window before send also preserves a quiet period if the process crashes.
    Only boot identity and monotonic cooldown are stored, never response data.
    """
    def reserve(self):
        import fcntl
        import math
        from pathlib import Path
        import stat
        from uuid import UUID
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if str(UUID(boot)) != boot:
            raise SourceError('Host clock identity unavailable.')
        fd = os.open(_MOSHTIX_RATE_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise SourceError('Source limiter is not private.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MoshtixRateLimited('Source slot already reserved.') from None
            raw, now = os.read(fd, 513), time.monotonic()
            if len(raw) > 512 or not math.isfinite(now) or now < 0:
                raise SourceError('Invalid source limiter state.')
            if raw:
                previous = strict_json(raw)
                if (type(previous) is not dict or set(previous) != {'boot', 'not_before'}
                        or type(previous['boot']) is not str or str(UUID(previous['boot'])) != previous['boot']
                        or type(previous['not_before']) not in (int, float)
                        or not math.isfinite(previous['not_before']) or previous['not_before'] < 0):
                    raise SourceError('Invalid source limiter state.')
                if previous['boot'] == boot and now < previous['not_before']:
                    raise MoshtixRateLimited('Source cooldown has not elapsed.')
            slot = _MoshtixReservation(fd, boot, now + 6)
            slot.write()
            return slot
        except BaseException:
            os.close(fd)
            raise


class _MoshtixReservation:
    def __init__(self, fd, boot, not_before):
        self.fd, self.boot, self.not_before = fd, boot, not_before

    def write(self):
        raw = json.dumps({'boot': self.boot, 'not_before': self.not_before}).encode('ascii')
        os.lseek(self.fd, 0, os.SEEK_SET)
        if os.write(self.fd, raw) != len(raw):
            raise SourceError('Source reservation failed.')
        os.ftruncate(self.fd, len(raw))
        os.fsync(self.fd)

    def current(self):
        return self.fd is not None

    def finish(self, status, headers, *, now):
        try:
            self.not_before = max(self.not_before,
                time.monotonic() + moshtix_cooldown(status, headers, now=now))
            self.write()
        finally:
            os.close(self.fd)
            self.fd = None
