"""Fictional legacy contract checks; no account, key or provider execution."""
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib
import json
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qsl

from shelf import tastedive_contract as contract
from shelf.disclosure import canonical_bytes


class TasteDiveContractTests(TestCase):
    def operation(self):
        return contract.TasteDiveOperation('movie', 'Synthetic Lantern', 'book')

    def reply(self, value, operation=None):
        return replace(contract.synthetic_tastedive_fixture(operation or self.operation()),
                       body=canonical_bytes(value))

    def test_public_operation_encodes_one_typed_literal_without_credentials_or_url(self):
        operation = contract.TasteDiveOperation('movie', 'Synthetic & ? + / 雪', 'book')
        self.assertEqual(operation.public_query,
            'q=movie%3ASynthetic+%26+%3F+%2B+%2F+%E9%9B%AA&type=book&limit=5&slimit=1&info=0')
        expected = [('q', 'movie:Synthetic & ? + / 雪'), ('type', 'book'),
                    ('limit', '5'), ('slimit', '1'), ('info', '0')]
        self.assertEqual(parse_qsl(operation.public_query), expected)
        material = operation.material
        self.assertEqual(set(material), {'method', 'origin', 'path', 'parameters',
                                         'body_digest', 'authentication_slot'})
        self.assertEqual((material['method'], material['origin'], material['path']),
                         ('GET', 'https://tastedive.com', '/api/similar'))
        self.assertEqual(material['parameters'], [list(pair) for pair in expected])
        self.assertEqual(material['body_digest'], sha256(b'').hexdigest())
        self.assertEqual(material['authentication_slot'], 'private-query-k')
        self.assertEqual(operation.operation_digest, sha256(json.dumps(material,
            sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest())
        self.assertNotIn('k', dict(parse_qsl(operation.public_query)))
        self.assertFalse(hasattr(operation, 'url'))
        self.assertFalse(hasattr(operation, 'credential'))
        material['parameters'][0][1] = 'CALLER MUTATION'
        self.assertEqual(dict(operation.material['parameters'])['q'], expected[0][1])

    def test_ambiguous_seed_syntax_and_unsupported_types_refuse_without_rewriting(self):
        for title in ('Synthetic,book:injected', 'Synthetic: subtitle', '', '   ',
                      'x' * 201, 'Synthetic\nquery', 'Synthetic\x85query', '\ud800'):
            with self.subTest(title=repr(title)), self.assertRaises(contract.TasteDiveContractError):
                contract.TasteDiveOperation('movie', title, 'book')
        for field in ('seed_type', 'result_type'):
            for kind in ('music', 'podcast', 'Movie', None, True):
                with self.subTest(field=field, kind=kind), self.assertRaises(contract.TasteDiveContractError):
                    replace(self.operation(), **{field: kind})
        with self.assertRaises(TypeError):
            contract.TasteDiveOperation('movie', 'Synthetic', 'book', url='https://foreign.invalid')

    def test_cross_category_names_order_and_original_observation_stay_unresolved(self):
        operation = self.operation()
        original = datetime(2026, 1, 1, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        value = {'Similar': {'Info': [{'Name': 'Provider interpretation', 'Type': 'show'}],
            'Results': [{'Name': name, 'Type': 'book'} for name in ('  Écho 雪  ', 'Echo', 'Echo')]}}
        # Preserve exact input bytes in the response digest, not a reserialization.
        reply = replace(self.reply(value), body=json.dumps(value, indent=2).encode(), observed_at=original)
        batch = contract.normalize_synthetic_tastedive(reply, operation)
        self.assertEqual([(item.name, item.item_type) for item in batch.matched_inputs],
                         [('Provider interpretation', 'show')])
        self.assertEqual([(item.name, item.item_type, item.provider_position) for item in batch.suggestions],
                         [('  Écho 雪  ', 'book', 1), ('Echo', 'book', 2), ('Echo', 'book', 3)])
        self.assertEqual(batch.observed_at, original)
        self.assertEqual(batch.operation_digest, operation.operation_digest)
        self.assertEqual(batch.response_digest, sha256(reply.body).hexdigest())
        self.assertEqual(batch.evidence_kind, 'synthetic_fixture_not_live')
        self.assertFalse(batch.model_eligible)
        self.assertFalse(batch.native_eligible)
        self.assertEqual(set(asdict(batch.suggestions[0])), {'name', 'item_type', 'provider_position'})
        for obj in (batch, *batch.suggestions, *batch.matched_inputs):
            for name in ('source_id', 'key', 'source_url', 'rights', 'records', 'body'):
                self.assertFalse(hasattr(obj, name))
        self.assertNotIn('Écho', repr(batch))
        self.assertNotIn('Écho', repr(reply))
        with self.assertRaises(FrozenInstanceError):
            batch.suggestions = ()

    def test_empty_bounded_response_and_fixture_limits(self):
        operation = self.operation()
        reply = contract.synthetic_tastedive_fixture(operation, count=0)
        batch = contract.normalize_synthetic_tastedive(reply, operation)
        self.assertEqual(batch.suggestions, ())
        self.assertEqual(len(batch.matched_inputs), 1)
        empty = self.reply({'Similar': {'Info': [], 'Results': []}})
        self.assertEqual(contract.normalize_synthetic_tastedive(empty, operation).matched_inputs, ())
        full = contract.synthetic_tastedive_fixture(operation, count=5)
        self.assertEqual(len(contract.normalize_synthetic_tastedive(full, operation).suggestions), 5)
        for count in (-1, 6, True):
            with self.subTest(count=count), self.assertRaises(contract.TasteDiveContractError):
                contract.synthetic_tastedive_fixture(operation, count=count)

    def test_operation_binding_requested_type_and_fixture_provenance_are_required(self):
        operation = self.operation()
        reply = contract.synthetic_tastedive_fixture(operation)
        with self.assertRaisesRegex(contract.TasteDiveContractError, '^OPERATION_MISMATCH$'):
            contract.normalize_synthetic_tastedive(reply, replace(operation, seed_title='Different synthetic seed'))
        wrong_type = self.reply({'Similar': {'Info': [], 'Results': [{'Name': 'Fictional show', 'Type': 'show'}]}})
        with self.assertRaisesRegex(contract.TasteDiveContractError, '^RESULT_TYPE_MISMATCH$'):
            contract.normalize_synthetic_tastedive(wrong_type, operation)
        for changes in ({'evidence_kind': 'live_lookup'}, {'observed_at': datetime(2026, 1, 1)},
                        {'operation_digest': 'not-a-digest'}, {'body': 'not-bytes'}):
            with self.subTest(changes=changes), self.assertRaises(contract.TasteDiveContractError):
                replace(reply, **changes)
        with self.assertRaises(contract.TasteDiveContractError):
            contract.normalize_synthetic_tastedive(reply.body, operation)

    def test_duplicate_nonfinite_invalid_and_oversized_json_are_rejected(self):
        operation = self.operation()
        fixture = contract.synthetic_tastedive_fixture(operation)
        for body in (b'{"Similar":{"Info":[],"Results":[]},"Similar":{}}',
                     b'{"Similar":{"Info":[],"Results":[],"Results":[]}}',
                     b'{"Similar":{"Info":[],"Results":[{"Name":"A","Name":"B","Type":"book"}]}}',
                     b'{"Similar":{"Info":[],"Results":NaN}}',
                     b'{"Similar":{"Info":[],"Results":1e999}}', b'[]', b'null', b'\xff', b'{'):
            with self.subTest(body=body), self.assertRaises(contract.TasteDiveContractError):
                contract.normalize_synthetic_tastedive(replace(fixture, body=body), operation)
        with self.assertRaisesRegex(contract.TasteDiveContractError, '^REPLY_SIZE_INVALID$'):
            replace(fixture, body=b' ' * (128 * 1024 + 1))

    def test_closed_envelopes_lists_and_display_names_reject_rich_or_malformed_rows(self):
        operation = self.operation()
        valid = {'Name': 'Synthetic', 'Type': 'book'}
        malformed = [
            {'Error': 'provider error'}, {'Similar': {'Info': [], 'Results': [], 'Other': []}},
            {'Similar': {'Info': [], 'Results': [valid]}, 'Other': None},
            {'Similar': {'Info': [valid, valid], 'Results': []}},
            {'Similar': {'Info': [], 'Results': [valid] * 6}},
            {'Similar': {'Info': {}, 'Results': []}},
        ]
        for row in ({'Name': 'Synthetic'}, {**valid, 'wUrl': 'https://foreign.invalid'},
                    {'Name': 7, 'Type': 'book'}, {'Name': 'x' * 201, 'Type': 'book'},
                    {'Name': 'Synthetic\nname', 'Type': 'book'}, {'Name': '', 'Type': 'book'},
                    {'Name': 'Synthetic', 'Type': 'music'}):
            malformed.append({'Similar': {'Info': [], 'Results': [row]}})
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(contract.TasteDiveContractError):
                contract.normalize_synthetic_tastedive(self.reply(value), operation)

    def test_import_and_normalization_do_not_touch_network_credentials_or_native_registration(self):
        from shelf.source_adapters import SOURCES
        from shelf.source_authority import NATIVE_SETUP
        sources, setup = tuple(SOURCES), dict(NATIVE_SETUP)
        with ExitStack() as stack:
            for target in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen',
                           'shelf.credential_store.open_store', 'shelf.credential_store.read_record',
                           'shelf.secret_leases.MemoryCredentials.describe',
                           'shelf.secret_leases.MemoryCredentials.ephemeral',
                           'shelf.source_authority.configure_source',
                           'shelf.source_evidence.SourceCard', 'shelf.source_evidence.build_evidence',
                           'shelf.source_adapters.SourceReply', 'shelf.source_adapters.SourcePermit'):
                stack.enter_context(patch(target, side_effect=AssertionError('No external or native effect')))
            importlib.reload(contract)
            operation = self.operation()
            batch = contract.normalize_synthetic_tastedive(contract.synthetic_tastedive_fixture(operation), operation)
            self.assertFalse(batch.native_eligible)
            self.assertFalse(batch.model_eligible)
            self.assertEqual(tuple(SOURCES), sources)
            self.assertEqual(NATIVE_SETUP, setup)
