"""Stdlib-only actual CLI fixture; no alternate product factory or test switch.

Terminal bootstrap values stay in RAM. The child runs the normal entrypoint with
an audit hook that denies every outbound socket/lookup; parent HTTP may reach
only the recorded private loopback origin. stop() owns only this child session.
"""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import socket
import subprocess
import sys
import time

_CLI_STARTS = 0
_CHILD = r'''
import os, runpy, sys
entry, audit = sys.argv[1:3]
args = sys.argv[3:]
def deny(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        with open(audit, 'a') as stream:
            stream.write('OUTBOUND_DENIED\n')
        raise OSError('ISOLATED_EGRESS_DENIED')
sys.addaudithook(deny)
sys.path.insert(0, os.path.dirname(entry))
sys.argv = [entry, *args]
runpy.run_path(entry, run_name='__main__')
'''


class PortableFixtureFailure(Exception):
    pass


def members(group):
    result = []
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            try:
                fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == group or int(fields[3]) == group:
                    result.append(int(path.name))
            except (OSError, ValueError, IndexError):
                pass
    return sorted(result)


class PortableFixture:
    def __init__(self, source_root, data_root, audit_path, timeout=30, *, port=0, environment=None):
        self.source_root, self.data_root, self.audit_path = map(Path, [source_root, data_root, audit_path])
        self.timeout, self.port, self.environment = timeout, port, environment or {}
        self.process = None
        self.master = None
        self.origin = self.claim_code = None
        self._terminal = b''
        self.exit_code = None
        self.graceful_exit_code = None
        self.emergency_kill_used = False
        self.teardown_errors = []

    @property
    def pid(self):
        return self.process.pid if self.process is not None else None

    @property
    def audit_count(self):
        return len(self.audit_path.read_text().splitlines()) if self.audit_path.exists() else 0

    def start(self):
        global _CLI_STARTS
        if self.process is not None or _CLI_STARTS >= 8:
            raise PortableFixtureFailure('CLI_START_BOUND')
        self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.audit_path.touch(mode=0o600, exist_ok=False)
        self.master, slave = pty.openpty()
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', **self.environment}
        try:
            try:
                self.process = subprocess.Popen([sys.executable, '-I', '-B', '-c', _CHILD,
                    str(self.source_root / 'backend/portable.py'), str(self.audit_path),
                    '--data-root', str(self.data_root), '--port', str(self.port)],
                    stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, env=env,
                    cwd=self.source_root, start_new_session=True)
                _CLI_STARTS += 1  # Count actual children, not injected spawn failures.
            finally:
                os.close(slave)
        except BaseException:
            self.stop()  # Includes master ownership even when Popen never returns.
            raise
        end = time.monotonic() + self.timeout
        try:
            while time.monotonic() < end:
                ready, _, _ = select.select([self.master], [], [], min(0.1, max(0, end-time.monotonic())))
                if ready:
                    try:
                        chunk = os.read(self.master, 4096)
                    except OSError:
                        chunk = b''
                    self._terminal += chunk
                    if len(self._terminal) > 32768:
                        break
                    text = self._terminal.decode('utf-8', 'replace')
                    match = re.search(r'^Origin: (http://127\.0\.0\.1:\d+)\r?$', text, re.M)
                    if match:
                        self.origin = match[1]
                    challenge = re.search(r'Claim code[^\r\n]*\r?\n([A-Za-z0-9_-]{32})\r?\n', text)
                    if challenge:
                        self.claim_code = challenge[1]
                    if 'PORTABLE_READY\r\n' in text and self.origin and self.process.poll() is None:
                        return self
                if self.process.poll() is not None:
                    self.exit_code = self.process.returncode
                    break
            raise PortableFixtureFailure('PORTABLE_CHILD_NOT_READY')
        except BaseException:
            self.stop()
            raise

    def _signal(self, sig):
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            # Exiting between poll and signal is normal. Still reap and probe.
            pass

    def stop(self):
        """Each probe is independent; local descriptor/secret cleanup always runs.

        Emergency containment is not graceful product shutdown. None in the PID
        receipt means cleanup was not fully established after a genuine failure;
        it must never be mistaken for the successful empty-list proof.
        """
        remaining = []
        listener_closed = lock_released = True
        try:
            if self.process is not None:
                try:
                    if self.process.poll() is None:
                        self._signal(signal.SIGTERM)
                        try:
                            self.process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            self.emergency_kill_used = True
                            self._signal(signal.SIGKILL)
                            self.process.wait(timeout=2)
                    self.exit_code = self.process.returncode
                    if not self.emergency_kill_used:
                        self.graceful_exit_code = self.exit_code
                except Exception:
                    self.teardown_errors.append('PROCESS_STOP_FAILED')
                try:
                    if members(self.process.pid):
                        self.emergency_kill_used = True
                        self._signal(signal.SIGKILL)
                        end = time.monotonic()+1
                        while members(self.process.pid) and time.monotonic() < end:
                            time.sleep(0.02)
                    remaining = members(self.process.pid)
                except Exception:
                    self.teardown_errors.append('PROCESS_PROBE_FAILED')
            if self.origin:
                try:
                    port = int(self.origin.rsplit(':', 1)[1])
                    with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                        listener_closed = False
                except ConnectionRefusedError:
                    pass
                except Exception:
                    listener_closed = None
                    self.teardown_errors.append('LISTENER_PROBE_FAILED')
            # A lock-probe failure must not skip the independent PTY finalizer.
            try:
                lock = self.data_root / '.portable-lock'
                if lock.exists():
                    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        lock_released = False
                    finally:
                        os.close(fd)
            except Exception:
                lock_released = None
                self.teardown_errors.append('LOCK_PROBE_FAILED')
        finally:
            master, self.master = self.master, None
            try:
                if master is not None:
                    os.close(master)
            except OSError:
                self.teardown_errors.append('PTY_CLOSE_FAILED')
            finally:
                self._terminal = b''
                self.claim_code = None
        if self.teardown_errors:
            remaining = None
        return dict(remaining_owned_pids=remaining, listener_closed=listener_closed, lock_released=lock_released)
