"""Real loopback HTTP regressions; no catalogue runtime or production credentials."""
from contextlib import contextmanager
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import re
import socket
import threading
import time
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.handlers.wsgi import WSGIHandler
from django.core.servers.basehttp import WSGIRequestHandler, WSGIServer
from django.test import SimpleTestCase, TransactionTestCase

from shelf.catalogue_server import CatalogueWSGIServer
from shelf.management.commands.catalogue_runserver import Command


class AcceptedClientTimeoutTests(SimpleTestCase):
    def test_timeout_is_on_accepted_client_only_and_command_uses_server(self):
        default_timeout = socket.getdefaulttimeout()
        with CatalogueWSGIServer(('127.0.0.1', 0), WSGIRequestHandler) as server:
            listener_timeout = server.socket.gettimeout()
            with socket.create_connection(server.server_address, timeout=3) as peer:
                client, address = server.get_request()
                try:
                    self.assertEqual(client.gettimeout(), 2.0)
                    self.assertEqual(address, peer.getsockname())
                    self.assertEqual(server.socket.gettimeout(), listener_timeout)
                    self.assertEqual(socket.getdefaulttimeout(), default_timeout)
                finally:
                    client.close()
        self.assertIs(Command.server_cls, CatalogueWSGIServer)

    def test_failed_timeout_closes_accepted_socket_and_preserves_exception(self):
        server = CatalogueWSGIServer.__new__(CatalogueWSGIServer)
        for error in (OSError('socket setup failed'), ValueError('invalid socket')):
            with self.subTest(error=type(error).__name__):
                client = Mock()
                client.settimeout.side_effect = error
                with patch.object(WSGIServer, 'get_request', return_value=(client, ('127.0.0.1', 1))):
                    with self.assertRaises(type(error)) as caught:
                        server.get_request()
                self.assertIs(caught.exception, error)
                client.close.assert_called_once_with()


class IdleClientRegressionTests(TransactionTestCase):
    password = 'Fictional-socket-regression-73!'

    @contextmanager
    def running_server(self, server_class):
        accepted = threading.Event()

        class ObservedServer(server_class):
            def get_request(self):
                request = super().get_request()
                accepted.set()
                return request

        class QuietHandler(WSGIRequestHandler):
            def log_message(self, format, *args):
                pass

        server = ObservedServer(('127.0.0.1', 0), QuietHandler)
        server.set_app(WSGIHandler())
        server.timeout = 0.05
        clients = []
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                server.handle_request()

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            yield server, accepted, clients
        finally:
            # Releasing all client input also frees the intentionally stalled baseline.
            for client in clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client.close()
            stop.set()
            thread.join(timeout=5)
            server.server_close()
            self.assertFalse(thread.is_alive(), 'fixture HTTP server did not stop')

    def connect(self, server, clients):
        client = socket.create_connection(server.server_address, timeout=4)
        clients.append(client)
        return client

    def read_response(self, client):
        response = bytearray()
        while True:
            block = client.recv(65536)
            if not block:
                return bytes(response)
            response.extend(block)

    def test_baseline_idle_client_blocks_queued_real_login(self):
        with self.running_server(WSGIServer) as (server, accepted, clients):
            idle = self.connect(server, clients)
            self.assertTrue(accepted.wait(2), 'idle connection was not accepted')
            queued = self.connect(server, clients)
            queued.sendall(b'GET /login/ HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n')
            queued.settimeout(0.2)
            with self.assertRaises(socket.timeout):
                queued.recv(1)
            idle.shutdown(socket.SHUT_RDWR)
            queued.settimeout(4)
            response = self.read_response(queued)
            self.assertTrue(response.startswith(b'HTTP/1.1 200'), response[:80])
            self.assertIn(b'csrfmiddlewaretoken', response)

    def test_idle_partial_line_and_partial_post_release_queued_login(self):
        stalled_requests = (
            b'',
            b'GET /login/ HTTP/1.',
            b'POST /login/ HTTP/1.0\r\nHost: 127.0.0.1\r\n'
            b'Content-Type: application/x-www-form-urlencoded\r\n'
            b'Content-Length: 100\r\n\r\nusername=',
        )
        for incomplete in stalled_requests:
            with self.subTest(incomplete=incomplete):
                with self.running_server(CatalogueWSGIServer) as (server, accepted, clients):
                    stalled = self.connect(server, clients)
                    if incomplete:
                        stalled.sendall(incomplete)
                    self.assertTrue(accepted.wait(2), 'stalled connection was not accepted')
                    queued = self.connect(server, clients)
                    started = time.monotonic()
                    queued.sendall(b'GET /login/ HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n')
                    response = self.read_response(queued)
                    self.assertLess(time.monotonic() - started, 4)
                    self.assertTrue(response.startswith(b'HTTP/1.1 200'), response[:80])
                    self.assertIn(b'csrfmiddlewaretoken', response)

    def test_completed_http11_request_does_not_hold_queued_login(self):
        with self.running_server(CatalogueWSGIServer) as (server, accepted, clients):
            connection = HTTPConnection(*server.server_address, timeout=4)
            try:
                connection.request('GET', '/login/')
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                # Django may close single-threaded connections explicitly. If it
                # keeps HTTP/1.1 alive, leave it idle while the next client waits.
                if connection.sock is not None:
                    clients.append(connection.sock)
                queued = self.connect(server, clients)
                started = time.monotonic()
                queued.sendall(b'GET /login/ HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n')
                response = self.read_response(queued)
                self.assertLess(time.monotonic() - started, 4)
                self.assertTrue(response.startswith(b'HTTP/1.1 200'), response[:80])
                self.assertIn(b'csrfmiddlewaretoken', response)
            finally:
                connection.close()

    def test_normal_get_login_form_and_authenticated_session_cookie(self):
        user = get_user_model().objects.create_user(
            'fictional-socket-login', password=self.password)
        for server_class in (WSGIServer, CatalogueWSGIServer):
            with self.subTest(server=server_class.__name__):
                with self.running_server(server_class) as (server, accepted, clients):
                    connection = HTTPConnection(*server.server_address, timeout=4)
                    try:
                        connection.request('GET', '/')
                        response = connection.getresponse()
                        response.read()
                        self.assertEqual(response.status, 302)
                        self.assertTrue(response.getheader('Location').startswith('/login/'))

                        connection.request('GET', '/login/')
                        response = connection.getresponse()
                        form = response.read().decode()
                        self.assertEqual(response.status, 200)
                        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', form)
                        self.assertIsNotNone(token)
                        csrf = SimpleCookie()
                        for key, value in response.getheaders():
                            if key.lower() == 'set-cookie':
                                csrf.load(value)
                        csrf_cookie = csrf[settings.CSRF_COOKIE_NAME]
                        origin = 'http://127.0.0.1:%s' % server.server_port
                        body = urlencode({'username': user.username, 'password': self.password,
                                          'csrfmiddlewaretoken': token.group(1)})
                        connection.request('POST', '/login/', body, {
                            'Content-Type': 'application/x-www-form-urlencoded',
                            'Cookie': '%s=%s' % (csrf_cookie.key, csrf_cookie.value),
                            'Origin': origin,
                        })
                        response = connection.getresponse()
                        response.read()
                        self.assertEqual(response.status, 302)
                        self.assertEqual(response.getheader('Location'), '/')
                        cookies = SimpleCookie()
                        for key, value in response.getheaders():
                            if key.lower() == 'set-cookie':
                                cookies.load(value)
                        session = cookies[settings.SESSION_COOKIE_NAME]
                        self.assertEqual(session['path'], '/')
                        self.assertTrue(session['httponly'])
                        self.assertEqual(session['samesite'], settings.SESSION_COOKIE_SAMESITE)
                        connection.request('GET', '/', headers={
                            'Cookie': '%s=%s' % (session.key, session.value)})
                        response = connection.getresponse()
                        response.read()
                        self.assertEqual(response.status, 302)
                        self.assertEqual(response.getheader('Location'), '/profiles/')
                    finally:
                        connection.close()
