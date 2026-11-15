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
import types
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from django.test import SimpleTestCase


class StartupStageTests(SimpleTestCase):
    """Synthetic run() coverage; injected failures never touch an installation."""

    def run_startup(self, *, fresh=False, failure_at=None, failure=None,
                    ready_output_failure=False, cleanup_failure=False):
        from cabinet import portable_runtime as runtime

        events = []
        settings = types.SimpleNamespace(configured=True)
        boundary = types.SimpleNamespace(origin='http://127.0.0.1:1', closed=False)
        def close_boundary():
            events.append('boundary.close')
            boundary.closed = True
            if cleanup_failure:
                raise OSError('cleanup sentinel')
        boundary.close = close_boundary
        authority = types.SimpleNamespace(
            _installation=lambda: types.SimpleNamespace(owner_id=1), _obtain_workers=[])
        checkpoint = types.SimpleNamespace(seal=lambda: events.append('checkpoint.seal'), enroll=lambda *_: None)

        class Server:
            error_stream = types.SimpleNamespace(close=lambda: events.append('server.error_close'))

            def __init__(self):
                self.closed = 0

            def server_close(self):
                self.closed += 1
                events.append('server.close')

            def handle_request(self):
                if failure_at == 'POST_READY':
                    raise failure or RuntimeError('post-ready sentinel')
                from cabinet.portable_runtime import PortableStop
                raise PortableStop()

        server = Server()

        class RecoveryContext:
            def __enter__(self):
                events.append('recovery.enter')
                return self

            def __exit__(self, *_):
                events.append('recovery.exit')

        class SupervisorContext:
            closed = True
            failed = False

            def __enter__(self):
                events.append('supervisor.enter')
                return self

            def __exit__(self, *_):
                events.append('supervisor.exit')

        def fail_if(point):
            events.append(point)
            if failure_at == point:
                raise failure or RuntimeError('API_SECRET_SENTINEL')

        def module(name, **values):
            result = types.ModuleType(name)
            result.__dict__.update(values)
            return result

        boundary_module = module('cabinet.portable_boundary',
            BoundaryAlreadyRunning=type('BoundaryAlreadyRunning', (Exception,), {}),
            open_boundary=lambda *_args, **_kwargs: (events.append('boundary.open'), boundary)[1])
        if failure_at == 'LOCK':
            boundary_module.open_boundary = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                boundary_module.BoundaryAlreadyRunning())
        django_modules = {
            'django.conf': module('django.conf', settings=settings),
            'django.core.management': module('django.core.management',
                call_command=lambda *_args, **_kwargs: events.append('store.migrate')),
            'django.core.wsgi': module('django.core.wsgi', get_wsgi_application=lambda: object()),
            'django.db': module('django.db', connections=types.SimpleNamespace(
                close_all=lambda: events.append('db.close_all'))),
        }
        def clean_restart(*_):
            events.append('recovery.restart')
            if failure_at == 'RECOVERY':
                raise failure or RuntimeError('API_SECRET_SENTINEL')
            return RecoveryContext()

        def first_start(*_):
            events.append('recovery.first_start')
            return RecoveryContext()

        recovery_module = module('shelf.recovery',
            NativeCleanCheckpoint=lambda *_: (events.append('checkpoint.create'), checkpoint)[1],
            native_first_start=first_start,
            native_clean_restart=clean_restart)
        credential_module = module('shelf.saved_credentials',
            SavedCredentials=lambda *_args, **_kwargs: object(), secret_service_request=object())
        credentials_module = module('shelf.secret_leases', MemoryCredentials=lambda *_: object())
        connection_module = module('shelf.connection_validation', ConnectionValidation=lambda *_: (
            types.SimpleNamespace(vault=object())))
        def create_authority(*_):
            events.append('authority.create')
            return authority
        authority_module = module('shelf.portable_authority', PortableAuthority=create_authority)
        supervisor_module = module('shelf.obtain_runtime', selected_supervisor=lambda *_: (
            fail_if('service.supervisor') if failure_at == 'SERVICE_COMPOSITION'
            else SupervisorContext()))
        disclosure_module = module('shelf.portable_disclosure', DisclosureServices=lambda *_: None)
        fake_imports = {
            'cabinet.portable_boundary': boundary_module,
            'shelf.recovery': recovery_module,
            'shelf.saved_credentials': credential_module,
            'shelf.secret_leases': credentials_module,
            'shelf.connection_validation': connection_module,
            'shelf.portable_authority': authority_module,
            'shelf.obtain_runtime': supervisor_module,
            'shelf.portable_disclosure': disclosure_module,
            **django_modules,
        }
        stdout, stderr = StringIO(), StringIO()
        original_print = print

        def controlled_print(*args, **kwargs):
            if ready_output_failure and args == ('PORTABLE_READY',):
                raise RuntimeError('API_SECRET_SENTINEL')
            return original_print(*args, **kwargs)

        def validate(_boundary):
            fail_if('store.validate')

        with patch.dict(sys.modules, fake_imports), \
             patch.object(runtime, 'protect_core'), \
             patch.object(runtime, 'prepare_database', side_effect=lambda _: fresh), \
             patch.object(runtime, 'configure', side_effect=lambda _: events.append('django.configure')), \
             patch.object(runtime, 'validate_existing', side_effect=validate), \
             patch.object(runtime, 'remove_fresh_fixtures', side_effect=lambda: events.append('store.fixtures')), \
             patch.object(runtime, '_compose_enhance', return_value=object()), \
             patch.object(runtime, 'make_server', side_effect=lambda *_: server), \
             patch.object(runtime.os, 'umask'), patch.object(runtime.logging, 'disable'), \
             patch.object(runtime.signal, 'signal', side_effect=lambda *_: object()), \
             patch('builtins.print', side_effect=controlled_print), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            result = runtime.run('/synthetic-only')
        return result, stdout.getvalue(), stderr.getvalue(), events, server

    def test_stage_failures_are_fixed_and_do_not_print_exception_text(self):
        for failure_at, expected in (
                ('store.validate', 'STORE_VALIDATE'),
                ('RECOVERY', 'RECOVERY'),
                ('SERVICE_COMPOSITION', 'SERVICE_COMPOSITION')):
            with self.subTest(stage=expected):
                result, out, err, events, _server = self.run_startup(
                    failure_at=failure_at, failure=RuntimeError('API_SECRET_SENTINEL'))
                self.assertEqual(result, 1)
                self.assertEqual(err.splitlines(), [
                    'PORTABLE_START_OR_RUNTIME_REFUSED',
                    'PORTABLE_STARTUP_STAGE=' + expected])
                self.assertNotIn('API_SECRET_SENTINEL', out + err)
                self.assertIn('boundary.close', events)
                self.assertIn('db.close_all', events)
                self.assertLess(events.index('boundary.close'), events.index('db.close_all'))
                if expected == 'SERVICE_COMPOSITION':
                    self.assertIn('recovery.exit', events)

    def test_fresh_and_existing_branches_keep_recovery_and_authority_order(self):
        result, _, _, fresh_events, _ = self.run_startup(fresh=True)
        self.assertEqual(result, 0, fresh_events)
        self.assertLess(fresh_events.index('checkpoint.create'), fresh_events.index('supervisor.enter'))
        self.assertLess(fresh_events.index('authority.create'), fresh_events.index('recovery.first_start'))
        self.assertLess(fresh_events.index('store.migrate'), fresh_events.index('store.fixtures'))

        result, _, _, existing_events, _ = self.run_startup(fresh=False)
        self.assertEqual(result, 0, existing_events)
        self.assertLess(existing_events.index('store.validate'), existing_events.index('checkpoint.create'))
        self.assertLess(existing_events.index('recovery.restart'), existing_events.index('authority.create'))

    def test_lock_stop_ready_and_post_ready_keep_existing_diagnostics(self):
        result, _, err, _events, _server = self.run_startup(failure_at='LOCK')
        self.assertEqual(result, 1)
        self.assertEqual(err.strip(), 'PORTABLE_ALREADY_RUNNING')

        result, out, err, events, _server = self.run_startup()
        self.assertEqual(result, 0, (out, err, events))
        self.assertIn('PORTABLE_READY', out)
        self.assertNotIn('PORTABLE_STARTUP_STAGE=', out + err)

        result, out, err, _events, _server = self.run_startup(
            failure_at='POST_READY', failure=RuntimeError('API_SECRET_SENTINEL'))
        self.assertEqual(result, 1)
        self.assertIn('PORTABLE_READY', out)
        self.assertEqual(err.strip(), 'PORTABLE_START_OR_RUNTIME_REFUSED')

    def test_ready_output_failure_gets_stage_and_cleanup_failure_propagates(self):
        result, out, err, events, server = self.run_startup(ready_output_failure=True)
        self.assertEqual(result, 1)
        self.assertIn('PORTABLE_STARTUP_STAGE=READY_OUTPUT', err)
        self.assertNotIn('API_SECRET_SENTINEL', out + err)
        self.assertEqual(server.closed, 2)
        self.assertLess(events.index('boundary.close'), events.index('db.close_all'))

        with self.assertRaisesRegex(OSError, 'cleanup sentinel'):
            self.run_startup(failure_at='store.validate', cleanup_failure=True)


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
                            refusal=self.run_child('second',expect_refusal=True)
                            self.assertIn('PORTABLE_ALREADY_RUNNING',refusal)
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
        refusal=self.run_child('second',expect_refusal=True)
        self.assertIn('PORTABLE_START_OR_RUNTIME_REFUSED',refusal)
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
