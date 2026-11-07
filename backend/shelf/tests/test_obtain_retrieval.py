"""Synthetic SAB receipts and Hydra GET responses; no sockets or live files."""
from dataclasses import replace
import json
import tempfile
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from shelf import credential_store as store, obtain_acquisition as acquire
from shelf.acquisition_backend import _queue_id
from shelf.acquisition_contract import Blocked, ContractError, MAX_NZB_BYTES, PublicRequest
from shelf.acquisition_pipeline import AdapterUnavailable, QueueRejected, Snapshot
from shelf.nzb_parser import validate_nzb
from shelf.tests import test_acquisition_backend as backend_fixtures
from shelf.tests.test_acquisition_pipeline import saved_job
from shelf.tests.test_acquisition_transport import FakeHTTP, KEY, client

GUID = '-3173234321348194361'
JOB = '3459bf43-b3af-4ce0-b62c-5bf2b8111c84'  # Invented fixture; not the real job.
NZB = b'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><file subject="fiction">
<groups><group>synthetic.example</group></groups><segments>
<segment bytes="12345" number="1">opaque-private-message@example.invalid</segment>
</segments></file></nzb>'''


class ReceiptCompatibilityTests(TestCase):
    def test_legacy_and_canonical_uuid_single_receipt(self):
        for value in ('SABnzbd_nzo_fixture', JOB):
            self.assertEqual(_queue_id(json.dumps({'status': True, 'nzo_ids': [value]})), value)

    def test_malformed_multiple_inconsistent_and_duplicate_fields_stay_unknown(self):
        values = ['', JOB.upper(), JOB.replace('-', ''), '{' + JOB + '}', JOB + 'x', None, 1]
        receipts = [{'status': True, 'nzo_ids': [value]} for value in values]
        receipts += [{'status': True, 'nzo_ids': [JOB, JOB]}, {'status': True, 'nzo_ids': []},
                     {'status': False, 'nzo_ids': [JOB]}, {'status': 1, 'nzo_ids': [JOB]},
                     {'status': True, 'nzo_ids': [JOB], 'error': 'SECRET'},
                     {'status': False, 'error': 'SECRET', 'nzo_ids': [JOB]}]
        for payload in [json.dumps(row) for row in receipts] + [
                '{"status":false,"status":true,"nzo_ids":["' + JOB + '"]}', '{', 'null']:
            with self.subTest(payload=payload), self.assertRaises(AdapterUnavailable) as caught:
                _queue_id(payload)
            self.assertEqual(str(caught.exception), 'Enqueue outcome unknown')
        with self.assertRaises(QueueRejected):
            _queue_id('{"status":false,"error":"SECRET"}')


class NZBTests(TestCase):
    def test_both_exact_newzbin_namespaces_accept_synthetic_nzb(self):
        for namespace in (b'http://www.newzbin.com/DTD/2003/nzb',
                          b'https://www.newzbin.com/DTD/2003/nzb'):
            payload = NZB.replace(b'http://www.newzbin.com/DTD/2003/nzb', namespace)
            for album in (False, True):
                with self.subTest(namespace=namespace, album=album):
                    self.assertIsNone(validate_nzb(payload, album=album))

    def test_external_doctype_is_inert_and_message_ids_remain_private(self):
        # DTDs are not fetched even when their external identifiers are local paths.
        for payload in (NZB, NZB.replace(b'http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd', b'file:///fiction-private'),
                        NZB.replace(b'opaque-private-message@example.invalid', b'opaque&amp;private')):
            self.assertIsNone(validate_nzb(payload))

    def test_unrelated_or_inexact_namespaces_are_rejected_at_root_and_child(self):
        for namespace in (b'https://example.invalid/nzb', b'https://www.newzbin.com/DTD/2003/nzb/',
                          b'https://www.newzbin.com/DTD/2003/NZB', b'https://newzbin.com/DTD/2003/nzb',
                          b'https://www.newzbin.com.evil.invalid/DTD/2003/nzb'):
            for payload in (NZB.replace(b'http://www.newzbin.com/DTD/2003/nzb', namespace),
                            NZB.replace(b'<segments>', b'<segments xmlns="' + namespace + b'">')):
                for album in (False, True):
                    with self.subTest(namespace=namespace, album=album), self.assertRaises(ContractError):
                        validate_nzb(payload, album=album)

    def test_https_keeps_required_segment_fields_entities_and_album_exclusions(self):
        for namespace in (b'http://www.newzbin.com/DTD/2003/nzb',
                          b'https://www.newzbin.com/DTD/2003/nzb'):
            source = NZB.replace(b'http://www.newzbin.com/DTD/2003/nzb', namespace)
            body = source[source.index(b'<nzb'):].replace(b'opaque-private-message@example.invalid', b'&x;')
            invalid = [source.replace(b' number="1"', b''), source.replace(b' bytes="12345"', b''),
                       source.replace(b'number="1"', b'number="0"'),
                       source.replace(b'bytes="12345"', b'bytes="0"'),
                       source.replace(b'number="1"', b'number="9223372036854775808"'),
                       source.replace(b'opaque-private-message@example.invalid', b''),
                       source.replace(b'</segments>', b'<segment bytes="1" number="1">duplicate</segment></segments>'),
                       source.replace(b'<groups>', b'<unexpected><groups>'),
                       source.replace(b'<file subject="fiction">',
                                      b'<file ' + b' '.join(f'a{i}="x"'.encode() for i in range(17)) + b'>'),
                       b'<!DOCTYPE nzb [<!ENTITY x SYSTEM "file:///fiction">]>' + body,
                       b'<!DOCTYPE nzb [<!ENTITY x "secret">]>' + body]
            for payload in invalid:
                for album in (False, True):
                    with self.subTest(namespace=namespace, payload=payload, album=album), self.assertRaises(ContractError):
                        validate_nzb(payload, album=album)
            for subject in (b'album_passworded.flac', b'album-encrypted.flac', b'album.protected.flac',
                            b'album_discography.flac', b'album_pack.flac', b'setup.exe'):
                protected = source.replace(b'subject="fiction"', b'subject="' + subject + b'"')
                with self.assertRaises(ContractError):
                    validate_nzb(protected, album=True)
                validate_nzb(protected)
            for marker in (b'password', b'passworded', b'encrypted', b'protected'):
                protected = source.replace(b'<file', b'<head><meta type="' + marker + b'">fixture</meta></head><file')
                with self.assertRaises(ContractError):
                    validate_nzb(protected, album=True)
            for limit in ('MAX_ELEMENTS', 'MAX_SEGMENTS'):
                with patch('shelf.nzb_parser.' + limit, 0), self.assertRaises(ContractError):
                    validate_nzb(source)

    def test_entities_internal_dtd_errors_structure_and_bounds_rejected(self):
        bad = [b'<!DOCTYPE nzb [<!ENTITY x "SECRET">]><nzb>&x;</nzb>',
               b'<!DOCTYPE nzb []><nzb/>', b'<error code="100" description="SECRET"/>',
               b'<nzb/>', b'<nzb><file/></nzb>', NZB[:-5], NZB + b'<extra/>',
               NZB.replace(b'number="1"', b'number="0"'),
               NZB.replace(b'opaque-private-message@example.invalid', b'x'*1025),
               NZB.replace(b'opaque-private-message@example.invalid', b'&missing;'),
               NZB.replace(b'bytes="12345"', b'bytes="-1"'),
               NZB.replace(b'</segments>', b'<segment bytes="1" number="1">duplicate</segment></segments>'),
               b'x'*(MAX_NZB_BYTES+1), '<nzb/>'.encode('utf-16')]
        for payload in bad:
            with self.subTest(length=len(payload)), self.assertRaises(ContractError) as caught:
                validate_nzb(payload)
            self.assertNotIn('SECRET', str(caught.exception))
        with patch('shelf.nzb_parser.MAX_ELEMENTS', 3), self.assertRaises(ContractError):
            validate_nzb(NZB)
        with patch('shelf.nzb_parser.MAX_SEGMENTS', 0), self.assertRaises(ContractError):
            validate_nzb(NZB)


class HydraTransportTests(TestCase):
    def test_http200_https_namespace_nzb_uses_only_synthetic_transport(self):
        payload = NZB.replace(b'http://www.newzbin.com/DTD/2003/nzb',
                              b'https://www.newzbin.com/DTD/2003/nzb')
        http = FakeHTTP(payload, status=200, headers={'Content-Type': 'application/x-nzb'})
        with patch('socket.create_connection', side_effect=AssertionError('no network')):
            self.assertEqual(client(http).retrieve(GUID, credential=KEY), payload)
        self.assertTrue(http.closed)

    def test_exact_get_private_encoding_fixed_host_and_larger_than_search_limit(self):
        payload = NZB.replace(b'<groups>', b'<!--' + b'x'*70000 + b'--><groups>')
        http = FakeHTTP(payload, headers={'Content-Type': 'application/x-nzb; charset=utf-8',
                                         'Content-Length': str(len(payload))})
        self.assertEqual(client(http).retrieve(GUID, credential=KEY), payload)
        method, path, body, headers = http.calls[0]
        self.assertEqual((method, urlsplit(path).path, body), ('GET', '/api', None))
        self.assertEqual(parse_qs(urlsplit(path).query), {'t': ['get'], 'id': [GUID], 'apikey': [KEY]})
        self.assertEqual(http.connections, [('127.0.0.1', 5076, 2.0)])
        self.assertEqual(http.read_limit, MAX_NZB_BYTES + 1)
        self.assertNotIn(KEY, repr(PublicRequest('get', (('id', GUID),))))
        self.assertTrue(http.closed)

    def test_get_contract_rejects_url_noncanonical_or_out_of_range_ids_before_io(self):
        http = FakeHTTP(NZB)
        for value in ('http://127.0.0.1:5076/api', '-0', '+1', '01', str(2**63), str(-2**63-1), 1):
            with self.subTest(value=value), self.assertRaises(ContractError):
                client(http).retrieve(value, credential=KEY)
        self.assertEqual(http.connections, [])

    def test_get_refuses_redirects_compression_wrong_type_oversize_and_timeout_without_retry(self):
        cases = [FakeHTTP(NZB, status=302, headers={'Location': 'http://fiction.invalid/'}),
                 FakeHTTP(NZB, headers={'Content-Type': 'text/html'}),
                 FakeHTTP(NZB, headers={'Content-Type': 'application/x-nzb', 'Content-Encoding': 'gzip'}),
                 FakeHTTP(NZB, headers={'Content-Type': 'application/x-nzb', 'Content-Length': str(MAX_NZB_BYTES+1)}),
                 FakeHTTP(b'x'*(MAX_NZB_BYTES+1), headers={'Content-Type': 'application/x-nzb'}),
                 FakeHTTP(b'<error description="SECRET"/>', headers={'Content-Type': 'application/x-nzb'}),
                 FakeHTTP(failure=TimeoutError('SECRET'))]
        for http in cases:
            with self.subTest(status=http.status), self.assertRaises(ContractError) as caught:
                client(http).retrieve(GUID, credential=KEY)
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertEqual(len(http.calls), 1)
            self.assertTrue(http.closed)


class HydraBackendTests(TestCase):
    setUp = backend_fixtures.BackendTests.setUp

    def test_uuid_acknowledgement_persists_and_inconsistent_receipt_never_retries(self):
        for receipt, expected in (({'status': True, 'nzo_ids': [JOB]}, 'queued'),
                ({'status': True, 'nzo_ids': [JOB, JOB]}, 'outcome_unknown'),
                ({'status': False, 'nzo_ids': [JOB]}, 'outcome_unknown')):
            self.http.calls.clear()
            self.http.payload = json.dumps(receipt).encode()
            with tempfile.TemporaryDirectory() as folder:
                pipeline, evidence = backend_fixtures.BackendTests.pipeline(self, folder)
                result = pipeline.dispatch(evidence)
                self.assertEqual(result.state, expected)
                if expected == 'queued':
                    self.assertEqual(result.job.job_id, JOB)
                again, evidence = backend_fixtures.BackendTests.pipeline(self, folder)
                self.assertEqual(again.dispatch(evidence).state, expected)
            self.assertEqual(len(self.http.calls), 1)

    def exact(self):
        return acquire.ExactResult(Snapshot.from_saved_job(saved_job()), JOB, GUID, 'a'*64, 12345)

    def test_real_retriever_uses_existing_private_store_and_exact_binding(self):
        self.http.payload = NZB
        self.http.headers = {'Content-Type': 'application/x-nzb'}
        exact = self.exact()
        result = acquire.Retriever(self.backend).retrieve(exact)
        self.assertEqual(result.result, exact)
        self.assertEqual(result.payload, NZB)
        self.assertEqual(len(result.sha256(exact)), 64)
        self.assertEqual(self.opened, ['hydra'])
        self.assertNotIn('opaque-private', repr(result))

    def test_expired_or_wrong_ownership_never_releases_key(self):
        with patch.object(store, 'utc_now', return_value=self.grants['hydra'].expires_at):
            with self.assertRaises(Blocked):
                acquire.Retriever(self.backend).retrieve(self.exact())
        with self.assertRaises(Blocked):
            acquire.Retriever(self.backend).retrieve(replace(self.exact(),
                snapshot=replace(self.exact().snapshot, actor_id=12)))
        self.assertEqual(self.opened, [])
        self.assertEqual(self.http.calls, [])
