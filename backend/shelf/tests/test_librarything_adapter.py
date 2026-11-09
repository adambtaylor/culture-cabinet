"""Owned invented responses only; no actual LibraryThing content or I/O."""
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from unittest import TestCase
from unittest.mock import patch

from shelf import librarything_adapter as adapter
from shelf.disclosure import canonical_bytes


class LibraryThingAdapterTests(TestCase):
    def reply(self, value):
        return replace(adapter.synthetic_librarything_fixture(), body=canonical_bytes(value))

    def test_closed_operation_is_public_descriptor_not_key_or_executable_request(self):
        operation = adapter.LibraryThingOperation()
        expected = {'source': 'librarything', 'operation': 'multirecommendations_v2_diagnostic',
            'method': 'GET', 'origin': 'https://www.librarything.com',
            'path': '/api/multirecommendations.php',
            'parameters': [['v', '2'], ['isbns', '0300178727'], ['maxItems', '5'],
                           ['maxPerAuthor', '2'], ['showtitles', 'false']]}
        self.assertEqual(operation.material, expected)
        self.assertEqual(operation.operation_digest, sha256(json.dumps(expected, sort_keys=True,
            separators=(',', ':'), ensure_ascii=False).encode()).hexdigest())
        self.assertNotIn('apiKey', json.dumps(operation.material))
        self.assertFalse(hasattr(operation, 'url'))
        changed = operation.material
        changed['parameters'][1][1] = 'CALLER-CHANGE'
        self.assertEqual(operation.material, expected)

    def test_normalized_fixture_zero_to_five_keeps_original_provenance_without_native_claim(self):
        for count in range(6):
            with self.subTest(count=count):
                reply = adapter.synthetic_librarything_fixture(count)
                result = adapter.normalize_synthetic_librarything(reply)
                self.assertEqual((result.status, result.reason), ('ok', 'none'))
                self.assertEqual(len(result.suggestions), count)
                self.assertEqual(result.operation_digest, reply.operation_digest)
                self.assertEqual(result.response_digest, sha256(reply.body).hexdigest())
                self.assertEqual(result.observed_at, datetime(2026, 1, 1, tzinfo=timezone.utc))
                self.assertEqual(result.evidence_kind, 'synthetic_fixture_not_live')
                self.assertFalse(result.model_eligible)
                self.assertFalse(hasattr(result, 'body'))
                self.assertFalse(hasattr(result, 'records'))
                for row in result.suggestions:
                    self.assertFalse(hasattr(row, 'title'))
                    self.assertFalse(hasattr(row, 'source_url'))
                with self.assertRaises(FrozenInstanceError):
                    result.status = 'live'

    def test_work_id_normalization_is_deterministic_and_rank_is_only_reported_data(self):
        reply = adapter.synthetic_librarything_fixture()
        payload = json.loads(reply.body)
        payload['recommendations'][0].update(work='000700', fromworks=['0999', 999, 998])
        reversed_payload = {**payload, 'recommendations': list(reversed(payload['recommendations']))}
        one = adapter.normalize_synthetic_librarything(self.reply(payload))
        two = adapter.normalize_synthetic_librarything(self.reply(reversed_payload))
        self.assertEqual(one.suggestions, two.suggestions)
        self.assertEqual(one.suggestions[0].work_id, '700')
        self.assertEqual(one.suggestions[0].from_work_ids, ('998', '999'))
        self.assertEqual(one.suggestions[0].reported_rank, 1)
        payload['recommendations'][1]['work'] = 700
        with self.assertRaisesRegex(adapter.LibraryThingAdapterError, '^DUPLICATE_WORK$'):
            adapter.normalize_synthetic_librarything(self.reply(payload))

    def test_operation_echo_and_fixture_label_are_not_borrowable_provenance(self):
        reply = adapter.synthetic_librarything_fixture()
        with self.assertRaisesRegex(adapter.LibraryThingAdapterError, '^OPERATION_MISMATCH$'):
            adapter.normalize_synthetic_librarything(replace(reply, operation_digest='0' * 64))
        payload = json.loads(reply.body)
        payload['request'][0]['isbn'] = 'OTHER-UNRELATED-REQUEST'
        with self.assertRaisesRegex(adapter.LibraryThingAdapterError, '^REQUEST_PROVENANCE_MISMATCH$'):
            adapter.normalize_synthetic_librarything(self.reply(payload))
        with self.assertRaisesRegex(adapter.LibraryThingAdapterError, '^SYNTHETIC_REPLY_REQUIRED$'):
            replace(reply, evidence_kind='live_lookup')
        for value in (reply.body, {}, None, {'evidence_kind': adapter.FIXTURE_KIND}):
            with self.subTest(type=type(value)), self.assertRaises(adapter.LibraryThingAdapterError):
                adapter.normalize_synthetic_librarything(value)

    def test_wrong_shapes_return_safe_diagnostic_failure_without_response_content(self):
        valid = json.loads(adapter.synthetic_librarything_fixture().body)
        cases = [None, [], {}, {'request': [], 'recommendations': []},
                 {**valid, 'recommendations': 'not-a-list'}]
        for field, value in [('rank', True), ('rank', 6), ('work', False),
                             ('fromworks', [False]), ('isbns', ['x' * 33]), ('upcs', {})]:
            payload = json.loads(json.dumps(valid))
            payload['recommendations'][0][field] = value
            cases.append(payload)
        for payload in cases:
            with self.subTest(payload=payload):
                result = adapter.normalize_synthetic_librarything(self.reply(payload))
                self.assertEqual(result.status, 'bad_response')
                self.assertEqual(result.suggestions, ())

    def test_json_and_size_boundaries_do_not_leak_parser_errors(self):
        reply = adapter.synthetic_librarything_fixture()
        for body in (b'\xff', b'{', b'{"errors":NaN}',
                     b'{"errorCode":"aut00001","errorCode":"CANARY"}'):
            result = adapter.normalize_synthetic_librarything(replace(reply, body=body))
            self.assertEqual((result.status, result.suggestions), ('bad_response', ()))
            self.assertNotIn('CANARY', repr(result))
        with self.assertRaisesRegex(adapter.LibraryThingAdapterError, '^SYNTHETIC_REPLY_REQUIRED$'):
            replace(reply, body=b'x' * (adapter.MAX_BODY + 1))

    def test_known_errors_empty_success_and_unselected_text_remain_distinct_and_redacted(self):
        for code, status, reason in [('aut00001', 'auth', 'api_auth'),
                                    ('bnf00003', 'no_recommendations', 'api_no_recommendations'),
                                    ('bnf00005', 'invalid_seed', 'api_invalid_seed'),
                                    ('SYNTHETIC_SECRET_CANARY', 'bad_response', 'api_unknown')]:
            result = adapter.normalize_synthetic_librarything(self.reply(
                {'errorCode': code, 'message': 'SYNTHETIC_SECRET_CANARY'}))
            self.assertEqual((result.status, result.reason, result.suggestions), (status, reason, ()))
            self.assertNotIn('SYNTHETIC_SECRET_CANARY', json.dumps(asdict(result), default=str))
        payload = json.loads(adapter.synthetic_librarything_fixture().body)
        payload['recommendations'][0].update(title='SYNTHETIC_UNSELECTED_TEXT',
            description='SYNTHETIC_UNSELECTED_TEXT', author_id='SYNTHETIC_UNSELECTED_TEXT')
        result = adapter.normalize_synthetic_librarything(self.reply(payload))
        self.assertNotIn('SYNTHETIC_UNSELECTED_TEXT', json.dumps(asdict(result), default=str))
        empty = adapter.normalize_synthetic_librarything(adapter.synthetic_librarything_fixture(0))
        self.assertEqual((empty.status, empty.reason, empty.suggestions), ('ok', 'none', ()))

    def test_use_specific_rights_never_turn_fixture_or_key_report_into_native_permission(self):
        fixture = adapter.librarything_use_decision('synthetic_contract')
        self.assertEqual((fixture.allowed, fixture.code), (True, 'FIXTURE_CONTRACT_ONLY'))
        for use in ('model_context', 'prompt', 'embedding', 'grounding'):
            decision = adapter.librarything_use_decision(use)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.code, 'LIBRARYTHING_AI_WRITTEN_PERMISSION_REQUIRED')
        self.assertEqual(adapter.librarything_use_decision('diagnostic').code,
            'ACTUAL_DIAGNOSTIC_CUSTODY_AND_EXECUTION_UNPROVED')
        for use in ('native_collection', 'fetch', 'display', 'retain', 'cache', 'export', 'redistribute'):
            decision = adapter.librarything_use_decision(use)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.code, 'NATIVE_OPERATION_RIGHTS_AND_BINDING_UNAVAILABLE')
        for value in (None, {}, True, 'SSH-key-provided', 'approved'):
            with self.subTest(value=value), self.assertRaisesRegex(
                adapter.LibraryThingAdapterError, '^UNSUPPORTED_USE$'):
                adapter.librarything_use_decision(value)

    def test_fixture_clock_and_count_are_explicit_not_current_authority(self):
        reply = adapter.synthetic_librarything_fixture()
        for value in (None, '2026-01-01', datetime(2026, 1, 1),
                      datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))):
            with self.subTest(value=value), self.assertRaisesRegex(
                adapter.LibraryThingAdapterError, '^FIXTURE_CLOCK_INVALID$'):
                replace(reply, observed_at=value)
        for count in (True, -1, 6, '2'):
            with self.subTest(count=count), self.assertRaisesRegex(
                adapter.LibraryThingAdapterError, '^FIXTURE_COUNT_INVALID$'):
                adapter.synthetic_librarything_fixture(count)

    def test_pure_adapter_does_not_call_transport_credentials_quota_or_registration(self):
        with patch('shelf.librarything_smoke._send', side_effect=AssertionError('No transport')) as send, \
             patch('shelf.librarything_smoke._charge', side_effect=AssertionError('No quota')) as charge, \
             patch('shelf.credential_store.open_store', side_effect=AssertionError('No credential')) as store, \
             patch('subprocess.Popen', side_effect=AssertionError('No child')) as child:
            result = adapter.normalize_synthetic_librarything(adapter.synthetic_librarything_fixture())
            self.assertEqual(len(result.suggestions), 2)
            self.assertFalse(adapter.librarything_use_decision('native_collection').allowed)
            for operation in (send, charge, store, child):
                operation.assert_not_called()
