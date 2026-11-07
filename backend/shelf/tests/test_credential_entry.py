"""E01/E02/P01: disposable controlling PTYs, strict hidden input and isolation."""
import errno
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

from shelf import credential_entry as entry
from shelf import credential_store as store
from shelf.tests.fixtures_credentials import CANARY, FIXTURE, PYTHON, FakeTree, metadata


class PrivatePTY:
    def __init__(self, directory, verb='enter', *, foreground=True, timeout=False, redirected=None):
        self.master, self.slave = os.openpty()
        self.saved = termios.tcgetattr(self.slave)
        self.output = bytearray()
        def controlling_terminal():
            os.setsid()
            if foreground:
                fcntl.ioctl(1 if redirected == 0 else 0, termios.TIOCSCTTY, 0)
        arguments = [PYTHON, '-I', '-S', '-B', FIXTURE, 'pty', str(directory), verb]
        if timeout:
            arguments.append('timeout')
        try:
            self.child = subprocess.Popen(arguments,
                stdin=subprocess.PIPE if redirected == 0 else self.slave,
                stdout=subprocess.PIPE if redirected == 1 else self.slave,
                stderr=subprocess.PIPE if redirected == 2 else self.slave, preexec_fn=controlling_terminal,
                close_fds=True)
        except BaseException:
            os.close(self.master)
            os.close(self.slave)
            raise

    def until(self, needle, timeout=5):
        deadline = time.monotonic() + timeout
        while needle not in self.output:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.master], [], [], remaining)[0]:
                raise AssertionError('PTY prompt deadline')
            block = os.read(self.master, 4096)
            if not block:
                raise AssertionError('PTY closed before prompt')
            self.output.extend(block)
        return bytes(self.output)

    def send(self, prompt, value):
        self.until(prompt)
        flags = termios.tcgetattr(self.slave)[3]
        if flags & (termios.ECHO | termios.ECHONL | termios.ICANON):
            raise AssertionError('PTY was not hidden/noncanonical')
        payload = value + b'\n'
        while payload:
            sent = os.write(self.master, payload[:2048])
            payload = payload[sent:]

    def finish(self):
        result = self.child.wait(timeout=10)
        while select.select([self.master], [], [], 0)[0]:
            self.output.extend(os.read(self.master, 4096))
        for stream in (self.child.stdout, self.child.stderr):
            if stream is not None:
                self.output.extend(stream.read())
        if termios.tcgetattr(self.slave) != self.saved:
            raise AssertionError('PTY flags not restored')
        return result, bytes(self.output)

    def close(self):
        if self.child.poll() is None:
            self.child.kill()
            self.child.wait(timeout=10)
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            if stream is not None:
                stream.close()
        os.close(self.master)
        os.close(self.slave)


class CredentialEntryTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTree(store)
        self.addCleanup(self.fake.close)
        self.path = self.fake.path
        self.fake.store._lock.close()

    def tty(self, verb='enter', **kwargs):
        terminal = PrivatePTY(self.path, verb, **kwargs)
        self.addCleanup(terminal.close)
        return terminal

    def binding(self, terminal, *, profile=b'23'):
        terminal.send(b'Account ID: ', b'FICTION_ACCOUNT')
        terminal.send(b'Actor ID: ', b'17')
        terminal.send(b'Profile ID: ', profile)

    def inputs(self, terminal, *, token=CANARY, profile=b'23'):
        self.binding(terminal, profile=profile)
        values = metadata(store)
        terminal.send(b'Local expiry UTC (YYYY-MM-DDTHH:MM:SSZ): ', store._time_text(values.expires_at).encode())
        terminal.send(b'Declared provider expiry UTC: ', store._time_text(values.declared_provider_expires_at).encode())
        terminal.send(b'Token: ', token)

    def test_E01_actual_hidden_entry_replace_and_remove(self):
        terminal = self.tty()
        self.inputs(terminal)
        terminal.send(b'Type STORE to confirm: ', b'STORE')
        code, output = terminal.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b'credential-entry enter stored complete', output)
        self.assertNotIn(CANARY, output)
        self.assertNotIn(b'FICTION_ACCOUNT', output)
        original = json.loads((self.path / store.RECORD).read_bytes())
        replacement = self.tty('replace')
        self.inputs(replacement, token=b'REPLACEMENT-NONCREDENTIAL')
        replacement.send(b'Type STORE to confirm: ', b'STORE')
        code, output = replacement.finish()
        self.assertEqual(code, 0, output)
        current = json.loads((self.path / store.RECORD).read_bytes())
        self.assertNotEqual(original['generation'], current['generation'])
        self.assertNotIn(b'REPLACEMENT-NONCREDENTIAL', output)
        removal = self.tty('remove')
        self.binding(removal)
        removal.send(b'Type REMOVE to confirm: ', b'REMOVE')
        code, output = removal.finish()
        self.assertEqual(code, 0, output)
        self.assertFalse((self.path / store.RECORD).exists())
        self.assertEqual(os.listdir(self.path), [store.LOCK])

    def test_E01_actual_CLI_writer_lock_is_nonblocking(self):
        store.lock_store(self.fake.store)
        terminal = self.tty()
        code, output = terminal.finish()
        self.assertEqual(code, 2)
        self.assertIn(b'credential-entry enter refused busy', output)
        self.assertNotIn(b'Account ID:', output)

    def test_E01_expired_remove_remains_available(self):
        store.lock_store(self.fake.store)
        store.commit_entry(self.fake.store, metadata(store), bytearray(CANARY))
        body = json.loads((self.path / store.RECORD).read_bytes())
        body['entered_at'] = '2020-01-01T00:00:00Z'
        body['expires_at'] = body['declared_provider_expires_at'] = '2020-01-02T00:00:00Z'
        (self.path / store.RECORD).write_text(json.dumps(body))
        self.fake.store._lock.close()
        terminal = self.tty('remove')
        self.binding(terminal)
        terminal.send(b'Type REMOVE to confirm: ', b'REMOVE')
        self.assertEqual(terminal.finish()[0], 0)
        self.assertFalse((self.path / store.RECORD).exists())

    def test_E01_explicit_absent_reconcile_and_dirty_refusal(self):
        store.lock_store(self.fake.store)
        store._marker(self.fake.store, 'enter')
        self.fake.store._lock.close()
        terminal = self.tty('reconcile')
        terminal.send(b'Intended state (present/absent/discard): ', b'absent')
        terminal.send(b'Type RECONCILE to confirm: ', b'RECONCILE')
        code, output = terminal.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b'credential-entry reconcile reconciled complete', output)
        self.assertFalse((self.path / store.PENDING).exists())

    def test_E02_overlong_token_is_drained_and_never_truncated_to_valid(self):
        terminal = self.tty()
        self.inputs(terminal, token=CANARY + b'A' * 5000)
        code, output = terminal.finish()
        self.assertEqual(code, 2, output)
        self.assertNotIn(CANARY, output)
        self.assertNotIn(b'A' * 32, output)
        self.assertNotIn(b'Type STORE', output)
        self.assertFalse((self.path / store.RECORD).exists())

    def test_E02_nonascii_control_and_header_tokens_refused_hidden(self):
        for token in (b'Bearer-NONCREDENTIAL canary', b'FICTION\x00VALUE', b'FICTION\xffVALUE'):
            with self.subTest(token=token):
                terminal = self.tty()
                self.inputs(terminal, token=token)
                code, output = terminal.finish()
                self.assertEqual(code, 2)
                self.assertNotIn(b'FICTION_ACCOUNT', output)
                self.assertNotIn(token, output)
                self.assertFalse((self.path / store.RECORD).exists())

    def test_E02_profile_decimal_bound_rejected_before_secret(self):
        terminal = self.tty()
        self.binding(terminal, profile=b'1234567890123456789')
        code, output = terminal.finish()
        self.assertEqual(code, 2, output)
        self.assertNotIn(b'Local expiry', output)
        self.assertNotIn(b'1234567890123456789', output)

    def test_E02_catchable_signals_restore_and_flush_hidden_partial_input(self):
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGTSTP):
            with self.subTest(signal=signum):
                terminal = self.tty()
                terminal.until(b'Account ID: ')
                os.write(terminal.master, CANARY)
                terminal.child.send_signal(signum)
                code, output = terminal.finish()
                self.assertEqual(code, 2, output)
                self.assertNotIn(CANARY, output)
                self.assertFalse((self.path / store.RECORD).exists())

    def test_E02_all_standard_fds_and_foreground_controlling_identity(self):
        result = subprocess.run([PYTHON, '-I', '-S', '-B', FIXTURE, 'pty', str(self.path), 'enter'],
                                input=CANARY, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b'')
        self.assertNotIn(CANARY, result.stderr)
        terminal = self.tty(foreground=False)
        code, output = terminal.finish()
        self.assertEqual(code, 2)
        self.assertNotIn(b'Account ID:', output)

    def test_E02_each_redirected_standard_fd_refuses_before_private_input(self):
        for fd in (0, 1, 2):
            with self.subTest(fd=fd):
                terminal = self.tty(redirected=fd)
                code, output = terminal.finish()
                self.assertEqual(code, 2, output)
                self.assertNotIn(b'Account ID:', output)
                self.assertFalse((self.path / store.RECORD).exists())

    def test_E02_lost_diagnostic_channel_does_not_leak_raw_errors(self):
        with patch.object(entry.os, 'write', side_effect=OSError(errno.EIO, 'PRIVATE-NONCREDENTIAL')):
            self.assertEqual(entry._receipt(store.EntryReceipt('enter', 'stored', 'complete')), 3)
            self.assertEqual(entry.main(['invalid']), 2)

    def test_E02_one_absolute_deadline_includes_metadata_and_overflow_drain(self):
        readfd, writefd = os.pipe()
        self.addCleanup(os.close, readfd)
        self.addCleanup(os.close, writefd)
        os.write(writefd, b'X' * 32)  # no delimiter: bounded drain must time out
        start = time.monotonic()
        with patch.object(entry.os, 'write', return_value=1):
            with self.assertRaises(entry.EntryFailure):
                entry._prompt(readfd, 'Hidden: ', 8, start + 0.08)
        self.assertLess(time.monotonic() - start, 1)
        # A deadline already spent on earlier metadata refuses even queued input.
        os.write(writefd, b'FICTION\n')
        with patch.object(entry.os, 'write', return_value=1):
            with self.assertRaises(entry.EntryFailure):
                entry._prompt(readfd, 'Hidden: ', 64, time.monotonic() - 1)

    def test_E02_terminal_restoration_after_real_timeout(self):
        terminal = self.tty(timeout=True)
        terminal.until(b'Account ID: ')
        started = time.monotonic()
        code, output = terminal.finish()
        self.assertEqual(code, 2, output)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse((self.path / store.RECORD).exists())

    def test_P01_standalone_invalid_verb_ignores_cwd_env_and_site_hooks(self):
        script = str(Path(entry.__file__).absolute())
        marker = self.path / 'IMPORT-MUST-NOT-RUN'
        poison = "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('BAD')\n"
        for name in ('sitecustomize.py', 'usercustomize.py', 'credential_store.py', 'json.py'):
            (self.path / name).write_text(poison)
        environment = dict(os.environ, PYTHONPATH=str(self.path), PYTHONSTARTUP=str(self.path / 'sitecustomize.py'))
        result = subprocess.run([PYTHON, '-I', '-S', '-B', script, 'INVALID'], cwd=self.path,
                                env=environment, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'credential-entry refused invalid_command\n')
        self.assertFalse(marker.exists())
        self.assertFalse((self.path / store.RECORD).exists())

    def test_P01_import_code_rejects_writable_link_and_substitution(self):
        code = self.path / 'fake_component.py'
        code.write_text('SAFE = True\n')
        directory = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, directory)
        with self.assertRaises(entry.EntryFailure):
            entry._checked_code(directory, code.name, owner=os.getuid())
        code.chmod(0o444)
        self.assertEqual(entry._checked_code(directory, code.name, owner=os.getuid()), b'SAFE = True\n')
        link = self.path / 'linked.py'
        os.link(code, link)
        with self.assertRaises(entry.EntryFailure):
            entry._checked_code(directory, code.name, owner=os.getuid())
        link.unlink()
        code.unlink()
        code.symlink_to(self.path / 'missing-owned-code')
        with self.assertRaises(OSError) as caught:
            entry._checked_code(directory, code.name, owner=os.getuid())
        self.assertEqual(caught.exception.errno, errno.ELOOP)

    def test_P01_source_off_no_broker_or_production_test_path(self):
        self.assertEqual(store.STORE_DIRECTORY, '/home/tang/.local/state/culture-cabinet-secrets/hardcover-backend')
        self.assertEqual(entry.ENTRY_SCRIPT, '/home/tang/Projects/culture-cabinet/backend/shelf/credential_entry.py')
        self.assertFalse(hasattr(store, 'release'))
        self.assertFalse(hasattr(store, 'CredentialBroker'))
        terminal = self.tty()
        self.inputs(terminal)
        terminal.send(b'Type STORE to confirm: ', b'STORE')
        self.assertEqual(terminal.finish()[0], 0)

    def test_P01_audit_observability_is_not_exclusion_or_erasure(self):
        # An owned fake audit hook observes OS events. Actual deployment must
        # exclude hostile hooks/debuggers/session recording independently.
        code = "import sys,os; seen=[]; sys.addaudithook(lambda event,args: seen.append(event)); fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,b'OBVIOUS-NONCREDENTIAL'); os.close(fd); print('AUDIT_OBSERVED' if 'open' in seen else 'MISSING')"
        result = subprocess.run([PYTHON, '-I', '-S', '-B', '-c', code, str(self.path / 'audit-NONCREDENTIAL')], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'AUDIT_OBSERVED\n')
        self.assertEqual(result.stderr, b'')

    def test_CV1_hidden_discard_cancellation_and_lost_token_fresh_entry(self):
        store.lock_store(self.fake.store)
        self.assertEqual(store.commit_entry(self.fake.store, metadata(store), bytearray(CANARY)).outcome, 'stored')
        store._marker(self.fake.store, 'remove')
        self.fake.store._lock.close()
        before = {p.name: p.read_bytes() for p in self.path.iterdir()}
        cancelled = self.tty('reconcile')
        cancelled.send(b'Intended state (present/absent/discard): ', b'discard')
        cancelled.send(b'Type DISCARD to confirm: ', b'RECONCILE')
        code, output = cancelled.finish()
        self.assertEqual(code, 2, output)
        self.assertEqual({p.name: p.read_bytes() for p in self.path.iterdir()}, before)
        self.assertNotIn(CANARY, output)
        terminal = self.tty('reconcile')
        terminal.send(b'Intended state (present/absent/discard): ', b'discard')
        terminal.send(b'Type DISCARD to confirm: ', b'DISCARD')
        code, output = terminal.finish()
        self.assertEqual(code, 0, output)
        self.assertIn(b'without the old token', output)
        self.assertIn(b'lost provider key must be revoked separately by the human', output)
        self.assertNotIn(b'Token: ', output)
        self.assertNotIn(CANARY, output)
        self.assertEqual(os.listdir(self.path), [store.LOCK])
        fresh = self.tty()
        self.inputs(fresh, token=b'FRESH-NONCREDENTIAL')
        fresh.send(b'Type STORE to confirm: ', b'STORE')
        code, output = fresh.finish()
        self.assertEqual(code, 0, output)
        self.assertNotIn(b'FRESH-NONCREDENTIAL', output)
        self.assertEqual(json.loads((self.path / store.RECORD).read_bytes())['token'], 'FRESH-NONCREDENTIAL')

    def test_CV1_hidden_discard_unsafe_artifact_refusal_restores_terminal(self):
        store.lock_store(self.fake.store)
        store._create(self.fake.store, store.PENDING, b'{"schema')
        self.fake.store._lock.close()
        (self.path / store.RECORD).symlink_to(self.path / 'missing-owned-fixture')
        terminal = self.tty('reconcile')
        terminal.send(b'Intended state (present/absent/discard): ', b'discard')
        terminal.send(b'Type DISCARD to confirm: ', b'DISCARD')
        code, output = terminal.finish()
        self.assertEqual(code, 2, output)
        self.assertIn(b'credential-entry reconcile refused unsafe_store', output)
        self.assertTrue((self.path / store.RECORD).is_symlink())
        self.assertEqual((self.path / store.PENDING).read_bytes(), b'{"schema')

    def bootstrap_fixture(self):
        home = self.path / 'bootstrap-root' / 'home'
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        home.parent.chmod(0o700)
        directory = home / 'checked-code'
        directory.mkdir(mode=0o700)
        (directory / 'credential_entry.py').write_bytes(Path(entry.__file__).read_bytes())
        raw = Path(store.__file__).read_bytes()
        raw += b'\n\ndef open_store():\n    raise AssertionError("production store forbidden in bootstrap fixture")\n'
        (directory / 'credential_store.py').write_bytes(raw)
        for code in directory.iterdir():
            code.chmod(0o444)
        return directory

    def bootstrap(self, directory, scenario='accept', *, cwd=None, env=None):
        return subprocess.run([PYTHON, '-I', '-S', '-B', FIXTURE, 'bootstrap', str(directory), scenario],
                              cwd=cwd, env=env, capture_output=True, timeout=10)

    def test_CV2_actual_bootstrap_pinned_owner_checked_sibling_poisoned_imports(self):
        directory = self.bootstrap_fixture()
        self.assertEqual((directory / 'credential_store.py').stat().st_uid, 1000)
        poison_dir = self.path / 'poison'
        poison_dir.mkdir(mode=0o700)
        marker = poison_dir / 'MUST-NOT-IMPORT'
        poison = 'from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("BAD")\nraise RuntimeError("POISON")\n'
        for name in ('sitecustomize.py', 'usercustomize.py', 'credential_store.py', 'json.py', 'stat.py', 'shelf.py', 'django.py'):
            (poison_dir / name).write_text(poison)
        environment = dict(os.environ, PYTHONPATH=str(poison_dir), PYTHONSTARTUP=str(poison_dir / 'sitecustomize.py'),
                           DJANGO_SETTINGS_MODULE='poison.settings')
        result = self.bootstrap(directory, cwd=poison_dir, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, b'BOOTSTRAP_ACCEPTED_OPENER_BLOCKED\n')
        self.assertEqual(result.stderr, b'')
        self.assertFalse(marker.exists())
        self.assertFalse((self.path / store.RECORD).exists())

    def test_CV2_actual_bootstrap_wrong_owner_identity_and_fixed_runtime_refused(self):
        directory = self.bootstrap_fixture()
        for scenario in ('wrong-owner', 'wrong-group', 'substitution', 'directory-substitution', 'mutation',
                         'no-isolation', 'site-enabled', 'bytecode-enabled', 'wrong-interpreter', 'wrong-script'):
            with self.subTest(scenario=scenario):
                # Mutating scenarios get their own source tree; no policy is
                # overridden to manufacture acceptance of untrusted metadata.
                import shutil
                if directory.exists():
                    shutil.rmtree(directory)
                retired = directory.with_name('retired-code')
                if retired.exists():
                    shutil.rmtree(retired)
                directory = self.bootstrap_fixture()
                replacement = directory / 'replacement.py'
                replacement.write_bytes((directory / 'credential_store.py').read_bytes())
                replacement.chmod(0o444)
                result = self.bootstrap(directory, scenario)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, b'BOOTSTRAP_REFUSED\n')
                self.assertEqual(result.stderr, b'')

    def test_CV2_actual_bootstrap_writable_exact_mode_alias_type_ACL_refused(self):
        for scenario in ('writable', 'mode', 'hardlink', 'symlink', 'fifo', 'acl'):
            with self.subTest(scenario=scenario):
                import shutil
                directory = self.path / 'bootstrap-root' / 'home' / 'checked-code'
                if directory.exists():
                    shutil.rmtree(directory)
                directory = self.bootstrap_fixture()
                code = directory / 'credential_store.py'
                if scenario == 'writable':
                    code.chmod(0o644)
                elif scenario == 'mode':
                    code.chmod(0o400)
                elif scenario == 'hardlink':
                    os.link(code, directory / 'alias.py')
                elif scenario == 'symlink':
                    code.rename(directory / 'target.py'); code.symlink_to(directory / 'target.py')
                elif scenario == 'fifo':
                    code.unlink(); os.mkfifo(code, 0o444)
                else:
                    import struct
                    acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', tag, perm, ident) for tag, perm, ident in
                        [(1, 4, 0xffffffff), (2, 4, os.getuid()), (4, 4, 0xffffffff), (16, 4, 0xffffffff), (32, 4, 0xffffffff)])
                    os.setxattr(code, 'system.posix_acl_access', acl); code.chmod(0o444)
                result = self.bootstrap(directory)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, b'BOOTSTRAP_REFUSED\n')
                self.assertEqual(result.stderr, b'')


    def test_smoke_requires_exact_manual_live_flag_before_loading_or_store_io(self):
        for verb in entry._SMOKES:
            for args in ([verb], [verb, '--live=false'], [verb, '--live', '--live'],
                         [verb, '--token=NONCREDENTIAL'], [verb, '--live', '--path=/fake']):
                with self.subTest(args=args), patch.object(entry, '_deployment_store') as loader, \
                        patch.object(entry, '_emit') as emit:
                    self.assertEqual(entry.main(args), 2)
                    loader.assert_not_called()
                    emit.assert_called_once_with(b'credential-entry refused explicit_live_required\n')

    def test_smoke_dispatch_uses_checked_loader_and_fixed_provider(self):
        from unittest.mock import Mock
        for verb, provider in (('smoke-hardcover', 'hardcover'), ('smoke-tmdb', 'tmdb')):
            sender = Mock()
            sender.run_live.return_value = 2
            with self.subTest(verb=verb), patch.object(entry, '_deployment_store', return_value=(store, sender)) as loader:
                self.assertEqual(entry.main([verb, '--live']), 2)
            loader.assert_called_once_with(smoke=True)
            sender.run_live.assert_called_once_with(store, provider)

    def test_smoke_actual_bootstrap_checks_sibling_before_loading(self):
        from shelf import provider_smoke
        directory = self.bootstrap_fixture()
        code = directory / 'provider_smoke.py'
        code.write_bytes(Path(provider_smoke.__file__).read_bytes())
        code.chmod(0o444)
        result = self.bootstrap(directory, 'smoke')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, b'BOOTSTRAP_ACCEPTED_OPENER_BLOCKED\n')
        self.assertEqual(result.stderr, b'')
        code.chmod(0o644)
        result = self.bootstrap(directory, 'smoke-writable')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, b'BOOTSTRAP_REFUSED\n')
        self.assertEqual(result.stderr, b'')


class QuickSetupTests(unittest.TestCase):
    def terminal(self, provider, **kwargs):
        fake = FakeTree(store, provider=provider)
        self.addCleanup(fake.close)
        fake.store._lock.close()
        terminal = PrivatePTY(fake.path, 'setup-' + provider, **kwargs)
        self.addCleanup(terminal.close)
        return fake, terminal

    def assert_no_metadata_prompts(self, output):
        for prompt in (b'Account ID:', b'Actor ID:', b'Profile ID:', b'Local expiry UTC', b'Declared provider expiry UTC'):
            self.assertNotIn(prompt, output)
        self.assertNotIn(CANARY, output)
        self.assertNotIn(b'FICTION_ACCOUNT', output)

    def test_quick_hardcover_and_tmdb_hidden_token_STORE_actual_defaults_and_caps(self):
        from datetime import timedelta
        for provider in ('hardcover', 'tmdb'):
            with self.subTest(provider=provider):
                fake, terminal = self.terminal(provider)
                before = store.utc_now()
                terminal.send(b'Token: ', CANARY)
                terminal.send(b'Type STORE to confirm: ', b'STORE')
                code, output = terminal.finish()
                after = store.utc_now()
                self.assertEqual(code, 0, output)
                self.assertIn(b'credential-entry enter stored complete', output)
                self.assert_no_metadata_prompts(output)
                self.assertIn(b'89-day local cap and declared expiry', output)
                self.assertIn(b'UNVERIFIED/not provider-attested', output)
                self.assertIn(b'source off, identity verification required before activation', output)
                if provider == 'hardcover':
                    for scope in store.SCOPES:
                        self.assertIn(scope.encode(), output)
                    self.assertIn(b'fresh 90-day key', output)
                else:
                    self.assertIn(b'API Read Access Token (application Bearer)', output)
                    self.assertIn(b'local token-type contract', output)
                    self.assertIn(b'GET authentication/catalogue only, no user/session/account writes', output)
                store.lock_store(fake.store)
                record = store.read_record(fake.store)
                self.addCleanup(record.close)
                values = record.metadata
                self.assertEqual(values.binding, store.Binding('unverified', '1', '1', provider, fake.store.context.purpose))
                self.assertEqual(values.scopes, fake.store.context.scopes)
                self.assertFalse(record.verified)
                self.assertEqual(record.secret, CANARY)
                self.assertLessEqual(before, values.entered_at)
                self.assertLessEqual(values.entered_at, after)
                self.assertEqual(values.expires_at, values.declared_provider_expires_at)
                self.assertLessEqual(before + timedelta(days=89), values.expires_at)
                self.assertLessEqual(values.expires_at, after + timedelta(days=89))
                self.assertLessEqual(values.expires_at - values.entered_at, timedelta(days=89))
                body = json.loads((fake.path / store.RECORD).read_bytes())
                self.assertEqual(body['schema'], 1)
                self.assertNotIn('verified', body)

    def test_quick_STORE_cancel_and_confirmation_overflow_do_not_create_records(self):
        for provider in ('hardcover', 'tmdb'):
            for confirmation in (b'NO', b'STOREX'):
                with self.subTest(provider=provider, confirmation=confirmation):
                    fake, terminal = self.terminal(provider)
                    terminal.send(b'Token: ', CANARY)
                    terminal.send(b'Type STORE to confirm: ', confirmation)
                    code, output = terminal.finish()
                    self.assertEqual(code, 2, output)
                    self.assertIn(b'credential-entry enter refused interrupted', output)
                    self.assert_no_metadata_prompts(output)
                    self.assertEqual(os.listdir(fake.path), [store.LOCK])

    def test_quick_token_overflow_invalid_input_and_signals_remain_hidden(self):
        for provider in ('hardcover', 'tmdb'):
            for token in (CANARY + b'x' * 4096, b'Bearer NONCREDENTIAL', b'bad\x00token', b'bad\xfftoken', b''):
                with self.subTest(provider=provider, length=len(token)):
                    fake, terminal = self.terminal(provider)
                    terminal.send(b'Token: ', token)
                    code, output = terminal.finish()
                    self.assertEqual(code, 2, output)
                    self.assert_no_metadata_prompts(output)
                    self.assertNotIn(b'Type STORE', output)
                    self.assertEqual(os.listdir(fake.path), [store.LOCK])
            fake, terminal = self.terminal(provider)
            terminal.until(b'Token: ')
            os.write(terminal.master, CANARY)
            os.kill(terminal.child.pid, signal.SIGINT)
            code, output = terminal.finish()
            self.assertEqual(code, 2, output)
            self.assert_no_metadata_prompts(output)
            self.assertEqual(os.listdir(fake.path), [store.LOCK])

    def test_quick_deadline_and_existing_record_conflict_preserve_store(self):
        for provider in ('hardcover', 'tmdb'):
            with self.subTest(provider=provider):
                fake, terminal = self.terminal(provider, timeout=True)
                terminal.until(b'Token: ')
                os.write(terminal.master, CANARY)
                code, output = terminal.finish()
                self.assertEqual(code, 2, output)
                self.assert_no_metadata_prompts(output)
                self.assertEqual(os.listdir(fake.path), [store.LOCK])
                store.lock_store(fake.store)
                receipt = store.commit_entry(fake.store, entry._setup_metadata(store, provider), bytearray(CANARY))
                self.assertEqual(receipt.outcome, 'stored')
                original = (fake.path / store.RECORD).read_bytes()
                fake.store._lock.close()
                second = PrivatePTY(fake.path, 'setup-' + provider)
                self.addCleanup(second.close)
                second.send(b'Token: ', b'OTHER-NONCREDENTIAL')
                second.send(b'Type STORE to confirm: ', b'STORE')
                code, output = second.finish()
                self.assertEqual(code, 2, output)
                self.assertIn(b'credential-entry enter refused conflict', output)
                self.assertEqual((fake.path / store.RECORD).read_bytes(), original)
                self.assertNotIn(b'OTHER-NONCREDENTIAL', output)

    def test_fixed_command_dispatch_and_no_provider_path_secret_options(self):
        for verb in (*entry._VERBS, *entry._SETUPS):
            with self.subTest(verb=verb), patch.object(entry, '_deployment_store', return_value=store), \
                    patch.object(store, 'open_store') as opener:
                def run(operation, module, fixed_opener):
                    self.assertEqual(operation, verb)
                    self.assertIs(module, store)
                    fixed_opener()
                    return 0
                with patch.object(entry, '_run', side_effect=run):
                    self.assertEqual(entry.main([verb]), 0)
                if verb in entry._SETUPS:
                    opener.assert_called_once_with(provider='tmdb' if verb == 'setup-tmdb' else 'hardcover')
                else:
                    opener.assert_called_once_with()
        for args in (['setup-other'], ['setup-tmdb', '--provider=hardcover'], ['setup-hardcover', '--path=/tmp/fake'],
                     ['setup-tmdb', '--token=NONCREDENTIAL'], ['setup-hardcover', 'NONCREDENTIAL']):
            with self.subTest(args=args), patch.object(entry, '_deployment_store', side_effect=AssertionError('must not load')), \
                    patch.object(entry, '_emit') as emit:
                self.assertEqual(entry.main(args), 2)
                emit.assert_called_once_with(b'credential-entry refused invalid_command\n')

    def test_quick_wrong_context_opener_refuses_before_secret_prompt(self):
        fake = FakeTree(store)
        self.addCleanup(fake.close)
        fake.store._lock.close()
        from contextlib import nullcontext
        with patch.object(entry, '_hidden_terminal', nullcontext), patch.object(entry, '_emit') as emit, \
                patch.object(entry, '_prompt', side_effect=AssertionError('no secret prompt')):
            self.assertEqual(entry._run('setup-tmdb', store, lambda: store._fixture_store(fake.path)), 2)
        emit.assert_called_once_with(b'credential-entry enter refused invalid_input\n')
