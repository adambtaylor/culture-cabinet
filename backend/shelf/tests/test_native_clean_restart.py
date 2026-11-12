"""Native process/CLEAN-guard tests; dummy private store is not OS proof."""
from copy import deepcopy
import json
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import time
import uuid

from django.test import SimpleTestCase


class PrivateStore:
    def __init__(self):
        self.items, self.receipts = {}, {}
        self.locked = False
        self.credential_puts = 0
        self.anchor_puts = 0
        self.unknown_anchor_put = False

    def execute(self,value):
        reply={'version':value['version'],'operation_id':value['operation_id'],
               'status':'AVAILABLE','revision':None,'record':None}
        if self.locked:return {**reply,'status':'LOCKED'}
        key=(value['record_kind'],json.dumps(value['binding'],sort_keys=True))
        current=self.items.get(key)
        if value['operation']=='availability':
            return {**reply,'status':'AVAILABLE' if current else 'MISSING'}
        if value['operation']=='reconcile':
            return deepcopy(self.receipts.get(value['operation_id'],{**reply,'status':'UNKNOWN'}))
        if value['operation']=='put' and value['record_kind']=='clean_restart_anchor':
            self.anchor_puts+=1
            if self.unknown_anchor_put:return {**reply,'status':'UNKNOWN'}
        if value['expected_revision']!=(current['revision'] if current else None):
            return {**reply,'status':'CONFLICT'}
        if value['operation']=='read':
            return {**reply,'revision':current['revision'],'record':deepcopy(current['record'])}
        assert value['operation']=='put'
        revision=str(uuid.uuid4())
        self.items[key]={'revision':revision,'record':deepcopy(value['record_if_put'])}
        if value['record_kind']=='credential':self.credential_puts+=1
        result={**reply,'revision':revision}
        self.receipts[value['operation_id']]=result
        return result


class NativeCleanRestartTests(SimpleTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='native-clean-checkpoint-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'installation'
        self.store=PrivateStore()

    def run_child(self,phase,*,kill_on_ready=False,expect_refusal=False,concurrent_probe=False):
        request_read,request_write=os.pipe()
        response_read,response_write=os.pipe()
        child=subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).with_name('native_clean_restart_child.py')),
            str(self.root),phase,str(request_write),str(response_read)],
            pass_fds=(request_write,response_read),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},stdin=subprocess.DEVNULL)
        os.close(request_write);os.close(response_read)
        pending=b'';output=b'';end=time.monotonic()+35
        try:
            while child.poll() is None:
                if time.monotonic()>end:raise AssertionError('Bounded native fixture timed out')
                readable,_,_=select.select([request_read,child.stdout],[],[],0.1)
                if request_read in readable:
                    block=os.read(request_read,65540)
                    if block:pending+=block
                    while len(pending)>=4 and len(pending)>=4+struct.unpack('>I',pending[:4])[0]:
                        size=struct.unpack('>I',pending[:4])[0]
                        self.assertTrue(0<size<=65536)
                        value=json.loads(pending[4:4+size]);pending=pending[4+size:]
                        raw=json.dumps(self.store.execute(value),separators=(',',':')).encode()
                        frame=struct.pack('>I',len(raw))+raw
                        while frame:frame=frame[os.write(response_write,frame):]
                if child.stdout in readable:
                    output+=os.read(child.stdout.fileno(),4096)
                    if kill_on_ready and b'NATIVE_CHECKPOINT_HOLD_READY' in output:
                        if concurrent_probe:
                            self.run_child('second',expect_refusal=True)
                        child.kill();break
            child.wait(timeout=5)
            output+=child.stdout.read()
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            child.stdout.close();os.close(request_read);os.close(response_write)
        if kill_on_ready:
            self.assertEqual(child.returncode,-signal.SIGKILL)
            return output.decode()
        if expect_refusal:
            self.assertNotEqual(child.returncode,0)
            receipts=[json.loads(line) for line in output.decode().splitlines() if line.startswith('{')]
            if receipts:
                self.assertEqual(receipts[-1]['observed'],{})
        else:
            self.assertEqual(child.returncode,0,output.decode())
            self.assertIn('NATIVE_DUMMY_LIFECYCLE_CLOSED',output.decode())
        return output.decode()

    def test_native_form_sighup_same_installation_fresh_login_and_consumer(self):
        first=self.run_child('first')
        second=self.run_child('second')
        self.assertEqual(self.store.credential_puts,1)
        self.assertIn('"events": 2',second)
        self.assertIn('"keyReentry": false',second)
        self.assertNotEqual(first,second)

    def test_fresh_start_without_credential_can_cleanly_restart(self):
        self.run_child('first-no-credential')
        self.assertEqual(self.store.credential_puts,0)
        anchor=next(v['record'] for k,v in self.store.items.items() if k[0]=='clean_restart_anchor')
        self.assertEqual(anchor['state'],'CLEAN')
        self.assertEqual(self.store.anchor_puts,2)
        second=self.run_child('second-no-credential')
        self.assertIn('"keyReentry": false',second)
        self.assertEqual(self.store.credential_puts,0)
        anchor=next(v['record'] for k,v in self.store.items.items() if k[0]=='clean_restart_anchor')
        self.assertEqual(anchor['state'],'CLEAN')
        self.assertEqual(self.store.anchor_puts,4)

    def test_unknown_first_checkpoint_enrollment_refuses_clean_restart(self):
        self.store.unknown_anchor_put=True
        self.run_child('first-no-credential',expect_refusal=True)
        self.assertEqual(self.store.credential_puts,0)
        self.assertEqual(self.store.anchor_puts,1)
        self.assertFalse(self.store.items)
        self.assertFalse((self.root/'.portable-clean-restart.json').exists())

    def test_locked_restart_refuses_before_database_writes_then_unlock_succeeds(self):
        self.run_child('first')
        original=(self.root/'portable.sqlite3').read_bytes()
        self.store.locked=True
        self.run_child('second',expect_refusal=True)
        self.assertEqual((self.root/'portable.sqlite3').read_bytes(),original)
        self.store.locked=False
        self.run_child('second')

    def test_stale_database_against_current_anchor_refuses(self):
        self.run_child('first')
        old=(self.root/'portable.sqlite3').read_bytes()
        self.run_child('second')
        (self.root/'portable.sqlite3').write_bytes(old)
        self.run_child('second',expect_refusal=True)

    def test_hard_kill_keeps_key_but_active_anchor_refuses_restart(self):
        self.run_child('first-hold',kill_on_ready=True)
        self.assertEqual(self.store.credential_puts,1)
        self.run_child('second',expect_refusal=True)

    def test_missing_locator_and_modified_journal_refuse_without_database_write(self):
        self.run_child('first')
        database=(self.root/'portable.sqlite3').read_bytes()
        locator=self.root/'.portable-clean-restart.json'
        saved=locator.read_bytes()
        locator.unlink()
        self.run_child('second',expect_refusal=True)
        self.assertEqual((self.root/'portable.sqlite3').read_bytes(),database)
        locator.write_bytes(saved);locator.chmod(0o600)
        anchor=next(v['record'] for k,v in self.store.items.items() if k[0]=='clean_restart_anchor')
        journal=Path(anchor['journal_path'])/'journal'
        journal.write_bytes(journal.read_bytes()+b'partial')
        self.run_child('second',expect_refusal=True)
        self.assertEqual((self.root/'portable.sqlite3').read_bytes(),database)

    def test_concurrent_native_opener_cannot_consume_checkpoint(self):
        self.run_child('first-hold',kill_on_ready=True,concurrent_probe=True)
        self.assertEqual(self.store.credential_puts,1)

    def test_unresolved_restriction_prepare_never_publishes_clean(self):
        self.run_child('first-pending',expect_refusal=True)
        anchor=next(v['record'] for k,v in self.store.items.items() if k[0]=='clean_restart_anchor')
        self.assertEqual(anchor['state'],'ACTIVE')
        self.assertIn(b'"phase":"prepare"',(Path(anchor['journal_path'])/'journal').read_bytes())
        self.run_child('second',expect_refusal=True)

    def test_unconfirmed_worker_cleanup_never_publishes_clean(self):
        self.run_child('first-cleanup-failure',expect_refusal=True)
        anchor=next(v['record'] for k,v in self.store.items.items() if k[0]=='clean_restart_anchor')
        self.assertEqual(anchor['state'],'ACTIVE')
        self.run_child('second',expect_refusal=True)
