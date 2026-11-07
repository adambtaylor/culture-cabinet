"""One bounded clock owned by the fixed catalogue server process.

The controller remains the lifecycle owner. This module never starts processes,
selects a deployment, calls a model, or grants a replacement session.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading

from cabinet import boundary

ROOT = Path('/home/tang/Projects/culture-cabinet')
SERVER = ROOT / '.p1-readiness/P2.1-funnel/deployment/server.json'
RUNTIME = ROOT / '.p1-readiness/P3.26-weekly-synthesis/runtime'
LEDGER = RUNTIME / 'cursor.json'
STATUS = RUNTIME / 'status.json'
EXPIRY = datetime(2026, 11, 6, 4, 17, 29, tzinfo=timezone.utc)
RENEWAL = '20261030-approved-seven-days'
INTERVAL = 60


class SessionUnavailable(ValueError):
    """Current controller metadata has not established this runtime's authority."""


def utcnow():
    return datetime.now(timezone.utc)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SessionUnavailable('Duplicate controller metadata')
        result[key] = value
    return result


def safe_path(path):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise SessionUnavailable('Symlink in runtime path')


def private_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise SessionUnavailable('Unsafe private runtime file')


def session_guard():
    """Re-read the shared controller receipt before every service mutation."""
    now = utcnow()
    if now >= EXPIRY or boundary.ROOT != ROOT or Path.cwd() != ROOT:
        raise SessionUnavailable('Outside fixed deployed session')
    safe_path(ROOT)
    if ROOT.stat().st_uid != os.getuid():
        raise SessionUnavailable('Deployment owner mismatch')
    expected = ['catalogue_runserver', '--provider-demo']
    if (Path(sys.argv[0]).resolve() != ROOT / 'backend/manage.py'
            or sys.argv[1:] != expected):
        raise SessionUnavailable('Fixed catalogue process required')
    safe_path(SERVER)
    fd = os.open(SERVER, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_size > 16384):
            raise SessionUnavailable('Unsafe controller receipt')
        server = json.load(handle, object_pairs_hook=unique_keys)
    expiry = datetime.fromisoformat(server['expires_utc'].replace('Z', '+00:00'))
    if (server['renewal_id'] != RENEWAL or expiry != EXPIRY
            or type(server['pid']) is not int or server['pid'] != os.getpid()):
        raise SessionUnavailable('Controller receipt does not own this process')
    controller_pid = server['controller_pid']
    controller = Path(server['controller_path'])
    if (type(controller_pid) is not int or controller_pid != os.getppid()
            or server['bind'] != '127.0.0.1:8000'
            or server['automatic_renewal'] is not False
            or not controller.is_relative_to(ROOT / '.p1-readiness')):
        raise SessionUnavailable('Current bounded controller required')
    safe_path(controller)
    if not controller.is_file() or controller.stat().st_uid != os.getuid():
        raise SessionUnavailable('Owned controller required')
    proc = Path('/proc') / str(controller_pid)
    if (proc.stat().st_uid != os.getuid() or (proc / 'cwd').resolve() != ROOT
            or (proc / 'cmdline').read_bytes().split(b'\0')[:-1]
            != [b'/usr/bin/python3', b'-I', b'-S', b'-B', os.fsencode(controller)]):
        raise SessionUnavailable('Live fixed controller required')
    return expiry


def prepare_runtime():
    """The service shares this directory and owns its cursor lock."""
    if boundary.ROOT != ROOT:
        raise SessionUnavailable('Fixed deployed runtime required')
    safe_path(RUNTIME)
    safe_path(STATUS)
    for directory in (RUNTIME.parent, RUNTIME):
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise SessionUnavailable('Unsafe runtime directory')


def write_status(value):
    """Publish only bounded operational state, never exception text or content."""
    prepare_runtime()
    if STATUS.exists():
        fd = os.open(STATUS, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            private_file(fd)
        finally:
            os.close(fd)
    fd, name = tempfile.mkstemp(prefix='.status-', dir=RUNTIME)
    try:
        with os.fdopen(fd, 'w') as handle:
            private_file(handle.fileno())
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, STATUS)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run_clock(stop, *, clock=utcnow):
    from django.db import close_old_connections
    from shelf import weekly_synthesis

    while not stop.is_set():
        now = clock()
        if now >= EXPIRY:
            publish_status({'status': 'expired', 'automatic_model_calls': 0})
            return
        result = {'status': 'pending_metadata', 'automatic_model_calls': 0}
        try:
            session_guard()
        except Exception:
            pass  # The controller writes its receipt just after Popen.
        else:
            try:
                prepare_runtime()
                close_old_connections()
                result = weekly_synthesis.tick(now=now, session_guard=session_guard,
                                               ledger_path=LEDGER)
            except Exception:
                # An uncertain service result must be reconciled by its ledger.
                result = {'status': 'unavailable', 'automatic_model_calls': 0}
            finally:
                try:
                    close_old_connections()
                except Exception:
                    result = {'status': 'unavailable', 'automatic_model_calls': 0}
        publish_status(result)
        stop.wait(min(INTERVAL, max(0, (EXPIRY - clock()).total_seconds())))


def publish_status(result):
    try:
        write_status(dict(result, cadence='Monday 08:00 Australia/Melbourne',
                          checked_at=utcnow().isoformat(),
                          first_due_utc='2026-11-01T21:00:00+00:00',
                          expires_utc=EXPIRY.isoformat(), automatic_model_calls=0))
    except Exception:
        # A private status-file problem must not interrupt HTTP service.
        pass


def start_clock():
    if boundary.ROOT != ROOT:
        raise SessionUnavailable('Fixed deployed runtime required')
    stop = threading.Event()
    thread = threading.Thread(target=run_clock, args=(stop,),
                              name='weekly-synthesis-clock', daemon=True)
    thread.start()
    return stop, thread
