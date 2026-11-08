"""Stdlib-only actual CLI fixture; no alternate product factory or test switch.

Terminal bootstrap values stay in RAM. The child runs the normal entrypoint with
an audit hook that denies every outbound socket/lookup; parent HTTP may reach
only the recorded private loopback origin. stop() owns only this child session.
"""
import atexit
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
import threading

_CLI_STARTS = 0
_USAGE = []
_ACTIVE = set()


def _usage_summary():
    # Best effort on normal interpreter exit only; outer supervision must own
    # cleanup after termination signals or abrupt runner loss.
    for fixture in tuple(_ACTIVE):
        try:
            fixture.stop()
        except BaseException:
            # Keep the incomplete record visible; never replace it with success.
            pass
    print('PORTABLE_FIXTURE_AGGREGATE ' + json.dumps(dict(
        starts=len(_USAGE), records=_USAGE,
        complete=all(row.get('complete') is True for row in _USAGE))), flush=True)


atexit.register(_usage_summary)

_CHILD = r'''
import os, runpy, sys
entry, audit = sys.argv[1:3]
args = sys.argv[3:]
def deny(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen',
                 'os.fork', 'os.forkpty', 'os.posix_spawn', 'os.exec'}:
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


_RESOURCE_LOCK = threading.RLock()
_RESOURCE_COUNTS = {}


def resource_event(**record):
    """Secret-free task evidence; unset channel leaves ordinary fixtures alone."""
    channel = os.environ.get('PR001_RESOURCE_LOG')
    if channel:
        data = (json.dumps(record, sort_keys=True) + '\n').encode()
        fd = os.open(channel, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            if os.write(fd, data) != len(data):
                raise PortableFixtureFailure('RESOURCE_RECORD_INCOMPLETE')
        finally:
            os.close(fd)


def reserve_resource(category, case_id, maximum):
    if not os.environ.get('PR001_RESOURCE_LOG'):
        return None
    with _RESOURCE_LOCK:
        count = _RESOURCE_COUNTS.get(category, 0)
        if count >= maximum:
            raise PortableFixtureFailure('RESOURCE_ACQUISITION_BOUND')
        _RESOURCE_COUNTS[category] = count + 1
        start = time.monotonic()
        resource_event(category=category, case_id=case_id, phase='reserved',
                       ordinal=count+1, start=start, owner_pid=os.getpid())
        return start


class BoundSocketLease:
    """One pre-reserved BoundRoot acquisition and idempotent measured close.

    Normal fixture cleanup is primary; the timer starts shutdown early. Forced
    cleanup is retained as failure, never substituted for a passing test. An
    independent execution supervisor is still required for abrupt runner loss.
    """
    def __init__(self, case_id, *, lifetime=20):
        if type(lifetime) is not int or lifetime not in (20, 25):
            raise PortableFixtureFailure('BOUND_SOCKET_LIFETIME')
        self.lifetime = lifetime
        self.case_id = case_id
        self.start = reserve_resource('bound_socket', case_id, 13)
        self.boundary = None
        self.lock = threading.RLock()
        self.closed = self.forced = False
        self.timer = None

    def attach(self, boundary):
        self.boundary = boundary
        if self.start is not None:
            info = os.fstat(boundary._socket.fileno())
            resource_event(category='bound_socket', case_id=self.case_id,
                phase='acquired', start=self.start, owner_pid=os.getpid(),
                socket_identity=[info.st_dev, info.st_ino], fd=boundary._socket.fileno())
            self.timer = threading.Timer(max(0, self.start+self.lifetime-time.monotonic()), self.expire)
            self.timer.daemon = True
            self.timer.start()

    def expire(self):
        self.forced = True
        self.close()

    def close(self):
        with self.lock:
            if self.closed:
                if self.forced:
                    raise PortableFixtureFailure('BOUND_SOCKET_DEADLINE')
                return
            if self.timer:
                self.timer.cancel()
            try:
                if self.boundary is not None:
                    self.boundary.close()
            finally:
                self.closed = True
                if self.start is not None:
                    end = time.monotonic()
                    complete = (self.boundary is not None and
                        self.boundary._socket.fileno() == -1 and end-self.start <= 30 and not self.forced)
                    resource_event(category='bound_socket', case_id=self.case_id,
                        phase='closed' if self.boundary is not None else 'failed_acquisition',
                        start=self.start, end=end, elapsed=end-self.start,
                        complete=complete, forced=self.forced, owner_pid=os.getpid())
                    if not complete:
                        raise PortableFixtureFailure('BOUND_SOCKET_CUSTODY_UNPROVEN')


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
        self._stop_lock = threading.RLock()
        self._timer = None
        self._usage = None
        self._stop_receipt = None

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
        reserved = None
        try:
            try:
                case_id = 'shelf.tests.test_portable_launch.PortableLaunchTests.test_actual_html_commands_and_security_guards'
                measured_attempt = isinstance(subprocess.Popen, type)
                reserved = reserve_resource('cli_child', case_id, 1) if measured_attempt else None
                spawned_at = time.monotonic() if reserved is None else reserved
                self.process = subprocess.Popen([sys.executable, '-I', '-B', '-c', _CHILD,
                    str(self.source_root / 'backend/portable.py'), str(self.audit_path),
                    '--data-root', str(self.data_root), '--port', str(self.port)],
                    stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, env=env,
                    cwd=self.source_root, start_new_session=True)
                _CLI_STARTS += 1  # Count actual children, not injected spawn failures.
                self._usage = dict(pid=self.process.pid, start=spawned_at, complete=False)
                if reserved is not None:
                    self._usage['case_id'] = case_id
                    resource_event(category='cli_child', case_id=case_id, phase='acquired',
                        start=spawned_at, pid=self.process.pid, group=self.process.pid, owner_pid=os.getpid())
                _USAGE.append(self._usage)
                _ACTIVE.add(self)
                # Shutdown has up to 6.2 seconds of bounded waits/probes. Start
                # at 20 seconds to leave headroom inside the whole-life cap.
                self._timer = threading.Timer(max(0, spawned_at + 20 - time.monotonic()), self.stop)
                self._timer.daemon = True
                self._timer.start()
            finally:
                os.close(slave)
        except BaseException:
            if reserved is not None and self.process is None:
                resource_event(category='cli_child', case_id=case_id,
                    phase='failed_acquisition', start=reserved, end=time.monotonic(),
                    owner_pid=os.getpid(), complete=False)
            self.stop()  # Includes master ownership even when Popen never returns.
            raise
        end = min(time.monotonic() + self.timeout, spawned_at + 20)
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
                    if match and self.origin is None:
                        self.origin = match[1]
                        resource_event(category='cli_listener', case_id=case_id, phase='acquired',
                            start=spawned_at, pid=self.process.pid,
                            port=int(self.origin.rsplit(':', 1)[1]), owner_pid=os.getpid())
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
        # The deadline callback and caller must never race signals or reaping.
        with self._stop_lock:
            if self._stop_receipt is not None:
                return dict(self._stop_receipt)
            if self._timer is not None:
                self._timer.cancel()
            result = self._stop()
            if self._usage is not None:
                ended = time.monotonic()
                elapsed = ended - self._usage['start']
                reaped = self.process.poll() is not None
                self._usage.update(reap=ended, elapsed=elapsed,
                    termination='KILL' if self.emergency_kill_used else 'TERM_OR_EXIT',
                    remaining_owned_pids=result['remaining_owned_pids'],
                    complete=(reaped and elapsed <= 30 and
                              result['remaining_owned_pids'] == [] and
                              result['listener_closed'] is True and
                              result['lock_released'] is True))
                resource_event(category='cli_child', phase='closed', **self._usage)
                if self.origin:
                    resource_event(category='cli_listener', phase='closed', **self._usage,
                                   listener_closed=result['listener_closed'])
                print('PORTABLE_FIXTURE_USAGE ' + json.dumps(self._usage), flush=True)
                # Unknown cleanup stays active for the exit safety attempt.
                if self._usage['complete']:
                    _ACTIVE.discard(self)
                    self._stop_receipt = dict(result)
                else:
                    raise PortableFixtureFailure('CLI_LIFETIME_OR_CLEANUP_UNPROVEN')
            return result

    def _stop(self):
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
                    group_members = members(self.process.pid)
                    if self._usage is not None and any(pid != self.process.pid for pid in group_members):
                        self.teardown_errors.append('UNEXPECTED_APPLICATION_DESCENDANTS')
                    if group_members:
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
