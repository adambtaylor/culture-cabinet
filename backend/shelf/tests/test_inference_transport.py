"""Synthetic DNS/TLS/HTTP spies. No live resolver or socket can be created."""
import hashlib
import io
import socket
import ssl
import threading
import unittest
from unittest.mock import patch

from shelf.inference_transport import CheckedTransport, HOST, PATH, ProbePermit, TransportFault, _resolve

KEY = b'SYNTHETIC-TRANSPORT-CANARY'
BODY = b'{"technical":"neutral"}'


class FakeRaw:
    def __init__(self, order):
        self.order, self.closed = order, False

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, address):
        self.order.append(('connect', address))

    def close(self):
        self.closed = True


class FakeTLS(FakeRaw):
    def __init__(self, order, response):
        super().__init__(order)
        self.response, self.written = io.BytesIO(response), b''
        self.peer = '8.8.8.8'
        self.read_failure = None
        self.slow_clock = None

    def getpeername(self):
        self.order.append(('peer', self.peer))
        return self.peer, 443

    def sendall(self, data):
        self.order.append(('send', len(data)))
        self.written += data

    def recv_into(self, buffer):
        if self.read_failure:
            raise self.read_failure
        if self.slow_clock is not None:
            self.slow_clock[0] += 10
            value = self.response.read(1)
        else:
            value = self.response.read(len(buffer))
        buffer[:len(value)] = value
        return len(value)


class FakeContext:
    check_hostname = True
    verify_mode = ssl.CERT_REQUIRED

    def __init__(self, order, tls):
        self.order, self.tls = order, tls
        self.failure = None

    def wrap_socket(self, raw, server_hostname):
        self.order.append(('tls', server_hostname))
        if self.failure:
            raise self.failure
        return self.tls


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.order = []
        self.body = BODY
        self.raw = FakeRaw(self.order)
        self.set_response()
        self.context = FakeContext(self.order, self.tls)
        self.addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]
        self.patches = [patch('shelf.inference_transport.socket.getaddrinfo', return_value=self.addresses),
                        patch('shelf.inference_transport.socket.socket', return_value=self.raw),
                        patch('shelf.inference_transport.ssl.create_default_context', return_value=self.context)]
        self.dns, self.create, self.ssl = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)

    def set_response(self, status=200, body=b'{}', extra=''):
        head = f'HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n{extra}\r\n'
        self.tls = FakeTLS(self.order, head.encode() + body)
        if hasattr(self, 'context'):
            self.context.tls = self.tls

    def permit(self):
        def credential():
            self.order.append(('credential', HOST))
            return KEY
        return ProbePermit(hashlib.sha256(self.body).hexdigest(), credential)

    def send(self, permit=None):
        return CheckedTransport().send(self.body, permit or self.permit())

    def fault(self, outcome, *, unknown=False):
        with self.assertRaises(TransportFault) as caught:
            self.send()
        self.assertEqual(caught.exception.outcome, outcome)
        self.assertEqual(caught.exception.unknown, unknown)
        return caught.exception

    def test_only_checked_origin_peer_then_authentication_then_one_request(self):
        self.assertEqual(self.send(), b'{}')
        self.dns.assert_called_once_with(HOST, 443, type=socket.SOCK_STREAM)
        self.assertEqual(self.order[:4], [('connect', ('8.8.8.8', 443)), ('tls', HOST), ('peer', '8.8.8.8'), ('credential', HOST)])
        self.assertEqual(self.tls.written.count(b'POST /v1/responses '), 1)
        self.assertEqual(self.tls.written.count(KEY), 1)
        self.assertIn(b'Authorization: Bearer ' + KEY, self.tls.written)
        self.assertNotIn(KEY, BODY)
        self.assertTrue(self.tls.closed)

    def test_private_loopback_metadata_mapped_and_multicast_dns_get_no_secret(self):
        for address in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '::ffff:8.8.8.8', '224.0.0.1']:
            self.dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, 443))]
            self.fault('unsupported')
        self.create.assert_not_called()
        self.assertFalse(any(item[0] == 'credential' for item in self.order))

    def test_mixed_public_and_private_dns_is_rejected_without_address_fallback(self):
        self.dns.return_value = self.addresses + [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443))]
        self.fault('unsupported')
        self.create.assert_not_called()

    def test_peer_rebinding_fails_before_credential_attachment(self):
        self.tls.peer = '1.1.1.1'
        self.fault('unsupported')
        self.assertFalse(any(item[0] == 'credential' for item in self.order))
        self.assertTrue(self.tls.closed)

    def test_tls_hostname_verification_cannot_be_disabled(self):
        self.context.check_hostname = False
        self.fault('unsupported')
        self.assertTrue(self.raw.closed)
        self.assertFalse(any(item[0] == 'credential' for item in self.order))

    def test_tls_certificate_failure_does_not_get_a_secret_or_retry(self):
        self.context.failure = ssl.SSLError('synthetic-certificate-failure')
        self.fault('unreachable')
        self.create.assert_called_once()
        self.assertFalse(any(item[0] == 'credential' for item in self.order))

    def test_redirect_is_not_followed_and_credentials_stay_on_selected_request(self):
        self.set_response(302, extra='Location: https://foreign.invalid/steal\r\n')
        self.fault('incompatible')
        self.create.assert_called_once()
        self.assertNotIn(b'foreign.invalid', self.tls.written)
        self.assertEqual(self.tls.written.count(KEY), 1)

    def test_env_proxies_are_not_consumed(self):
        with patch.dict('os.environ', {'HTTPS_PROXY': 'http://foreign.invalid:1234', 'ALL_PROXY': 'http://foreign.invalid'}):
            self.assertEqual(self.send(), b'{}')
        self.assertEqual(self.order[0], ('connect', ('8.8.8.8', 443)))

    def test_denied_throttled_and_unavailable_are_known_without_retry(self):
        for status, outcome in [(401, 'denied'), (429, 'exhausted'), (503, 'unreachable')]:
            self.set_response(status, extra='Retry-After: 5\r\n')
            fault = self.fault(outcome)
            self.assertEqual(fault.retry_after, 5)
            self.assertEqual(self.tls.written.count(b'POST '), 1)

    def test_timeout_after_request_is_unknown_and_never_retried(self):
        self.tls.read_failure = TimeoutError()
        self.fault('unknown', unknown=True)
        self.assertEqual(self.tls.written.count(b'POST '), 1)
        self.assertTrue(self.tls.closed)

    def test_whole_deadline_is_enforced_across_slow_header_reads(self):
        clock = [0.0]
        self.tls.slow_clock = clock
        with patch('shelf.inference_transport.time.monotonic', side_effect=lambda: clock[0]):
            self.fault('unknown', unknown=True)
        self.assertLessEqual(clock[0], 90)
        self.assertTrue(self.tls.closed)

    def test_oversized_response_is_not_truncated_into_success(self):
        self.set_response(body=b'x' * 131073)
        self.fault('incompatible')
        self.assertTrue(self.tls.closed)

    def test_one_shot_permit_rejects_reuse_and_changed_body(self):
        permit = self.permit()
        self.assertEqual(self.send(permit), b'{}')
        with self.assertRaises(TransportFault):
            permit.authorize(self.body)
        with self.assertRaises(TransportFault):
            self.permit().authorize(self.body + b' ')
        self.assertEqual(sum(item[0] == 'credential' for item in self.order), 1)

    def test_invalid_header_credential_is_never_sent(self):
        for secret in [b'bad\r\nInjected: yes', b'Bearer two words', b'', b'\xff']:
            permit = ProbePermit(hashlib.sha256(BODY).hexdigest(), lambda: secret)
            with self.assertRaises(TransportFault) as caught:
                self.send(permit)
            self.assertEqual(caught.exception.outcome, 'denied')
        self.assertEqual(self.tls.written, b'')

    def test_abandoned_resolver_cannot_send_or_accumulate_more_resolvers(self):
        release = threading.Event()
        def blocked(*args, **kwargs):
            release.wait(3)
            return self.addresses
        with patch('shelf.inference_transport.socket.getaddrinfo', side_effect=blocked), patch(
                'shelf.inference_transport._remaining', return_value=0.001):
            try:
                with self.assertRaises(TransportFault):
                    _resolve(999999)
                with self.assertRaises(TransportFault):
                    _resolve(999999)
                self.create.assert_not_called()
            finally:
                release.set()
                for thread in threading.enumerate():
                    if thread.name == 'cabinet-probe-resolver':
                        thread.join(timeout=1)
                        self.assertFalse(thread.is_alive())
