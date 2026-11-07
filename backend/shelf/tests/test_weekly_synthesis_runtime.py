"""The clock uses fake ticks and waits; no daemon, production DB or processes."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from shelf import weekly_synthesis_runtime as runtime


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.server = self.root / 'server.json'
        self.controller = self.root / '.p1-readiness/deployment/controller.py'
        self.controller.parent.mkdir(parents=True)
        self.controller.write_text('# fixture')
        self.proc = self.root / 'proc/123'
        self.proc.mkdir(parents=True)
        (self.proc / 'cwd').symlink_to(self.root)
        (self.proc / 'cmdline').write_bytes(b'\0'.join([
            b'/usr/bin/python3', b'-I', b'-S', b'-B',
            bytes(self.controller), b'']))
        self.metadata = dict(pid=456, controller_pid=123,
                             controller_path=str(self.controller),
                             expires_utc=runtime.EXPIRY.isoformat(),
                             renewal_id=runtime.RENEWAL, bind='127.0.0.1:8000',
                             automatic_renewal=False)
        self.server.write_text(json.dumps(self.metadata))
        patches = [patch.object(runtime, 'ROOT', self.root),
                   patch.object(runtime, 'SERVER', self.server),
                   patch.object(runtime.boundary, 'ROOT', self.root),
                   patch.object(runtime, 'utcnow', return_value=runtime.EXPIRY-timedelta(days=1)),
                   patch.object(runtime.os, 'getpid', return_value=456),
                   patch.object(runtime.os, 'getppid', return_value=123),
                   patch.object(runtime.sys, 'argv', [str(self.root / 'backend/manage.py'),
                                                      'catalogue_runserver', '--provider-demo']),
                   patch.object(runtime, 'Path', side_effect=lambda value=None: (
                       self.root if value is None else self.root / 'proc'
                       if value == '/proc' else Path(value)))]
        # Path.cwd is deliberately independent of process-wide chdir in tests.
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        runtime.Path.cwd.return_value = self.root

    def test_current_owned_receipt_allows_exact_expiry(self):
        self.assertEqual(runtime.session_guard(), runtime.EXPIRY)

    def test_stale_pid_renewal_expiry_or_controller_never_authorizes(self):
        for key, value in [('pid', 999), ('controller_pid', 999),
                           ('renewal_id', 'other'), ('bind', '0.0.0.0:8000'),
                           ('automatic_renewal', True),
                           ('expires_utc', (runtime.EXPIRY+timedelta(seconds=1)).isoformat())]:
            with self.subTest(key=key):
                self.server.write_text(json.dumps(dict(self.metadata, **{key: value})))
                with self.assertRaises(runtime.SessionUnavailable):
                    runtime.session_guard()

    def test_expiry_and_missing_receipt_fail_closed(self):
        with patch.object(runtime, 'utcnow', return_value=runtime.EXPIRY):
            with self.assertRaises(runtime.SessionUnavailable):
                runtime.session_guard()
        self.server.unlink()
        with self.assertRaises(OSError):
            runtime.session_guard()


class ClockTests(unittest.TestCase):
    def run_fake(self, guard, tick, *, expired=False):
        db = ModuleType('django.db')
        db.close_old_connections = Mock()
        django = ModuleType('django')
        service = ModuleType('shelf.weekly_synthesis')
        service.tick = tick
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = lambda delay: stop.is_set.configure_mock(return_value=True)
        now = runtime.EXPIRY if expired else datetime(2026, 10, 31, tzinfo=timezone.utc)
        with patch.dict('sys.modules', {'django': django, 'django.db': db,
                                       'shelf.weekly_synthesis': service}), \
                patch('shelf.weekly_synthesis', service, create=True), \
                patch.object(runtime, 'session_guard', guard), \
                patch.object(runtime, 'prepare_runtime'), \
                patch.object(runtime, 'publish_status') as publish:
            runtime.run_clock(stop, clock=lambda: now)
        return db, stop, publish

    def test_metadata_race_defers_tick_and_database_connections(self):
        tick = Mock()
        db, stop, publish = self.run_fake(Mock(side_effect=FileNotFoundError), tick)
        tick.assert_not_called()
        db.close_old_connections.assert_not_called()
        self.assertEqual(publish.call_args.args[0]['status'], 'pending_metadata')
        stop.wait.assert_called_once_with(60)

    def test_tick_is_guarded_and_connections_closed_after_exception(self):
        tick = Mock(side_effect=RuntimeError('private error'))
        db, stop, publish = self.run_fake(Mock(return_value=runtime.EXPIRY), tick)
        self.assertEqual(db.close_old_connections.call_count, 2)
        self.assertEqual(tick.call_args.kwargs['ledger_path'], runtime.LEDGER)
        self.assertEqual(publish.call_args.args[0]['status'], 'unavailable')

    def test_expired_clock_never_calls_guard_tick_or_database(self):
        guard, tick = Mock(), Mock()
        db, stop, publish = self.run_fake(guard, tick, expired=True)
        guard.assert_not_called()
        tick.assert_not_called()
        db.close_old_connections.assert_not_called()
        stop.wait.assert_not_called()
        self.assertEqual(publish.call_args.args[0]['status'], 'expired')

    def test_status_files_are_private_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            state = root / 'weekly/runtime'
            with patch.object(runtime, 'ROOT', root), \
                    patch.object(runtime.boundary, 'ROOT', root), \
                    patch.object(runtime, 'RUNTIME', state), \
                    patch.object(runtime, 'STATUS', state / 'status.json'):
                runtime.write_status({'status': 'pending_metadata'})
                self.assertEqual(state.stat().st_mode & 0o777, 0o700)
                self.assertEqual((state / 'status.json').stat().st_mode & 0o777, 0o600)
                (state / 'status.json').unlink()
                (state / 'status.json').symlink_to(root / 'outside')
                with self.assertRaises(runtime.SessionUnavailable):
                    runtime.write_status({'status': 'pending_metadata'})


class CommandLifecycleTests(unittest.TestCase):
    def test_fixed_checks_precede_start_and_exception_stops_and_joins(self):
        # Load the command against a minimal runserver fake even without Django.
        import importlib.util
        from types import SimpleNamespace

        class FakeRunserver:
            def inner_run(self, *args, **kwargs):
                raise RuntimeError('HTTP stopped')

        settings = SimpleNamespace(ROOT_URLCONF='cabinet.provider_urls')
        conf = ModuleType('django.conf')
        conf.settings = settings
        base = ModuleType('django.core.management.base')
        base.CommandError = ValueError
        server = ModuleType('django.core.management.commands.runserver')
        server.Command = FakeRunserver
        basehttp = ModuleType('django.core.servers.basehttp')
        basehttp.WSGIServer = type('FakeWSGIServer', (), {})
        path = Path(runtime.__file__).parent / 'management/commands/catalogue_runserver.py'
        spec = importlib.util.spec_from_file_location('clock_command_fixture', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'django.conf': conf,
                                       'django.core.management.base': base,
                                       'django.core.servers.basehttp': basehttp,
                                       'django.core.management.commands.runserver': server}):
            spec.loader.exec_module(module)
        command = module.Command()
        command.addr, command.port, command.use_ipv6 = '127.0.0.1', '8000', False
        stop, thread = Mock(), Mock()
        with patch.object(runtime, 'start_clock', return_value=(stop, thread)) as start:
            with self.assertRaises(ValueError):
                command.inner_run(use_reloader=True, use_threading=False)
            start.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, 'HTTP stopped'):
                command.inner_run(use_reloader=False, use_threading=False)
            start.assert_called_once_with()
            stop.set.assert_called_once_with()
            thread.join.assert_called_once_with(timeout=2)
