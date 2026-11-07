"""LibraryThing candidate: owned fictional data only; all transport is mocked."""
from contextlib import ExitStack
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import selectors
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from shelf import credential_entry as entry, credential_store as store, librarything_smoke as smoke
from shelf.tests.fixtures_credentials import CANARY, FakeTree
from shelf.tests import test_credential_entry as legacy


def response(value, code=200, content_type=b'application/json'):
    return json.dumps(value).encode() + b'\n' + str(code).encode() + b'\n' + content_type


def fiction(count=1):
    # Invented fixture values; no fetched provider content.
    return {'request': [{'isbn': 'FICTION-SEED'}], 'recommendations': [
        {'rank': i + 1, 'work': 700 + i, 'fromworks': [999], 'author_id': 'FICTION_AUTHOR',
         'isbns': ['FICTION-ISBN'], 'upcs': []} for i in range(count)]}


class LibraryThingStoreTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTree(store, provider='librarything')
        self.addCleanup(self.fake.close)
        self.path = self.fake.path

    def test_fixed_context_and_cap_are_local_only(self):
        context = store._provider_context('librarything')
        self.assertEqual(context.directory, '/home/tang/.local/state/culture-cabinet-secrets/librarything-backend')
        self.assertEqual(context.scopes, ('read-multirecommendations',))
        metadata = entry._setup_metadata(store, 'librarything')
        self.assertEqual(metadata.binding, store.Binding('unverified', '1', '1', 'librarything', context.purpose))
        self.assertEqual(metadata.expires_at - metadata.entered_at, timedelta(days=89))
        self.assertEqual(metadata.declared_provider_expires_at, metadata.expires_at)
        instruction = entry._setup_instruction('librarything')
        for label in (b'LOCAL purpose, not an OAuth scope', b'provider expiry is unchanged', b'cannot prove account identity'):
            self.assertIn(label, instruction)

    def test_roundtrip_keeps_unverified_schema_and_private_repr(self):
        receipt = store.commit_entry(self.fake.store, entry._setup_metadata(store, 'librarything'), bytearray(CANARY))
        self.assertEqual(receipt.outcome, 'stored')
        record = store.read_record(self.fake.store)
        self.addCleanup(record.close)
        self.assertEqual(record.secret, CANARY)
        self.assertFalse(record.verified)
        self.assertNotIn(CANARY.decode(), repr(record))
        self.assertEqual((self.path / store.RECORD).stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.path / store.RECORD).read_bytes())['schema'], 1)
        record.close()
        self.assertEqual(set(record.secret), {0})

    def test_wrong_provider_and_purpose_rejected(self):
        metadata = entry._setup_metadata(store, 'librarything')
        for provider in ('hardcover', 'tmdb'):
            with self.subTest(provider=provider), self.assertRaises(store.Refused):
                store.validate_metadata(metadata, now=store.utc_now(), provider=provider)
        for purpose in ('culture-cabinet-tmdb-backend', 'read-multirecommendations'):
            bad = store.EntryMetadata(store.Binding('unverified', '1', '1', 'librarything', purpose),
                                      metadata.entered_at, metadata.expires_at, metadata.declared_provider_expires_at,
                                      metadata.scopes)
            self.assertEqual(store.commit_entry(self.fake.store, bad, bytearray(CANARY)).outcome, 'refused')
        self.assertEqual(os.listdir(self.path), [store.LOCK])

    def test_cross_provider_record_swaps_refused(self):
        for provider in ('hardcover', 'tmdb', 'librarything'):
            metadata = entry._setup_metadata(store, provider)
            raw = store._encode(metadata, bytearray(CANARY))
            for other in ('hardcover', 'tmdb', 'librarything'):
                if provider == other:
                    continue
                with self.subTest(provider=provider, other=other), self.assertRaises(store.Refused):
                    store._decode(raw, store.StoreIdentity(()), provider=other)

    def test_expired_and_overcap_and_wrong_scope_records_refused(self):
        metadata = entry._setup_metadata(store, 'librarything')
        for delta, scopes in ((90, metadata.scopes), (1, store.SCOPES), (1, store.TMDB_SCOPES)):
            bad = store.EntryMetadata(metadata.binding, metadata.entered_at,
                metadata.entered_at + timedelta(days=delta), metadata.entered_at + timedelta(days=100), scopes)
            with self.assertRaises(store.Refused):
                store.validate_metadata(bad, now=store.utc_now(), provider='librarything')
        with self.assertRaisesRegex(store.Refused, 'expired'):
            store.validate_metadata(metadata, now=metadata.expires_at, provider='librarything')

    def test_hidden_PTY_dummy_CANCEL_does_not_store_or_echo(self):
        self.fake.store._lock.close()
        terminal = legacy.PrivatePTY(self.path, 'setup-librarything')
        self.addCleanup(terminal.close)
        terminal.send(b'Token: ', CANARY)
        terminal.send(b'Type STORE to confirm: ', b'CANCEL')
        code, output = terminal.finish()
        self.assertEqual(code, 2, output)
        self.assertNotIn(CANARY, output)
        self.assertIn(b'credential-entry enter refused interrupted', output)
        self.assertEqual(os.listdir(self.path), [store.LOCK])
        for prompt in (b'Account ID:', b'Actor ID:', b'Profile ID:', b'Local expiry UTC', b'Declared provider expiry UTC'):
            self.assertNotIn(prompt, output)

    def test_hidden_PTY_dummy_STORE_binds_fixed_owner_profile(self):
        self.fake.store._lock.close()
        terminal = legacy.PrivatePTY(self.path, 'setup-librarything')
        self.addCleanup(terminal.close)
        terminal.send(b'Token: ', CANARY)
        terminal.send(b'Type STORE to confirm: ', b'STORE')
        code, output = terminal.finish()
        self.assertEqual(code, 0, output)
        self.assertNotIn(CANARY, output)
        store.lock_store(self.fake.store)
        record = store.read_record(self.fake.store)
        self.addCleanup(record.close)
        self.assertEqual(record.metadata.binding, entry._setup_metadata(store, 'librarything').binding)

    def test_librarything_symlink_record_refused(self):
        (self.path / store.RECORD).symlink_to(self.path / 'missing-fiction')
        with self.assertRaises(store.Refused):
            store.read_record(self.fake.store)

    def test_setup_dispatch_fixed_provider_and_no_options(self):
        with patch.object(entry, '_deployment_store', return_value=store), patch.object(store, 'open_store') as opener:
            def run(verb, module, fixed):
                self.assertEqual(verb, 'setup-librarything')
                fixed()
                return 0
            with patch.object(entry, '_run', side_effect=run):
                self.assertEqual(entry.main(['setup-librarything']), 0)
            opener.assert_called_once_with(provider='librarything')
        for args in (['setup-librarything', '--path=/fiction'], ['setup-librarything', '--token=FICTION']):
            with patch.object(entry, '_deployment_store') as loader, patch.object(entry, '_emit'):
                self.assertEqual(entry.main(args), 2)
                loader.assert_not_called()

    def test_explicit_live_guard_before_any_loader_or_state_IO(self):
        for args in (['smoke-librarything'], ['smoke-librarything', '--live=false'],
                     ['smoke-librarything', '--live', '--live'], ['smoke-librarything', '--token=FICTION']):
            with patch.object(entry, '_deployment_store') as loader, patch.object(entry, '_emit') as emit:
                self.assertEqual(entry.main(args), 2)
                loader.assert_not_called()
                emit.assert_called_once_with(b'credential-entry refused explicit_live_required\n')

    def test_manual_live_dispatch_checked_sibling(self):
        sender = Mock()
        sender.run_live.return_value = 2
        with patch.object(entry, '_deployment_store', return_value=(store, sender)) as loader:
            self.assertEqual(entry.main(['smoke-librarything', '--live']), 2)
        loader.assert_called_once_with(smoke=True, librarything=True)
        sender.run_live.assert_called_once_with(store, 'librarything')

    def test_actual_bootstrap_new_sibling_and_legacy_policy(self):
        directory = legacy.CredentialEntryTests.bootstrap_fixture(self)
        code = directory / 'librarything_smoke.py'
        code.write_bytes(Path(smoke.__file__).read_bytes())
        code.chmod(0o444)
        result = legacy.CredentialEntryTests.bootstrap(self, directory, 'librarything-smoke')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, b'BOOTSTRAP_ACCEPTED_OPENER_BLOCKED\n')
        for unsafe in ('writable', 'symlink', 'missing'):
            if unsafe == 'writable':
                code.chmod(0o644)
            elif unsafe == 'symlink':
                code.unlink()
                code.symlink_to(directory / 'credential_store.py')
            else:
                code.unlink()
            result = legacy.CredentialEntryTests.bootstrap(self, directory, 'librarything-smoke')
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(result.stdout, b'BOOTSTRAP_REFUSED\n')
            self.assertEqual(result.stderr, b'')


class FakeSelector:
    def __init__(self, *, hang=False):
        self.keys = {}
        self.hang = hang

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def register(self, stream, mask):
        self.keys[stream] = SimpleNamespace(fileobj=stream, events=mask)

    def unregister(self, stream):
        del self.keys[stream]

    def get_map(self):
        return self.keys

    def select(self, timeout):
        return [] if self.hang else [(key, key.events) for key in list(self.keys.values())]


class FakePipe(io.BytesIO):
    def fileno(self):
        return id(self)


class LibraryThingTransportTests(unittest.TestCase):
    def transport(self, output=None, *, returncode=0, hang=False):
        process = Mock()
        process.stdin, process.stdout = FakePipe(), FakePipe()
        process.returncode = returncode
        process.poll.return_value = None if hang else returncode
        captured = []
        chunks = [output if output is not None else response(fiction()), b'']
        def read(fd, cap):
            chunk = chunks.pop(0)
            if len(chunk) > cap:
                chunks.insert(0, chunk[cap:])
                chunk = chunk[:cap]
            return chunk
        def write(fd, data):
            captured.append(bytes(data))
            return len(data)
        with patch.object(smoke.subprocess, 'Popen', return_value=process) as popen, \
                patch.object(smoke.selectors, 'DefaultSelector', return_value=FakeSelector(hang=hang)), \
                patch.object(smoke.os, 'set_blocking'), patch.object(smoke.os, 'read', side_effect=read), \
                patch.object(smoke.os, 'write', side_effect=write):
            result = smoke._send(bytearray(CANARY + b'+/='))
        return result, popen.call_args, captured, process

    def test_API_query_only_in_stdin_never_argv_env_or_header(self):
        result, call, captured, process = self.transport()
        self.assertEqual(result, ('ok', 200, 1, 'none', 'json'))
        command = call.args[0]
        self.assertEqual(command[:4], ['/usr/bin/curl', '-q', '--config', '-'])
        self.assertEqual(call.kwargs['env'], {'LANG': 'C', 'LC_ALL': 'C'})
        self.assertIs(call.kwargs['stderr'], smoke.subprocess.DEVNULL)
        self.assertTrue(call.kwargs['close_fds'])
        self.assertNotIn(CANARY.decode(), repr(call))
        self.assertNotIn(smoke.ENDPOINT, repr(call))
        self.assertNotIn('Authorization', repr(call))
        config = b''.join(captured)
        self.assertIn(b'apiKey=' + CANARY + b'%2B%2F%3D', config)
        for field in (b'v=2', b'isbns=0300178727', b'maxItems=5', b'maxPerAuthor=2', b'showtitles=false'):
            self.assertIn(field, config)
        self.assertEqual(config.count(b'url = '), 1)
        self.assertTrue(process.stdin.closed and process.stdout.closed)
        self.assertEqual(command[command.index('--max-time') + 1], '10')
        self.assertEqual(command[command.index('--cacert') + 1], smoke.CA)
        for flag, value in (('--proxy', ''), ('--max-redirs', '0'), ('--retry', '0'), ('--proto', '=https')):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn('--no-netrc', command)
        self.assertEqual(command[command.index('--write-out') + 1], '\n%{http_code}\n%{content_type}')
        self.assertNotIn('--dump-header', command)

    def test_config_injection_is_encoded_and_buffers_cleared(self):
        config = smoke._config(bytearray(b'FAKE"\nurl=evil&x=1'))
        self.assertEqual(config.count(b'\n'), 1)
        self.assertIn(b'%22%0Aurl%3Devil%26x%3D1', config)
        observed = []
        real_config = smoke._config
        def build(secret):
            result = real_config(secret)
            observed.append(result)
            return result
        with patch.object(smoke, '_config', side_effect=build):
            self.transport()
        self.assertEqual(set(observed[0]), {0})
        outputs = []
        real_response = smoke._response
        def parse(returncode, output):
            outputs.append(output)
            return real_response(returncode, output)
        with patch.object(smoke, '_response', side_effect=parse):
            self.transport()
        self.assertEqual(set(outputs[0]), {0})

    def test_transport_timeout_kills_and_waits_without_retry(self):
        result, call, captured, process = self.transport(hang=True)
        self.assertEqual(result, ('timeout', 0, 0, 'transport_timeout', 'unavailable'))
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1)
        self.assertTrue(process.stdin.closed and process.stdout.closed)

    def test_transport_parent_cap_even_if_child_ignores_curl_limit(self):
        result, _, _, _ = self.transport(output=b'x' * (smoke.MAX_OUTPUT + 1))
        self.assertEqual(result, ('size', 0, 0, 'transport_size', 'unavailable'))

    def test_transport_raw_exception_and_key_never_escape(self):
        with patch.object(smoke.subprocess, 'Popen', side_effect=OSError(CANARY.decode())):
            self.assertEqual(smoke._send(bytearray(CANARY)), ('bad_response', 0, 0, 'transport_failure', 'unavailable'))

    def test_http_auth_rate_redirect_server_and_tls_timeout(self):
        for code, status in ((401, 'auth'), (403, 'auth'), (429, 'rate'), (302, 'bad_response'), (500, 'bad_response')):
            self.assertEqual(smoke._response(0, response({}, code)), (status, code, 0, 'http_auth' if code in (401, 403) else
                              'http_rate' if code == 429 else 'http_status', 'json'))
        for code, status in ((28, 'timeout'), (35, 'tls'), (60, 'tls'), (77, 'tls'), (63, 'size'), (7, 'bad_response')):
            self.assertEqual(smoke._response(code, b'PRIVATE-FAKE-ERROR'), (status, 0, 0, 'transport_' + (status if status != 'bad_response' else 'failure'), 'unavailable'))

    def test_provider_error_codes_do_not_echo_provider_text(self):
        for code, status in (('aut00001', 'auth'), ('bnf00003', 'no_recommendations'), ('bnf00005', 'invalid_seed'), ('other', 'bad_response')):
            for errors in ({'errorCode': code, 'message': CANARY.decode()}, [{'errorCode': code, 'message': CANARY.decode()}]):
                self.assertEqual(smoke._response(0, response({'errors': errors})), (status, 200, 0, {'auth': 'api_auth', 'no_recommendations': 'api_no_recommendations',
                                  'invalid_seed': 'api_invalid_seed', 'bad_response': 'api_unknown'}[status], 'json'))

    def test_valid_counts_zero_to_five_and_no_content_in_result(self):
        for count in range(6):
            result = smoke._response(0, response(fiction(count)))
            self.assertEqual(result, ('ok', 200, count, 'none', 'json'))
            self.assertNotIn('FICTION', repr(result))

    def test_nullable_identifier_lists_and_optional_author(self):
        for isbns, upcs in ((None, []), ([], None), (None, None), ([], [])):
            for author in (False, True):
                value = fiction()
                item = value['recommendations'][0]
                item.update(isbns=isbns, upcs=upcs)
                if not author:
                    del item['author_id']
                with self.subTest(isbns=isbns, upcs=upcs, author=author):
                    self.assertEqual(smoke._response(0, response(value)), ('ok', 200, 1, 'none', 'json'))

    def test_flat_errors_unknown_errors_and_zero_success_are_distinct(self):
        for error, status, reason in (('aut00001', 'auth', 'api_auth'),
                                      ('bnf00003', 'no_recommendations', 'api_no_recommendations'),
                                      ('bnf00005', 'invalid_seed', 'api_invalid_seed'),
                                      (CANARY.decode(), 'bad_response', 'api_unknown')):
            result, _, _, _ = self.transport(response({'errorCode': error, 'message': CANARY.decode()}))
            self.assertEqual(result, (status, 200, 0, reason, 'json'))
            self.assertNotIn(CANARY.decode(), repr(result))
        self.assertEqual(smoke._response(0, response(fiction(0))), ('ok', 200, 0, 'none', 'json'))

    def test_conflicting_error_envelopes_fail_closed(self):
        for error in ({'errorCode': 'aut00001'}, {'errors': {'errorCode': 'bnf00003'}}):
            for success in (fiction(), {'request': []}, {'recommendations': []}):
                self.assertEqual(smoke._response(0, response(dict(success, **error))),
                                 ('bad_response', 200, 0, 'envelope_conflict', 'json'))
        self.assertEqual(smoke._response(0, response({'errorCode': 'aut00001', 'errors': []})),
                         ('bad_response', 200, 0, 'envelope_conflict', 'json'))

    def test_strict_types_bounds_missing_fields_and_error_shapes(self):
        for field, values in {
            'author_id': (None, False, 0, '', 'x' * 257),
            'rank': (0, 6, 1.0), 'work': (False, 0, 10**18, 'x' * 65),
            'fromworks': ([False], [0], [1] * 6),
            'isbns': (False, {}, [1], ['x' * 33], ['x'] * 101),
            'upcs': (0, {}, [False], ['x' * 33], ['x'] * 101),
        }.items():
            for bad in values:
                value = fiction()
                value['recommendations'][0][field] = bad
                with self.subTest(field=field, bad=bad):
                    self.assertEqual(smoke._response(0, response(value)),
                                     ('bad_response', 200, 0, 'shape_invalid', 'json'))
        for field in ('isbns', 'upcs'):
            value = fiction()
            del value['recommendations'][0][field]
            self.assertEqual(smoke._response(0, response(value)),
                             ('bad_response', 200, 0, 'shape_invalid', 'json'))
        for value in ({'errorCode': None}, {'errorCode': 1}, {'errorCode': []},
                      {'errors': [None]}, {'errors': [{}]}, {'errors': [{'errorCode': 'bnf00003'}] * 9}):
            self.assertEqual(smoke._response(0, response(value)),
                             ('bad_response', 200, 0, 'shape_invalid', 'json'))
        body = b'{"errorCode":"aut00001","errorCode":"bnf00003"}\n200\napplication/json'
        self.assertEqual(smoke._response(0, body), ('bad_response', 200, 0, 'json_invalid', 'json'))

    def test_content_type_is_safe_diagnostic_only_for_valid_json(self):
        for raw, category in ((b'application/json', 'json'),
                              (b'Application/JSON; charset=utf-8', 'json'),
                              (b'application/json; ' + CANARY, 'json'),
                              (b'text/plain', 'other'), (b'text/html', 'html'),
                              (b'text/html; ' + CANARY, 'html'),
                              (CANARY, 'other'), (b'application/' + CANARY + b'+json', 'other'),
                              (b'', 'missing')):
            result, _, _, _ = self.transport(response(fiction(), content_type=raw))
            self.assertEqual(result, ('ok', 200, 1, 'none', category))
            self.assertNotIn(CANARY.decode(), repr(result))
        result, _, _, _ = self.transport(response(fiction(), content_type=b'x' * 1025))
        self.assertEqual(result, ('size', 0, 0, 'transport_size', 'unavailable'))
        result, _, _, _ = self.transport(response(fiction(), content_type=CANARY + b'\n200\napplication/json'))
        self.assertEqual(result, ('bad_response', 200, 0, 'json_invalid', 'json'))

    def test_html_and_non_json_bodies_fail_parsing_regardless_of_content_type(self):
        for raw, category in ((b'application/json', 'json'), (b'', 'missing'),
                              (b'text/plain', 'other'), (b'text/html', 'html'),
                              (b'application/' + CANARY + b'+json', 'other')):
            for body in (b'<html>' + CANARY + b'</html>', b'not JSON ' + CANARY):
                result, _, _, _ = self.transport(body + b'\n200\n' + raw)
                self.assertEqual(result, ('bad_response', 200, 0, 'json_invalid', category))
                self.assertNotIn(CANARY.decode(), repr(result))

    def test_flat_auth_errors_remain_identified_regardless_of_content_type(self):
        for raw, category in ((b'application/json', 'json'), (b'', 'missing'),
                              (b'text/plain', 'other'), (b'text/html', 'html'),
                              (b'application/' + CANARY + b'+json', 'other')):
            result, _, _, _ = self.transport(response(
                {'errorCode': 'aut00001', 'message': CANARY.decode()}, content_type=raw))
            self.assertEqual(result, ('auth', 200, 0, 'api_auth', category))
            self.assertNotIn(CANARY.decode(), repr(result))

    def test_body_limit_remains_independent_of_trailer_allowance(self):
        result, _, _, _ = self.transport(b' ' * (smoke.MAX_BODY + 1) + b'\n200\napplication/json')
        self.assertEqual(result, ('size', 0, 0, 'transport_size', 'unavailable'))

    def test_malformed_JSON_duplicates_constants_depth_and_shapes(self):
        for body in (b'{"errors":[],"errors":[]}', b'{"x":NaN}', b'\xff', b'[' * 2000):
            self.assertEqual(smoke._response(0, body + b'\n200\napplication/json'),
                             ('bad_response', 200, 0, 'json_invalid', 'json'))
        for value in (None, [], {}, {'errors': []}, {'errors': ['aut00001']}, {'request': {}, 'recommendations': []},
                      fiction(6), {'request': [], 'recommendations': []}):
            self.assertEqual(smoke._response(0, response(value)), ('bad_response', 200, 0, 'shape_invalid', 'json'))

    def test_malformed_recommendation_fields_refused(self):
        for field, value in (('rank', True), ('work', {}), ('fromworks', 'FICTION'),
                             ('author_id', []), ('isbns', 'FICTION'), ('upcs', [None])):
            item = fiction()
            item['recommendations'][0][field] = value
            self.assertEqual(smoke._response(0, response(item)), ('bad_response', 200, 0, 'shape_invalid', 'json'))
        for output in (b'{}', b'{}\n20', b'{}\nABC', b'{}\n200\napplication/json\n'):
            self.assertEqual(smoke._response(0, output), ('bad_response', 0, 0, 'transport_format', 'unavailable'))


class LibraryThingQuotaTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTree(store, provider='librarything')
        self.addCleanup(self.fake.close)
        self.fake.store._lock.close()
        self.path = self.fake.path
        self.receipt = self.path / smoke.RECEIPT
        self.receipt.write_text('{"schema":1,"attempted_at":0}')
        self.receipt.chmod(0o600)
        self.now = 2000000000

    def charge(self):
        with patch.object(smoke, '_open_quota', side_effect=lambda module: module._fixture_store(self.path, provider='librarything')), \
                patch.object(smoke.time, 'time', return_value=self.now):
            return smoke._charge(store)

    def test_charge_is_durable_before_send_and_one_per_rolling_day(self):
        self.assertTrue(self.charge())
        raw = self.receipt.read_bytes()
        self.assertEqual(json.loads(raw), {'schema': 1, 'attempted_at': self.now})
        self.assertNotIn(CANARY, raw)
        for delta in (0, 1, 3600, 86399):
            self.now = 2000000000 + delta
            self.assertFalse(self.charge())
        self.now = 2000000000 + 86400
        self.assertTrue(self.charge())

    def test_one_per_day_also_bounds_second_and_25_per_rolling_24h(self):
        sends = []
        for second in range(0, 3 * 86400 + 1, 500):
            self.now = 2000000000 + second
            if self.charge():
                sends.append(self.now)
        self.assertTrue(all(b - a >= 86400 for a, b in zip(sends, sends[1:])))
        self.assertLessEqual(max(sum(t <= s < t + 86400 for s in sends) for t in sends), 1)

    def test_missing_corrupt_future_and_unknown_fields_fail_closed(self):
        for raw in (b'', b'{', b'{}', b'{"schema":1,"attempted_at":true}',
                    b'{"schema":1,"attempted_at":2000000001}', b'{"schema":1,"attempted_at":-1}',
                    b'{"schema":1,"attempted_at":0,"extra":0}', b'x' * 129):
            self.receipt.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(store.Refused):
                self.charge()
            self.assertEqual(self.receipt.read_bytes(), raw)
        self.receipt.unlink()
        with self.assertRaises(store.Refused):
            self.charge()

    def test_private_modes_symlink_and_hardlink_receipts_refused(self):
        self.receipt.chmod(0o644)
        with self.assertRaises(store.Refused):
            self.charge()
        self.receipt.chmod(0o600)
        alias = self.path / 'alias'
        os.link(self.receipt, alias)
        with self.assertRaises(store.Refused):
            self.charge()
        alias.unlink()
        self.receipt.unlink()
        self.receipt.symlink_to(self.path / 'missing-fiction')
        with self.assertRaises(store.Refused):
            self.charge()

    def test_contending_lock_refuses_without_changing_receipt(self):
        store.lock_store(self.fake.store)
        before = self.receipt.read_bytes()
        with self.assertRaisesRegex(store.Refused, 'busy'):
            self.charge()
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_clock_rollback_and_fsync_failure_do_not_authorize(self):
        self.assertTrue(self.charge())
        self.now -= 1
        with self.assertRaises(store.Refused):
            self.charge()
        self.now += 86402
        with patch.object(store.os, 'fsync', side_effect=OSError('FAKE')):
            with self.assertRaises(OSError):
                self.charge()
        self.assertFalse(self.charge())

    def test_quota_fixed_project_path_and_contract(self):
        with patch.object(store, '_walk') as walk:
            smoke._open_quota(store)
        args, kwargs = walk.call_args
        self.assertEqual('/' + '/'.join(args[0]), '/home/tang/Projects/culture-cabinet/.librarything-smoke')
        self.assertEqual(len(args[0]), len(args[1]))
        self.assertEqual(args[1][-1], (1000, 1000, True))
        self.assertEqual(kwargs, {'provider': 'librarything'})


class LibraryThingLiveTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTree(store, provider='librarything')
        self.addCleanup(self.fake.close)
        self.assertEqual(store.commit_entry(self.fake.store, entry._setup_metadata(store, 'librarything'), bytearray(CANARY)).outcome, 'stored')
        self.fake.store._lock.close()

    def run_fake(self, *, quota=True, result=('ok', 200, 1, 'none', 'json'), error=None):
        with patch.object(store, 'open_store', side_effect=lambda **kw: store._fixture_store(self.fake.path, provider=kw['provider'])), \
                patch.object(smoke.resource, 'setrlimit'), patch.object(smoke, '_charge', return_value=quota) as charge, \
                patch.object(smoke, '_send', return_value=result, side_effect=error) as send, patch.object(smoke.os, 'write') as write:
            code = smoke.run_live(store, 'librarything')
        return code, charge, send, write

    def test_live_fixed_output_and_secret_cleared_without_identity_claim(self):
        code, charge, send, write = self.run_fake()
        self.assertEqual(code, 0)
        charge.assert_called_once_with(store)
        send.assert_called_once()
        self.assertEqual(set(send.call_args.args[0]), {0})
        write.assert_called_once_with(1, b'provider-smoke status=ok provider=librarything http=200 count=1 reason=none content_type=json\n')

    def test_local_limit_refuses_before_transport(self):
        code, charge, send, write = self.run_fake(quota=False)
        self.assertEqual(code, 2)
        send.assert_not_called()
        write.assert_called_once_with(1, b'provider-smoke status=local_limit provider=librarything http=0 count=0 reason=local_limit content_type=unavailable\n')

    def test_failed_send_is_charged_and_exception_text_not_output(self):
        code, charge, send, write = self.run_fake(error=OSError(CANARY.decode()))
        self.assertEqual(code, 2)
        charge.assert_called_once_with(store)
        self.assertNotIn(CANARY.decode(), repr(write.call_args))
        write.assert_called_once_with(1, b'provider-smoke status=bad_response provider=librarything http=0 count=0 reason=transport_failure content_type=unavailable\n')

    def test_malicious_error_and_content_type_never_reach_diagnostic_output(self):
        for payload, content_type, reason, category, exit_code in (
            ({'errorCode': CANARY.decode(), 'message': CANARY.decode()},
             b'application/json; ' + CANARY, 'api_unknown', 'json', 2),
            ({'errorCode': CANARY.decode(), 'message': CANARY.decode()},
             CANARY, 'api_unknown', 'other', 2),
            (fiction(), CANARY, 'none', 'other', 0),
            (fiction(), b'text/html; ' + CANARY, 'none', 'html', 0),
            ({'errorCode': 'aut00001', 'message': CANARY.decode()},
             b'application/json', 'api_auth', 'json', 2),
            ({'errorCode': 'aut00001', 'message': CANARY.decode()},
             b'text/html; ' + CANARY, 'api_auth', 'html', 2),
            ({'errorCode': 'aut00001', 'message': CANARY.decode()},
             b'application/' + CANARY + b'+json', 'api_auth', 'other', 2),
        ):
            result = smoke._response(0, response(payload, content_type=content_type))
            code, charge, send, write = self.run_fake(result=result)
            self.assertEqual(code, exit_code)
            charge.assert_called_once_with(store)
            self.assertNotIn(CANARY.decode(), repr(write.call_args))
            output = write.call_args.args[1]
            self.assertIn(f' reason={reason} content_type={category}\n'.encode('ascii'), output)

    def test_wrong_binding_purpose_scope_and_expiry_before_quota_or_send(self):
        path = self.fake.path / store.RECORD
        original = json.loads(path.read_bytes())
        mutations = [lambda v: v['binding'].update(profile_id='2'), lambda v: v['binding'].update(account_id='FICTION'),
                     lambda v: v['binding'].update(provider='tmdb'), lambda v: v['binding'].update(purpose='wrong'),
                     lambda v: v.update(scopes=list(store.TMDB_SCOPES)),
                     lambda v: v.update(expires_at=store._time_text(store.utc_now() - timedelta(seconds=1)))]
        for mutate in mutations:
            value = json.loads(json.dumps(original))
            mutate(value)
            path.write_text(json.dumps(value))
            code, charge, send, write = self.run_fake()
            self.assertEqual(code, 2)
            charge.assert_not_called()
            send.assert_not_called()
            write.assert_called_once_with(1, b'provider-smoke status=metadata provider=librarything http=0 count=0 reason=metadata content_type=unavailable\n')

    def test_wrong_provider_rejected_before_store_IO(self):
        for provider in ('hardcover', 'tmdb', 'other'):
            with patch.object(store, 'open_store') as opener, patch.object(smoke.os, 'write') as write:
                self.assertEqual(smoke.run_live(store, provider), 2)
                opener.assert_not_called()
                write.assert_not_called()

    def test_wrong_provider_handle_refuses_before_read(self):
        with FakeHandleContext(self.fake.path) as handle, patch.object(store, 'open_store', return_value=handle), \
                patch.object(smoke.resource, 'setrlimit'), patch.object(store, 'read_record') as read, \
                patch.object(smoke.os, 'write'), patch.object(smoke, '_send') as send:
            self.assertEqual(smoke.run_live(store, 'librarything'), 2)
            read.assert_not_called()
            send.assert_not_called()


class FakeHandleContext:
    def __init__(self, path):
        self.handle = store._fixture_store(path, provider='tmdb')

    def __enter__(self):
        return self.handle

    def __exit__(self, *args):
        if self.handle.fd >= 0:
            self.handle.close()
