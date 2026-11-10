"""Synthetic source checks and fake transport only; no live credentials or I/O."""
from dataclasses import FrozenInstanceError, asdict
from hashlib import sha256
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from shelf import discovery_transport as transport
from shelf.discovery_sources import SourceError
from shelf.source_check_contract import SourceCheckOperation, parse_source_check
from shelf.tests import test_media_authorized_transport as fixtures


class SourceCheckContractTests(TestCase):
    def test_closed_wires_and_digest_have_no_personal_or_credential_parameters(self):
        for provider, method, origin, path, body in [
            ('tmdb', 'GET', 'https://api.themoviedb.org', '/3/authentication', b''),
            ('hardcover', 'POST', 'https://api.hardcover.app', '/v1/graphql',
             b'{"query":"{ books(limit:1) { id } }"}')]:
            operation = SourceCheckOperation(provider)
            expected = {'provider': provider, 'version': 'source-check/v1', 'method': method,
                        'origin': origin, 'path': path, 'query': '', 'body_digest': sha256(body).hexdigest()}
            self.assertEqual(operation.body, body)
            self.assertEqual(operation.material, expected)
            self.assertEqual(operation.operation_digest, sha256(json.dumps(expected,
                ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()).hexdigest())
            with self.assertRaises(FrozenInstanceError):
                operation.provider = 'evil'
            operation.material['path'] = '/evil'
            self.assertEqual(operation.path, path)
        for provider in ['openai', 'tmdb_movie', 'https://evil.invalid', '', [], None]:
            with self.subTest(provider=provider), self.assertRaises(SourceError):
                SourceCheckOperation(provider)

    def test_pass_means_only_authentication_or_minimal_public_read(self):
        samples = [('tmdb', b'{"success":true,"status_message":"UNTRUSTED"}'),
                   ('hardcover', b'{"data":{"books":[{"id":81001}]}}'),
                   ('hardcover', b'{"data":{"books":[]}}')]
        for provider, body in samples:
            operation = SourceCheckOperation(provider)
            result = parse_source_check(operation, 200, body)
            self.assertEqual((result.state, result.code, result.http_status), ('passed', 'OK', 200))
            self.assertEqual(result.operation_digest, operation.operation_digest)
            self.assertEqual(result.test_scope, operation.test_scope)
            self.assertNotIn('UNTRUSTED', repr(result))
            self.assertNotIn('81001', repr(result))
            self.assertNotIn('connectionReady', asdict(result))

    def test_errors_and_hostile_shapes_never_become_pass_or_echo_provider_text(self):
        common = [b'[]', b'null', b'{', b'\xff', b'{"success":true,"success":false}',
                  b'{"success":true,"errors":[]}', b'{"success":NaN}',
                  b'[' * 2000 + b']' * 2000]
        for provider in ['tmdb', 'hardcover']:
            for body in common:
                result = parse_source_check(SourceCheckOperation(provider), 200, body)
                self.assertEqual((result.state, result.code), ('failed', 'BAD_RESPONSE'))
        for body in [b'{}', b'{"success":1}', b'{"success":"true"}']:
            self.assertEqual(parse_source_check(SourceCheckOperation('tmdb'), 200, body).state, 'failed')
        for rows in [[{'id': True}], [{'id': 0}], [{'id': -1}], [{'id': 2**31}],
                     [{'id': 1}, {'id': 2}], [{'id': 1, 'title': 'UNTRUSTED'}], None]:
            body = json.dumps({'data': {'books': rows}}).encode()
            self.assertEqual(parse_source_check(SourceCheckOperation('hardcover'), 200, body).code,
                             'BAD_RESPONSE')
        for status, code in [(401, 'AUTHENTICATION_FAILED'), (403, 'AUTHENTICATION_FAILED'),
                             (429, 'RATE_LIMITED'), (302, 'SOURCE_UNAVAILABLE'), (503, 'SOURCE_UNAVAILABLE')]:
            result = parse_source_check(SourceCheckOperation('tmdb'), status,
                                        b'{"success":true,"message":"SECRET_CANARY"}')
            self.assertEqual((result.state, result.code), ('failed', code))
            self.assertNotIn('SECRET_CANARY', repr(result))
        for status, body in [(True, b'{}'), (99, b'{}'), (200, '{}'), (200, b' ' * 262145)]:
            with self.assertRaises(SourceError):
                parse_source_check(SourceCheckOperation('tmdb'), status, body)

    def test_checks_reuse_the_existing_private_stdin_bounded_transport(self):
        # Reuse the established fake process/selector harness; this is not a
        # second transport or a credential-store smoke invocation.
        fixture = fixtures.MediaAuthorizedTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for provider in ['tmdb', 'hardcover']:
            operation = SourceCheckOperation(provider)
            _, spawn, stdin = fixture.fake_send(operation=operation)
            args = spawn.args[0]
            self.assertEqual(args[args.index('--url') + 1], operation.origin + operation.path)
            self.assertEqual(args[args.index('--request') + 1], operation.method)
            self.assertEqual(args[args.index('--retry') + 1], '0')
            self.assertEqual(args[args.index('--max-redirs') + 1], '0')
            self.assertEqual(args[args.index('--max-time') + 1], '5')
            self.assertEqual(args[args.index('--max-filesize') + 1], '262144')
            self.assertTrue(b''.join(stdin).startswith(b'Authorization: Bearer '))
            if operation.body:
                self.assertEqual(args[args.index('--data-binary') + 1].encode(), operation.body)
            else:
                self.assertNotIn('--data-binary', args)

    def test_changed_authority_refuses_before_spawn_and_wipes_the_lease(self):
        secret = bytearray(b'FICTIONAL')
        with patch.object(transport.subprocess, 'Popen') as spawn, \
             patch.object(transport.MediaHostLimiter, 'reserve') as reserve:
            with self.assertRaises(SourceError):
                transport.media_authorized(SourceCheckOperation('tmdb'), secret=secret,
                    revalidate=Mock(return_value=False), limiter=transport.MediaHostLimiter('tmdb'))
        spawn.assert_not_called()
        reserve.assert_not_called()
        self.assertEqual(secret, bytearray(9))
