"""Manual smoke contracts using NONcredentials and fake processes only."""
from dataclasses import replace
from datetime import timedelta
from io import BytesIO
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from shelf import credential_entry as entry
from shelf import credential_store as store
from shelf import provider_smoke as smoke
from shelf.tests.fixtures_credentials import CANARY


class ProviderSmokeTests(unittest.TestCase):
    def child(self, body=b'{"success":true}', code=200, returncode=0):
        child = Mock(returncode=returncode, stdin=BytesIO(), stdout=BytesIO())
        child.communicate.return_value = (body + b'\n' + str(code).encode(), None)
        child.poll.return_value = returncode
        return child

    def live(self, provider='tmdb', *, metadata=None, context=None, child=None, uid=1000):
        context = context or store._provider_context(provider)
        record = store.PrivateRecord(metadata or entry._setup_metadata(store, provider),
                                     bytearray(CANARY), 'NONCREDENTIAL', None)
        handle = SimpleNamespace(context=context, uid=uid, gid=1000, close=Mock())
        writes = []
        with patch.object(store, 'open_store', return_value=handle) as opener, \
                patch.object(store, 'lock_store') as locker, \
                patch.object(store, 'read_record', return_value=record) as reader, \
                patch.object(smoke.resource, 'setrlimit') as core, \
                patch.object(smoke.subprocess, 'Popen', return_value=child or self.child()) as process, \
                patch.object(smoke.os, 'write', side_effect=lambda fd, raw: writes.append((fd, raw))):
            result = smoke.run_live(store, provider)
        handle.close.assert_called_once_with()
        if reader.called:
            self.assertEqual(record.secret, bytearray(len(CANARY)))
        self.assertNotIn(CANARY, repr(writes).encode())
        self.assertNotIn(CANARY, repr(process.call_args).encode())
        self.assertNotIn(CANARY, repr(record).encode())
        return result, writes, opener, locker, core, process

    def test_fixed_curl_contract_both_providers(self):
        for provider, endpoint, method in (
                ('tmdb', 'https://api.themoviedb.org/3/authentication', 'GET'),
                ('hardcover', 'https://api.hardcover.app/v1/graphql', 'POST')):
            with self.subTest(provider=provider):
                args = smoke._command(provider)
                self.assertEqual(args[:2], ['/usr/bin/curl', '-q'])
                for flag, value in (('--url', endpoint), ('--request', method), ('--proto', '=https'),
                                    ('--proto-redir', '=https'), ('--proxy', ''), ('--noproxy', '*'),
                                    ('--retry', '0'), ('--max-redirs', '0'), ('--max-time', '10'),
                                    ('--connect-timeout', '10'), ('--max-filesize', '262144'),
                                    ('--cacert', smoke.CA), ('--write-out', '\n%{http_code}')):
                    self.assertEqual(args[args.index(flag) + 1], value)
                self.assertIn('--no-netrc', args)
                self.assertIn('--silent', args)
                self.assertEqual(args[args.index('--header') + 1], '@-')
                for forbidden in ('--location', '--follow', '-L', '--config', '-K', '--netrc',
                                  '--verbose', '--trace', '--insecure', '--show-error', '--cookie-jar'):
                    self.assertNotIn(forbidden, args)
                self.assertEqual(sum(a.startswith('https://') for a in args), 1)
                if provider == 'hardcover':
                    self.assertEqual(json.loads(args[args.index('--data-binary') + 1]),
                                     {'query': '{ books(limit:1) { id } }'})
                else:
                    self.assertNotIn('--data-binary', args)

    def test_secret_stdin_only_minimal_env_and_no_raw_diagnostics(self):
        child = self.child(b'{"success":true,"provider_text":"DO-NOT-PRINT"}')
        captured = []
        def communicate(*, input, timeout):
            captured.append(bytes(input))
            self.assertEqual(timeout, 11)
            return child.communicate.return_value
        child.communicate.side_effect = communicate
        result, writes, opener, locker, core, process = self.live(child=child)
        self.assertEqual(result, 0)
        self.assertEqual(captured, [b'Authorization: Bearer ' + CANARY + b'\n'])
        self.assertEqual(writes, [(1, b'provider-smoke status=ok provider=tmdb http=200 count=1\n')])
        opener.assert_called_once_with(provider='tmdb')
        locker.assert_called_once()
        core.assert_called_once_with(smoke.resource.RLIMIT_CORE, (0, 0))
        self.assertEqual(process.call_args.kwargs, {
            'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE, 'stderr': subprocess.DEVNULL,
            'env': {'LANG': 'C', 'LC_ALL': 'C'}, 'close_fds': True})
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
        self.assertEqual(child.communicate.call_args.kwargs['input'],
                         bytearray(len(b'Authorization: Bearer ' + CANARY + b'\n')))

    def test_hardcover_catalogue_success_counts_only(self):
        for books, count in (([], 0), ([{'id': 'FICTION-ID', 'text': 'DO-NOT-PRINT'}], 1)):
            with self.subTest(count=count):
                child = self.child(json.dumps({'data': {'books': books}}).encode())
                result, writes, *_ = self.live('hardcover', child=child)
                self.assertEqual(result, 0)
                self.assertEqual(writes, [(1, f'provider-smoke status=ok provider=hardcover http=200 count={count}\n'.encode())])

    def test_graphql_errors_fail_even_with_data_and_http200(self):
        for errors in ([{'message': 'DO-NOT-PRINT'}], [], None):
            body = json.dumps({'data': {'books': [{'id': 7}]}, 'errors': errors}).encode()
            self.assertEqual(smoke._response('hardcover', 0, body + b'\n200'), ('bad_response', 200, 0))

    def test_tmdb_requires_boolean_true(self):
        for value in (False, None, 1, 'true', [], {}):
            body = json.dumps({'success': value}).encode()
            self.assertEqual(smoke._response('tmdb', 0, body + b'\n200'), ('bad_response', 200, 0))

    def test_http_auth_rate_redirect_and_other_failures(self):
        for code, status in ((401, 'auth'), (403, 'auth'), (429, 'rate'), (302, 'bad_response'),
                             (500, 'bad_response'), (204, 'bad_response')):
            with self.subTest(code=code):
                result, writes, *_ = self.live(child=self.child(CANARY, code))
                self.assertEqual(result, 2)
                self.assertEqual(writes, [(1, f'provider-smoke status={status} provider=tmdb http={code} count=0\n'.encode())])

    def test_curl_tls_timeout_oversize_and_other_transport_failures(self):
        for returncode, status in ((60, 'tls'), (35, 'tls'), (77, 'tls'), (28, 'timeout'),
                                   (63, 'bad_response'), (6, 'bad_response'), (7, 'bad_response')):
            with self.subTest(returncode=returncode):
                result, writes, *_ = self.live(child=self.child(CANARY, returncode=returncode))
                self.assertEqual(result, 2)
                self.assertEqual(writes, [(1, f'provider-smoke status={status} provider=tmdb http=0 count=0\n'.encode())])

    def test_parent_deadline_kills_reaps_and_closes(self):
        child = self.child()
        child.communicate.side_effect = subprocess.TimeoutExpired('fake', 11, output=CANARY, stderr=CANARY)
        child.poll.return_value = None
        result, writes, *_ = self.live(child=child)
        self.assertEqual(result, 2)
        self.assertEqual(writes, [(1, b'provider-smoke status=timeout provider=tmdb http=0 count=0\n')])
        child.kill.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=1)
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)

    def test_nonjson_malformed_shape_and_oversized_response(self):
        for body in (b'<html>DO-NOT-PRINT</html>', b'[]', b'null', b'{}', b'\xff', b'{',
                     b'x' * (smoke.MAX_BODY + 1)):
            with self.subTest(length=len(body)):
                self.assertEqual(smoke._response('tmdb', 0, body + b'\n200'), ('bad_response', 200 if len(body) <= smoke.MAX_BODY else 0, 0))
        for body in (b'{}', b'{"data":{"books":null}}', b'{"data":{"books":[{}]}}',
                     b'{"data":{"books":[{"id":true}]}}', b'{"data":{"books":[{"id":1},{"id":2}]}}'):
            self.assertEqual(smoke._response('hardcover', 0, body + b'\n200'), ('bad_response', 200, 0))
        for output in (b'', b'200', b'{}\nxx0', b'{}\n2000'):
            self.assertEqual(smoke._response('tmdb', 0, output), ('bad_response', 0, 0))

    def test_exact_body_limit_is_accepted(self):
        body = b'{"success":true}'
        body += b' ' * (smoke.MAX_BODY - len(body))
        self.assertEqual(smoke._response('tmdb', 0, body + b'\n200'), ('ok', 200, 1))

    def test_metadata_provider_purpose_scopes_owner_profile_and_expiry_refuse_before_transport(self):
        base = entry._setup_metadata(store, 'tmdb')
        wrong = [replace(base, binding=replace(base.binding, provider='hardcover')),
                 replace(base, binding=replace(base.binding, purpose='wrong')),
                 replace(base, binding=replace(base.binding, actor_id='2')),
                 replace(base, binding=replace(base.binding, profile_id='2')),
                 replace(base, scopes=store.SCOPES),
                 replace(base, entered_at=store.utc_now() - timedelta(days=2),
                         expires_at=store.utc_now() - timedelta(days=1)),
                 replace(base, declared_provider_expires_at=store.utc_now())]
        for metadata in wrong:
            with self.subTest(metadata=repr(metadata)):
                result, writes, *_, process = self.live(metadata=metadata)
                self.assertEqual(result, 2)
                process.assert_not_called()
                self.assertEqual(writes, [(1, b'provider-smoke status=metadata provider=tmdb http=0 count=0\n')])
        for context, uid in ((store._provider_context('hardcover'), 1000),
                             (replace(store._provider_context('tmdb'), purpose='wrong'), 1000),
                             (store._provider_context('tmdb'), 17)):
            result, _, _, locker, _, process = self.live(context=context, uid=uid)
            self.assertEqual(result, 2)
            locker.assert_not_called()
            process.assert_not_called()

    def test_core_zero_precedes_store_and_process(self):
        events = []
        with patch.object(smoke.resource, 'setrlimit', side_effect=lambda *a: events.append('core')), \
                patch.object(store, 'open_store', side_effect=lambda **kw: (_ for _ in ()).throw(store.Refused())) as opener, \
                patch.object(smoke.os, 'write'), patch.object(smoke.subprocess, 'Popen') as process:
            self.assertEqual(smoke.run_live(store, 'tmdb'), 2)
        self.assertEqual(events, ['core'])
        opener.assert_called_once_with(provider='tmdb')
        process.assert_not_called()

    def test_unknown_provider_refuses_before_any_io(self):
        with patch.object(store, 'open_store') as opener, patch.object(smoke.os, 'write') as write, \
                patch.object(smoke.resource, 'setrlimit') as core, patch.object(smoke.subprocess, 'Popen') as process:
            self.assertEqual(smoke.run_live(store, 'other'), 2)
        for action in (opener, write, core, process):
            action.assert_not_called()

    def test_process_exception_is_sanitized_and_copies_cleared(self):
        child = self.child()
        child.communicate.side_effect = OSError(CANARY.decode())
        child.poll.return_value = None
        result, writes, *_ = self.live(child=child)
        self.assertEqual(result, 2)
        self.assertEqual(writes, [(1, b'provider-smoke status=bad_response provider=tmdb http=0 count=0\n')])
        child.kill.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=1)
