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


class CorruptionDiagnosticTests(unittest.TestCase):
    """Public fixtures only: diagnostic attribution never opens an OS store."""
    CANARY = 'sk-PUBLIC-NONSECRET-CANARY-MUST-NOT-LEAVE-DIAGNOSTIC'

    def anchor(self):
        binding=dict(os_uid=1000,installation_id='10000000-0000-4000-8000-000000000001',
            store_id='20000000-0000-4000-8000-000000000002',root_identity='1'*64)
        record=dict(schema='cabinet-clean-restart/v1',**{k:binding[k] for k in
            ('installation_id','store_id','root_identity')},database_identity='2'*64,
            checkpoint_id='40000000-0000-4000-8000-000000000004',database_digest='3'*64,
            journal_path='/'+self.CANARY,journal_length=0,journal_digest='4'*64,journal_head='5'*64,state='ACTIVE')
        return dict(version=store.VERSION,operation='put',record_kind='clean_restart_anchor',
            binding=binding,expected_revision=None,operation_id='30000000-0000-4000-8000-000000000003',record_if_put=record)

    def observe(self, action):
        observed=store._CorruptionDiagnostic();token=store._CORRUPTION_DIAGNOSTIC.set(observed)
        try:result=action()
        finally:store._CORRUPTION_DIAGNOSTIC.reset(token)
        raw=store.canonical(observed.projection())
        self.assertLessEqual(len(raw),256);self.assertNotIn(self.CANARY.encode(),raw)
        return result,observed.projection()

    def test_anchor_success_and_default_off_semantic_equivalence(self):
        first,second=DummyCollection(),DummyCollection()
        value=self.anchor()
        with patch.object(store.uuid,'uuid4',return_value='50000000-0000-4000-8000-000000000005'):
            ordinary=store.Store(first).execute(value)
            diagnostic,projection=self.observe(lambda:store.Store(second).execute(value))
        self.assertEqual(ordinary,diagnostic)
        self.assertEqual((first.loads,first.sets,first.deletes),(second.loads,second.sets,second.deletes))
        self.assertEqual(projection,dict(schema=store.DIAGNOSTIC_SCHEMA,reason='NONE',slot='none',phase='not_observed'))

    def test_master_and_encrypted_slot_corruption_fixed_attribution(self):
        backend=DummyCollection();backend.key=b'PUBLIC-BAD-MASTER'
        reply,diag=self.observe(lambda:store.Store(backend).execute(self.anchor()))
        self.assertEqual((reply['status'],diag['reason'],diag['slot']),('CORRUPT','MASTER_FORMAT','master'))
        self.assertEqual(backend.sets,0)
        for slot in ('receipt','record'):
            for reason,value in (('SLOT_SIZE_TYPE',object()),('SLOT_DECRYPT',b'PUBLIC-BAD-TOKEN'),('SLOT_DECODE',None)):
                with self.subTest(slot=slot,reason=reason):
                    backend=DummyCollection();request=self.anchor()
                    digest=hashlib.sha256(store.canonical(request['binding'])).hexdigest()
                    backend.items[(request['record_kind'],digest,slot)]=(Fernet(backend.key).encrypt(b'PUBLIC-NOT-JSON') if value is None else value)
                    reply,diag=self.observe(lambda:store.Store(backend).execute(request))
                    self.assertEqual((reply['status'],diag['reason'],diag['slot'],diag['phase']),('CORRUPT',reason,slot,'before_mutation'))
                    self.assertEqual(backend.sets,0)

    def test_lookup_and_secret_encoding_each_affected_slot(self):
        for slot in ('master','receipt','record'):
            attrs=store._SecretBackend._attrs('clean_restart_anchor','1'*64,slot)
            good=SimpleNamespace(get_attributes=lambda:attrs,get_locked=lambda:False)
            bad=SimpleNamespace(get_attributes=lambda:{**attrs,'foreign':self.CANARY},get_locked=lambda:False)
            for reason,items in (('LOOKUP_DUPLICATE',[good,good]),('ATTRIBUTES_MISMATCH',[bad])):
                backend=object.__new__(store._SecretBackend);backend._unlocked=lambda:None
                backend.Secret=SimpleNamespace(SearchFlags=SimpleNamespace(ALL=1));backend.collection=object()
                backend.async_=SimpleNamespace(call=lambda *args:items)
                def action():
                    try:backend._item('clean_restart_anchor','1'*64,slot)
                    except store.Refused as error:return dict(status=error.status)
                reply,diag=self.observe(action)
                self.assertEqual((reply['status'],diag['reason'],diag['slot']),('CORRUPT',reason,slot))
            for text in (b'PUBLIC-NOT-STR','nonascii-\u2603'):
                backend=object.__new__(store._SecretBackend);backend._unlocked=lambda:None
                backend._item=lambda *args:SimpleNamespace(get_secret=lambda:SimpleNamespace(get_text=lambda:text))
                backend.async_=SimpleNamespace(call=lambda *args:None)
                def action():
                    try:backend.get('clean_restart_anchor','1'*64,slot)
                    except store.Refused as error:return dict(status=error.status)
                reply,diag=self.observe(action)
                self.assertEqual((reply['status'],diag['reason'],diag['slot']),('CORRUPT','SECRET_ENCODING',slot))

    def test_existing_invariants_and_receipt_link_keep_corrupt_mapping(self):
        for reason,slot,changes in (('RECORD_INVARIANT','record',dict(kind='credential')),
                ('RECEIPT_INVARIANT','receipt',dict(kind='credential')),
                ('RECEIPT_RECORD_LINK','receipt',dict(revision='60000000-0000-4000-8000-000000000006'))):
            backend=DummyCollection();request=self.anchor();engine=store.Store(backend)
            saved=engine.execute(request);digest=hashlib.sha256(store.canonical(request['binding'])).hexdigest()
            key=(request['record_kind'],digest,slot);cipher=Fernet(backend.key)
            decoded=store.strict_json(cipher.decrypt(backend.items[key]));decoded.update(changes)
            backend.items[key]=cipher.encrypt(store.canonical(decoded));writes=backend.sets
            reply,diag=self.observe(lambda:engine.execute(request))
            self.assertEqual((reply['status'],diag['reason']),('CORRUPT',reason))
            self.assertEqual(diag['slot'],'receipt_record_pair' if reason=='RECEIPT_RECORD_LINK' else slot)
            self.assertEqual(backend.sets,writes)

    def test_first_post_intent_corruption_remains_unknown(self):
        backend=DummyCollection()
        def fail(*args):
            store._corrupt_require(False,'ATTRIBUTES_MISMATCH','receipt')
        backend.set=fail
        reply,diag=self.observe(lambda:store.Store(backend).execute(self.anchor()))
        self.assertEqual((reply['status'],diag['reason'],diag['phase']),('UNKNOWN','ATTRIBUTES_MISMATCH','mutation_started'))
        self.assertEqual(store._validate_diagnostic(store.canonical(diag),'UNKNOWN'),diag)
        def first():
            store._note_corrupt('MASTER_FORMAT','master');store._note_corrupt('SLOT_DECODE','record')
        _,diag=self.observe(first)
        self.assertEqual((diag['reason'],diag['slot']),('MASTER_FORMAT','master'))


class DiagnosticPipeTests(unittest.TestCase):
    """A fabricated child writes public frames; packaged OS worker is never run."""
    def setUp(self):PrivateClientTests.setUp(self)
    def fake_child(self,suffix):return PrivateClientTests.fake_child(self,suffix)
    def child(self,raw,status='AVAILABLE'):
        revision="'50000000-0000-4000-8000-000000000001'" if status=='AVAILABLE' else 'None'
        return self.fake_child(f'''if len(sys.argv)==7:
 diag_fd=int(sys.argv[6])
 os.write(diag_fd,{raw!r});os.close(diag_fd)
reply={{'version':value['version'],'operation_id':value['operation_id'],'status':{status!r},'revision':{revision},'record':None}}
raw=json.dumps(reply).encode();os.write(write_fd,struct.pack('!I',len(raw))+raw)
''')
    def diagnostic(self,reason='NONE',slot='none',phase='not_observed'):
        return store.canonical(dict(schema=store.DIAGNOSTIC_SCHEMA,reason=reason,slot=slot,phase=phase))

    def test_optin_one_child_unchanged_result_and_private_third_pipe(self):
        seen=[];raw=self.diagnostic()
        with self.child(raw):ordinary=store.invoke(request())
        with self.child(raw):observed=store.invoke(request(),diagnostic_sink=seen.append)
        self.assertEqual(observed,ordinary);self.assertEqual(len(self.processes),2)
        self.assertEqual(seen,[store.strict_json(raw)])
        default,on=self.invocations
        self.assertEqual(len(default[0]),8);self.assertEqual(len(default[1]['pass_fds']),2)
        self.assertEqual(len(on[0]),10);self.assertEqual(on[0][8],'--diagnostic-fd')
        self.assertEqual(int(on[0][9]),on[1]['pass_fds'][2]);self.assertEqual(len(set(on[1]['pass_fds'])),3)
        self.assertNotIn(DUMMY,repr(on));self.assertTrue(all(p.returncode==0 for p in self.processes))

    def test_missing_malformed_oversized_and_contradictory_are_invalid_only(self):
        cases=[b'',b'PUBLIC-NOT-JSON',b'x'*257,self.diagnostic('MASTER_FORMAT','record','before_mutation'),
            self.diagnostic('MASTER_FORMAT','master','before_mutation'),self.diagnostic()+b'{}']
        for raw in cases:
            seen=[]
            with self.subTest(length=len(raw)),self.child(raw):reply=store.invoke(request(),diagnostic_sink=seen.append)
            self.assertEqual(reply['status'],'AVAILABLE');self.assertEqual(seen,[None])
        seen=[]
        with self.child(self.diagnostic(),'CORRUPT'):reply=store.invoke(request(),diagnostic_sink=seen.append)
        self.assertEqual(reply['status'],'CORRUPT');self.assertEqual(seen,[None])

    def test_sink_failure_and_all_descriptors_closed_after_valid_result(self):
        allocated=[];pipe=os.pipe
        def own_pipe():
            pair=pipe();allocated.extend(pair);return pair
        observed={}
        def sink(value):
            closed=[]
            for fd in allocated:
                try:os.fstat(fd);closed.append(False)
                except OSError:closed.append(True)
            observed.update(valid=value is not None,closed=closed,reaped=all(p.returncode==0 for p in self.processes))
            raise ValueError('PUBLIC-SINK-FAILURE')
        with self.child(self.diagnostic()),patch.object(store.os,'pipe',side_effect=own_pipe):
            reply=store.invoke(request(),diagnostic_sink=sink)
        self.assertEqual(reply['status'],'AVAILABLE')
        self.assertEqual(observed,dict(valid=True,closed=[True]*len(allocated),reaped=True))

    def test_observer_setup_failure_aborts_before_dispatch_and_closes_fds(self):
        seen=[];allocated=[];pipe=os.pipe
        def own_pipe():
            pair=pipe();allocated.extend(pair);return pair
        with patch.object(store.os,'pipe',side_effect=own_pipe),patch.object(store.os,'set_blocking',side_effect=OSError()),patch.object(store.subprocess,'Popen') as start:
            reply=store.invoke(request(),diagnostic_sink=seen.append)
        self.assertEqual(reply['status'],'UNKNOWN');self.assertEqual(seen,[None]);start.assert_not_called()
        for fd in allocated:
            with self.assertRaises(OSError):os.fstat(fd)
        with patch.object(store.subprocess,'Popen') as start,self.assertRaises(store.Refused):
            store.invoke(request(),diagnostic_sink=object())
        start.assert_not_called()

    def test_timeout_one_child_cleanup_and_no_extra_wait_for_metadata(self):
        seen=[]
        with self.fake_child('time.sleep(5)\n'):
            started=time.monotonic();reply=store.invoke(request(),timeout=.1,diagnostic_sink=seen.append)
        self.assertLess(time.monotonic()-started,2);self.assertEqual(reply['status'],'UNKNOWN');self.assertEqual(seen,[None])
        self.assertEqual(len(self.processes),1);self.assertIsNotNone(self.processes[0].returncode)

    def test_nonblocking_full_or_broken_emit_does_not_rewrite_or_wait(self):
        read,write=os.pipe();os.set_blocking(write,False)
        try:
            while True:os.write(write,b'PUBLIC-FILL'*1000)
        except BlockingIOError:pass
        started=time.monotonic();store._emit_diagnostic(write,store._CorruptionDiagnostic())
        self.assertLess(time.monotonic()-started,.5)
        with self.assertRaises(OSError):os.fstat(write)
        os.close(read)
        read,write=os.pipe();os.close(read);os.set_blocking(write,False)
        store._emit_diagnostic(write,store._CorruptionDiagnostic())
        with self.assertRaises(OSError):os.fstat(write)

    def test_closed_emit_descriptor_is_observer_loss_only(self):
        read,write=os.pipe();os.close(write)
        try:store._emit_diagnostic(write,store._CorruptionDiagnostic())
        finally:os.close(read)

    def test_cancelled_parent_reaps_one_fake_child_and_closes_observer(self):
        seen=[];allocated=[];pipe=os.pipe
        def own_pipe():
            pair=pipe();allocated.extend(pair);return pair
        with self.fake_child('time.sleep(5)\n'),patch.object(store.os,'pipe',side_effect=own_pipe), \
             patch.object(store,'_exchange',side_effect=KeyboardInterrupt()),self.assertRaises(KeyboardInterrupt):
            store.invoke(request(),diagnostic_sink=seen.append)
        self.assertEqual(seen,[None]);self.assertEqual(len(self.processes),1)
        self.assertIsNotNone(self.processes[0].returncode)
        for fd in allocated:
            with self.assertRaises(OSError):os.fstat(fd)

    def test_worker_distinct_fd_validation_and_context_cleanup(self):
        request_read,request_write=os.pipe();response_read,response_write=os.pipe();diag_read,diag_write=os.pipe()
        try:
            args=[store.HELPER,'--request-fd',str(request_read),'--response-fd',str(response_write)]
            duplicate=os.dup(response_write)
            try:
                with patch.object(store,'_protect_process'),patch.object(store,'_request_main') as worker,self.assertRaises(store.Refused):
                    store._main(args+['--diagnostic-fd',str(duplicate)])
                worker.assert_not_called()
            finally:os.close(duplicate)
            def worker(argv):store._note_corrupt('MASTER_FORMAT','master')
            with patch.object(store,'_protect_process'),patch.object(store,'_request_main',side_effect=worker):
                store._main(args+['--diagnostic-fd',str(diag_write)])
            self.assertIsNone(store._CORRUPTION_DIAGNOSTIC.get())
            self.assertEqual(store._validate_diagnostic(os.read(diag_read,257),'CORRUPT')['reason'],'MASTER_FORMAT')
            with self.assertRaises(OSError):os.fstat(diag_write)
        finally:
            for fd in (request_read,request_write,response_read,response_write,diag_read):os.close(fd)



class GenericSchemaCompatibilityTests(unittest.TestCase):
    def test_exact_generic_schema_allowlist_for_all_slots(self):
        generic='org.freedesktop.Secret.Generic'
        for slot in ('master','receipt','record'):
            kind='master' if slot=='master' else 'clean_restart_anchor'
            expected=store._SecretBackend._attrs(kind,'0'*64,slot)
            accepted=(dict(expected),{**expected,'xdg:schema':generic})
            actions=('read',) if slot=='master' else ('read','write')
            rejected=[]
            for key in expected:
                for base in accepted:
                    rejected.append({k:v for k,v in base.items() if k!=key})
                    rejected.append({**base,key:'PUBLIC-WRONG-IDENTITY'})
            for value in ('org.example.Other','',None,False,1,b'org.freedesktop.Secret.Generic',[],{}):
                rejected.append({**expected,'xdg:schema':value})
            rejected.extend(({**expected,'foreign':'PUBLIC-EXTRA'},
                             {**expected,'xdg:schema':generic,'foreign':'PUBLIC-EXTRA'},None,{}))

            def attempt(attributes,*,action='lookup',duplicate=False,locked=False):
                counts=dict(search=0,attributes=0,locked=0,secret_loads=0,writes=0)
                def get_attributes():counts['attributes']+=1;return attributes
                def get_locked():counts['locked']+=1;return locked
                item=SimpleNamespace(get_attributes=get_attributes,get_locked=get_locked)
                backend=object.__new__(store._SecretBackend);backend._unlocked=lambda:None
                def forbidden_value(*args):
                    counts['writes']+=1;raise AssertionError('Rejected metadata reached write preparation')
                backend.Secret=SimpleNamespace(SearchFlags=SimpleNamespace(ALL=1),Value=SimpleNamespace(new=forbidden_value))
                backend.collection=object()
                def call(*args):
                    counts['search']+=1
                    if counts['search']>1:
                        counts['secret_loads']+=1;raise AssertionError('Rejected metadata reached secret load')
                    return [item,item] if duplicate else [item]
                backend.async_=SimpleNamespace(call=call)
                try:
                    if action=='read':backend.get(kind,'0'*64,slot)
                    elif action=='write':backend.set(kind,'0'*64,slot,b'PUBLIC-DUMMY')
                    else:self.assertIs(backend._item(kind,'0'*64,slot),item)
                    status='AVAILABLE'
                except store.Refused as error:status=error.status
                return status,counts

            for attributes in accepted:
                with self.subTest(slot=slot,case='accepted'):
                    status,counts=attempt(attributes)
                    self.assertEqual(status,'AVAILABLE')
                    self.assertEqual(counts,dict(search=1,attributes=1,locked=1,secret_loads=0,writes=0))
            for number,attributes in enumerate(rejected):
                for action in actions:
                    with self.subTest(slot=slot,case=number,action=action):
                        status,counts=attempt(attributes,action=action)
                        self.assertEqual(status,'CORRUPT')
                        self.assertEqual(counts,dict(search=1,attributes=1,locked=0,secret_loads=0,writes=0))
            for action in actions:
                status,counts=attempt(accepted[1],action=action,duplicate=True)
                self.assertEqual(status,'CORRUPT')
                self.assertEqual(counts,dict(search=1,attributes=0,locked=0,secret_loads=0,writes=0))
                status,counts=attempt(accepted[1],action=action,locked=True)
                self.assertEqual(status,'LOCKED')
                self.assertEqual(counts,dict(search=1,attributes=1,locked=1,secret_loads=0,writes=0))


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
        def run_prompt(connection, owner, path, *, capture, observe):
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


def pending_evidence_fixture():
    """Actual six-entry UTF8 key lengths, without any runtime evidence path read."""
    lengths = (129, 132, 134, 139, 101, 64)
    evidence = {}
    for index, length in enumerate(lengths):
        prefix = '/owned-dummy/evidence-' + str(index) + '/'
        evidence[prefix + 'x' * (length - len(prefix))] = hashlib.sha256(str(index).encode()).hexdigest()
    return evidence


# Frozen NONSECRET actual receipts: hermetic raw-byte/parser/third-link regression.
DIALOG_ACTUAL_FAILURE = r'''{
  "id": "K1-A4A-ACTUAL-CONTINUATION-TERMINAL-02",
  "date": "2026-11-11",
  "observed_terminal_utc": "2026-11-11T02:59:29.066617+00:00",
  "state": "NOT_READY_ACTUAL_INPUT_GEOMETRY_REFUSAL_RETAINED",
  "accepted_head": "cbd02e446609fa89d86e45f6eb04a35f83bce221",
  "merged_main": "6efcc3185ee03564d5ad41e89233157f3c72ad87",
  "helper_sha256": "6dc69b1a38578e028e90f846efd4723c62bb7ed3ae70ba29f8547bf01a4c4905",
  "retirement": "PASS exact ended ce102960 null/pre-master pointer archived then CAS-retired; both immutable ancestors retained; no OS orphan located/deleted.",
  "launcher_correction": "An initial here-document wrapper made stdin non-TTY; helper refused TTY_REQUIRED before positive receipt, monitor, IPC or Create. Failure/start record retained. Same accepted helper was then launched directly in an actual PTY; no source/driver change or repeated Create.",
  "creation_operation_id": "b8cc865d-df30-46da-969a-cf7fa40c6eba",
  "requested_label": "CultureCabinet8d51ac84657f4162acb559873d8b3ab8",
  "returned_prompt_ref": "/org/freedesktop/secrets/prompt/p3",
  "returned_collection_ref": null,
  "protocol_observation": {
    "acknowledgement": "ACKNOWLEDGED",
    "completion": "UNOBSERVED",
    "termination": "UNOBSERVED"
  },
  "terminal_status": "CORRECTED_PROVISION_REFUSED_NO_RETRY / UNAVAILABLE; exec session25970 exit1",
  "original_bounds": "One original120s monitor/provision bound; no extension or input fallback. Internal numeric deadline not exported. Source did not retain a TIMEOUT event: termination remains UNOBSERVED, not relabelled.",
  "input": {
    "stage": "INPUT_REFUSED_NO_RETRY",
    "attempts": 0,
    "actual_fields_verified": false,
    "focus_edit_Create_actions": 0,
    "private_passphrase_generated_or_entered": false,
    "precise_cause": "Accepted same-dialog pre-input binding requires corresponding compositor/AT-SPI bounds. Actual ACTIVE GCR dialog SCREEN extents (0,0,517,262) do not equal compositor logical (-107,-29,518,262) or observed-scale2 physical bounds. _select_dialog found zero matching candidates and refused before nonce/control traversal or focus/input.",
    "readonly_recheck": "Exact new owned GCR PID31844/window0xb728f3122720 only; same refusal; no fill/focus/action invoked. Monitor origin0,0 and scale2 supported, so bounds correspondence is the concrete failed gate."
  },
  "storage": {
    "canary_state": "NOT_STARTED",
    "master_phase": "NOT_STARTED",
    "file_binding": null,
    "verified_filename": null,
    "dummy_write": "UNRUN",
    "fresh_helper_retrieval": "UNRUN",
    "normal_native_retrieval": "UNRUN",
    "empty_and_transition_negatives": "UNRUN"
  },
  "cleanup": {
    "known_proof_parent_pid": 31808,
    "proof_parent_exited": true,
    "helper_caller": ":1.21914",
    "helper_caller_has_owner": false,
    "exact_owned_prompt_window_remaining": false,
    "monitor_thread": "Returned INPUT_REFUSED_NO_RETRY with0 attempts; parent subsequently exited.",
    "collection_cleanup": "No captured exact collection identity; no OS collection/file deletion, scan, guessed cleanup or oldPrompt replay. Pending pointer and possible unknown OS outcome preserved.",
    "shared_GCR_or_keyring_service_killed": false
  },
  "counts": {
    "approved_collection_create_attempts": 1,
    "old_prompt_calls": 0,
    "input_or_canary_or_master_writes": 0,
    "provider_model_requests": 0,
    "real_key_reads_or_requests": 0,
    "core_browser_launches": 0
  },
  "next_precise_dependency": "Root routes a supported same-owned-dialog identity strategy for real GTK/GCR Wayland coordinates, with H1 containment preserved; do not merely accept0,0 or loosen the reviewed guard. The new exact b8cc865d PROMPT_PENDING/ACK/no-input/null-result/zero-master attempt also needs its bounded disposition before any new Create, because accepted retirement is pinned only to ce102960. No current runtime remains to recover or replay its prompt.",
  "legitimate_key_entry_dependency": "Only after accepted input readiness, actual returned identity/canary-linked encrypted-file proof, dummy persistence, terminal closure/fresh-helper retrieval and required negatives may Root separately route normal native retrieval and legitimate owner key entry. No actual private custodian value was generated/entered by this denied input run.",
  "evidence": {
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owned-gnome-a4a-retire-02.json": "33d9028675ba237391ce83dcac074d5bac2bcae12fb43fa89cab92e63b55d1b0",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owned-gnome-a4a-positive-02.json": "fab97b368ea32df24906f86d90e2b94d3a44da4e76dcd8709045de9a0b0a0641",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owned-gnome-a4a-input-02.json": "d71a2cab22c6b42bc5af87d6e7da636a770658274d5fbc0a9d70f6d79a798c99",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/k1-a4a-actual-input-metadata-02.json": "47389a0a1967820b0d03a07b9887fd8ff2e850400174dfaf8a88227d41afcc57",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/k1-a4a-owned-caller-closed-02.json": "d9c7800d5765b61dcdc0b90945953fea970691d8dc0a6b99c470b17525060b3f",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/k1-a4a-run-custody-start-02.json": "42f756e4ae33e2e83c00b48a5753e9ddf1a13ff0af19a0133e448693ce7fb94f",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/k1-a4a-run-custody-active-02.json": "5a157726022b8b1cf37858dfc8b0e792d3b7b5515b2e01514564c25abed5fcc4",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owned-gnome-a4a-proof-08.py": "3f4fb808940757363fd83ec09c5ac5bc1c8f133b3ba920fa5c7574b18922a845",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owned-gcr-labelled-driver-07.py": "02c2b11397596ef6d463c1774abdb822472510c655057cee06e04ccc04aa3416",
    "/home/tang/.local/state/culture-cabinet-key-store/abandoned-f6ed3437-1f93-4564-8a6d-e558e5dd6462.json": "184e1a5ac45fe03f4bd59659c3dca3dc15d2a67b873c619c86009533a09a9dfb",
    "/home/tang/.local/state/culture-cabinet-key-store/abandoned-ce102960-02f0-4697-8008-371439a02d04.json": "fe7a79afb45e22a6ac1d7b4cc4080d4ac5c60d1abc5f494277aa19900c12cc77",
    "/home/tang/.local/state/culture-cabinet-key-store/selection.json": "861c04607e5f6d39b078a935cdaee96a1a73730a5fb028055fe486607e4dd6c9"
  }
}
'''.encode('utf-8')

DIALOG_ACTUAL_POSITIVE = r'''{
  "candidate": "cbd02e446609fa89d86e45f6eb04a35f83bce221",
  "source_sha256": "6dc69b1a38578e028e90f846efd4723c62bb7ed3ae70ba29f8547bf01a4c4905",
  "stage": "CORRECTED_PROVISION_REFUSED_NO_RETRY",
  "parent_pid": 31808,
  "no_real_key": true,
  "provider_requests": 0,
  "provision_status": "UNAVAILABLE",
  "selection": {
    "canary_prompt_ref": null,
    "canary_ref": null,
    "canary_state": "NOT_STARTED",
    "continuation_archive": "ce102960-02f0-4697-8008-371439a02d04",
    "creation_operation_id": "b8cc865d-df30-46da-969a-cf7fa40c6eba",
    "decoded_identifier": null,
    "file_binding": null,
    "keyring_directory": "/home/tang/.local/share/keyrings",
    "master_phase": "NOT_STARTED",
    "prompt_observation": {
      "acknowledgement": "ACKNOWLEDGED",
      "completion": "UNOBSERVED",
      "termination": "UNOBSERVED"
    },
    "requested_label": "CultureCabinet8d51ac84657f4162acb559873d8b3ab8",
    "returned_collection_ref": null,
    "returned_prompt_ref": "/org/freedesktop/secrets/prompt/p3",
    "schema": "cabinet-key-store-selection/v2",
    "service_provenance": {
      "caller": ":1.21914",
      "implementation": "gnome-keyring-51.1",
      "service_owner": ":1.0",
      "service_uid": 1000
    },
    "state": "PROMPT_PENDING",
    "terminal_fact": null,
    "verified_filename": null
  }
}
'''.encode('utf-8')

DIALOG_ACTUAL_INPUT = r'''{
  "stage": "INPUT_REFUSED_NO_RETRY",
  "attempts": 0,
  "actual_gcr_fields_verified": false,
  "passphrase_output": false,
  "completion_confirmed": false,
  "input_events": [],
  "error_type": "RuntimeError"
}
'''.encode('utf-8')

DIALOG_ACTUAL_CALLER = r'''{
  "operation_id": "b8cc865d-df30-46da-969a-cf7fa40c6eba",
  "caller": ":1.21914",
  "caller_has_owner": false,
  "method": "NameHasOwner exact owned caller only",
  "prompt_calls": 0,
  "secret_calls": 0
}
'''.encode('utf-8')


# Exact relationless-form terminal receipts, including their recorded clock atoms.
PAIR_ACTUAL_FAILURE = r'''{
  "id": "K1-A4B-ACTUAL-CONTINUATION-TERMINAL-03",
  "date": "2026-11-11",
  "observed_terminal_utc": "2026-11-11T04:25:35.182234+00:00",
  "state": "NOT_READY_NATIVE_CONTROL_RELATION_REFUSAL_RETAINED",
  "source_head": "d0d9f3bd52e7c5db70ca90afccfdac60882edc5a",
  "merged_main": "25bffd9d7bee4a2e5973d9e69520028ccae6f052",
  "helper_sha256": "745e946b899323b0bbddab0caab0024c940175babd91953506697774ed91bee7",
  "retirement": "PASS exact b8cc865d/861c null-pre-master record archived before CAS retirement. All three immutable ancestors retained. No OS orphan lookup/adoption/deletion.",
  "approved_Create_attempts": 1,
  "original_bounds": {
    "monitor_started_utc": "2026-11-11T04:18:39.490785+00:00",
    "monitor_original_deadline_monotonic": 1340075.871266088,
    "provision_bound_seconds": 120,
    "extension_or_retry": false,
    "termination_event": "UNOBSERVED per retained source record; not relabelled TIMEOUT"
  },
  "operation_id": "1cc2bd37-8f62-420d-928d-ec9c28945013",
  "requested_label": "CultureCabinet40169a13b00b4459adf1022962a61eb4",
  "service_owner": ":1.0",
  "original_caller": ":1.22631",
  "returned_prompt_ref": "/org/freedesktop/secrets/prompt/p4",
  "returned_collection_ref": null,
  "prompt_observation": {
    "acknowledgement": "ACKNOWLEDGED",
    "completion": "UNOBSERVED",
    "termination": "UNOBSERVED"
  },
  "terminal_result": "CORRECTED_PROVISION_REFUSED_NO_RETRY / UNAVAILABLE; direct-PTY exec session26690 exit1",
  "binding_facts": {
    "singleton_process_application_window_top_level": "PASS actual:1 compositor,1 accessibility application,1 ACTIVE top-level, process generation guarded",
    "same_dialog_nonce": "PASS actual nonce label confirmed within exact owned dialog",
    "coordinates": "Different compositor/AT-SPI coordinates were diagnostics and did not block the singleton binding",
    "failure_category": "IDENTITY",
    "failure_phase": "CONTROLS",
    "precise_observed_gap": "Both actual PASSWORD_TEXT nodes have empty relation sets, so neither supplies the required single LABELLED_BY relation. Actual push buttons are Continue and Cancel; driver requires Create. These are unsupported actual form semantics for the reviewed control contract, not a Secret Service callback/provider error.",
    "field_interface_support": "UNRUN; constructor refused before native focus/edit/Create interfaces or private generation",
    "readonly_diagnosis": "Only current owned PID409325/window0xb728f32b63a0/dialog tree and in-dialog LABEL/PUSH_BUTTON/relation metadata; no password-widget name/text/value/contents read, no UI action or old prompt invocation"
  },
  "effects": {
    "input_attempts": 0,
    "focus_edit_Create_actions": 0,
    "private_passphrase_generated_or_acquired": false,
    "canary_state": "NOT_STARTED",
    "master_phase": "NOT_STARTED",
    "file_binding": null,
    "verified_filename": null,
    "dummy_write": "UNRUN",
    "fresh_helper_read": "UNRUN_STOPPED_ON_FIRST_FAILURE",
    "native_retrieval_and_protection_negatives": "UNRUN",
    "provider_or_model_requests": 0,
    "real_key_read_or_entry": false
  },
  "custody_and_cleanup": {
    "custody_metadata": {
      "socket_path": "/run/user/1000/culture-cabinet-owned-proof/787e1968-a8c9-4faa-91db-fae9b0b961d7.sock",
      "pid": 409304,
      "process_generation": [
        409304,
        133995586
      ]
    },
    "custody_retained_value_before_terminal": false,
    "custody_disposed_empty": true,
    "socket_exists": false,
    "custodian_process_exists": false,
    "enrollment_parent_pid": 409294,
    "enrollment_parent_exists": false,
    "monitor_joined": true,
    "helper_caller_has_owner": false,
    "exact_owned_prompt_window_remaining": false,
    "native_handoff": "No private value exists to hand off and no extra custody-dispose needed. Historical metadata retained as disposed.",
    "shared_service_kill": false,
    "OS_collection_cleanup_claim": false
  },
  "next_precise_dependency": "A supported same-owned-dialog semantic association for GCR password/confirmation controls without LABELLED_BY and its actual Continue action, preserving singleton/nonce/current identity and no guessed/global input. Do not loosen/fallback or retry p4. The ended exact1cc2bd37 ACK/null-result/zero-input/zero-master pending pointer needs its own bounded disposition before any further Create; current approved retirement targets b8 only.",
  "legitimate_key_entry": "Still blocked on actual input/Completed/returned identity/canary-linked encrypted association/dummy persistence/fresh-helper and separately routed native/protection proofs. No real key request is appropriate.",
  "evidence": {
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owned-gnome-a4b-retire-03.json": "5bbd72874603a5c7b276211b5387142e7d95fd882ac4fafd4a6e534f28c56d7b",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owned-gnome-a4b-positive-03.json": "447fe12c68f64889c058310750187ebd6b776391a1bf204cd0475d35f75cce74",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owned-gnome-a4b-input-03.json": "da540384ac7118177d0c513d415a6959087ca78d9ab9bd33c715e6055e4b428c",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/k1-a4b-active-custody-03.json": "548cba8e1ec4869dd500064e00ec0d4b8011de8207e9b86d39d717e721228a91",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/k1-a4b-owned-controls-readonly-03.json": "192410980df337ad9b9468368622b92b648edeb7660fdaed78e1e940f79b88aa",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/k1-a4b-owned-caller-closed-03.json": "ae07d0f7bf7cee1a4e47de5d3a460c0b9dc42643c1bfdb0b67520785fccb4ba5",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owned-gnome-a4b-proof-09.py": "f501c73b09741c824cc1fb2c92106c2bc85f9ca49f652909e4b9c6d13f56d4c2",
    "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owned-gcr-single-surface-driver-08.py": "fe560c11d2596795e1fea74c4b83d9ea39f001d8dd9974e807accb1829254c2d",
    "/home/tang/.local/state/culture-cabinet-key-store/selection.json": "c7cd0519577efc5884e650e0b4a6ae842d72b2aafefe4df214b9b4025c10e3d5",
    "/home/tang/.local/state/culture-cabinet-key-store/abandoned-f6ed3437-1f93-4564-8a6d-e558e5dd6462.json": "184e1a5ac45fe03f4bd59659c3dca3dc15d2a67b873c619c86009533a09a9dfb",
    "/home/tang/.local/state/culture-cabinet-key-store/abandoned-ce102960-02f0-4697-8008-371439a02d04.json": "fe7a79afb45e22a6ac1d7b4cc4080d4ac5c60d1abc5f494277aa19900c12cc77",
    "/home/tang/.local/state/culture-cabinet-key-store/abandoned-b8cc865d-df30-46da-969a-cf7fa40c6eba.json": "9d6ada8785c4d05c8d4c77bb56754391ebe812da8d624273a4e0fef291d4819d"
  }
}
'''.encode('utf-8')

PAIR_ACTUAL_POSITIVE = r'''{
  "candidate": "d0d9f3bd52e7c5db70ca90afccfdac60882edc5a",
  "source_sha256": "745e946b899323b0bbddab0caab0024c940175babd91953506697774ed91bee7",
  "stage": "CORRECTED_PROVISION_REFUSED_NO_RETRY",
  "parent_pid": 409294,
  "parent_generation": [
    409294,
    133995575
  ],
  "no_real_key": true,
  "provider_requests": 0,
  "custody": {
    "socket_path": "/run/user/1000/culture-cabinet-owned-proof/787e1968-a8c9-4faa-91db-fae9b0b961d7.sock",
    "pid": 409304,
    "process_generation": [
      409304,
      133995586
    ]
  },
  "monitor_original_deadline_monotonic": 1340075.871266088,
  "provision_bound_seconds": 120,
  "monitor_started_utc": "2026-11-11T04:18:39.490785+00:00",
  "monitor_joined": true,
  "custody_disposed_empty": true,
  "provision_status": "UNAVAILABLE",
  "selection": {
    "canary_prompt_ref": null,
    "canary_ref": null,
    "canary_state": "NOT_STARTED",
    "continuation_archive": "b8cc865d-df30-46da-969a-cf7fa40c6eba",
    "creation_operation_id": "1cc2bd37-8f62-420d-928d-ec9c28945013",
    "decoded_identifier": null,
    "file_binding": null,
    "keyring_directory": "/home/tang/.local/share/keyrings",
    "master_phase": "NOT_STARTED",
    "prompt_observation": {
      "acknowledgement": "ACKNOWLEDGED",
      "completion": "UNOBSERVED",
      "termination": "UNOBSERVED"
    },
    "requested_label": "CultureCabinet40169a13b00b4459adf1022962a61eb4",
    "returned_collection_ref": null,
    "returned_prompt_ref": "/org/freedesktop/secrets/prompt/p4",
    "schema": "cabinet-key-store-selection/v2",
    "service_provenance": {
      "caller": ":1.22631",
      "implementation": "gnome-keyring-51.1",
      "service_owner": ":1.0",
      "service_uid": 1000
    },
    "state": "PROMPT_PENDING",
    "terminal_fact": null,
    "verified_filename": null
  }
}
'''.encode('utf-8')

PAIR_ACTUAL_INPUT = r'''{
  "stage": "INPUT_REFUSED_NO_RETRY",
  "attempts": 0,
  "actual_gcr_fields_verified": false,
  "passphrase_output": false,
  "completion_confirmed": false,
  "input_events": [],
  "error_type": "DriverRefused",
  "refusal_category": "IDENTITY",
  "refusal_phase": "CONTROLS",
  "binding_metadata": {
    "compositor_windows": 1,
    "applications": 1,
    "top_levels": 1,
    "binding_method": "single-owned-surface/v1",
    "compositor_geometry": {
      "at": [
        -83,
        -5
      ],
      "size": [
        518,
        262
      ]
    },
    "accessible_geometry": [
      0,
      0,
      517,
      262
    ]
  }
}
'''.encode('utf-8')

PAIR_ACTUAL_CALLER = r'''{
  "operation_id": "1cc2bd37-8f62-420d-928d-ec9c28945013",
  "caller": ":1.22631",
  "caller_has_owner": false,
  "method": "NameHasOwner exact current owned caller only",
  "prompt_or_collection_or_secret_calls": 0
}
'''.encode('utf-8')


class PinnedReceiptDecoderTests(unittest.TestCase):
    def sample(self, **changes):
        return {'evidence': pending_evidence_fixture(), 'observed': 'dummy-only', **changes}

    def decode_fixture(self, raw):
        # Only owned adversarial fixtures substitute the production pin. There
        # is no environment/configuration override in the production decoder.
        with patch.object(store, 'PENDING_FAILURE_SHA', hashlib.sha256(raw).hexdigest()):
            return store._pending_failure_json(raw)

    def test_actual_six_entry_length_shape_decodes_only_through_pinned_private_entry(self):
        raw = store.canonical(self.sample())
        decoded = self.decode_fixture(raw)
        self.assertEqual([len(key.encode()) for key in decoded['evidence']], [129, 132, 134, 139, 101, 64])
        with self.assertRaises(store.Refused):
            store.strict_json(raw)
        # Ordinary JSON never acquired receipt-specific evidence semantics.
        ordinary = {'evidence': {'relative': True}}
        self.assertEqual(store.strict_json(store.canonical(ordinary)), ordinary)

    def test_hash_mismatch_refuses_before_any_decoder_call(self):
        raw = store.canonical(self.sample())
        with patch.object(store, 'PENDING_FAILURE_SHA', hashlib.sha256(raw).hexdigest()), \
             patch.object(store, '_decode_json') as decode, self.assertRaises(store.Refused):
            store._pending_failure_json(raw + b' ')
        decode.assert_not_called()
        for value in (None, raw.decode(), bytearray(raw)):
            with self.subTest(type=type(value).__name__), self.assertRaises(store.Refused):
                store._pending_failure_json(value)

    def test_private_path_bound_is_256_bytes_and_other_keys_stay_128(self):
        digest = 'a' * 64
        allowed = '/' + 'x' * 255
        self.decode_fixture(store.canonical({'evidence': {allowed: digest}}))
        cases = (
            {'evidence': {'/' + 'x' * 256: digest}},
            self.sample(**{'x' * 129: True}),
            self.sample(other={'evidence': {'/' + 'x' * 128: digest}}),
        )
        for value in cases:
            with self.subTest(value_keys=list(value)), self.assertRaises(store.Refused):
                self.decode_fixture(store.canonical(value))
        # The bound is UTF8 bytes, not Unicode character count.
        with self.assertRaises(store.Refused):
            self.decode_fixture(store.canonical({'evidence': {'/' + 'é' * 128: digest}}))

    def test_evidence_requires_absolute_control_free_paths_and_digest_values(self):
        for key in ('', 'relative', 'C:/windows', '/line\nbreak', '/nul\x00key', '/del\x7fkey', '/c1\x85key', '/bidi\u202ekey'):
            with self.subTest(key=repr(key)), self.assertRaises(store.Refused):
                self.decode_fixture(store.canonical({'evidence': {key: 'a' * 64}}))
        for digest in ('short', 'g' * 64, None, 7, ['a' * 64], {'/nested': 'a' * 64}):
            with self.subTest(digest_type=type(digest).__name__), self.assertRaises(store.Refused):
                self.decode_fixture(store.canonical({'evidence': {'/owned': digest}}))
        for evidence in (None, [], 'not a mapping'):
            with self.assertRaises(store.Refused):
                self.decode_fixture(store.canonical({'evidence': evidence}))
        with self.assertRaises(store.Refused):
            self.decode_fixture(b'{}')

    def test_duplicate_keys_at_all_levels_and_invalid_utf8_are_refused(self):
        raw_cases = (
            b'{"evidence":{},"evidence":{}}',
            b'{"evidence":{"/owned":"' + b'a' * 64 + b'","/owned":"' + b'a' * 64 + b'"}}',
            b'{"evidence":{},"nested":{"x":1,"x":2}}',
            b'{"evidence":{},"bad":"\xff"}',
        )
        for raw in raw_cases:
            with self.subTest(raw_length=len(raw)), self.assertRaises(store.Refused):
                self.decode_fixture(raw)

    def test_frame_depth_collection_and_scalar_limits_remain_unchanged(self):
        deep = 0
        for _ in range(16):
            deep = [deep]
        cases = (
            store.canonical(self.sample(other=deep)),
            store.canonical({'evidence': {'/' + str(index): 'a' * 64 for index in range(65)}}),
            store.canonical(self.sample(other=list(range(129)))),
            store.canonical({**{str(index): True for index in range(64)}, 'evidence': {}}),
            store.canonical(self.sample(padding='x' * store.MAX_FRAME)),
            store.canonical(self.sample(number=1.5)),
            b'{"evidence":{},"number":NaN}',
            b'{"evidence":{},"number":Infinity}',
            b'{"evidence":{},"number":-Infinity}',
            b'',
        )
        for raw in cases:
            with self.subTest(raw_length=len(raw)), self.assertRaises(store.Refused):
                self.decode_fixture(raw)


class PendingPromptRetirementTests(unittest.TestCase):
    # Owned files and bus metadata fixtures only; never a real keyring reset.

    def setUp(self):
        DummyRetirementTests.setUp(self)
        store.retire_known_dummy(self.failure)
        predecessor = (self.directory / store._archive_name()).read_bytes()
        self.enterContext(patch.object(store, 'PENDING_PREDECESSOR_SHA', hashlib.sha256(predecessor).hexdigest()))
        value = store._new_selection(store.PENDING_OPERATION, SimpleNamespace(owner=':1.0', connection=SimpleNamespace(get_unique_name=lambda: ':1.20377')), store.OLD_DUMMY_OPERATION)
        value.pop('prompt_observation')
        value.update(state='PROMPT_PENDING', requested_label='CultureCabinetd8276bb67a8f41beb17e82bac48a3609', returned_prompt_ref='/org/freedesktop/secrets/prompt/p2')
        self.value = value
        self.pending_failure = store.canonical({'corrected_operation_id': store.PENDING_OPERATION, 'returned_collection_ref': None, 'real_api_keys_used': False, 'canary_state': 'NOT_STARTED', 'master_phase': 'NOT_STARTED', 'evidence': pending_evidence_fixture()})
        self.caller_closed = self.enterContext(patch.object(store._SecretBackend, '_original_caller_closed', return_value=True))
        self.set_pending(value)

    def set_pending(self, value):
        self.pending_original = store.canonical(value)
        self.pointer.write_bytes(self.pending_original)
        self.pointer.chmod(384)
        self.terminal = store.canonical({'selection': value, 'stage': 'CORRECTED_PROVISION_REFUSED_NO_RETRY', 'provision_status': 'UNAVAILABLE', 'no_real_key': True, 'provider_requests': 0})
        for name, raw in (('PENDING_SELECTION_SHA', self.pending_original), ('PENDING_FAILURE_SHA', self.pending_failure), ('PENDING_TERMINAL_SHA', self.terminal)):
            self.enterContext(patch.object(store, name, hashlib.sha256(raw).hexdigest()))

    def retire(self):
        return store.retire_prompt_pending_dummy(self.pending_failure, self.terminal)

    def assert_pending_refused(self):
        with self.assertRaises(store.Refused):
            self.retire()
        self.assertTrue(self.pointer.exists())
        self.assertFalse((self.directory / store._pending_archive_name()).exists())

    def test_exact_pre_master_archive_preserves_bytes_and_requires_explicit_link_for_next_setup(self):
        predecessor = (self.directory / store._archive_name()).read_bytes()
        self.assertEqual(self.retire(), store.PENDING_OPERATION)
        self.assertFalse(self.pointer.exists())
        archived = self.directory / store._pending_archive_name()
        self.assertEqual(archived.stat().st_mode & 511, 256)
        value = store._validate_pending_archive(archived.read_bytes())
        self.assertEqual(value['original_selection'].encode(), self.pending_original)
        self.assertEqual(value['terminal_receipt'].encode(), self.terminal)
        self.assertEqual(value['failure_receipt'].encode(), self.pending_failure)
        self.assertEqual(len(store._pending_failure_json(self.pending_failure)['evidence']), 6)
        self.assertEqual(max(len(key.encode()) for key in json.loads(self.pending_failure)['evidence']), 139)
        with self.assertRaises(store.Refused):
            store.strict_json(self.pending_failure)
        store.strict_json(archived.read_bytes())  # Embedded original bytes are strings, not wrapper keys.
        self.assertFalse(value['os_orphan_deleted'])
        self.assertEqual(value['possible_collection_exists'], 'unknown')
        self.assertEqual((self.directory / store._archive_name()).read_bytes(), predecessor)
        store._check_continuation(store.PENDING_OPERATION)
        for reference in (None, store.OLD_DUMMY_OPERATION, '10000000-0000-4000-8000-000000000001'):
            with self.assertRaises(store.Refused):
                store._check_continuation(reference)

    def test_changed_pointer_cannot_retire(self):
        self.pointer.write_bytes(self.pending_original + b' ')
        self.assert_pending_refused()

    def test_post_master_cannot_retire_even_with_matching_test_hash(self):
        self.set_pending({**self.value, 'master_phase': 'WRITE_PENDING'})
        self.assert_pending_refused()

    def test_live_original_caller_cannot_retire(self):
        self.caller_closed.return_value = False
        self.assert_pending_refused()

    def test_wrong_failure_receipt_cannot_retire(self):
        with self.assertRaises(store.Refused):
            store.retire_prompt_pending_dummy(self.pending_failure + b' ', self.terminal)
        self.assertTrue(self.pointer.exists())

    def test_pointer_change_after_archive_is_not_unlinked_and_archive_is_retained(self):
        read = store._read_private_at
        calls = []

        def race(directory, name, *args):
            if name == 'selection.json':
                calls.append(name)
                if len(calls) == 2:
                    self.pointer.write_bytes(self.pending_original + b' ')
            return read(directory, name, *args)
        with patch.object(store, '_read_private_at', side_effect=race), self.assertRaises(store.Refused):
            self.retire()
        self.assertEqual(self.pointer.read_bytes(), self.pending_original + b' ')
        self.assertTrue((self.directory / store._pending_archive_name()).exists())

    def test_retained_completed_and_canary_progress_refuse_even_with_matching_fixture_hashes(self):
        for change in ({'state': 'CREATED_UNVERIFIED', 'returned_collection_ref': store.COLLECTION_ROOT + 'returned'}, {'canary_state': 'CREATE_PENDING'}, {'creation_operation_id': str(__import__('uuid').uuid4())}):
            with self.subTest(change=change):
                self.set_pending({**self.value, **change})
                self.assert_pending_refused()

    def test_identical_archive_replay_after_failed_file_fsync_resyncs_file_before_pointer_removal(self):
        original_sync = os.fsync
        with patch.object(store.os, 'fsync', side_effect=OSError()), self.assertRaises(OSError):
            self.retire()
        self.assertTrue(self.pointer.exists())
        archive = self.directory / store._pending_archive_name()
        raw = archive.read_bytes()
        identity = archive.stat().st_ino
        synced = []

        def sync(fd):
            synced.append(os.fstat(fd).st_ino)
            original_sync(fd)
        with patch.object(store.os, 'fsync', side_effect=sync):
            self.retire()
        self.assertEqual(archive.read_bytes(), raw)
        self.assertEqual(archive.stat().st_ino, identity)
        self.assertEqual(synced[0], identity)
        self.assertFalse(self.pointer.exists())

    def test_mismatched_existing_archive_refuses_without_overwrite(self):
        archive = self.directory / store._pending_archive_name()
        archive.write_bytes(b'foreign')
        archive.chmod(256)
        with self.assertRaises(store.Refused):
            self.retire()
        self.assertEqual(archive.read_bytes(), b'foreign')
        self.assertTrue(self.pointer.exists())

    def test_second_archive_requires_exact_immutable_predecessor(self):
        self.retire()
        predecessor = self.directory / store._archive_name()
        original = predecessor.read_bytes()
        predecessor.chmod(384)
        with self.assertRaises(store.Refused):
            store._check_continuation(store.PENDING_OPERATION)
        predecessor.write_bytes(original + b' ')
        predecessor.chmod(256)
        with self.assertRaises(store.Refused):
            store._check_continuation(store.PENDING_OPERATION)
        predecessor.unlink()
        with self.assertRaises(FileNotFoundError):
            store._check_continuation(store.PENDING_OPERATION)

    def test_terminal_only_entry_and_nonmatching_terminal_refuse_before_connect(self):
        with patch.object(store.os, 'isatty', return_value=False), self.assertRaises(store.Refused):
            self.retire()
        with self.assertRaises(store.Refused):
            store.retire_prompt_pending_dummy(self.pending_failure, self.terminal + b' ')
        self.assertTrue(self.pointer.exists())

    def test_malformed_receipt_refuses_before_backend_or_pointer_mutation(self):
        failure = json.loads(self.pending_failure)
        for evidence in ({'/' + 'x' * 256: 'a' * 64}, {'/owned': 'bad-digest'}, ['wrong-type']):
            raw = store.canonical({**failure, 'evidence': evidence})
            with self.subTest(evidence_type=type(evidence).__name__), \
                 patch.object(store, 'PENDING_FAILURE_SHA', hashlib.sha256(raw).hexdigest()), \
                 patch.object(store._SecretBackend, '_connect') as connect, self.assertRaises(store.Refused):
                store.retire_prompt_pending_dummy(raw, self.terminal)
            connect.assert_not_called()
            self.assertEqual(self.pointer.read_bytes(), self.pending_original)
            self.assertFalse((self.directory / store._pending_archive_name()).exists())

    def test_original_caller_check_uses_only_exact_bus_metadata(self):
        from gi.repository import GLib
        backend = object.__new__(store._SecretBackend)
        backend.GLib = GLib
        backend._verify_service = lambda: None
        calls = []
        backend._dbus = lambda *args: (calls.append(args), GLib.Variant('(b)', (False,)))[1]
        with patch.object(store._SecretBackend, '_original_caller_closed', original_caller_check):
            self.assertTrue(backend._original_caller_closed())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], 'NameHasOwner')
        self.assertEqual(calls[0][4].unpack(), (':1.20377',))
# Save the implementation before fixture patches; no bus is contacted.
original_caller_check = store._SecretBackend._original_caller_closed

class PromptObservationTests(unittest.TestCase):

    def run_prompt(self, reply, *, fail_ack=False, closed=False):
        worker = store._Async(time.monotonic() + 0.02)
        V = worker.GLib.Variant
        observed = []
        captured = []

        class Connection:

            def signal_subscribe(self, *args):
                self.callback = args[-2]
                return 1

            def call(self, owner, path, interface, method, parameters, *args):
                if reply is not None:
                    self.callback(self, owner, path, interface, 'Completed', reply)
                args[-2](self, object())

            def call_finish(self, result):
                if fail_ack:
                    raise RuntimeError()
                return V('()', ())

            def is_closed(self):
                return closed

            def signal_unsubscribe(self, subscription):
                pass
        try:
            value = worker.prompt(Connection(), ':1.2', '/org/freedesktop/secrets/prompt/owned', capture=captured.append, observe=lambda field, value: observed.append((field, value)))
        except store.Refused:
            value = None
        return (value, observed, captured)

    def test_ack_completion_dismissal_malformed_timeout_and_closed_are_actual_distinct_events(self):
        from gi.repository import GLib
        V = GLib.Variant
        path = store.COLLECTION_ROOT + 'owned'
        value, observed, captured = self.run_prompt(V('(bv)', (False, V('o', path))))
        self.assertEqual((value, captured), (path, [path]))
        self.assertIn(('completion', 'RETURNED'), observed)
        self.assertIn(('acknowledgement', 'ACKNOWLEDGED'), observed)
        for reply, expected in ((V('(bv)', (True, V('o', path))), 'DISMISSED'), (V('(bv)', (False, V('s', path))), 'MALFORMED_OR_UNRETAINED')):
            value, observed, captured = self.run_prompt(reply)
            self.assertIsNone(value)
            self.assertEqual(captured, [])
            self.assertIn(('completion', expected), observed)
            self.assertNotIn(('termination', 'TIMEOUT'), observed)
        value, observed, captured = self.run_prompt(None)
        self.assertIn(('termination', 'TIMEOUT'), observed)
        value, observed, captured = self.run_prompt(None, fail_ack=True, closed=True)
        self.assertIn(('termination', 'CONNECTION_CLOSED'), observed)
        self.assertNotIn(('termination', 'TIMEOUT'), observed)

    def test_delayed_valid_completion_survives_failed_ack_and_is_not_replayed(self):
        from gi.repository import GLib
        path = store.COLLECTION_ROOT + 'retained'
        value, observed, captured = self.run_prompt(GLib.Variant('(bv)', (False, GLib.Variant('o', path))), fail_ack=True)
        self.assertIsNone(value)
        self.assertEqual(captured, [path])
        self.assertIn(('completion', 'RETURNED'), observed)
        self.assertIn(('termination', 'CALL_FAILED'), observed)
        self.assertNotIn(('termination', 'TIMEOUT'), observed)


class OwnedDialogRetirementTests(unittest.TestCase):
    def set_pending(self, value):
        PendingPromptRetirementTests.set_pending(self, value)

    def setUp(self):
        PendingPromptRetirementTests.setUp(self)
        store.retire_prompt_pending_dummy(self.pending_failure, self.terminal)
        self.second = self.directory / store._pending_archive_name()
        self.predecessor = self.second.read_bytes()
        self.enterContext(patch.object(store, 'DIALOG_PREDECESSOR_SHA', hashlib.sha256(self.predecessor).hexdigest()))
        self.value = json.loads(DIALOG_ACTUAL_POSITIVE)['selection']
        self.original = store.canonical(self.value)
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), store.DIALOG_SELECTION_SHA)
        self.pointer.write_bytes(self.original)
        self.pointer.chmod(0o600)
        self.closed = self.enterContext(patch.object(store._SecretBackend, '_dialog_caller_closed', return_value=True))

    def retire(self, failure=DIALOG_ACTUAL_FAILURE, positive=DIALOG_ACTUAL_POSITIVE,
               entered=DIALOG_ACTUAL_INPUT, caller=DIALOG_ACTUAL_CALLER):
        return store.retire_owned_dialog_dummy(failure, positive, entered, caller)

    def assert_untouched(self):
        self.assertTrue(self.pointer.exists())
        self.assertFalse((self.directory / store._dialog_archive_name()).exists())

    def test_actual_receipt_bytes_full_three_link_retirement_reread_and_continuation(self):
        self.assertEqual(hashlib.sha256(DIALOG_ACTUAL_FAILURE).hexdigest(), store.DIALOG_FAILURE_SHA)
        self.assertEqual(hashlib.sha256(DIALOG_ACTUAL_POSITIVE).hexdigest(), store.DIALOG_TERMINAL_SHA)
        self.assertEqual(hashlib.sha256(DIALOG_ACTUAL_INPUT).hexdigest(), store.DIALOG_INPUT_SHA)
        self.assertEqual(hashlib.sha256(DIALOG_ACTUAL_CALLER).hexdigest(), store.DIALOG_CALLER_SHA)
        raw = store._dialog_archive(self.original, DIALOG_ACTUAL_FAILURE, DIALOG_ACTUAL_POSITIVE)
        self.assertLessEqual(len(raw), 32768)
        first = (self.directory / store._archive_name()).read_bytes()
        self.assertEqual(self.retire(), store.DIALOG_OPERATION)
        self.assertFalse(self.pointer.exists())
        archive = self.directory / store._dialog_archive_name()
        self.assertEqual(archive.stat().st_mode & 0o777, 0o400)
        saved = store._validate_pending_archive(archive.read_bytes())
        self.assertEqual(saved['failure_receipt'].encode(), DIALOG_ACTUAL_FAILURE)
        self.assertEqual(saved['terminal_receipt'].encode(), DIALOG_ACTUAL_POSITIVE)
        self.assertEqual(saved['original_selection'].encode(), self.original)
        self.assertEqual(self.second.read_bytes(), self.predecessor)
        self.assertEqual((self.directory / store._archive_name()).read_bytes(), first)
        store._check_continuation(store.DIALOG_OPERATION)
        for link in (None, store.OLD_DUMMY_OPERATION, store.PENDING_OPERATION):
            with self.assertRaises(store.Refused):
                store._check_continuation(link)

    def test_wrong_input_caller_failure_or_positive_refuses_before_connect(self):
        for index, raw in enumerate((DIALOG_ACTUAL_FAILURE, DIALOG_ACTUAL_POSITIVE,
                                     DIALOG_ACTUAL_INPUT, DIALOG_ACTUAL_CALLER)):
            args = [DIALOG_ACTUAL_FAILURE, DIALOG_ACTUAL_POSITIVE, DIALOG_ACTUAL_INPUT, DIALOG_ACTUAL_CALLER]
            args[index] = raw + b' '
            with self.subTest(index=index), patch.object(store._SecretBackend, '_connect') as connect, self.assertRaises(store.Refused):
                self.retire(*args)
            connect.assert_not_called()
            self.assert_untouched()

    def test_live_current_caller_refuses_without_archive_or_provision(self):
        self.closed.return_value = False
        with patch.object(store, '_provision_worker') as provision, self.assertRaises(store.Refused):
            self.retire()
        provision.assert_not_called()
        self.assert_untouched()

    def test_identity_ack_completed_and_progress_changes_refuse(self):
        cases = ({'creation_operation_id': store.PENDING_OPERATION},
                 {'returned_prompt_ref': '/org/freedesktop/secrets/prompt/other'},
                 {'service_provenance': {**self.value['service_provenance'], 'caller': ':1.9'}},
                 {'state': 'CREATED_UNVERIFIED', 'returned_collection_ref': store.COLLECTION_ROOT + 'owned'},
                 {'canary_state': 'CREATE_PENDING'}, {'master_phase': 'WRITE_PENDING'},
                 {'prompt_observation': {**self.value['prompt_observation'], 'completion': 'RETURNED'}},
                 {'prompt_observation': {**self.value['prompt_observation'], 'termination': 'TIMEOUT'}})
        for changes in cases:
            raw = store.canonical({**self.value, **changes})
            self.pointer.write_bytes(raw)
            with self.subTest(changes=changes), patch.object(store, 'DIALOG_SELECTION_SHA', hashlib.sha256(raw).hexdigest()), \
                 patch.object(store._SecretBackend, '_connect') as connect, self.assertRaises(store.Refused):
                self.retire()
            connect.assert_not_called()
            self.assert_untouched()

    def test_replaced_inode_or_changed_bytes_after_archive_prevent_unlink(self):
        read = store._read_private_at
        reads = []
        def race(directory, name, *args):
            if name == 'selection.json':
                reads.append(name)
                if len(reads) == 2:
                    replacement = self.directory / 'replacement'
                    replacement.write_bytes(self.original)
                    replacement.chmod(0o600)
                    os.replace(replacement, self.pointer)
            return read(directory, name, *args)
        with patch.object(store, '_read_private_at', side_effect=race), self.assertRaises(store.Refused):
            self.retire()
        self.assertEqual(self.pointer.read_bytes(), self.original)
        self.assertTrue((self.directory / store._dialog_archive_name()).exists())

    def test_fsync_interruption_identical_replay_and_mismatched_archive(self):
        with patch.object(store.os, 'fsync', side_effect=OSError()), self.assertRaises(OSError):
            self.retire()
        self.assertTrue(self.pointer.exists())
        archive = self.directory / store._dialog_archive_name()
        raw, inode = archive.read_bytes(), archive.stat().st_ino
        self.retire()
        self.assertEqual(archive.read_bytes(), raw)
        self.assertEqual(archive.stat().st_ino, inode)
        store._check_continuation(store.DIALOG_OPERATION)

    def test_ancestor_absence_or_hash_mismatch_refuses_before_pointer_or_provision(self):
        self.second.chmod(0o600)
        self.second.write_bytes(self.predecessor + b' ')
        self.second.chmod(0o400)
        with patch.object(store, '_provision_worker') as provision, self.assertRaises(store.Refused):
            self.retire()
        provision.assert_not_called()
        self.assert_untouched()
        self.second.unlink()
        with self.assertRaises(FileNotFoundError):
            self.retire()
        self.assert_untouched()

    def test_third_tombstone_needs_both_ancestors_and_cannot_use_second_under_wrong_name(self):
        third = self.directory / store._dialog_archive_name()
        third.write_bytes(self.predecessor)
        third.chmod(0o400)
        with self.assertRaises(store.Refused):
            store._check_continuation(store.DIALOG_OPERATION)
        third.unlink()
        self.retire()
        self.second.unlink()
        with self.assertRaises(FileNotFoundError):
            store._check_continuation(store.DIALOG_OPERATION)

    def test_terminal_only_entry_and_input_progress_refuse(self):
        with patch.object(store.os, 'isatty', return_value=False), self.assertRaises(store.Refused):
            self.retire()
        value = json.loads(DIALOG_ACTUAL_INPUT)
        value['attempts'] = 1
        raw = store.canonical(value)
        with patch.object(store, 'DIALOG_INPUT_SHA', hashlib.sha256(raw).hexdigest()), self.assertRaises(store.Refused):
            self.retire(entered=raw)
        self.assert_untouched()

    def test_mismatched_third_archive_is_not_overwritten(self):
        archive = self.directory / store._dialog_archive_name()
        archive.write_bytes(b'foreign archived bytes')
        archive.chmod(0o400)
        with self.assertRaises(store.Refused):
            self.retire()
        self.assertEqual(archive.read_bytes(), b'foreign archived bytes')
        self.assertEqual(self.pointer.read_bytes(), self.original)

    def test_third_continuation_requires_first_ancestor_too(self):
        self.retire()
        (self.directory / store._archive_name()).unlink()
        with self.assertRaises(FileNotFoundError):
            store._check_continuation(store.DIALOG_OPERATION)


class PairReceiptCodecTests(unittest.TestCase):
    def test_actual_new_terminal_and_positive_decode_only_in_their_pinned_private_profiles(self):
        failure=store._pending_failure_json(PAIR_ACTUAL_FAILURE)
        positive=store._pair_positive_json(PAIR_ACTUAL_POSITIVE)
        self.assertEqual(failure['operation_id'],store.PAIR_OPERATION)
        self.assertEqual(failure['original_bounds']['monitor_original_deadline_monotonic'],store.PAIR_RECORDED_CLOCK)
        self.assertEqual(positive['monitor_original_deadline_monotonic'],store.PAIR_RECORDED_CLOCK)
        for raw in (PAIR_ACTUAL_FAILURE,PAIR_ACTUAL_POSITIVE):
            with self.assertRaises(store.Refused):store.strict_json(raw)
        with self.assertRaises(store.Refused):store._pair_positive_json(PAIR_ACTUAL_POSITIVE+b' ')

    def test_recorded_clock_does_not_permit_other_paths_values_or_nonfinite_numbers(self):
        cases=(
            store.canonical({'evidence':{},'original_bounds':{'monitor_original_deadline_monotonic':store.PAIR_RECORDED_CLOCK},'other':1.5}),
            store.canonical({'evidence':{},'original_bounds':{'monitor_original_deadline_monotonic':store.PAIR_RECORDED_CLOCK+1}}),
            b'{"evidence":{},"original_bounds":{"monitor_original_deadline_monotonic":NaN}}',
        )
        for raw in cases:
            with self.subTest(raw_length=len(raw)),patch.object(store,'PAIR_FAILURE_SHA',hashlib.sha256(raw).hexdigest()),self.assertRaises(store.Refused):
                store._pending_failure_json(raw)
        raw=store.canonical({'monitor_original_deadline_monotonic':store.PAIR_RECORDED_CLOCK,'other':2.5})
        with patch.object(store,'PAIR_TERMINAL_SHA',hashlib.sha256(raw).hexdigest()),self.assertRaises(store.Refused):
            store._pair_positive_json(raw)


class PasswordPairRetirementTests(unittest.TestCase):
    def set_pending(self,value):PendingPromptRetirementTests.set_pending(self,value)
    def setUp(self):
        OwnedDialogRetirementTests.setUp(self)
        store.retire_owned_dialog_dummy(DIALOG_ACTUAL_FAILURE,DIALOG_ACTUAL_POSITIVE,DIALOG_ACTUAL_INPUT,DIALOG_ACTUAL_CALLER)
        self.third=self.directory/store._dialog_archive_name()
        self.third_bytes=self.third.read_bytes()
        self.enterContext(patch.object(store,'PAIR_PREDECESSOR_SHA',hashlib.sha256(self.third_bytes).hexdigest()))
        self.value=json.loads(PAIR_ACTUAL_POSITIVE)['selection']
        self.original=store.canonical(self.value)
        self.assertEqual(hashlib.sha256(self.original).hexdigest(),store.PAIR_SELECTION_SHA)
        self.pointer.write_bytes(self.original);self.pointer.chmod(0o600)
        self.pair_closed=self.enterContext(patch.object(store._SecretBackend,'_pair_caller_closed',return_value=True))
    def retire(self,failure=PAIR_ACTUAL_FAILURE,positive=PAIR_ACTUAL_POSITIVE,entered=PAIR_ACTUAL_INPUT,caller=PAIR_ACTUAL_CALLER):
        return store.retire_password_pair_dummy(failure,positive,entered,caller)
    def assert_untouched(self):
        self.assertTrue(self.pointer.exists())
        self.assertFalse((self.directory/store._pair_archive_name()).exists())

    def test_actual_shapes_retirement_archive_reread_and_all_four_links(self):
        first=(self.directory/store._archive_name()).read_bytes()
        second=(self.directory/store._pending_archive_name()).read_bytes()
        self.assertEqual(self.retire(),store.PAIR_OPERATION)
        archive=self.directory/store._pair_archive_name()
        self.assertEqual(archive.stat().st_mode & 0o777,0o400)
        raw=archive.read_bytes();self.assertLessEqual(len(raw),32768)
        saved=store._validate_pending_archive(raw)
        self.assertEqual(saved['original_selection'].encode(),self.original)
        self.assertEqual(saved['failure_receipt'].encode(),PAIR_ACTUAL_FAILURE)
        self.assertEqual(saved['terminal_receipt'].encode(),PAIR_ACTUAL_POSITIVE)
        self.assertEqual(self.third.read_bytes(),self.third_bytes)
        self.assertEqual((self.directory/store._archive_name()).read_bytes(),first)
        self.assertEqual((self.directory/store._pending_archive_name()).read_bytes(),second)
        store._check_continuation(store.PAIR_OPERATION)
        for ref in (None,store.OLD_DUMMY_OPERATION,store.PENDING_OPERATION,store.DIALOG_OPERATION):
            with self.assertRaises(store.Refused):store._check_continuation(ref)

    def test_changed_four_receipt_hashes_refuse_before_connection_or_pointer_effect(self):
        for index,raw in enumerate((PAIR_ACTUAL_FAILURE,PAIR_ACTUAL_POSITIVE,PAIR_ACTUAL_INPUT,PAIR_ACTUAL_CALLER)):
            args=[PAIR_ACTUAL_FAILURE,PAIR_ACTUAL_POSITIVE,PAIR_ACTUAL_INPUT,PAIR_ACTUAL_CALLER]
            args[index]=raw+b' '
            with self.subTest(index=index),patch.object(store._SecretBackend,'_connect') as connect,self.assertRaises(store.Refused):self.retire(*args)
            connect.assert_not_called();self.assert_untouched()

    def test_nonnull_result_identity_or_progress_refuses_even_with_fixture_hash(self):
        for changes in ({'creation_operation_id':store.DIALOG_OPERATION},{'returned_prompt_ref':'/org/freedesktop/secrets/prompt/other'},
                        {'service_provenance':{**self.value['service_provenance'],'caller':':1.999'}},
                        {'state':'CREATED_UNVERIFIED','returned_collection_ref':store.COLLECTION_ROOT+'owned'},
                        {'canary_state':'CREATE_PENDING'},{'master_phase':'WRITE_PENDING'},
                        {'prompt_observation':{**self.value['prompt_observation'],'completion':'RETURNED'}}):
            raw=store.canonical({**self.value,**changes});self.pointer.write_bytes(raw)
            with self.subTest(changes=changes),patch.object(store,'PAIR_SELECTION_SHA',hashlib.sha256(raw).hexdigest()),self.assertRaises(store.Refused):self.retire()
            self.assert_untouched()

    def test_live_caller_malformed_caller_schema_or_input_progress_refuse(self):
        self.pair_closed.return_value=False
        with self.assertRaises(store.Refused):self.retire()
        self.assert_untouched();self.pair_closed.return_value=True
        entered=json.loads(PAIR_ACTUAL_INPUT);entered['attempts']=1;raw=store.canonical(entered)
        with patch.object(store,'PAIR_INPUT_SHA',hashlib.sha256(raw).hexdigest()),self.assertRaises(store.Refused):self.retire(entered=raw)
        self.assert_untouched()
        with self.assertRaises(store.Refused):self.retire(caller=DIALOG_ACTUAL_CALLER)
        self.assert_untouched()

    def test_changed_selection_inode_after_archive_is_never_unlinked(self):
        read=store._read_private_at;seen=[]
        def race(directory,name,*args):
            if name=='selection.json':
                seen.append(name)
                if len(seen)==2:
                    replacement=self.directory/'replacement'
                    replacement.write_bytes(self.original);replacement.chmod(0o600)
                    os.replace(replacement,self.pointer)
            return read(directory,name,*args)
        with patch.object(store,'_read_private_at',side_effect=race),self.assertRaises(store.Refused):self.retire()
        self.assertEqual(self.pointer.read_bytes(),self.original)
        self.assertTrue((self.directory/store._pair_archive_name()).exists())

    def test_interrupted_fsync_identical_replay_and_foreign_archive_refusal(self):
        with patch.object(store.os,'fsync',side_effect=OSError()),self.assertRaises(OSError):self.retire()
        self.assertTrue(self.pointer.exists())
        archive=self.directory/store._pair_archive_name();raw=archive.read_bytes();inode=archive.stat().st_ino
        self.retire();self.assertEqual(archive.read_bytes(),raw);self.assertEqual(archive.stat().st_ino,inode)
        store._check_continuation(store.PAIR_OPERATION)

    def test_missing_wrong_ancestor_or_foreign_new_archive_never_provisions(self):
        archive=self.directory/store._pair_archive_name();archive.write_bytes(b'foreign');archive.chmod(0o400)
        with patch.object(store,'_provision_worker') as provision,self.assertRaises(store.Refused):self.retire()
        provision.assert_not_called();self.assertTrue(self.pointer.exists());self.assertEqual(archive.read_bytes(),b'foreign')
        archive.unlink();self.third.chmod(0o600);self.third.write_bytes(self.third_bytes+b' ');self.third.chmod(0o400)
        with self.assertRaises(store.Refused):self.retire()
        self.assert_untouched();self.third.unlink()
        with self.assertRaises(FileNotFoundError):self.retire()
        self.assert_untouched()

    def test_terminal_and_envelope_size_guards_precede_mutation(self):
        with patch.object(store.os,'isatty',return_value=False),self.assertRaises(store.Refused):self.retire()
        self.assert_untouched()
        original_canonical=store.canonical
        def oversized(value):
            raw=original_canonical(value)
            return raw+b' '*(32769-len(raw)) if value.get('schema')=='cabinet-abandoned-prompt/v1' else raw
        with patch.object(store,'canonical',side_effect=oversized),patch.object(store._SecretBackend,'_connect') as connect,self.assertRaises(store.Refused):self.retire()
        connect.assert_not_called();self.assert_untouched()


# Frozen nonsecret actual receipts, embedded so tests never read operator state.
OWNER_ENROLLMENT = b'{\n  "state": "CANCELLED_OR_UNCERTAIN_NO_RETRY",\n  "stage": "enroll",\n  "source_head": "7af0023c70f1fde66b4da94bfb2557dd5f57974c",\n  "source_tree": "bedcd9cb5e77de06772d98b22fe82108350f687d",\n  "keyring_helper_sha256": "3466ea63ed310dae59bde7ce4d55eabcdeac8bd6d6717d1596cfe2098a925697",\n  "wrapper_sha256": "4e3729d41d3ad74093b1cc5a2b258c75b9a32ddf9e67ad530c8c6f51bb6e1bc4",\n  "profile": "owner-operated-os/v1",\n  "custody_mode": "owner_os_prompt",\n  "real_key": false,\n  "provider_calls": 0,\n  "started_utc": "2026-11-11T08:48:03.972137+00:00",\n  "parent_generation": [\n    1616904,\n    135612024\n  ],\n  "provision_bound_seconds": 120,\n  "owner_os_prompt_only": true,\n  "provision_effect_pending": true,\n  "provision_result": {\n    "status": "UNAVAILABLE",\n    "collection_ref": null\n  },\n  "error_type": "RuntimeError",\n  "reason": "PROVISION_UNAVAILABLE_OR_UNCERTAIN_NO_RETRY",\n  "owned_metadata_callers_closed": [],\n  "cancelled": false,\n  "handled_signals": [],\n  "last_selection": {\n    "canary_prompt_ref": null,\n    "canary_ref": null,\n    "canary_state": "NOT_STARTED",\n    "continuation_archive": "1cc2bd37-8f62-420d-928d-ec9c28945013",\n    "creation_operation_id": "2806e95c-fcb6-4ab8-94a7-b090bc52affc",\n    "decoded_identifier": null,\n    "file_binding": null,\n    "keyring_directory": "/home/tang/.local/share/keyrings",\n    "master_phase": "NOT_STARTED",\n    "prompt_observation": {\n      "acknowledgement": "ACKNOWLEDGED",\n      "completion": "UNOBSERVED",\n      "termination": "UNOBSERVED"\n    },\n    "requested_label": "CultureCabineta915d0e1fe954b27bb810365171828e2",\n    "returned_collection_ref": null,\n    "returned_prompt_ref": "/org/freedesktop/secrets/prompt/p5",\n    "schema": "cabinet-key-store-selection/v2",\n    "service_provenance": {\n      "caller": ":1.24305",\n      "implementation": "gnome-keyring-51.1",\n      "service_owner": ":1.0",\n      "service_uid": 1000\n    },\n    "state": "PROMPT_PENDING",\n    "terminal_fact": null,\n    "verified_filename": null\n  }\n}\n'
OWNER_BINDING = b'{\n  "state": "ACTUAL_OWNER_PROMPT_PENDING",\n  "selection_sha256": "6d962b12224290b5820c7258797462f57e714bbe7d23cb5775415c56f8f47779",\n  "selection": {\n    "canary_prompt_ref": null,\n    "canary_ref": null,\n    "canary_state": "NOT_STARTED",\n    "continuation_archive": "1cc2bd37-8f62-420d-928d-ec9c28945013",\n    "creation_operation_id": "2806e95c-fcb6-4ab8-94a7-b090bc52affc",\n    "decoded_identifier": null,\n    "file_binding": null,\n    "keyring_directory": "/home/tang/.local/share/keyrings",\n    "master_phase": "NOT_STARTED",\n    "prompt_observation": {\n      "acknowledgement": "ACKNOWLEDGED",\n      "completion": "UNOBSERVED",\n      "termination": "UNOBSERVED"\n    },\n    "requested_label": "CultureCabineta915d0e1fe954b27bb810365171828e2",\n    "returned_collection_ref": null,\n    "returned_prompt_ref": "/org/freedesktop/secrets/prompt/p5",\n    "schema": "cabinet-key-store-selection/v2",\n    "service_provenance": {\n      "caller": ":1.24305",\n      "implementation": "gnome-keyring-51.1",\n      "service_owner": ":1.0",\n      "service_uid": 1000\n    },\n    "state": "PROMPT_PENDING",\n    "terminal_fact": null,\n    "verified_filename": null\n  },\n  "no_password_read_or_input": true,\n  "owner_ready": "DIRECT_REPLY_CONFIRMED_THROUGH_ROOT",\n  "original_bound_seconds": 120\n}\n'
OWNER_TERMINAL = b'{\n  "id": "OWNER_OS_ACTUAL_2806_TERMINAL_AND_CLEANUP_01",\n  "state": "ENDED_UNAVAILABLE_NO_REPLAY_NO_STORAGE_ACCEPTANCE",\n  "observed_utc": "2026-11-11T08:56:21.642822+00:00",\n  "accepted_packet_sha256": "090ce5d504b680a56f0e3a89572223c4737008ba24eccdea34a01ccdd9982dd6",\n  "source_head": "7af0023c70f1fde66b4da94bfb2557dd5f57974c",\n  "source_tree": "bedcd9cb5e77de06772d98b22fe82108350f687d",\n  "keyring_helper_sha256": "3466ea63ed310dae59bde7ce4d55eabcdeac8bd6d6717d1596cfe2098a925697",\n  "wrapper_sha256": "4e3729d41d3ad74093b1cc5a2b258c75b9a32ddf9e67ad530c8c6f51bb6e1bc4",\n  "retirement": {\n    "receipt": "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owner-os-positive-v2/owner-retire-01.json",\n    "sha256": "7643482cd49c7ffc59148480f646fdde8a49433133efa677d8f3fbaaa2f5c5ec",\n    "state": "PASS",\n    "operation": "1cc2bd37-8f62-420d-928d-ec9c28945013"\n  },\n  "prompt_binding": {\n    "receipt": "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owner-os-positive-v2/owner-actual-prompt-binding-01.json",\n    "sha256": "57a9bf752bce0cdc09fdf012a46fbc7bb0ffabcb9ebcf71c5ba21041a04e7807"\n  },\n  "enrollment": {\n    "receipt": "/home/tang/.codex/visualizations/2026/11/08/01a1b94d-3181-7451-84dc-55d2d7d637f8/api-wiring-2026-11-10/owner-os-positive-v2/owner-enroll-01.json",\n    "sha256": "0750f70efee6b6864bf665e22da8b04f2e1cb47445199efdcbb937f340bb7b62",\n    "exit_code": 1,\n    "original_bound_seconds": 120,\n    "root_observed_elapsed_ms": 120133,\n    "result": {\n      "status": "UNAVAILABLE",\n      "collection_ref": null\n    },\n    "cancelled": false,\n    "reason": "PROVISION_UNAVAILABLE_OR_UNCERTAIN_NO_RETRY"\n  },\n  "preserved_selection": {\n    "canary_prompt_ref": null,\n    "canary_ref": null,\n    "canary_state": "NOT_STARTED",\n    "continuation_archive": "1cc2bd37-8f62-420d-928d-ec9c28945013",\n    "creation_operation_id": "2806e95c-fcb6-4ab8-94a7-b090bc52affc",\n    "decoded_identifier": null,\n    "file_binding": null,\n    "keyring_directory": "/home/tang/.local/share/keyrings",\n    "master_phase": "NOT_STARTED",\n    "prompt_observation": {\n      "acknowledgement": "ACKNOWLEDGED",\n      "completion": "UNOBSERVED",\n      "termination": "UNOBSERVED"\n    },\n    "requested_label": "CultureCabineta915d0e1fe954b27bb810365171828e2",\n    "returned_collection_ref": null,\n    "returned_prompt_ref": "/org/freedesktop/secrets/prompt/p5",\n    "schema": "cabinet-key-store-selection/v2",\n    "service_provenance": {\n      "caller": ":1.24305",\n      "implementation": "gnome-keyring-51.1",\n      "service_owner": ":1.0",\n      "service_uid": 1000\n    },\n    "state": "PROMPT_PENDING",\n    "terminal_fact": null,\n    "verified_filename": null\n  },\n  "selection_sha256": "6d962b12224290b5820c7258797462f57e714bbe7d23cb5775415c56f8f47779",\n  "selection_identity": [\n    57,\n    1042776,\n    1000,\n    1000,\n    33152,\n    1,\n    830,\n    1794386884106007671,\n    1794386884108007672\n  ],\n  "cleanup": {\n    "original_parent_generation": [\n      1616904,\n      135612024\n    ],\n    "original_parent_generation_ended": true,\n    "original_caller": ":1.24305",\n    "caller_has_owner": false,\n    "gcr_windows_present": 0,\n    "accepted_provision_client_returned": true,\n    "helper_child": "Accepted source provision finally returned and wrapper exited; child reaping attributed to exact accepted client. No helper PID invented or orphan scan.",\n    "agent_password_keeper_ever_started": false,\n    "live_agent_password_custody": false,\n    "prompt_methods_after_exit": 0,\n    "collection_or_file_deletion": 0,\n    "os_collection_orphan_disposition": "UNKNOWN; no returned identity/no guessed cleanup target"\n  },\n  "actual_facts": {\n    "prompt_method_acknowledged": true,\n    "completed_result_retained": false,\n    "collection_ref": null,\n    "file_binding": null,\n    "canary": "NOT_STARTED",\n    "master": "NOT_STARTED",\n    "dummy_put": "NOT_STARTED",\n    "fresh_read": "NOT_STARTED",\n    "owner_native_handoff": "NOT_CREATED",\n    "native_proof": "NOT_STARTED",\n    "provider_calls": 0,\n    "real_key_used": false\n  },\n  "owner_observation": {\n    "source": "Direct owner messages relayed by Root",\n    "saw_expected_dialog": false,\n    "viewing": "remote desktop window on Mac",\n    "same_compositor_output_workspace_verified": false,\n    "rendered_dialog_observed_by_agent": false,\n    "password_or_widget_contents_inspected": false\n  },\n  "failure_scope": "Original owner-operated prompt did not supply retained Completed before original120s result UNAVAILABLE. ACK/expired client is not proof that no collection exists, and no exact cancellation/dismissal subtype is invented.",\n  "next": "No reenroll/replay/fresh-read. Root diagnoses actual remote-view/session match and routes exact ended2806 pointer disposition if needed. Existing hard-coded1cc2 retirement is not a generic2806 reset. Preserve raw current pointer/archive chain and all receipts."\n}\n'
OWNER_RETIREMENT = b'{\n  "state": "PASS",\n  "stage": "retire",\n  "source_head": "7af0023c70f1fde66b4da94bfb2557dd5f57974c",\n  "source_tree": "bedcd9cb5e77de06772d98b22fe82108350f687d",\n  "keyring_helper_sha256": "3466ea63ed310dae59bde7ce4d55eabcdeac8bd6d6717d1596cfe2098a925697",\n  "wrapper_sha256": "4e3729d41d3ad74093b1cc5a2b258c75b9a32ddf9e67ad530c8c6f51bb6e1bc4",\n  "profile": "owner-operated-os/v1",\n  "custody_mode": "owner_os_prompt",\n  "real_key": false,\n  "provider_calls": 0,\n  "started_utc": "2026-11-11T08:47:58.308553+00:00",\n  "retirement_effect_pending": true,\n  "retirement_returned_operation": "1cc2bd37-8f62-420d-928d-ec9c28945013",\n  "retirement_archive_chain": [\n    {\n      "operation_id": "f6ed3437-1f93-4564-8a6d-e558e5dd6462",\n      "archive_sha256": "184e1a5ac45fe03f4bd59659c3dca3dc15d2a67b873c619c86009533a09a9dfb"\n    },\n    {\n      "operation_id": "ce102960-02f0-4697-8008-371439a02d04",\n      "archive_sha256": "fe7a79afb45e22a6ac1d7b4cc4080d4ac5c60d1abc5f494277aa19900c12cc77"\n    },\n    {\n      "operation_id": "b8cc865d-df30-46da-969a-cf7fa40c6eba",\n      "archive_sha256": "9d6ada8785c4d05c8d4c77bb56754391ebe812da8d624273a4e0fef291d4819d"\n    },\n    {\n      "operation_id": "1cc2bd37-8f62-420d-928d-ec9c28945013",\n      "archive_sha256": "96de05d1e92531b76ce8d998e7ca7a1b24798bdf7df8a75b19219ea3e6dfd89d"\n    }\n  ],\n  "owned_metadata_callers_closed": [],\n  "cancelled": false,\n  "handled_signals": []\n}\n'

class OwnerPromptRetirementTests(unittest.TestCase):
    def set_pending(self, value):
        PendingPromptRetirementTests.set_pending(self, value)

    def setUp(self):
        PasswordPairRetirementTests.setUp(self)
        store.retire_password_pair_dummy(PAIR_ACTUAL_FAILURE, PAIR_ACTUAL_POSITIVE,
                                        PAIR_ACTUAL_INPUT, PAIR_ACTUAL_CALLER)
        self.fourth = self.directory / store._pair_archive_name()
        self.fourth_bytes = self.fourth.read_bytes()
        digest = hashlib.sha256(self.fourth_bytes).hexdigest()
        self.enterContext(patch.object(store, 'OWNER_PROMPT_PREDECESSOR_SHA', digest))
        retired = json.loads(OWNER_RETIREMENT)
        retired['retirement_archive_chain'][-1]['archive_sha256'] = digest
        self.retirement = store.canonical(retired)
        self.enterContext(patch.object(store, 'OWNER_PROMPT_RETIREMENT_SHA', hashlib.sha256(self.retirement).hexdigest()))
        terminal = json.loads(OWNER_TERMINAL)
        terminal['retirement']['sha256'] = store.OWNER_PROMPT_RETIREMENT_SHA
        self.terminal = store.canonical(terminal)
        self.enterContext(patch.object(store, 'OWNER_PROMPT_TERMINAL_SHA', hashlib.sha256(self.terminal).hexdigest()))
        self.value = json.loads(OWNER_BINDING)['selection']
        self.original = store.canonical(self.value)
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), store.OWNER_PROMPT_SELECTION_SHA)
        self.pointer.write_bytes(self.original); self.pointer.chmod(0o600)
        self.closed = self.enterContext(patch.object(store._SecretBackend, '_owner_prompt_caller_closed', return_value=True))
        self.ended = self.enterContext(patch.object(store, '_owner_prompt_parent_ended', return_value=True))

    def retire(self, enrollment=None, binding=None, terminal=None, retirement=None):
        return store.retire_owner_prompt_dummy(
            OWNER_ENROLLMENT if enrollment is None else enrollment,
            OWNER_BINDING if binding is None else binding,
            self.terminal if terminal is None else terminal,
            self.retirement if retirement is None else retirement)

    def untouched(self):
        self.assertEqual(self.pointer.read_bytes(), self.original)
        self.assertFalse((self.directory / store._owner_prompt_archive_name()).exists())

    def test_five_links_archive_exact_bytes_and_reject_every_old_continuation(self):
        ancestors = {p.name: p.read_bytes() for p in self.directory.glob('abandoned-*')}
        self.assertEqual(self.retire(), store.OWNER_PROMPT_OPERATION)
        archive = self.directory / store._owner_prompt_archive_name()
        self.assertEqual(archive.stat().st_mode & 0o777, 0o400)
        self.assertLessEqual(len(archive.read_bytes()), 32768)
        saved = store._validate_owner_prompt_archive(archive.read_bytes())
        for field, raw in (('original_selection',self.original), ('enrollment_receipt',OWNER_ENROLLMENT),
                ('prompt_binding_receipt',OWNER_BINDING), ('terminal_receipt',self.terminal),
                ('retirement_receipt',self.retirement)):
            self.assertEqual(saved[field].encode(),raw)
        for name, raw in ancestors.items(): self.assertEqual((self.directory/name).read_bytes(),raw)
        self.assertFalse(self.pointer.exists())
        store._check_continuation(store.OWNER_PROMPT_OPERATION)
        for old in (None,store.OLD_DUMMY_OPERATION,store.PENDING_OPERATION,store.DIALOG_OPERATION,store.PAIR_OPERATION):
            with self.subTest(old=old), self.assertRaises(store.Refused): store._check_continuation(old)

    def test_changed_each_receipt_refuses_before_connection(self):
        for name, raw in (('enrollment',OWNER_ENROLLMENT), ('binding',OWNER_BINDING),
                          ('terminal',self.terminal), ('retirement',self.retirement)):
            with self.subTest(name=name), patch.object(store._SecretBackend,'_connect') as connect, self.assertRaises(store.Refused):
                self.retire(**{name:raw+b' '})
            connect.assert_not_called();self.untouched()

    def test_live_caller_or_parent_refuses(self):
        for mock in (self.closed,self.ended):
            mock.return_value=False
            with self.assertRaises(store.Refused):self.retire()
            self.untouched();mock.return_value=True

    def test_changed_exact_selection_or_any_progress_refuses(self):
        for changes in ({'creation_operation_id':store.PAIR_OPERATION},
                {'requested_label':'CultureCabinet'+'0'*32},
                {'returned_prompt_ref':'/org/freedesktop/secrets/prompt/other'},
                {'service_provenance':{**self.value['service_provenance'],'caller':':1.999'}},
                {'returned_collection_ref':store.COLLECTION_ROOT+'owned'},
                {'canary_state':'CREATE_PENDING'}, {'master_phase':'WRITE_PENDING'},
                {'terminal_fact':'UNCERTAIN_OR_REFUSED'}):
            changed=store.canonical({**self.value,**changes});self.pointer.write_bytes(changed)
            with self.subTest(changes=changes),patch.object(store,'OWNER_PROMPT_SELECTION_SHA',hashlib.sha256(changed).hexdigest()),self.assertRaises(store.Refused):self.retire()
            self.assertFalse((self.directory/store._owner_prompt_archive_name()).exists())
        self.pointer.write_bytes(self.original)

    def test_cas_race_leaves_new_pointer_and_durable_archive(self):
        read=store._read_private_at;seen=[]
        def race(directory,name,*args):
            if name=='selection.json':
                seen.append(name)
                if len(seen)==2:
                    replacement=self.directory/'replacement';replacement.write_bytes(self.original)
                    replacement.chmod(0o600);os.replace(replacement,self.pointer)
            return read(directory,name,*args)
        with patch.object(store,'_read_private_at',side_effect=race),self.assertRaises(store.Refused):self.retire()
        self.assertEqual(self.pointer.read_bytes(),self.original)
        self.assertTrue((self.directory/store._owner_prompt_archive_name()).exists())

    def test_fsync_interruption_identical_archive_recovery(self):
        with patch.object(store.os,'fsync',side_effect=OSError()),self.assertRaises(OSError):self.retire()
        self.assertTrue(self.pointer.exists())
        archive=self.directory/store._owner_prompt_archive_name();raw=archive.read_bytes();inode=archive.stat().st_ino
        self.retire();self.assertEqual(archive.read_bytes(),raw);self.assertEqual(archive.stat().st_ino,inode)
        store._check_continuation(store.OWNER_PROMPT_OPERATION)

    def test_foreign_archive_or_changed_missing_predecessor_refuses(self):
        archive=self.directory/store._owner_prompt_archive_name();archive.write_bytes(b'foreign');archive.chmod(0o400)
        with self.assertRaises(store.Refused):self.retire()
        self.assertEqual(archive.read_bytes(),b'foreign');self.assertTrue(self.pointer.exists())
        archive.unlink();self.fourth.chmod(0o600);self.fourth.write_bytes(self.fourth_bytes+b' ');self.fourth.chmod(0o400)
        with self.assertRaises(store.Refused):self.retire()
        self.untouched();self.fourth.unlink()
        with self.assertRaises(FileNotFoundError):self.retire()
        self.untouched()

    def test_worker_new_link_reaches_only_one_creation_and_pending_blocks_it(self):
        from unittest.mock import Mock
        operation='30000000-0000-4000-8000-000000000003'
        with patch.object(store,'_Creation') as creation:
            with self.assertRaises(store.Refused):
                store._provision_worker(operation,time.monotonic()+10,store.OWNER_PROMPT_OPERATION)
            creation.assert_not_called()
        self.retire()
        fixture=Mock();fixture.create.side_effect=store.Refused()
        with patch.object(store._SecretBackend,'_verify_service'),patch.object(store,'_Creation',return_value=fixture) as creation:
            with self.assertRaises(store.Refused):
                store._provision_worker(operation,time.monotonic()+10,store.OWNER_PROMPT_OPERATION)
        self.assertEqual(creation.call_args.args[2],store.OWNER_PROMPT_OPERATION)
        fixture.create.assert_called_once();fixture.bind_file.assert_not_called()

    def test_terminal_and_size_bounds(self):
        with patch.object(store.os,'isatty',return_value=False),self.assertRaises(store.Refused):self.retire()
        self.untouched()
        canonical=store.canonical
        def oversize(value):
            raw=canonical(value)
            return raw+b' '*(32769-len(raw)) if value.get('schema')=='cabinet-abandoned-owner-prompt/v1' else raw
        with patch.object(store,'canonical',side_effect=oversize),self.assertRaises(store.Refused):self.retire()
        self.untouched()

    def test_closed_archive_schema_rejects_unknown_fields(self):
        self.retire();archive=self.directory/store._owner_prompt_archive_name()
        value=json.loads(archive.read_bytes());value['extra']=True
        with self.assertRaises(store.Refused):store._validate_owner_prompt_archive(store.canonical(value))

    def test_new_continuation_selection_and_explicit_parent_client(self):
        self.retire()
        value=store._new_selection(str(store.uuid.uuid4()),SimpleNamespace(owner=':1.0',connection=SimpleNamespace(get_unique_name=lambda:':1.555')),store.OWNER_PROMPT_OPERATION)
        store._selection_shape(value,allow_pending=True)
        with patch.object(store.subprocess,'Popen',side_effect=OSError('captured')) as popen:
            store.provision(continuation_archive=store.OWNER_PROMPT_OPERATION)
        self.assertIn(store.OWNER_PROMPT_OPERATION,popen.call_args.args[0])


class OwnerPromptActualReceiptTests(unittest.TestCase):
    def test_exact_actual_receipts_without_operator_state(self):
        selection=store.canonical(json.loads(OWNER_BINDING)['selection'])
        store._owner_prompt_receipts(selection,OWNER_ENROLLMENT,OWNER_BINDING,OWNER_TERMINAL,OWNER_RETIREMENT)

    def test_child_cli_forwards_newest_link_without_os_effect(self):
        operation='30000000-0000-4000-8000-000000000003'
        with patch.object(store,'_protect_process'), patch.object(store,'_check_pipe'), \
             patch.object(store,'_provision_worker') as worker, patch.object(store,'_pipe_write'), \
             patch.object(store.os,'close'):
            store._main([store.HELPER,'--provision','--operation-id',operation,
                '--response-fd','99','--continuation-archive',store.OWNER_PROMPT_OPERATION])
        self.assertEqual(worker.call_args.args[0],operation)
        self.assertEqual(worker.call_args.args[2],store.OWNER_PROMPT_OPERATION)

    def test_bus_check_is_only_pinned_metadata_and_rejects_changed_service(self):
        backend=object.__new__(store._SecretBackend)
        backend.owner=':1.0';backend.GLib=SimpleNamespace(Variant=lambda signature,value:(signature,value))
        backend._verify_service=lambda:None
        from unittest.mock import Mock
        backend._dbus=Mock(return_value=SimpleNamespace(get_type_string=lambda:'(b)',unpack=lambda:(False,)))
        self.assertTrue(backend._owner_prompt_caller_closed())
        self.assertEqual(backend._dbus.call_args.args,('org.freedesktop.DBus','/org/freedesktop/DBus',
            'org.freedesktop.DBus','NameHasOwner',('(s)',(':1.24305',))))
        backend.owner=':1.9';backend._dbus.reset_mock()
        with self.assertRaises(store.Refused):backend._owner_prompt_caller_closed()
        backend._dbus.assert_not_called()

    def test_parent_generation_metadata_exact_reuse_and_absence(self):
        # Reused numeric PID is harmless; the original live generation refuses.
        stat_line='1616904 (public name) '+ ' '.join(['0']*19+['135612024'])
        with patch.object(Path,'stat',return_value=SimpleNamespace(st_uid=1000)),patch.object(Path,'read_text',return_value=stat_line):
            self.assertFalse(store._owner_prompt_parent_ended())
        with patch.object(Path,'stat',side_effect=FileNotFoundError):self.assertTrue(store._owner_prompt_parent_ended())
        with patch.object(Path,'stat',return_value=SimpleNamespace(st_uid=1000)),patch.object(Path,'read_text',return_value=stat_line+'0'):
            self.assertTrue(store._owner_prompt_parent_ended())


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
