"""Bootstrap invariants without a CLI child; launch behavior is tested separately."""
import os
from pathlib import Path
import resource
import sqlite3
import tempfile
import subprocess
import sys
from unittest.mock import patch
from types import SimpleNamespace
import uuid

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase

from cabinet.portable_boundary import open_boundary
from cabinet import portable_runtime as runtime


class SelectedSupervisorLauncherTests(SimpleTestCase):
    def test_enabled_file_launcher_progresses_idle_native_job_and_closes_before_witness(self):
        child = Path(__file__).with_name('portable_obtain_supervisor_child.py')
        with tempfile.TemporaryDirectory(prefix='selected-supervisor-launcher-') as folder:
            environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
            if os.environ.get('PR001_RESOURCE_LOG'):
                environment['PR001_RESOURCE_LOG'] = os.environ['PR001_RESOURCE_LOG']
            result = subprocess.run([sys.executable, '-I', '-B', str(child), folder],
                env=environment, capture_output=True, text=True, timeout=24)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('NATIVE_SUPERVISOR_FILE_LAUNCHER_PASS', result.stdout)
        print(next(line for line in result.stdout.splitlines()
            if line.startswith('NATIVE_SUPERVISOR_FILE_LAUNCHER_PASS ')), flush=True)

    def test_file_launcher_refuses_witness_loss_and_reports_cleanup_failure(self):
        child = Path(__file__).with_name('portable_obtain_supervisor_child.py')
        for mode, marker in (('witness_loss', 'NATIVE_SUPERVISOR_WITNESS_LOSS_REFUSED_PASS'),
                ('cleanup_failure', 'NATIVE_SUPERVISOR_CLEANUP_REFUSED_PASS')):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='selected-supervisor-denial-') as folder:
                environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
                if os.environ.get('PR001_RESOURCE_LOG'):
                    environment['PR001_RESOURCE_LOG'] = os.environ['PR001_RESOURCE_LOG']
                result = subprocess.run([sys.executable, '-I', '-B', str(child), folder, mode],
                    env=environment, capture_output=True, text=True, timeout=24)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(marker, result.stdout)
                print(next(line for line in result.stdout.splitlines() if line.startswith(marker)), flush=True)


class ObtainResponsePolicyTests(SimpleTestCase):
    def test_only_exact_named_successful_html_documents_enable_fixed_script(self):
        from django.http import HttpResponse
        from django.test import RequestFactory
        from django.urls import resolve
        from cabinet.portable_boundary import PortableSecurityMiddleware
        from shelf import portable_obtain_views
        with tempfile.TemporaryDirectory() as folder:
            boundary = open_boundary(Path(folder) / 'native')
            try:
                with self.settings(PORTABLE_AUTHORITY=SimpleNamespace(boundary=boundary),
                    ROOT_URLCONF='cabinet.portable_urls', SESSION_COOKIE_NAME=boundary.session_cookie,
                    CSRF_COOKIE_NAME=boundary.csrf_cookie, SESSION_COOKIE_DOMAIN=None,
                    CSRF_COOKIE_DOMAIN=None, SESSION_COOKIE_HTTPONLY=True, CSRF_COOKIE_HTTPONLY=True,
                    SESSION_COOKIE_SAMESITE='Strict', CSRF_COOKIE_SAMESITE='Strict',
                    SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False):
                    def policy(path, *, status=200, content_type='text/html', override=None):
                        request = RequestFactory().get(path, HTTP_HOST=boundary.origin.removeprefix('http://'))
                        request.resolver_match = override or resolve(path)
                        response = PortableSecurityMiddleware(lambda req: HttpResponse(
                            'synthetic document', status=status, content_type=content_type))(request)
                        return response['Content-Security-Policy']
                    for path in ('/cabinet', '/cabinet/obtain/jobs/7/'):
                        value = policy(path)
                        self.assertIn('script-src ' + boundary.origin + '/cabinet/obtain/status.js;', value)
                        self.assertIn('connect-src ' + boundary.origin + '/cabinet/obtain/jobs/;', value)
                        self.assertIn("script-src-attr 'none'", value)
                        self.assertNotIn("script-src 'self'", value)
                        self.assertNotIn('unsafe-', value)
                    for path, status, kind in (('/setup', 200, 'text/html'),
                        ('/cabinet/obtain/jobs/7/status/', 200, 'application/json'),
                        ('/cabinet/obtain/status.js', 200, 'application/javascript'),
                        ('/cabinet', 403, 'text/html'), ('/cabinet', 303, 'text/html'),
                        ('/cabinet', 200, 'text/plain')):
                        self.assertNotIn('script-src', policy(path, status=status, content_type=kind))
                    forged = SimpleNamespace(url_name='portable_cabinet', func=portable_obtain_views.job_page)
                    self.assertNotIn('script-src', policy('/cabinet', override=forged))
                    request = RequestFactory().get('/cabinet', HTTP_HOST='foreign.invalid')
                    request.resolver_match = resolve('/cabinet')
                    response = PortableSecurityMiddleware(lambda req: self.fail('Foreign Host reached handler'))(request)
                    self.assertEqual(response.status_code, 403)
                    self.assertNotIn('script-src', response['Content-Security-Policy'])
            finally:
                boundary.close()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class RuntimeFilesystemTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.boundary = open_boundary(Path(self.temp.name) / 'install')
        self.addCleanup(self.boundary.close)
        self.path = Path(self.boundary.database_name)

    def test_fresh_database_is_exclusive_and_private(self):
        self.assertTrue(runtime.prepare_database(self.boundary))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(runtime.prepare_database(self.boundary))

    def test_unknown_file_refuses_fresh_creation(self):
        (self.boundary.root / 'foreign').write_text('synthetic')
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.prepare_database(self.boundary)
        self.assertFalse(self.path.exists())

    def test_symlink_database_refused(self):
        target = Path(self.temp.name) / 'other'
        target.touch(mode=0o600)
        self.path.symlink_to(target)
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.prepare_database(self.boundary)

    def test_database_directory_refused(self):
        self.path.mkdir(mode=0o700)
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.prepare_database(self.boundary)

    def test_readable_database_refused(self):
        self.path.touch(mode=0o600)
        self.path.chmod(0o644)
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.prepare_database(self.boundary)

    def test_hardlinked_database_refused(self):
        self.path.touch(mode=0o600)
        os.link(self.path, Path(self.temp.name) / 'alias')
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.prepare_database(self.boundary)

    def test_foreign_sidecar_refused(self):
        self.path.touch(mode=0o600)
        Path(str(self.path)+'-wal').symlink_to(Path(self.temp.name) / 'missing')
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.prepare_database(self.boundary)

    def test_public_sidecar_refused(self):
        self.path.touch(mode=0o600)
        sidecar = Path(str(self.path)+'-shm')
        sidecar.touch(mode=0o600)
        sidecar.chmod(0o644)
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.prepare_database(self.boundary)

    def test_actual_socket_adopted_without_dns_or_rebind(self):
        fd = self.boundary._socket.fileno()
        with patch('socket.getfqdn', side_effect=AssertionError('DNS forbidden')):
            server = runtime.make_server(self.boundary, lambda environ, start_response: [])
        try:
            self.assertEqual(server.socket.fileno(), fd)
            self.assertEqual(server.server_port, int(self.boundary.origin.rsplit(':',1)[1]))
            self.assertEqual(server.server_name, '127.0.0.1')
            self.assertLessEqual(server.timeout, .2)
        finally:
            server.server_close()
            server.error_stream.close()

    def test_failed_server_factory_closes_stream_and_adopted_socket(self):
        import builtins
        from wsgiref.simple_server import WSGIServer
        streams=[]
        actual_open=builtins.open
        def capture(*args, **kwargs):
            stream=actual_open(*args, **kwargs)
            streams.append(stream)
            return stream
        with patch('builtins.open',side_effect=capture), patch.object(WSGIServer,'server_activate',side_effect=OSError):
            with self.assertRaises(OSError):
                runtime.make_server(self.boundary,lambda environ,start_response:[])
        self.assertEqual(len(streams),1)
        self.assertTrue(streams[0].closed)
        self.assertEqual(self.boundary._socket.fileno(),-1)

    def test_early_core_guard_applies_zero_limit(self):
        with patch.object(resource, 'getrlimit', return_value=(0, -1)), patch.object(resource, 'setrlimit') as setting:
            runtime.protect_core()
            setting.assert_called_once_with(resource.RLIMIT_CORE, (0, -1))

    def test_core_guard_fails_closed(self):
        with patch.object(resource, 'setrlimit', side_effect=OSError):
            with self.assertRaises(runtime.RuntimeFailure):
                runtime.protect_core()


class RuntimeSchemaTests(TransactionTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.boundary = open_boundary(Path(self.temp.name)/'install')
        self.addCleanup(self.boundary.close)
        self.path = Path(self.boundary.database_name)

    def make_store(self):
        # Copy schema only, not the current transaction/database or household data.
        with connection.cursor() as cursor:
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            definitions = [row[0] for row in cursor.fetchall()]
            cursor.execute('SELECT app,name,applied FROM django_migrations')
            migrations = cursor.fetchall()
        self.path.touch(mode=0o600)
        db = sqlite3.connect(self.path, factory=ClosingConnection)
        for sql in definitions:
            db.execute(sql)
        db.executemany('INSERT INTO django_migrations(app,name,applied) VALUES (?,?,?)', migrations)
        db.execute('INSERT INTO shelf_portableinstallation(id,root_identity,data_schema_version,owner_id,owner_revision,process_generation,failed_auth,cooldown_until) VALUES (?,?,1,NULL,0,?,0,NULL)',
            [self.boundary.installation_id.hex, self.boundary.root_identity, uuid.uuid4().hex])
        db.commit()
        return db

    def test_current_owned_schema_passes_readonly(self):
        with self.make_store():
            runtime.validate_existing(self.boundary)

    def test_existing_foreign_identity_refused(self):
        with self.make_store() as db:
            db.execute("UPDATE shelf_portableinstallation SET root_identity='foreign'")
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.validate_existing(self.boundary)

    def test_older_migration_set_refused_without_write(self):
        with self.make_store() as db:
            db.execute("DELETE FROM django_migrations WHERE app='shelf' AND name LIKE '0020%'")
        before = self.path.read_bytes()
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.validate_existing(self.boundary)
        self.assertEqual(before, self.path.read_bytes())

    def test_unknown_newer_migration_refused(self):
        with self.make_store() as db:
            db.execute("INSERT INTO django_migrations(app,name,applied) VALUES ('shelf','9999_future','2026-01-01')")
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.validate_existing(self.boundary)

    def test_missing_schema_column_refused(self):
        with self.make_store() as db:
            db.execute('ALTER TABLE shelf_portableconnection RENAME COLUMN reasoning TO unknown_column')
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.validate_existing(self.boundary)

    def test_foreign_unclaimed_user_refused(self):
        with self.make_store() as db:
            db.execute("INSERT INTO auth_user(password,is_superuser,username,first_name,last_name,email,is_staff,is_active,date_joined) VALUES ('!',0,'foreign','','','',0,1,'2026-01-01')")
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.validate_existing(self.boundary)

    def test_committed_wal_identity_is_authoritative(self):
        db = self.make_store()
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute("UPDATE shelf_portableinstallation SET root_identity='foreign-in-wal'")
            db.commit()
            with self.assertRaises(runtime.RuntimeFailure):
                runtime.validate_existing(self.boundary)
        finally:
            db.close()

    def test_corrupt_database_refused(self):
        self.path.write_bytes(b'synthetic-not-sqlite')
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.validate_existing(self.boundary)

    def seed(self):
        from shelf.models import FeedbackTarget, RecoveryAnchor
        RecoveryAnchor.objects.get_or_create(pk=1, defaults={'store_id':uuid.uuid4()})
        for suffix, key, title, category in [('101','paper-observatory','The Paper Observatory','Fictional film'),
            ('102','mosslight-station','Mosslight Station','Fictional book'),
            ('103','clockwork-lagoon','The Clockwork Lagoon','Fictional series')]:
            FeedbackTarget.objects.get_or_create(pk=uuid.UUID('00000000-0000-4000-8000-000000000'+suffix),
                defaults=dict(kind='item',title=title,category=category,source_namespace='fiction-p1101',source_key=key))
        return FeedbackTarget, RecoveryAnchor

    def test_fresh_cleanup_removes_exact_fixtures_keeps_anchor(self):
        target, anchor = self.seed()
        runtime.remove_fresh_fixtures()
        self.assertFalse(target.objects.exists())
        self.assertEqual(anchor.objects.count(), 1)

    def test_cleanup_refuses_modified_fixture(self):
        target, _ = self.seed()
        target.objects.filter(source_key='paper-observatory').update(title='Changed')
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.remove_fresh_fixtures()
        self.assertEqual(target.objects.count(), 3)

    def test_cleanup_refuses_extra_target(self):
        target, _ = self.seed()
        target.objects.create(id=uuid.uuid4(),kind='item',title='Synthetic extra',category='Book',source_namespace='other',source_key='extra')
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.remove_fresh_fixtures()
        self.assertEqual(target.objects.count(), 4)

    def test_cleanup_refuses_existing_user(self):
        target, _ = self.seed()
        get_user_model().objects.create(username='synthetic-foreign')
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.remove_fresh_fixtures()
        self.assertEqual(target.objects.count(), 3)

    def test_cleanup_refuses_missing_anchor(self):
        target, anchor = self.seed()
        anchor.objects.all().delete()
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.remove_fresh_fixtures()
        self.assertEqual(target.objects.count(), 3)


class FixtureFailureTests(SimpleTestCase):
    def setUp(self):
        from . import portable_fixture
        self.fixture_module = portable_fixture
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.f = portable_fixture.PortableFixture(Path(__file__).resolve().parents[3], root/'data', root/'audit')

    def attach_terminal(self):
        import pty
        master, slave = pty.openpty()
        os.close(slave)
        self.f.master = master
        self.f._terminal = b'SYNTHETIC terminal buffer'
        self.f.claim_code = 'SYNTHETIC claim'
        return master

    def assert_terminal_released(self, master):
        with self.assertRaises(OSError):
            os.fstat(master)
        self.assertIsNone(self.f.master)
        self.assertIsNone(self.f.claim_code)
        self.assertEqual(self.f._terminal, b'')

    def test_spawn_failure_releases_acquired_master(self):
        import pty
        acquired = []
        real_openpty = pty.openpty
        def capture():
            pair = real_openpty()
            acquired.append(pair[0])
            return pair
        # Simulate fresh fixture state, without consuming another real CLI start.
        with patch.object(self.fixture_module, '_CLI_STARTS', 0), patch('pty.openpty', side_effect=capture), \
             patch.object(self.fixture_module.subprocess, 'Popen', side_effect=OSError('injected spawn failure')):
            with self.assertRaises(OSError):
                self.f.start()
            self.assertEqual(self.fixture_module._CLI_STARTS, 0)
        self.assertEqual(len(acquired), 1)
        self.assert_terminal_released(acquired[0])

    def test_child_exit_before_signal_is_reaped_without_cleanup_loss(self):
        from unittest.mock import Mock
        master = self.attach_terminal()
        proc = self.f.process = Mock(pid=987654321, returncode=0)
        proc.poll.return_value = None
        with patch.object(self.fixture_module.os, 'killpg', side_effect=ProcessLookupError), \
             patch.object(self.fixture_module, 'members', return_value=[]):
            receipt = self.f.stop()
        proc.wait.assert_called_once_with(timeout=3)
        self.assertEqual(receipt, dict(remaining_owned_pids=[],listener_closed=True,lock_released=True))
        self.assertFalse(self.f.emergency_kill_used)
        self.assertEqual(self.f.teardown_errors, [])
        self.assert_terminal_released(master)

    def test_failed_probes_continue_and_never_claim_verified_cleanup(self):
        from unittest.mock import Mock
        master = self.attach_terminal()
        self.f.process = Mock(pid=987654321, returncode=0)
        self.f.process.poll.return_value = 0
        self.f.origin = 'http://127.0.0.1:1'
        self.f.data_root.mkdir(mode=0o700)
        (self.f.data_root/'.portable-lock').touch(mode=0o600)
        with patch.object(self.fixture_module, 'members', side_effect=OSError), \
             patch.object(self.fixture_module.socket, 'create_connection', side_effect=OSError), \
             patch.object(self.fixture_module.fcntl, 'flock', side_effect=OSError) as lock_probe:
            receipt = self.f.stop()
        lock_probe.assert_called_once()
        self.assertIsNone(receipt['remaining_owned_pids'])
        self.assertIsNone(receipt['listener_closed'])
        self.assertIsNone(receipt['lock_released'])
        self.assertEqual(self.f.teardown_errors, ['PROCESS_PROBE_FAILED','LISTENER_PROBE_FAILED','LOCK_PROBE_FAILED'])
        self.assert_terminal_released(master)


    def test_measured_stop_records_whole_lifetime_and_is_idempotent(self):
        from unittest.mock import Mock
        self.f.process = Mock(pid=987654321, returncode=0)
        self.f.process.poll.return_value = 0
        self.f._usage = dict(pid=987654321, start=100.0, complete=False)
        self.f._timer = Mock()
        with patch.object(self.fixture_module.time, 'monotonic', return_value=125.0), \
             patch.object(self.fixture_module, 'members', return_value=[]), \
             patch('builtins.print') as output:
            first = self.f.stop()
            self.assertEqual(self.f.stop(), first)
        self.f._timer.cancel.assert_called_once()
        self.assertEqual(self.f._usage['elapsed'], 25.0)
        self.assertTrue(self.f._usage['complete'])
        self.assertEqual(self.f._usage['remaining_owned_pids'], [])
        output.assert_called_once()

    def test_measured_stop_refuses_overdue_or_unreaped_children(self):
        from unittest.mock import Mock
        for elapsed, survivors in ((31.0, []), (25.0, [987654321])):
            with self.subTest(elapsed=elapsed, survivors=survivors):
                self.f._usage = dict(pid=987654321, start=100.0, complete=False)
                self.f._stop_receipt = None
                self.f.process = Mock(pid=987654321, returncode=0)
                self.f.process.poll.return_value = 0
                receipt = dict(remaining_owned_pids=survivors, listener_closed=True, lock_released=True)
                with patch.object(self.f, '_stop', return_value=receipt), \
                     patch.object(self.fixture_module.time, 'monotonic', return_value=100.0+elapsed), \
                     patch('builtins.print'):
                    with self.assertRaises(self.fixture_module.PortableFixtureFailure):
                        self.f.stop()
                self.assertFalse(self.f._usage['complete'])


    def test_measured_stop_refuses_unreaped_primary_child(self):
        from unittest.mock import Mock
        self.f.process = Mock(pid=987654321, returncode=None)
        self.f.process.poll.return_value = None
        self.f._usage = dict(pid=987654321, start=100.0, complete=False)
        receipt = dict(remaining_owned_pids=[], listener_closed=True, lock_released=True)
        with patch.object(self.f, '_stop', return_value=receipt), \
             patch.object(self.fixture_module.time, 'monotonic', return_value=125.0), \
             patch('builtins.print'):
            with self.assertRaises(self.fixture_module.PortableFixtureFailure):
                self.f.stop()
        self.assertFalse(self.f._usage['complete'])
        self.assertIsNone(self.f._stop_receipt)


# Reuse the accepted clock/owner/fake adapter fixture, which defines no test
# methods. No production clock flag, extra CLI child or real provider is added.
from django.test import Client
from urllib.parse import urlencode
from .test_connection_validation import ConnectionCase, CONFIG, SECRET
from .test_portable_authority import PASSWORD
from shelf.models import PortableProbeAttempt
from shelf.portable_authority import AuthorityFault


class RecentAuthIntegrationTests(ConnectionCase):
    def setUp(self):
        super().setUp()
        options = self.settings(ROOT_URLCONF='cabinet.portable_urls', PORTABLE_AUTHORITY=self.authority,
            PORTABLE_CONNECTIONS=self.service, SESSION_COOKIE_NAME=self.boundary.session_cookie,
            CSRF_COOKIE_NAME=self.boundary.csrf_cookie,
            MIDDLEWARE=['cabinet.portable_boundary.PortableSecurityMiddleware',
                        'django.contrib.sessions.middleware.SessionMiddleware',
                        'django.middleware.csrf.CsrfViewMiddleware'])
        options.enable()
        self.addCleanup(options.disable)
        self.http = Client(enforce_csrf_checks=True)
        self.headers = {'HTTP_HOST':self.boundary.origin.removeprefix('http://'), 'HTTP_ORIGIN':self.boundary.origin}
        import json
        self.assertEqual(self.http.post('/setup/login',json.dumps({'passphrase':PASSWORD}),content_type='application/json',
            HTTP_X_CSRFTOKEN=self.token(),**self.headers).status_code,200)
        self.ref = self.http.session['portable_session']
        self.put()

    def token(self):
        return self.http.get('/setup/status',**self.headers).json()['csrf_token']

    def form(self, name, value):
        response = self.raw_form(name, value)
        if response.status_code == 303:
            self.assertTrue(response['Location'] == '/setup' or response['Location'].startswith('/setup?connection_ref='))
            return self.http.get(response['Location'], **self.headers)
        return response

    def raw_form(self, name, value):
        # Native forms include CSRF even when they have no other fields. A zero-
        # byte DjangoClient body omits content type and cannot model that request.
        token = self.token()
        fields = dict(value)
        fields.setdefault('csrfmiddlewaretoken', token)
        return self.http.post('/setup/form/'+name,urlencode(fields),content_type='application/x-www-form-urlencoded',
                              HTTP_X_CSRFTOKEN=token,**self.headers)

    def test_recent_auth_recovery_preserves_key_selection_and_requires_explicit_retry(self):
        from .test_onboarding_experience_views import Document
        self.vault.configure(self.ref, CONFIG)  # Explicit selection matters with two saved configs.
        self.clock.advance(301)
        fields={'connection_ref':str(self.config.pk),'revision':'1'}
        response=self.form('preview',fields)
        self.assertEqual(response.status_code,401)
        document=Document(response.content.decode())
        self.assertEqual(document.body['data-state'],'model')
        recovery=document.forms['reauthenticate']
        self.assertEqual(recovery['attrs']['action'],'/setup/form/reauthenticate')
        password=next(v for v in recovery['inputs'] if v.get('name')=='passphrase')
        self.assertFalse(password.get('value'))
        self.assertEqual(next(v['value'] for v in recovery['inputs'] if v.get('name')=='connection_ref'),str(self.config.pk))
        self.assertNotIn(SECRET,response.content)
        old_session=self.http.session.session_key
        old_csrf=self.http.cookies[self.boundary.csrf_cookie].value
        response=self.form('reauthenticate',{'passphrase':PASSWORD,'connection_ref':str(self.config.pk)})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.http.session['portable_session'],self.ref)
        self.assertNotEqual(self.http.session.session_key,old_session)
        self.assertNotEqual(self.http.cookies[self.boundary.csrf_cookie].value,old_csrf)
        self.assertEqual(self.vault.resolve(self.ref,self.config.pk,1),SECRET)
        self.assertEqual(self.fake.calls,0)
        self.assertFalse(PortableProbeAttempt.objects.exists())
        self.assertEqual(self.form('preview',fields).status_code,200)  # Explicit subsequent owner action.
        self.assertEqual(self.fake.calls,0)

    def test_truly_expired_session_requires_login_not_recent_confirmation(self):
        from .test_onboarding_experience_views import Document
        self.clock.advance(1801)
        response=self.form('preview',{'connection_ref':str(self.config.pk),'revision':'1'})
        self.assertEqual(response.status_code,401)
        document=Document(response.content.decode())
        self.assertEqual(document.body['data-state'],'login')
        self.assertIn('login',document.forms)
        self.assertNotIn('reauthenticate',document.forms)
        self.assertFalse(self.vault._leases)
        self.assertEqual(self.fake.calls,0)

    def test_cancellation_after_reservation_leaves_unknown_blocked_without_resend(self):
        preview=self.preview_probe()
        intent=uuid.uuid4()
        self.fake.failure=runtime.PortableStop()
        with self.assertRaises(runtime.PortableStop):
            self.confirm_probe(preview,intent)
        attempt=PortableProbeAttempt.objects.get(pk=intent)
        self.assertEqual((attempt.state,attempt.outcome),('sending','unknown'))
        self.assertEqual(self.status_probe()['outcome'],'unknown')
        with self.assertRaises(AuthorityFault) as caught:
            self.preview_probe()
        self.assertEqual(caught.exception.code,'UNKNOWN_RECONCILIATION_REQUIRED')
        self.assertEqual(self.confirm_probe(preview,intent)['state'],'sending')
        self.assertEqual(self.fake.calls,1)  # Isolated fake only; replay never generates.

    def test_login_redirects_to_canonical_get_and_reload_retains_metadata(self):
        self.assertEqual(self.form('logout',{}).status_code,200)
        response=self.raw_form('login',{'passphrase':PASSWORD})
        self.assertEqual(response.status_code,303)
        self.assertEqual(response['Location'],'/setup')
        self.assertEqual(response.content,b'')
        for _ in range(2):
            page=self.http.get(response['Location'],**self.headers)
            self.assertEqual(page.status_code,200)
            self.assertIn(b'data-state="model"',page.content)
            self.assertIn(b'gpt-6-astra',page.content)
            self.assertIn(b'data-credential-configured="false"',page.content)
        self.assertEqual(self.authority._installation().failed_auth,0)
        self.assertEqual(self.fake.calls,0)

    def test_flash_is_fixed_single_use_and_cannot_override_error(self):
        session=self.http.session
        session['_portable_notice']='UNTRUSTED-FLASH-DO-NOT-RENDER'
        session.save()
        page=self.http.get('/setup',**self.headers)
        self.assertNotIn(b'UNTRUSTED-FLASH-DO-NOT-RENDER',page.content)
        response=self.raw_form('remove',{'connection_ref':str(self.config.pk),'revision':'1'})
        self.assertEqual(response.status_code,303)
        expected='/setup?'+urlencode({'connection_ref':str(self.config.pk)})
        self.assertEqual(response['Location'],expected)
        first=self.http.get(expected,**self.headers)
        self.assertIn(b'Session-only API key removed.',first.content)
        again=self.http.get(expected,**self.headers)
        self.assertNotIn(b'Session-only API key removed.',again.content)
        session=self.http.session
        session['_portable_notice']='KEY_ADDED'
        session.save()
        error=self.raw_form('credential',{'unexpected':'SYNTHETIC-never-echo'})
        self.assertEqual(error.status_code,422)
        self.assertNotIn('Location',error)
        self.assertNotIn(b'Session-only API key updated.',error.content)
        self.assertNotIn(b'SYNTHETIC-never-echo',error.content)
        self.assertNotIn('_portable_notice',self.http.session)

    def test_success_redirect_selection_is_owned_and_not_a_next_url(self):
        foreign=str(uuid.uuid4())
        response=self.raw_form('reauthenticate',{'passphrase':PASSWORD,'connection_ref':foreign})
        self.assertEqual(response.status_code,403)
        self.assertNotIn('Location',response)
        self.assertNotIn(foreign.encode(),response.content)
        external='https://unrelated.invalid/next'
        response=self.raw_form('reauthenticate',{'passphrase':PASSWORD,'connection_ref':external})
        self.assertEqual(response.status_code,422)
        self.assertNotIn('Location',response)
        self.assertNotIn(external.encode(),response.content)

    def test_html_mutation_redirect_keeps_json_and_error_contracts(self):
        import json
        response=self.raw_form('configure',CONFIG)
        self.assertEqual(response.status_code,303)
        self.assertTrue(response['Location'].startswith('/setup?connection_ref='))
        self.assertNotIn(str(self.config.pk),response['Location'])
        page=self.http.get(response['Location'],**self.headers)
        self.assertEqual(page.status_code,200)
        self.assertIn(b'data-configured="true"',page.content)
        self.assertIn(b'data-run-ready="false"',page.content)
        response=self.http.post('/setup/connections/configure',json.dumps(CONFIG),content_type='application/json',
                               HTTP_X_CSRFTOKEN=self.token(),**self.headers)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('Location',response)
        self.assertEqual(response.json()['validation'],'UNPROVEN')
        bad=self.raw_form('remove',{'connection_ref':str(self.config.pk),'revision':'999'})
        self.assertEqual(bad.status_code,409)
        self.assertNotIn('Location',bad)

    def test_preview_stays_explicit_confirm_redirects_and_replay_never_sends(self):
        import json
        from .test_onboarding_experience_views import Document
        response=self.raw_form('preview',{'connection_ref':str(self.config.pk),'revision':'1'})
        self.assertEqual(response.status_code,200)
        self.assertNotIn('Location',response)
        self.assertEqual(self.fake.calls,0)
        form=Document(response.content.decode()).forms['confirm']
        values={v['name']:v.get('value','') for v in form['inputs']}
        response=self.raw_form('confirm',values)
        self.assertEqual(response.status_code,303)
        self.assertEqual(response['Location'],'/setup?'+urlencode({'connection_ref':str(self.config.pk)}))
        self.assertEqual(self.fake.calls,1)  # Accepted isolated adapter, never a wire send.
        page=self.http.get(response['Location'],**self.headers)
        self.assertIn(b'data-connection-ready="false"',page.content)
        self.assertIn(b'data-run-ready="false"',page.content)
        replay={'preview_ref':values['preview_ref'],'intent_ref':values['intent_ref'],'confirm_billable_test':True}
        response=self.http.post('/setup/connections/confirm',json.dumps(replay),content_type='application/json',
                               HTTP_X_CSRFTOKEN=self.token(),**self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['state'],'completed')
        self.assertEqual(self.fake.calls,1)
        self.assertEqual(PortableProbeAttempt.objects.count(),1)

    def test_reauthentication_redirect_keeps_owner_lease_without_action_replay(self):
        self.clock.advance(301)
        self.assertEqual(self.raw_form('preview',{'connection_ref':str(self.config.pk),'revision':'1'}).status_code,401)
        response=self.raw_form('reauthenticate',{'passphrase':PASSWORD,'connection_ref':str(self.config.pk)})
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.http.session['portable_session'],self.ref)
        self.assertEqual(self.vault.resolve(self.ref,self.config.pk,1),SECRET)
        self.assertEqual(self.fake.calls,0)
        self.assertFalse(PortableProbeAttempt.objects.exists())
        page=self.http.get(response['Location'],**self.headers)
        self.assertEqual(page.status_code,200)
        self.assertIn(b'data-credential-configured="true"',page.content)
        self.assertNotIn(b'data-form="confirm"',page.content)
        self.assertNotIn('_portable_notice',self.http.session)


class NormalEnhanceHostSourceTests(SimpleTestCase):
    def test_runtime_composes_concrete_ports_without_policy_or_source_activation(self):
        import ast
        source = Path(runtime.__file__).read_text()
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == 'compose'
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == 'EnhanceService']
        self.assertEqual(len(calls), 1)
        self.assertIn('policy_provider', {arg.arg for arg in calls[0].keywords})
        self.assertNotIn('configure_source(', source)


class NormalDisclosureStartupTests(SimpleTestCase):
    """Actual run in a cold isolated interpreter; synthetic external reply only."""
    def cold_child(self, mode):
        import signal
        import time
        import subprocess
        import sys
        from .portable_fixture import _CHILD
        source = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory(prefix='disclosure-startup-') as folder:
            root = Path(folder)
            audit = root/'audit.log'
            audit.touch(mode=0o600)
            # Reuse the isolated child audit. This parent owns the one bounded
            # process group and always reaps it, including assertion failures.
            started = time.monotonic()
            child = subprocess.Popen([sys.executable, '-I', '-B', '-c', _CHILD,
                str(source/'backend/shelf/tests/portable_disclosure_startup_child.py'), str(audit),
                str(root/'install'), mode], cwd=source,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                output, _ = child.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    output, _ = child.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    output, _ = child.communicate()
                self.fail('Cold startup exceeded its existing bounded child window')
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            self.assertEqual(child.returncode, 0, output.decode()[-2500:])
            self.assertIn(('NORMAL_STARTUP_DISCLOSURE_PASS '+mode).encode(), output)
            self.assertEqual(audit.read_bytes(), b'')
            # The normal boundary released its exclusive lock and socket; the
            # child also checked every database connection was closed.
            self.assertFalse(Path('/proc', str(child.pid)).exists())
            print('NORMAL_DISCLOSURE_CHILD', mode, 'reaped=True auditBytes=0 seconds=',
                  round(time.monotonic()-started, 3))

    def test_normal_run_native_consent_and_credential_invalidation(self):
        self.cold_child('normal')

    def test_disclosure_constructor_failure_closes_before_ready(self):
        self.cold_child('constructor_failure')


class ThreeRoundFixturePolicyTests(SimpleTestCase):
    def test_extended_lease_is_exact_case_only_and_other_defaults_are_unchanged(self):
        from .portable_fixture import BoundSocketLease, PortableFixtureFailure
        exact = ('shelf.tests.test_workflow_three_round.NativeThreeRoundTests.'
            'test_three_native_rounds_feedback_undo_exact_input_and_persistent_publications')
        with patch('shelf.tests.portable_fixture.reserve_resource', return_value=None):
            for case, lifetime in [(exact, 60), ('ordinary', 20), ('ordinary', 25), (exact, 25)]:
                lease = BoundSocketLease(case, lifetime=lifetime)
                self.assertEqual(lease.closure_ceiling, 65 if lifetime == 60 else 30)
                lease.close()
            for case, lifetime in [('ordinary', 60), (exact+'extra', 60), (exact, 65), (exact, True)]:
                with self.assertRaises(PortableFixtureFailure):
                    BoundSocketLease(case, lifetime=lifetime)

    def test_extended_lease_still_rejects_forced_or_late_closure(self):
        from types import SimpleNamespace
        from .portable_fixture import BoundSocketLease, PortableFixtureFailure
        exact = ('shelf.tests.test_workflow_three_round.NativeThreeRoundTests.'
            'test_three_native_rounds_feedback_undo_exact_input_and_persistent_publications')
        for elapsed, forced, accepted in [(64, False, True), (66, False, False), (1, True, False)]:
            with self.subTest(elapsed=elapsed, forced=forced), patch(
                    'shelf.tests.portable_fixture.reserve_resource', return_value=100), patch(
                    'shelf.tests.portable_fixture.time.monotonic', return_value=100+elapsed), patch(
                    'shelf.tests.portable_fixture.resource_event') as evidence:
                lease = BoundSocketLease(exact, lifetime=60)
                lease.boundary = SimpleNamespace(close=lambda: None, _socket=SimpleNamespace(fileno=lambda: -1))
                lease.forced = forced
                if accepted:
                    lease.close()
                else:
                    with self.assertRaises(PortableFixtureFailure):
                        lease.close()
                self.assertEqual(evidence.call_args.kwargs['complete'], accepted)
