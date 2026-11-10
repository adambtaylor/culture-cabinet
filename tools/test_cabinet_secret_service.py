"""Owned dummy records only; never contact the user's keyring in these tests."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import unittest

from cryptography.fernet import Fernet

SPEC = importlib.util.spec_from_file_location('cabinet_secret_service',
    Path(__file__).with_name('cabinet_secret_service.py'))
store = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = store
SPEC.loader.exec_module(store)

DUMMY = 'NONCREDENTIAL-DUMMY-FOR-ENCRYPTED-STORE-ONLY'


class DummyCollection:
    """Own in-memory collection for engine faults, not live persistence proof."""
    def __init__(self):
        self.key, self.items = Fernet.generate_key(), {}
        self.state, self.loads = 'AVAILABLE', 0
        self.sets, self.deletes, self.fail_after_set, self.fail_after_delete = 0, 0, None, False

    def available(self):
        store.require(self.state == 'AVAILABLE', self.state)
        store.require(self.key is not None)

    def master_key(self):
        self.available()
        self.loads += 1
        return self.key

    def get(self, kind, binding, slot):
        self.available()
        self.loads += 1
        return self.items.get((kind, binding, slot))

    def exists(self, kind, binding, slot):
        self.available()
        return (kind, binding, slot) in self.items

    def set(self, kind, binding, slot, value):
        self.available()
        self.items[kind, binding, slot] = value
        self.sets += 1
        if self.sets == self.fail_after_set:
            raise store.Refused()

    def delete(self, kind, binding, slot):
        self.available()
        self.items.pop((kind, binding, slot), None)
        self.deletes += 1
        if self.fail_after_delete:
            raise store.Refused()


def request(operation='put', *, previous=None, operation_id='10000000-0000-4000-8000-000000000001'):
    binding = {'installation_id': '20000000-0000-4000-8000-000000000001',
        'owner_id': 7, 'owner_revision': 1, 'provider': 'openai', 'purpose': 'inference',
        'connection_id_or_null': '30000000-0000-4000-8000-000000000001',
        'legacy_account_actor_profile_or_null': None}
    record = {'schema': 'cabinet-credential/v1', 'binding': deepcopy(binding),
        'config_revision_or_null': 1, 'generation': '40000000-0000-4000-8000-000000000001',
        'operation_id': operation_id, 'entered_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:00Z', 'expires_at_or_null': None,
        'scopes': [], 'secret': DUMMY}
    return {'version': store.VERSION, 'operation': operation, 'record_kind': 'credential',
        'binding': binding, 'expected_revision': previous, 'operation_id': operation_id,
        'record_if_put': record if operation == 'put' else None}


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.collection = DummyCollection()
        self.engine = store.Store(self.collection)

    def execute(self, value):
        # The same strict framing that the native pipe consumer uses.
        value = store.decode_frame(store.encode_frame(value))
        return store.decode_frame(store.encode_frame(self.engine.execute(value)))

    def test_put_fresh_engine_read_delete_and_revision_acknowledgements(self):
        put = request()
        result = self.execute(put)
        self.assertEqual((result['status'], result['operation_id'], result['record']),
                         ('AVAILABLE', put['operation_id'], None))
        self.assertIsNotNone(result['revision'])
        self.assertTrue(all(DUMMY.encode() not in token for token in self.collection.items.values()))
        # New engine has no cached cipher/record state; the collection owns it.
        self.engine = store.Store(self.collection)
        read = self.execute(request('read', previous=result['revision']))
        self.assertEqual(read['record']['secret'], DUMMY)
        self.assertEqual(read['revision'], result['revision'])
        delete = request('delete', previous=result['revision'],
                         operation_id='10000000-0000-4000-8000-000000000002')
        removed = self.execute(delete)
        self.assertEqual((removed['status'], removed['record']), ('REMOVED', None))
        self.assertEqual(self.collection.deletes, 1)
        reconcile = {**delete, 'operation': 'reconcile'}
        self.assertEqual(self.execute(reconcile), removed)

    def test_initial_create_lost_ack_reconciles_null_previous_revision_without_retry(self):
        self.collection.fail_after_set = 2  # Target record committed; acknowledgement lost.
        put = request()
        unknown = self.execute(put)
        self.assertEqual((unknown['status'], unknown['revision']), ('UNKNOWN', None))
        self.assertEqual(self.execute(request('read', previous='50000000-0000-4000-8000-000000000001'))['status'], 'UNKNOWN')
        reconciled = self.execute({**put, 'operation': 'reconcile', 'record_if_put': None})
        self.assertEqual(reconciled['status'], 'AVAILABLE')
        self.assertIsNotNone(reconciled['revision'])
        self.assertIsNone(reconciled['record'])
        # Reconcile finalized the receipt; it did not repeat the target write.
        self.assertEqual(self.collection.sets, 3)
        self.assertEqual(self.execute(request('read', previous=reconciled['revision']))['record']['secret'], DUMMY)

    def test_lost_delete_ack_reconciles_exact_operation_and_keeps_old_key_disabled(self):
        saved = self.execute(request())
        delete = request('delete', previous=saved['revision'],
                         operation_id='10000000-0000-4000-8000-000000000002')
        self.collection.fail_after_delete = True
        self.assertEqual(self.execute(delete)['status'], 'UNKNOWN')
        self.assertEqual(self.execute(request('read', previous=saved['revision']))['status'], 'UNKNOWN')
        reconciled = self.execute({**delete, 'operation': 'reconcile'})
        self.assertEqual(reconciled['status'], 'REMOVED')
        self.assertIsNone(reconciled['record'])
        self.assertEqual(self.collection.deletes, 1)

    def test_passive_availability_never_loads_or_decrypts_and_lock_blocks_use(self):
        self.execute(request())
        self.collection.loads = 0
        result = self.execute(request('availability'))
        self.assertEqual((result['status'], result['revision'], result['record']), ('AVAILABLE', None, None))
        self.assertEqual(self.collection.loads, 0)
        self.collection.state = 'LOCKED'
        self.assertEqual(self.execute(request('availability'))['status'], 'LOCKED')
        self.assertEqual(self.collection.loads, 0)


if __name__ == '__main__':
    unittest.main()
