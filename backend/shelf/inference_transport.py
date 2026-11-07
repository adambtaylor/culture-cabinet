"""One fixed HTTPS origin, one checked peer and one send per neutral probe.

No socket/resolver work occurs on import. Resolver abandonment cannot send:
its daemon receives only the fixed hostname, and a shared lock prevents piling
up unresolved lookups. All request I/O shares one monotonic deadline.
"""
import hashlib
import http.client
import io
import ipaddress
import queue
import socket
import ssl
import threading
import time

from django.views.decorators.debug import sensitive_variables

HOST = 'api.openai.com'
ORIGIN = 'https://' + HOST
PATH = '/v1/responses'
POLICY_REVISION = 1
DEADLINE_SECONDS = 90
MAX_RESPONSE = 131072
_DNS_LOCK = threading.Lock()


class TransportFault(Exception):
    def __init__(self, outcome, *, unknown=False, retry_after=None):
        self.outcome, self.unknown, self.retry_after = outcome, unknown, retry_after
        super().__init__(outcome)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return min(8, remaining)


def _public_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (address.is_global and not address.is_multicast and not address.is_reserved
            and not address.is_unspecified and not address.is_loopback
            and getattr(address, 'ipv4_mapped', None) is None)


def _resolve(deadline):
    if not _DNS_LOCK.acquire(blocking=False):
        raise TransportFault('unreachable')
    result = queue.Queue(maxsize=1)
    def lookup():
        try:
            resolved = (True, socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM))
        except Exception:
            resolved = (False, None)
        finally:
            _DNS_LOCK.release()
        result.put(resolved)
    worker = threading.Thread(target=lookup, daemon=True, name='cabinet-probe-resolver')
    try:
        worker.start()
    except RuntimeError:
        _DNS_LOCK.release()
        raise TransportFault('unreachable') from None
    try:
        ok, addresses = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise TransportFault('unreachable') from None
    if not ok or not addresses or len(addresses) > 64:
        raise TransportFault('unreachable')
    # Reject the entire mixed answer, not just the chosen entry. Selecting one
    # checked address with no fallback preserves destination and one-send policy.
    for family, kind, protocol, canonical, address in addresses:
        if (family not in {socket.AF_INET, socket.AF_INET6} or kind != socket.SOCK_STREAM
                or protocol not in {0, socket.IPPROTO_TCP} or address[1] != 443
                or not _public_address(address[0])):
            raise TransportFault('unsupported')
    return addresses[0][0], addresses[0][4]


class ProbePermit:
    """Backend-only one-shot binding; never reconstructed from request JSON."""
    def __init__(self, body_digest, credential):
        self.body_digest = body_digest
        self._credential = credential
        self._used = False
        self._lock = threading.Lock()

    @sensitive_variables()
    def authorize(self, body):
        with self._lock:
            if self._used or hashlib.sha256(body).hexdigest() != self.body_digest:
                raise TransportFault('stale')
            # Consume before resolving authority: this permit can never authorize
            # a second request, even if setup or the first write later fails.
            self._used = True
            secret = self._credential()
            if type(secret) is not bytes or not secret or any(c < 33 or c > 126 for c in secret):
                raise TransportFault('denied')
            return secret.decode('ascii')


class _Reader(io.RawIOBase):
    """Reapply the whole-operation deadline before every underlying socket read.

    http.client buffers header/body parsing. A fixed per-read timeout alone lets
    a slow stream keep extending the operation, so its buffered reader uses this
    raw interface to recalculate the remaining time on each receive.
    """
    def __init__(self, wire):
        self.wire = wire

    def readable(self):
        return True

    def readinto(self, buffer):
        self.wire.limit()
        return self.wire.socket.recv_into(buffer)


class _Wire:
    """Adapt the checked TLS socket for bounded HTTP reads and writes."""
    def __init__(self, connection, deadline):
        self.socket, self.deadline = connection, deadline

    def limit(self):
        self.socket.settimeout(_remaining(self.deadline))

    @sensitive_variables()
    def sendall(self, body):
        self.limit()
        self.socket.sendall(body)

    def makefile(self, mode):
        if mode != 'rb':
            raise TransportFault('unsupported')
        return io.BufferedReader(_Reader(self))

    def close(self):
        self.socket.close()


class CheckedTransport:
    @sensitive_variables()
    def send(self, body, permit):
        if type(body) is not bytes or not 0 < len(body) <= 16384 or type(permit) is not ProbePermit:
            raise TransportFault('incompatible')
        deadline = time.monotonic() + DEADLINE_SECONDS
        raw = tls = connection = response = None
        sent = False
        try:
            family, address = _resolve(deadline)
            raw = socket.socket(family, socket.SOCK_STREAM)
            raw.settimeout(_remaining(deadline))
            raw.connect(address)
            context = ssl.create_default_context()
            if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
                raise TransportFault('unsupported')
            raw.settimeout(_remaining(deadline))
            tls = context.wrap_socket(raw, server_hostname=HOST)
            # Successful wrapping transfers raw socket ownership to TLS.
            raw = None
            if ipaddress.ip_address(tls.getpeername()[0]) != ipaddress.ip_address(address[0]):
                raise TransportFault('unsupported')
            connection = http.client.HTTPConnection(HOST, 443, timeout=_remaining(deadline))
            connection.set_debuglevel(0)
            connection.sock = _Wire(tls, deadline)
            # Resolve current authority/credential only after DNS, TLS and peer checks.
            secret = permit.authorize(body)
            # This means possible transmission: request() can fail after writing
            # some bytes. Such a failure cannot safely be retried as a fresh call.
            sent = True
            connection.request('POST', PATH, body=body, headers={
                'Host': HOST, 'Authorization': 'Bearer ' + secret,
                'Content-Type': 'application/json', 'Connection': 'close'})
            response = connection.getresponse()
            status = response.status
            if status != 200:
                retry = response.getheader('Retry-After', '')
                retry_after = min(int(retry), 3600) if retry.isascii() and retry.isdigit() and len(retry) <= 8 else None
                outcome = {401: 'denied', 403: 'denied', 404: 'unsupported', 429: 'exhausted',
                           503: 'unreachable'}.get(status, 'incompatible')
                raise TransportFault(outcome, retry_after=retry_after)
            if response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                raise TransportFault('incompatible')
            value = response.read(MAX_RESPONSE + 1)
            if len(value) > MAX_RESPONSE:
                raise TransportFault('incompatible')
            _remaining(deadline)
            return value
        except TransportFault:
            raise
        except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError):
            raise TransportFault('unknown' if sent else 'unreachable', unknown=sent) from None
        finally:
            # HTTP normally owns the wrapped socket, but setup may fail before
            # that transfer. Explicit TLS/raw fallback cleanup covers both paths.
            try:
                if response is not None:
                    response.close()
                if connection is not None:
                    connection.close()
            finally:
                if tls is not None:
                    tls.close()
                if raw is not None:
                    raw.close()
