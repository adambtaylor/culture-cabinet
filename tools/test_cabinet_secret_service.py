"""Owned dummy records only; never contact the user's keyring in these tests."""
from copy import deepcopy
import base64
import importlib.util
import hashlib
import os
import json
from pathlib import Path
from types import SimpleNamespace
import struct
import subprocess
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

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
        self.failure_status = 'UNAVAILABLE'

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
            raise store.Refused(self.failure_status)

    def delete(self, kind, binding, slot):
        self.available()
        self.items.pop((kind, binding, slot), None)
        self.deletes += 1
        if self.fail_after_delete:
            raise store.Refused(self.failure_status)


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

    def test_replacement_pending_before_effect_never_falls_back_or_blindly_retries(self):
        saved = self.execute(request())
        replacement = request(previous=saved['revision'],
            operation_id='10000000-0000-4000-8000-000000000002')
        replacement['record_if_put']['secret'] = 'NONCREDENTIAL-REPLACEMENT'
        self.collection.fail_after_set = self.collection.sets + 1
        self.assertEqual(self.execute(replacement)['status'], 'UNKNOWN')
        writes = self.collection.sets
        self.assertEqual(self.execute(request('read', previous=saved['revision']))['status'], 'UNKNOWN')
        reconciled = self.execute({**replacement, 'operation': 'reconcile', 'record_if_put': None})
        self.assertEqual((reconciled['status'], reconciled['revision'], reconciled['record']), ('UNKNOWN', None, None))
        self.assertEqual(self.collection.sets, writes)
        wrong = {**replacement, 'operation': 'reconcile', 'record_if_put': None,
                 'expected_revision': '50000000-0000-4000-8000-000000000001'}
        self.assertEqual(self.execute(wrong)['status'], 'CONFLICT')

    def test_tamper_wrong_binding_duplicate_operation_and_rollback_refuse_without_secret(self):
        put = request()
        saved = self.execute(put)
        changed = deepcopy(put)
        changed['record_if_put']['secret'] = 'NONCREDENTIAL-DIFFERENT-INTENT'
        self.assertEqual(self.execute(changed)['status'], 'CONFLICT')
        wrong = request('read', previous='50000000-0000-4000-8000-000000000001')
        self.assertEqual(self.execute(wrong)['status'], 'CONFLICT')
        key = next(key for key in self.collection.items if key[-1] == 'record')
        original = self.collection.items[key]
        self.collection.items[key] = original[:20] + b'X' + original[21:]
        corrupt = self.execute(request('read', previous=saved['revision']))
        self.assertEqual((corrupt['status'], corrupt['record']), ('CORRUPT', None))
        self.collection.items[key] = original
        foreign = request('read', previous=saved['revision'])
        foreign['binding']['owner_id'] = 8
        digest = hashlib.sha256(store.canonical(foreign['binding'])).hexdigest()
        # Even matching lookup hints cannot authorize another owner's ciphertext.
        for oldkey, token in tuple(self.collection.items.items()):
            self.collection.items[oldkey[0], digest, oldkey[2]] = token
        self.assertEqual(self.execute(foreign)['status'], 'CORRUPT')
        replace_ = request(previous=saved['revision'],
            operation_id='10000000-0000-4000-8000-000000000002')
        newer = self.execute(replace_)
        self.collection.items[key] = original
        self.assertEqual(self.execute(request('read', previous=newer['revision']))['status'], 'CORRUPT')

    def test_missing_master_and_locked_backend_never_initialize_a_key(self):
        self.execute(request())
        original = dict(self.collection.items)
        self.collection.key = None
        result = self.execute(request('read', previous='50000000-0000-4000-8000-000000000001'))
        self.assertEqual((result['status'], result['record']), ('UNAVAILABLE', None))
        self.assertEqual(self.collection.items, original)
        self.collection.state = 'LOCKED'
        self.assertEqual(self.execute(request('availability'))['status'], 'LOCKED')

    def test_direct_delete_locked_is_pre_intent_zero_effect_for_this_invocation_only(self):
        saved = self.execute(request())
        before, writes, deletes = dict(self.collection.items), self.collection.sets, self.collection.deletes
        self.collection.state = 'LOCKED'
        deletion = request('delete', previous=saved['revision'],
                           operation_id='10000000-0000-4000-8000-000000000002')
        result = self.execute(deletion)
        self.assertEqual(result, {'version': store.VERSION, 'operation_id': deletion['operation_id'],
                                  'status': 'LOCKED', 'revision': None, 'record': None})
        self.assertEqual(self.collection.items, before)
        self.assertEqual((self.collection.sets, self.collection.deletes), (writes, deletes))
        self.assertEqual(store.DELETE_NO_EFFECT_GUARANTEE, 'direct-delete-locked/v1')
        # The native layer owns current authority and consume-before-IPC proof.
        # Only that explicit later command may send this exact request again.
        self.collection.state = 'AVAILABLE'
        removed = self.execute(deletion)
        self.assertEqual(removed['status'], 'REMOVED')
        self.assertEqual(self.collection.deletes, deletes + 1)
        self.assertEqual(self.execute({**deletion, 'operation': 'reconcile'}), removed)
        self.assertEqual(self.collection.deletes, deletes + 1)

    def test_post_intent_locked_label_is_unknown_including_delete_after_effect(self):
        saved = self.execute(request())
        deletion = request('delete', previous=saved['revision'],
                           operation_id='10000000-0000-4000-8000-000000000002')
        self.collection.failure_status = 'LOCKED'
        self.collection.fail_after_set = self.collection.sets + 1
        result = self.execute(deletion)
        self.assertEqual((result['status'], result['revision'], result['record']), ('UNKNOWN', None, None))
        self.assertEqual(self.execute({**deletion, 'operation': 'reconcile'})['status'], 'UNKNOWN')
        self.assertEqual(self.collection.deletes, 0)
        # A separate fixture covers a LOCKED-labelled failure after target
        # deletion. It still cannot supply a no-effect token.
        self.collection = DummyCollection(); self.engine = store.Store(self.collection)
        saved = self.execute(request())
        deletion['expected_revision'] = saved['revision']
        self.collection.failure_status, self.collection.fail_after_delete = 'LOCKED', True
        result = self.execute(deletion)
        self.assertEqual((result['status'], result['revision'], result['record']), ('UNKNOWN', None, None))
        self.assertEqual(self.collection.deletes, 1)
        self.assertEqual(self.execute({**deletion, 'operation': 'reconcile'})['status'], 'REMOVED')
        self.assertEqual(self.collection.deletes, 1)

    def test_later_direct_locked_does_not_erase_an_earlier_unknown_intent(self):
        saved = self.execute(request())
        deletion = request('delete', previous=saved['revision'],
                           operation_id='10000000-0000-4000-8000-000000000002')
        self.collection.fail_after_set = self.collection.sets + 1
        self.assertEqual(self.execute(deletion)['status'], 'UNKNOWN')
        writes = self.collection.sets
        self.collection.state = 'LOCKED'
        self.assertEqual(self.execute(deletion)['status'], 'LOCKED')
        self.assertEqual(self.collection.sets, writes)
        self.collection.state = 'AVAILABLE'
        # Native must not manufacture proof from that later locked result: the
        # original uncertain lineage remains pending, and reconcile is UNKNOWN.
        self.assertEqual(self.execute({**deletion, 'operation': 'reconcile'})['status'], 'UNKNOWN')
        self.assertEqual(self.collection.deletes, 0)

    def test_strict_frame_schema_and_utf8_secret_byte_limit(self):
        valid = request()
        frame = store.encode_frame(valid)
        self.assertEqual(struct.unpack('!I', frame[:4])[0], len(frame) - 4)
        for raw in (frame[:-1], frame + b'x', frame + frame, b'\x00\x00\x00\x00',
                    struct.pack('!I', store.MAX_FRAME + 1),
                    struct.pack('!I', 13) + b'{"a":1,"a":2}',
                    struct.pack('!I', 9) + b'{"a":NaN}'):
            with self.subTest(raw=raw[:20]), self.assertRaises(store.Refused):
                store.decode_frame(raw)
        for change in ({'operation': 'export'}, {'version': 'foreign'}, {'extra': 'value'},
                       {'operation_id': 'not-a-uuid'}):
            with self.subTest(change=change), self.assertRaises(store.Refused):
                store.validate_request({**valid, **change})
        value = deepcopy(valid)
        value['record_if_put']['secret'] = 'é' * 4096
        store.validate_request(value)
        value['record_if_put']['secret'] += 'é'
        with self.assertRaises(store.Refused):
            store.validate_request(value)
        for provider in ('arbitrary', 'openai_responses'):
            value = deepcopy(valid); value['binding']['provider'] = provider
            with self.assertRaises(store.Refused):
                store.validate_request(value)


class PrivateClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cabinet-DUMMY-IPC-')
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name) / 'dummy_child.py'
        self.processes, self.invocations = [], []
        self.real_popen = subprocess.Popen

    def fake_child(self, suffix):
        # Only this test substitutes its child. Production invoke has no path,
        # executable, collection or environment selector in its request/call.
        self.fixture.write_text('''import json,os,struct,sys,time
read_fd=int(sys.argv[2]); write_fd=int(sys.argv[4])
raw=b''
while True:
 part=os.read(read_fd,8192)
 if not part:break
 raw+=part
value=json.loads(raw[4:])
''' + suffix)
        def start(argv, **kwargs):
            self.invocations.append((list(argv), dict(kwargs)))
            process = self.real_popen([*argv[:3], str(self.fixture), *argv[4:]], **kwargs)
            self.processes.append(process)
            return process
        return patch.object(store.subprocess, 'Popen', side_effect=start)

    def test_fixed_child_private_pipe_response_and_no_standard_stream_secret(self):
        suffix = '''print(value['record_if_put']['secret'],flush=True)
reply={'version':value['version'],'operation_id':value['operation_id'],'status':'AVAILABLE',
       'revision':'50000000-0000-4000-8000-000000000001','record':None}
raw=json.dumps(reply).encode();os.write(write_fd,struct.pack('!I',len(raw))+raw)
'''
        with self.fake_child(suffix):
            result = store.invoke(request())
        self.assertEqual(result['status'], 'AVAILABLE')
        argv, options = self.invocations[0]
        self.assertEqual(argv[:4], ['/usr/bin/python3', '-I', '-B', store.HELPER])
        self.assertNotIn(DUMMY, repr(argv) + repr(options['env']))
        self.assertEqual(options['stdout'], subprocess.DEVNULL)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(len(options['pass_fds']), 2)
        self.assertEqual(set(options['env']), {'PATH', 'LANG', 'DBUS_SESSION_BUS_ADDRESS'})
        self.assertEqual(options['env']['DBUS_SESSION_BUS_ADDRESS'], 'unix:path=/run/user/1000/bus')
        self.assertEqual(self.processes[0].returncode, 0)

    def test_timeout_kills_and_reaps_without_mutation_retry(self):
        with self.fake_child('time.sleep(5)\n'):
            started = time.monotonic()
            result = store.invoke(request(), timeout=.1)
        self.assertEqual((result['status'], result['record']), ('UNKNOWN', None))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(self.processes), 1)
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertLess(self.processes[0].returncode, 0)

    def test_child_death_trailing_frame_and_false_read_ack_refuse(self):
        for suffix in ('os._exit(1)\n', 'os.write(write_fd,b"\\x00\\x00\\x00\\x02{}x")\n',
                       '''reply={'version':value['version'],'operation_id':value['operation_id'],
                       'status':'AVAILABLE','revision':value['expected_revision'],'record':None}
raw=json.dumps(reply).encode();os.write(write_fd,struct.pack('!I',len(raw))+raw)
'''):
            with self.subTest(suffix=suffix), self.fake_child(suffix):
                result = store.invoke(request('read', previous='50000000-0000-4000-8000-000000000001'))
                self.assertEqual((result['status'], result['record']), ('UNAVAILABLE', None))
        self.assertTrue(all(process.returncode is not None for process in self.processes))

    def test_partial_pipe_allocation_failure_closes_owned_descriptors(self):
        original, allocated = os.pipe, []
        def fail_second():
            if allocated:
                raise OSError('OWNED DUMMY ALLOCATION FAILURE')
            allocated.extend(original())
            return tuple(allocated)
        with patch.object(store.os, 'pipe', side_effect=fail_second), \
             patch.object(store.subprocess, 'Popen') as child:
            result = store.invoke(request())
        self.assertEqual((result['status'], result['record']), ('UNKNOWN', None))
        child.assert_not_called()
        for fd in allocated:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_malformed_locked_or_lost_direct_delete_reply_is_unknown_never_proof(self):
        deletion = request('delete', previous='50000000-0000-4000-8000-000000000001')
        suffix = '''reply={'version':value['version'],'operation_id':value['operation_id'],
 'status':'LOCKED','revision':'60000000-0000-4000-8000-000000000001','record':None}
raw=json.dumps(reply).encode();os.write(write_fd,struct.pack('!I',len(raw))+raw)
'''
        with self.fake_child(suffix):
            result = store.invoke(deletion)
        self.assertEqual((result['status'], result['revision'], result['record']), ('UNKNOWN', None, None))
        with self.fake_child('os._exit(1)\n'):
            result = store.invoke(deletion)
        self.assertEqual((result['status'], result['revision'], result['record']), ('UNKNOWN', None, None))


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cabinet-DUMMY-HEADER-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
        self.identifier = 'CultureCabinet' + '1' * 32
        self.file = self.directory / (self.identifier + '.keyring')
        self.file.write_bytes(store.ENCRYPTED_PREFIX + b'OWNED DUMMY BODY MUST NOT BE READ')
        self.file.chmod(0o600)
        self.selection = store._new_selection('10000000-0000-4000-8000-000000000001',
            SimpleNamespace(owner=':1.2', connection=SimpleNamespace(get_unique_name=lambda: ':1.3')), None)
        self.selection.update(state='ACTIVE', requested_label='CultureCabinet' + '2' * 32,
            returned_collection_ref=store.COLLECTION_ROOT + self.identifier,
            decoded_identifier=self.identifier, verified_filename=self.identifier + '.keyring',
            canary_ref=store.COLLECTION_ROOT + self.identifier + '/1', canary_state='DELETED',
            master_phase='PRESENT', keyring_directory=str(self.directory),
            file_binding={'before': None, 'created': list(range(9)), 'deleted': list(range(1, 10))})
        self.enterContext(patch.object(store, 'KEYRING_DIRECTORY', str(self.directory)))
        # Temporary fixture parents are intentionally not a host trust path.
        # Only this unit fixture substitutes the checked directory opener.
        self.enterContext(patch.object(store, '_open_checked_directory',
            side_effect=lambda path: os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)))

    def test_only_twenty_prefix_bytes_are_read_and_completed_atomic_replacement_is_supported(self):
        original = os.pread
        with patch.object(store.os, 'pread', wraps=original) as reads:
            store._probe_protection(self.selection)
        reads.assert_called_once()
        self.assertEqual(reads.call_args.args[1:], (20, 0))
        replacement = self.directory / 'owned-replacement'
        replacement.write_bytes(store.ENCRYPTED_PREFIX + b'DIFFERENT OWNED DUMMY BODY')
        replacement.chmod(0o600)
        os.replace(replacement, self.file)
        store._probe_protection(self.selection)

    def test_textual_short_unknown_version_and_protected_to_empty_transition_refuse(self):
        store._probe_protection(self.selection)
        for prefix in (b'[keyring]\n', store.ENCRYPTED_PREFIX[:19], store.ENCRYPTED_PREFIX[:16] + b'\x01\x00\x00\x00'):
            with self.subTest(prefix_length=len(prefix)):
                self.file.write_bytes(prefix)
                with self.assertRaises(store.Refused) as error:
                    store._probe_protection(self.selection)
                self.assertEqual(error.exception.status, 'UNAVAILABLE')

    def test_selection_requires_new_exact_mapping_and_provisioning_provenance(self):
        for changes in ({'decoded_identifier': 'login'}, {'returned_collection_ref': self.selection['returned_collection_ref'] + '_1'},
                        {'keyring_directory': '/foreign'}, {'protection': True}, {'state': 'UNPROVED'}):
            with self.subTest(changes=changes), self.assertRaises(store.Refused):
                store._probe_protection({**self.selection, **changes})
        for changes in ({'caller': 'not-unique'}, {'service_uid': 0},
                        {'implementation': 'gnome-keyring-foreign'}, {'service_owner': 'not-unique'}):
            altered = deepcopy(self.selection)
            altered['service_provenance'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(store.Refused):
                store._probe_protection(altered)

    def test_symlink_mode_hardlink_and_changed_during_observation_refuse(self):
        self.file.chmod(0o644)
        with self.assertRaises(store.Refused):
            store._probe_protection(self.selection)
        self.file.chmod(0o600)
        link = self.directory / 'own-hardlink'
        os.link(self.file, link)
        with self.assertRaises(store.Refused):
            store._probe_protection(self.selection)
        link.unlink()
        self.file.rename(link)
        self.file.symlink_to(link)
        with self.assertRaises(store.Refused):
            store._probe_protection(self.selection)
        self.file.unlink(); link.rename(self.file)
        original = os.pread
        def replaced(fd, count, offset):
            prefix = original(fd, count, offset)
            temporary = self.directory / 'changed-during-read'
            temporary.write_bytes(store.ENCRYPTED_PREFIX)
            temporary.chmod(0o600)
            os.replace(temporary, self.file)
            return prefix
        with patch.object(store.os, 'pread', side_effect=replaced), self.assertRaises(store.Refused):
            store._probe_protection(self.selection)

    def test_changed_service_refuses_before_file_probe_and_no_permanent_protected_override(self):
        backend = object.__new__(store._SecretBackend)
        backend.selection = self.selection
        with patch.object(backend, '_verify_service', side_effect=store.Refused()), \
             patch.object(store, '_probe_protection') as probe, self.assertRaises(store.Refused):
            backend._prove()
        probe.assert_not_called()
        self.assertNotIn('protection', self.selection)

    def test_missing_wrong_owner_and_metadata_change_refuse_without_body_read(self):
        original_stat = os.stat
        def wrong_owner(name, *args, **kwargs):
            info = original_stat(name, *args, **kwargs)
            if name == self.identifier + '.keyring':
                values = {key: getattr(info, key) for key in ('st_dev', 'st_ino', 'st_uid', 'st_gid',
                    'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
                return SimpleNamespace(**{**values, 'st_uid': 0})
            return info
        with patch.object(store.os, 'stat', side_effect=wrong_owner), \
             patch.object(store.os, 'pread') as read, self.assertRaises(store.Refused):
            store._probe_protection(self.selection)
        read.assert_not_called()
        self.file.unlink()
        with patch.object(store.os, 'pread') as read, self.assertRaises(store.Refused):
            store._probe_protection(self.selection)
        read.assert_not_called()
        self.file.write_bytes(store.ENCRYPTED_PREFIX)
        self.file.chmod(0o600)
        original_read = os.pread
        def changed(fd, count, offset):
            prefix = original_read(fd, count, offset)
            with self.file.open('ab') as fixture:
                fixture.write(b'OWNED-DUMMY-MUTATION')
            return prefix
        with patch.object(store.os, 'pread', side_effect=changed), self.assertRaises(store.Refused):
            store._probe_protection(self.selection)


class WorkerPostProtectionTests(unittest.TestCase):
    def run_worker(self, value, collection):
        from contextlib import nullcontext
        request_read, request_write = os.pipe()
        response_read, response_write = os.pipe()
        try:
            os.write(request_write, store.encode_frame(value))
            os.close(request_write); request_write = None
            with patch.object(store, '_SecretBackend', return_value=collection), \
                 patch.object(store, 'mutation_lock', side_effect=lambda deadline: nullcontext()):
                store._main([store.HELPER, '--request-fd', str(request_read), '--response-fd', str(response_write)])
            request_read = response_write = None
            return store._pipe_read(response_read, time.monotonic() + 1)
        finally:
            for fd in (request_read, request_write, response_read, response_write):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_post_mutation_protection_loss_is_unknown_and_private_read_is_not_released(self):
        collection = DummyCollection()
        collection._prove = lambda: (_ for _ in ()).throw(store.Refused())
        written = self.run_worker(request(), collection)
        self.assertEqual((written['status'], written['revision'], written['record']), ('UNKNOWN', None, None))
        # Actual mutation exists despite absent acknowledgement. Reconcile exact
        # intent later; never roll back or report success from the attempted POST.
        receipt = store.Store(collection).execute({**request(), 'operation': 'reconcile', 'record_if_put': None})
        self.assertEqual(receipt['status'], 'AVAILABLE')
        read = self.run_worker(request('read', previous=receipt['revision']), collection)
        self.assertEqual((read['status'], read['record']), ('UNAVAILABLE', None))


class RuntimeLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cabinet-DUMMY-LOCK-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
        original = os.open
        def owned_fixture(path, *args, **kwargs):
            if path == '/run/user/1000':
                path = str(self.directory)
            return original(path, *args, **kwargs)
        self.enterContext(patch.object(store.os, 'open', side_effect=owned_fixture))

    def test_lock_is_empty_owned_file_and_contention_is_bounded(self):
        with store.mutation_lock(time.monotonic() + 1):
            with self.assertRaises(store.Refused) as error:
                with store.mutation_lock(time.monotonic() + .02):
                    self.fail('Contending writer acquired the lock')
            self.assertEqual(error.exception.status, 'UNKNOWN')
        with store.mutation_lock(time.monotonic() + 1):
            pass
        lock = self.directory / 'culture-cabinet-key-store' / 'control.lock'
        self.assertEqual(lock.read_bytes(), b'')
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_unsafe_runtime_directory_or_lock_symlink_refuses(self):
        child = self.directory / 'culture-cabinet-key-store'
        child.mkdir(mode=0o755)
        with self.assertRaises(store.Refused):
            with store.mutation_lock(time.monotonic() + 1):
                pass
        child.chmod(0o700)
        target = self.directory / 'own-target'
        target.touch(mode=0o600)
        (child / 'control.lock').symlink_to(target)
        with self.assertRaises(OSError):
            with store.mutation_lock(time.monotonic() + 1):
                pass


class AsyncDeadlineTests(unittest.TestCase):
    def test_gi_deadline_cancels_unfinished_work_without_secret_service_access(self):
        async_ = store._Async(time.monotonic() + 1)
        async_.deadline = time.monotonic() + .02
        cancellations = []
        def never_finishes(cancellable, done):
            cancellations.append(cancellable)
        with self.assertRaises(store.Refused):
            async_.call(never_finishes, lambda source, result: None)
        self.assertEqual(len(cancellations), 1)
        self.assertTrue(cancellations[0].is_cancelled())


class CollectionProxyInterfaceTests(unittest.TestCase):
    def test_raw_collection_constructor_requires_explicit_dbus_interface(self):
        backend = object.__new__(store._SecretBackend)
        backend.connection, backend.owner = object(), ':1.2'
        backend.service, backend.path = object(), '/org/freedesktop/secrets/collection/CultureCabinet' + '1' * 32
        backend.selection = {'requested_label': 'CultureCabinet' + '1' * 32}
        parameters = []
        def collection(**kwargs):
            parameters.append(kwargs)
            # The real raw GI proxy lacks a default interface; omitting it sent
            # NULL as the GetAll interface in the actual owned OS regression.
            store.require(kwargs.get('g_interface_name') == 'org.freedesktop.Secret.Collection')
            return SimpleNamespace(get_label=lambda: backend.selection['requested_label'])
        backend.Secret = SimpleNamespace(Collection=collection, CollectionFlags=SimpleNamespace(NONE=0))
        backend.Gio = SimpleNamespace(DBusProxyFlags=SimpleNamespace(DO_NOT_AUTO_START=4))
        backend._initialize = lambda proxy: None
        backend._prove = lambda: None
        backend._open_collection()
        self.assertEqual(parameters[0]['g_name'], backend.owner)
        self.assertEqual(parameters[0]['g_object_path'], backend.path)


class PrivateCrashContainmentTests(unittest.TestCase):
    def test_own_native_abort_is_nondumpable_before_loading_any_secret(self):
        code = '''import ctypes,importlib.util,json,os,resource,sys
spec=importlib.util.spec_from_file_location('owned_helper',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m._protect_process()
libc=ctypes.CDLL(None)
dumpable=libc.prctl(3,0,0,0,0)
os.write(int(sys.argv[2]),json.dumps({'core_limits':list(resource.getrlimit(resource.RLIMIT_CORE)),
                                  'dumpable':dumpable}).encode())
os.abort()
'''
        reader, writer = os.pipe()
        pid = None
        try:
            pid = os.posix_spawn('/usr/bin/python3', ['/usr/bin/python3', '-I', '-B', '-c', code,
                store.HELPER, str(writer)], {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                file_actions=[(os.POSIX_SPAWN_DUP2, writer, writer), (os.POSIX_SPAWN_CLOSE, reader)])
            os.close(writer); writer = None
            profile = json.loads(os.read(reader, 4096))
            _, status = os.waitpid(pid, 0); pid = None
            self.assertEqual(profile, {'core_limits': [0, 0], 'dumpable': 0})
            self.assertTrue(os.WIFSIGNALED(status))
            self.assertEqual(os.WTERMSIG(status), signal.SIGABRT)
            self.assertFalse(os.WCOREDUMP(status))
        finally:
            os.close(reader)
            if writer is not None:
                os.close(writer)
            if pid is not None:
                os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)


class HostProvisionTests(unittest.TestCase):
    def test_nonterminal_provision_and_web_unlock_selector_never_start_a_child(self):
        with patch.object(store.os, 'isatty', return_value=False), \
             patch.object(store.subprocess, 'Popen') as child, self.assertRaises(store.Refused):
            store.provision()
        child.assert_not_called()
        with patch.object(store.subprocess, 'Popen') as child, self.assertRaises(store.Refused):
            store.invoke({**request('availability'), 'operation': 'unlock'})
        child.assert_not_called()

    def test_registered_explicit_unlock_uses_only_selected_collection_and_never_regenerates(self):
        from contextlib import nullcontext
        collection = DummyCollection()
        selection_object, unlocked = object(), []
        def unlock(objects, cancel, done, user_data):
            unlocked.extend(objects)
            done(service, object())
        service = SimpleNamespace(unlock=unlock, unlock_finish=lambda result: (1, [selection_object]))
        def call(start, finish):
            result = []
            start(None, lambda source, reply: result.append(finish(source, reply)))
            return result[0]
        collection._prove = lambda: None
        collection.collection, collection.service = selection_object, service
        collection.async_ = SimpleNamespace(call=call)
        with patch.object(store.os, 'isatty', return_value=True), \
             patch.object(store.os, 'stat', return_value=object()), \
             patch.object(store, 'mutation_lock', side_effect=lambda deadline: nullcontext()), \
             patch.object(store, '_SecretBackend', return_value=collection), \
             patch.object(Fernet, 'generate_key') as generate:
            store._provision_worker('10000000-0000-4000-8000-000000000001', time.monotonic() + 1)
            self.assertEqual(unlocked, [selection_object])
            generate.assert_not_called()
            collection.key = None
            with self.assertRaises(store.Refused):
                store._provision_worker('10000000-0000-4000-8000-000000000002', time.monotonic() + 1)
            generate.assert_not_called()

    def test_cancelled_new_creation_keeps_intent_and_never_reaches_master_generation(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory(prefix='cabinet-DUMMY-PROVISION-') as directory:
            original_stat = os.stat
            def no_existing_selection(path, *args, **kwargs):
                if path == store.SELECTION:
                    raise FileNotFoundError()
                return original_stat(path, *args, **kwargs)
            def connect(backend, deadline):
                backend.owner = ':1.2'
                backend.connection = SimpleNamespace(get_unique_name=lambda: ':1.3')
                from gi.repository import GLib
                backend.GLib = GLib
                backend._dbus = lambda *args: (_ for _ in ()).throw(store.Refused())
                backend.async_ = SimpleNamespace(call=lambda *args: (_ for _ in ()).throw(store.Refused()))
            with patch.object(store.os, 'isatty', return_value=True), \
                 patch.object(store.os, 'stat', side_effect=no_existing_selection), \
                 patch.object(store, 'mutation_lock', side_effect=lambda deadline: nullcontext()), \
                 patch.object(store._SecretBackend, '_connect', connect), \
                 patch.object(store._SecretBackend, '_verify_service'), \
                 patch.object(store, '_check_continuation'), \
                 patch.object(store, '_open_checked_directory', side_effect=lambda path: os.open(directory, os.O_RDONLY)), \
                 patch.object(store, '_publish_selection', return_value=()) as publish, \
                 patch.object(store, '_probe_protection') as probe, \
                 patch.object(Fernet, 'generate_key') as generate, self.assertRaises(store.Refused):
                store._provision_worker('10000000-0000-4000-8000-000000000001', time.monotonic() + 1)
            self.assertEqual(publish.call_args.args[0]['state'], 'CREATE_PENDING')
            self.assertIsNone(publish.call_args.args[0]['returned_collection_ref'])
            self.assertEqual(publish.call_args.args[0]['terminal_fact'], 'UNCERTAIN_OR_REFUSED')
            probe.assert_not_called()
            generate.assert_not_called()


class CreationProvenanceTests(unittest.TestCase):
    """Real GLib variants and owned format files; no Secret Service connection."""
    def setUp(self):
        from gi.repository import GLib
        self.GLib = GLib
        self.temporary = tempfile.TemporaryDirectory(prefix='cabinet-DUMMY-PROVENANCE-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = store.COLLECTION_ROOT + 'returned_5fid_2d7'
        self.file = self.directory / 'returned_id-7.keyring'
        self.item = self.path + '/1'
        self.calls, self.receipts = [], []
        self.creation_reply = (self.path, '/')
        self.item_reply = (self.item, '/')
        self.delete_reply = '/'
        self.items = []
        self.change_create, self.change_delete, self.plaintext = True, True, False
        self.backend = object.__new__(store._SecretBackend)
        self.backend.owner = ':1.2'
        self.backend.connection = SimpleNamespace(get_unique_name=lambda: ':1.3')
        self.backend.GLib = GLib
        self.backend._verify_service = lambda: None
        self.backend._dbus = self.dbus
        self.backend.async_ = SimpleNamespace(prompt=lambda *args: self.path)
        self.enterContext(patch.object(store, 'KEYRING_DIRECTORY', str(self.directory)))
        self.enterContext(patch.object(store, '_open_checked_directory',
            side_effect=lambda path: os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)))
        def publish(value, **kwargs):
            store._selection_shape(value, allow_pending=True)
            self.receipts.append(deepcopy(value))
            return (len(self.receipts),)
        self.enterContext(patch.object(store, '_publish_selection', side_effect=publish))
        self.creation = store._Creation(self.backend, '10000000-0000-4000-8000-000000000001', None)

    def write(self, suffix):
        self.file.write_bytes((b'[keyring]\n' if self.plaintext else store.ENCRYPTED_PREFIX) + suffix)
        self.file.chmod(0o600)

    def dbus(self, owner, path, interface, method, parameters):
        self.calls.append((owner, path, interface, method, parameters))
        V = self.GLib.Variant
        if method == 'CreateCollection':
            self.assertEqual(parameters.unpack(),
                ({'org.freedesktop.Secret.Collection.Label': self.creation.selection['requested_label']}, ''))
            return V('(oo)', self.creation_reply)
        if method == 'Get':
            prop = parameters.unpack()[1]
            if prop == 'Label':
                # Returned identity MUST already be durable before metadata.
                self.assertEqual(self.receipts[-1]['returned_collection_ref'], self.path)
                return V('(v)', (V('s', self.creation.selection['requested_label']),))
            self.assertEqual(path, self.path)
            return V('(v)', (V('ao', self.items),))
        if method == 'OpenSession':
            return V('(vo)', (V('s', ''), '/org/freedesktop/secrets/session/owned'))
        if method == 'CreateItem':
            self.assertFalse(parameters.unpack()[-1])
            self.items = [self.item]
            if self.change_create:
                self.write(b'PUBLIC CANARY CREATED')
            return V('(oo)', self.item_reply)
        if method == 'Delete':
            self.assertEqual(path, self.item)
            self.assertEqual(self.receipts[-1]['canary_ref'], self.item)
            self.assertEqual(self.receipts[-1]['canary_state'], 'DELETE_PENDING')
            self.items = []
            if self.change_delete:
                self.write(b'PUBLIC CANARY DELETED DIFFERENT SIZE')
            return V('(o)', (self.delete_reply,))
        if method == 'Close':
            return V('()', ())
        raise AssertionError(method)

    def test_returned_identifier_differs_from_label_and_actual_canary_binds_file(self):
        self.creation.create()
        self.assertNotEqual(self.creation.selection['requested_label'], 'returned_id-7')
        self.assertEqual(self.creation.selection['decoded_identifier'], 'returned_id-7')
        with patch.object(store.os, 'pread', wraps=os.pread) as reads:
            self.creation.bind_file()
            store._probe_protection(self.creation.selection)
        self.assertEqual(self.creation.selection['state'], 'FILE_BOUND')
        self.assertEqual(self.creation.selection['master_phase'], 'NOT_STARTED')
        self.assertTrue(all(call.args[1:] == (20, 0) for call in reads.call_args_list))
        self.assertEqual([call[1] for call in self.calls if call[3] == 'Delete'], [self.item])

    def test_capture_precedes_label_or_proxy_failure_and_never_derives_filename(self):
        original = self.backend._dbus
        def fail_label(*args):
            if args[3] == 'Get':
                raise store.Refused()
            return original(*args)
        self.backend._dbus = fail_label
        with self.assertRaises(store.Refused):
            self.creation.create()
        self.assertEqual(self.receipts[-1]['returned_collection_ref'], self.path)
        self.assertIsNone(self.receipts[-1]['decoded_identifier'])
        self.assertFalse(self.file.exists())

    def test_prompt_reference_precedes_one_prompt_and_result_precedes_metadata(self):
        prompt = '/org/freedesktop/secrets/prompt/owned'
        self.creation_reply = ('/', prompt)
        observed = []
        def run_prompt(connection, owner, path, *, capture):
            self.assertEqual((connection, owner, path), (self.backend.connection, ':1.2', prompt))
            self.assertEqual(self.receipts[-1]['state'], 'PROMPT_PENDING')
            self.assertEqual(self.receipts[-1]['returned_prompt_ref'], prompt)
            observed.append(path)
            capture(self.path)
            return self.path
        self.backend.async_.prompt = run_prompt
        self.creation.create()
        self.assertEqual(observed, [prompt])
        self.assertEqual(self.receipts[-1]['returned_collection_ref'], self.path)

    def test_lost_prompt_retains_pending_without_another_create(self):
        self.creation_reply = ('/', '/org/freedesktop/secrets/prompt/owned')
        self.backend.async_.prompt = lambda *args, **kwargs: (_ for _ in ()).throw(store.Refused())
        with self.assertRaises(store.Refused):
            self.creation.create()
        self.assertEqual(self.receipts[-1]['state'], 'PROMPT_PENDING')
        self.assertIsNone(self.receipts[-1]['returned_collection_ref'])
        self.assertEqual([call[3] for call in self.calls], ['CreateCollection'])

    def test_header_without_create_transition_cannot_bind_or_initialize_master(self):
        self.creation.create()
        self.write(b'EXISTING ENCRYPTED HEADER')
        self.change_create = False
        with patch.object(Fernet, 'generate_key') as generate, self.assertRaises(store.Refused):
            self.creation.bind_file()
        generate.assert_not_called()
        self.assertEqual(self.receipts[-1]['canary_ref'], self.item)
        self.assertEqual(self.receipts[-1]['state'], 'CREATED_UNVERIFIED')
        self.assertNotIn('Delete', [call[3] for call in self.calls])

    def test_real_provisioning_flow_never_generates_master_when_canary_association_fails(self):
        from contextlib import nullcontext
        self.write(b'UNRELATED ENCRYPTED FILE')
        self.change_create = False
        original_stat, creation_type = os.stat, store._Creation
        def stat_without_selection(path, *args, **kwargs):
            if path == store.SELECTION:
                raise FileNotFoundError()
            return original_stat(path, *args, **kwargs)
        def connect(backend, deadline):
            backend.__dict__.update(self.backend.__dict__)
            self.backend = backend
        def create(backend, operation_id, continuation):
            self.creation = creation_type(backend, operation_id, continuation)
            return self.creation
        with patch.object(store.os, 'isatty', return_value=True), \
             patch.object(store.os, 'stat', side_effect=stat_without_selection), \
             patch.object(store, 'mutation_lock', side_effect=lambda deadline: nullcontext()), \
             patch.object(store, '_check_continuation'), \
             patch.object(store, '_Creation', side_effect=create), \
             patch.object(store._SecretBackend, '_connect', connect), \
             patch.object(store._SecretBackend, '_open_collection') as open_proxy, \
             patch.object(Fernet, 'generate_key') as generate, self.assertRaises(store.Refused):
            store._provision_worker('10000000-0000-4000-8000-000000000002', time.monotonic() + 1)
        generate.assert_not_called()
        open_proxy.assert_not_called()
        self.assertEqual(self.receipts[-1]['terminal_fact'], 'UNCERTAIN_OR_REFUSED')
        self.assertEqual(self.receipts[-1]['canary_ref'], self.item)
        self.assertEqual(self.receipts[-1]['master_phase'], 'NOT_STARTED')

    def assert_bind_refused_once(self):
        with self.assertRaises(store.Refused):
            self.creation.bind_file()
        self.assertEqual(self.creation.selection['state'], 'CREATED_UNVERIFIED')
        self.assertEqual(self.creation.selection['master_phase'], 'NOT_STARTED')
        self.assertEqual(sum(call[3] == 'CreateItem' for call in self.calls), 1)

    def test_delete_prompt_prevents_file_bound(self):
        self.creation.create()
        self.delete_reply = '/org/freedesktop/secrets/prompt/delete'
        self.assert_bind_refused_once()
        self.assertEqual(self.creation.selection['canary_prompt_ref'], self.delete_reply)

    def test_delete_without_file_transition_prevents_file_bound(self):
        self.creation.create()
        self.change_delete = False
        self.assert_bind_refused_once()

    def test_plaintext_after_canary_prevents_file_bound(self):
        self.creation.create()
        self.plaintext = True
        self.assert_bind_refused_once()

    def test_unexpected_canary_prompt_retains_both_refs_without_delete_or_retry(self):
        self.creation.create()
        self.item_reply = (self.item, '/org/freedesktop/secrets/prompt/canary')
        with self.assertRaises(store.Refused):
            self.creation.bind_file()
        self.assertEqual(self.receipts[-1]['canary_ref'], self.item)
        self.assertEqual(self.receipts[-1]['canary_prompt_ref'], self.item_reply[1])
        self.assertEqual(sum(call[3] == 'CreateItem' for call in self.calls), 1)
        self.assertNotIn('Delete', [call[3] for call in self.calls])

    def test_canonical_decoder_refuses_alias_traversal_noncanonical_and_unsafe_leaves(self):
        for component in ('login', 'session', 'default', '_2ehidden', 'a_2fescape', 'a_5cescape',
                          'a_00b', 'a_2e_2eb', 'a_41', 'a_5Fid', 'a_', 'x' * 129, 'a/b'):
            with self.subTest(component=component), self.assertRaises(store.Refused):
                store._decode_collection_ref(store.COLLECTION_ROOT + component)
        self.assertEqual(store._decode_collection_ref(self.path), 'returned_id-7')


class PromptSignalTests(unittest.TestCase):
    def test_completed_identity_is_captured_even_when_method_acknowledgement_is_lost(self):
        worker = store._Async(time.monotonic() + .02)
        V = worker.GLib.Variant
        returned = store.COLLECTION_ROOT + 'owned'
        captured = []
        class LostAcknowledgement:
            def signal_subscribe(self, *args):
                self.callback = args[-2]
                return 1
            def call(self, owner, path, interface, method, *args):
                self.callback(self, owner, path, interface, 'Completed',
                              V('(bv)', (False, V('o', returned))))
            def signal_unsubscribe(self, subscription):
                pass
        with self.assertRaises(store.Refused):
            worker.prompt(LostAcknowledgement(), ':1.2', '/org/freedesktop/secrets/prompt/owned',
                          capture=captured.append)
        self.assertEqual(captured, [returned])

    def test_exact_sender_path_subscription_precedes_prompt_and_accepts_both_reply_orders(self):
        for completed_first in (True, False):
            worker = store._Async(time.monotonic() + 1)
            GLib = worker.GLib
            path = '/org/freedesktop/secrets/prompt/owned'
            result_path = store.COLLECTION_ROOT + 'returned'
            events = []
            class Connection:
                def signal_subscribe(self, sender, interface, signal, object_path, arg, flags, callback, data):
                    self.callback = callback
                    self.context = GLib.MainContext.get_thread_default()
                    events.append(('subscribe', sender, object_path))
                    return 7
                def call(self, owner, target, interface, method, parameters, *args):
                    done = args[-2]
                    events.append(('call', method))
                    valid = GLib.Variant('(bv)', (False, GLib.Variant('o', result_path)))
                    # A forged sender/path cannot supply the selected identity.
                    self.callback(self, ':9.9', path, interface, 'Completed', valid)
                    self.callback(self, owner, path + 'Other', interface, 'Completed', valid)
                    actions = [lambda: self.callback(self, owner, target, interface, 'Completed', valid),
                               lambda: done(self, object())]
                    if not completed_first:
                        actions.reverse()
                    for action in actions:
                        source = GLib.idle_source_new()
                        source.set_callback(lambda *args, action=action: (action(), False)[1])
                        source.attach(self.context)
                def call_finish(self, reply):
                    return GLib.Variant('()', ())
                def signal_unsubscribe(self, subscription):
                    events.append(('unsubscribe', subscription))
            connection = Connection()
            self.assertEqual(worker.prompt(connection, ':1.2', path), result_path)
            self.assertEqual(events, [('subscribe', ':1.2', path), ('call', 'Prompt'), ('unsubscribe', 7)])

    def test_dismissed_wrong_type_root_and_missing_completion_are_unavailable(self):
        worker = store._Async(time.monotonic() + .02)
        V = worker.GLib.Variant
        for reply in (V('(bv)', (True, V('o', store.COLLECTION_ROOT + 'owned'))),
                      V('(bv)', (False, V('s', store.COLLECTION_ROOT + 'owned'))),
                      V('(bv)', (False, V('o', '/'))), V('(s)', ('wrong',))):
            with self.assertRaises(store.Refused):
                store._prompt_collection_result(reply)
        class NeverCompletes:
            def signal_subscribe(self, *args):
                return 1
            def call(self, *args):
                self.cancel = args[-3]
            def signal_unsubscribe(self, subscription):
                self.unsubscribed = subscription
        connection = NeverCompletes()
        with self.assertRaises(store.Refused):
            worker.prompt(connection, ':1.2', '/org/freedesktop/secrets/prompt/owned')
        self.assertTrue(connection.cancel.is_cancelled())
        self.assertEqual(connection.unsubscribed, 1)


class DummyRetirementTests(unittest.TestCase):
    def setUp(self):
        from contextlib import nullcontext
        self.temp = tempfile.TemporaryDirectory(prefix='cabinet-DUMMY-RETIRE-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.pointer = self.directory / 'selection.json'
        self.original = store.canonical({'schema': 'cabinet-key-store-selection/v1', 'state': 'PROVISIONING',
            'identifier': store.OLD_DUMMY_ID, 'collection_ref': store.COLLECTION_ROOT + store.OLD_DUMMY_ID,
            'provisioning': {'operation_id': store.OLD_DUMMY_OPERATION}})
        self.failure = b'{"status":"UNAVAILABLE","master_stored":false}\n'
        self.pointer.write_bytes(self.original); self.pointer.chmod(0o600)
        for name, value in (('SELECTION', str(self.pointer)),
                            ('OLD_SELECTION_SHA', hashlib.sha256(self.original).hexdigest()),
                            ('OLD_FAILURE_SHA', hashlib.sha256(self.failure).hexdigest())):
            self.enterContext(patch.object(store, name, value))
        self.enterContext(patch.object(store.os, 'isatty', return_value=True))
        self.enterContext(patch.object(store, 'mutation_lock', side_effect=lambda deadline: nullcontext()))
        self.enterContext(patch.object(store, '_open_checked_directory',
            side_effect=lambda path: os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)))
        self.enterContext(patch.object(store._SecretBackend, '_connect'))
        self.unresolved = self.enterContext(patch.object(store._SecretBackend, '_predicted_dummy_unresolved', return_value=True))
        self.enterContext(patch.object(store, '_observe_file', return_value=None))

    def test_exact_pre_master_archive_preserves_bytes_and_requires_explicit_link_for_next_setup(self):
        reference = store.retire_known_dummy(self.failure)
        self.assertEqual(reference, store.OLD_DUMMY_OPERATION)
        self.assertFalse(self.pointer.exists())
        archive_file = self.directory / store._archive_name()
        self.assertEqual(archive_file.stat().st_mode & 0o777, 0o400)
        archive = store._validate_archive(archive_file.read_bytes())
        self.assertEqual(archive['original_selection'].encode(), self.original)
        self.assertEqual(archive['failure_receipt'].encode(), self.failure)
        self.assertFalse(archive['os_orphan_deleted'])
        with self.assertRaises(store.Refused):
            store._check_continuation(None)
        store._check_continuation(reference)

    def assert_retirement_refused(self, failure=None):
        with self.assertRaises(store.Refused):
            store.retire_known_dummy(self.failure if failure is None else failure)
        self.assertTrue(self.pointer.exists())
        self.assertFalse((self.directory / store._archive_name()).exists())

    def test_changed_pointer_cannot_retire(self):
        self.pointer.write_bytes(self.original + b' ')
        self.assert_retirement_refused()

    def test_post_master_cannot_retire_even_with_matching_test_hash(self):
        changed = json.loads(self.original); changed['state'] = 'ACTIVE'
        raw = store.canonical(changed); self.pointer.write_bytes(raw)
        with patch.object(store, 'OLD_SELECTION_SHA', hashlib.sha256(raw).hexdigest()):
            self.assert_retirement_refused()

    def test_resolvable_prediction_cannot_retire(self):
        self.unresolved.return_value = False
        self.assert_retirement_refused()

    def test_wrong_failure_receipt_cannot_retire(self):
        self.assert_retirement_refused(b'wrong')

    def test_pointer_change_after_archive_is_not_unlinked_and_archive_is_retained(self):
        read = store._read_private_at
        calls = []
        def race(directory, name, *args):
            if name == 'selection.json':
                calls.append(name)
                if len(calls) == 2:
                    self.pointer.write_bytes(self.original + b' ')
            return read(directory, name, *args)
        with patch.object(store, '_read_private_at', side_effect=race), self.assertRaises(store.Refused):
            store.retire_known_dummy(self.failure)
        self.assertEqual(self.pointer.read_bytes(), self.original + b' ')
        self.assertTrue((self.directory / store._archive_name()).exists())


class FileDummyCollection(DummyCollection):
    """Test-only ciphertext fixture, NOT an installed/file fallback vault.

    A fixed PUBLIC dummy key lets independent test children decrypt invented
    records. Production never imports/selects this backend or this key.
    """
    def __init__(self, path):
        super().__init__()
        self.key = base64.urlsafe_b64encode(b'\x01' * 32)
        self.path = Path(path)
        if self.path.exists():
            self.items = {tuple(json.loads(key)): value.encode('ascii')
                          for key, value in json.loads(self.path.read_bytes()).items()}

    def _save(self):
        self.path.write_bytes(store.canonical({json.dumps(key): value.decode('ascii')
                                              for key, value in self.items.items()}))
        self.path.chmod(0o600)

    def set(self, *args):
        super().set(*args)
        self._save()

    def delete(self, *args):
        super().delete(*args)
        self._save()

    def _prove(self):
        self.available()


class FreshHelperTests(unittest.TestCase):
    def test_real_worker_framing_engine_and_fresh_process_dummy_retrieval(self):
        with tempfile.TemporaryDirectory(prefix='cabinet-FICTIONAL-PERSISTENCE-') as directory:
            fixture = Path(directory) / 'ciphertext-fixture.json'
            actual_popen, processes = subprocess.Popen, []
            def own_child(argv, **kwargs):
                child = actual_popen([*argv[:3], str(Path(__file__).resolve()), '--dummy-worker',
                                      argv[5], argv[7], str(fixture)], **kwargs)
                processes.append(child)
                return child
            with patch.object(store.subprocess, 'Popen', side_effect=own_child):
                saved = store.invoke(request())
                self.assertEqual(saved['status'], 'AVAILABLE')
                self.assertNotIn(DUMMY.encode(), fixture.read_bytes())
                read = store.invoke(request('read', previous=saved['revision']))
                self.assertEqual(read['record']['secret'], DUMMY)
                deletion = request('delete', previous=saved['revision'],
                    operation_id='10000000-0000-4000-8000-000000000002')
                removed = store.invoke(deletion)
                self.assertEqual(removed['status'], 'REMOVED')
                reconciled = store.invoke({**deletion, 'operation': 'reconcile'})
                self.assertEqual(reconciled, removed)
            self.assertEqual(len({process.pid for process in processes}), 4)
            self.assertTrue(all(process.returncode == 0 for process in processes))


if __name__ == '__main__':
    if sys.argv[1:] == ['--metadata-connect']:
        # Explicit, non-mutating platform regression. No collection object,
        # secret session, secret load, keyring file or provisioning is requested.
        store._protect_process()
        backend = object.__new__(store._SecretBackend)
        backend._connect(time.monotonic() + store.TIMEOUT)
        assert backend.service.get_name() == backend.owner
        assert backend.service.get_name_owner() == backend.owner
        assert not backend.service.get_flags() & backend.Secret.ServiceFlags.LOAD_COLLECTIONS
        assert not backend.service.get_flags() & backend.Secret.ServiceFlags.OPEN_SESSION
        print(json.dumps({'metadata_connection': 'PASS', 'pid': os.getpid(),
            'unique_owner_pinned': True, 'same_user_bus': True,
            'collections_loaded': False, 'secret_session_opened': False,
            'collection_created': False, 'secret_read': False, 'private_worker_nondumpable': True}))
    elif len(sys.argv) == 5 and sys.argv[1] == '--dummy-worker':
        # Full production worker/codec, but a test-only owned backend/lock. This
        # is not GNOME/protected-collection evidence or a production option.
        from contextlib import nullcontext
        with patch.object(store, '_SecretBackend', return_value=FileDummyCollection(sys.argv[4])), \
             patch.object(store, 'mutation_lock', side_effect=lambda deadline: nullcontext()):
            store._main([store.HELPER, '--request-fd', sys.argv[2], '--response-fd', sys.argv[3]])
    else:
        unittest.main()
