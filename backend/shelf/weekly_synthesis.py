"""One bounded weekly review receipt; never reserves or performs inference.

The runtime supplies its current metadata guard and fixed private ledger path.
A durable reservation precedes database creation, so recovery uses exactly the
captured digest even if taste history subsequently changes.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
import re
import uuid
from pathlib import Path
import stat
import tempfile

from django.contrib.auth import get_user_model

from shelf import synthesis
from shelf.acquisition_contract import Blocked
from shelf.calendar_handoff import digest
from shelf.models import SynthesisRun
from shelf.provider_views import _profiles

FIRST_DUE = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
FIXED_EXPIRY = datetime(2026, 11, 6, 4, 17, 29, tzinfo=timezone.utc)
CADENCE = 'Monday 08:00 Australia/Melbourne'
RENEWAL = '20261030-approved-seven-days'
WEEK = timedelta(days=7)
MAX_LEDGER = 2 * synthesis.MAX_JSON


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise Blocked('An aware runtime timestamp is required.')
    return value.astimezone(timezone.utc)


def stamp(value):
    return utc(value).isoformat().replace('+00:00', 'Z')


def expiry(session_guard):
    if not callable(session_guard):
        raise Blocked('Current bounded runtime metadata is required.')
    return min(utc(session_guard()), FIXED_EXPIRY)


def owner():
    actor = get_user_model().objects.filter(pk=1, username='demo-curator', is_active=True).first()
    if actor is None or not _profiles(actor, 'select').filter(pk=1).exists():
        raise Blocked('Existing active owner and current profile grant required.')
    return actor


def private_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise Blocked('A private owned single-link ledger file is required.')


@contextmanager
def locked(path):
    path = Path(path)
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise Blocked('An absolute ledger path without symbolic links is required.')
    info = path.parent.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise Blocked('An existing private owned ledger directory is required.')
    fd = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private_file(fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(fd)


def read_cursor(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        private_file(fd)
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_LEDGER + 1)
        if len(raw) > MAX_LEDGER:
            raise Blocked('Weekly cursor exceeds its bounded size.')
        value = json.loads(raw)
        common = {'version', 'renewal', 'due_utc', 'phase', 'input_digest'}
        if (type(value) is not dict or value.get('version') != 1 or value.get('renewal') != RENEWAL
                or value.get('phase') not in ('reserved', 'completed')
                or type(value.get('input_digest')) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', value['input_digest'])):
            raise ValueError
        due = datetime.fromisoformat(value['due_utc'].replace('Z', '+00:00'))
        if utc(due) < FIRST_DUE or due >= FIXED_EXPIRY or (due - FIRST_DUE) % WEEK:
            raise ValueError
        expected = common | ({'snapshot'} if value['phase'] == 'reserved' else {'run_id', 'run_state', 'reused'})
        if set(value) != expected:
            raise ValueError
        if value['phase'] == 'reserved':
            snapshot = synthesis.bounded(value['snapshot'])
            if (snapshot['actor_id'] != 1 or snapshot['profile_id'] != 1 or snapshot['mode'] != 'household'
                    or snapshot['input_digest'] != value['input_digest']
                    or digest({k: v for k, v in snapshot.items() if k != 'input_digest'}) != value['input_digest']):
                raise ValueError
        elif (value['run_state'] not in ('queued', 'running', 'completed', 'failed', 'unknown')
                or type(value['run_id']) is not str or type(value['reused']) is not bool):
            raise ValueError
        if value['phase'] == 'completed':
            uuid.UUID(value['run_id'])
        return value
    except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
        raise Blocked('Weekly cursor is corrupt; reconcile it before continuing.') from None
    finally:
        os.close(fd)


def write_cursor(path, value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
    if len(raw) > MAX_LEDGER:
        raise Blocked('Weekly cursor exceeds its bounded size.')
    fd, temporary = tempfile.mkstemp(prefix='.weekly-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def result(status, cursor=None, next_due=FIRST_DUE):
    return {'status': status, 'next_due_utc': stamp(next_due),
        'schedule_expired': next_due >= FIXED_EXPIRY,
        'cursor_state': cursor['phase'] if cursor else 'empty',
        'run_id': cursor.get('run_id') if cursor else None,
        'run_state': cursor.get('run_state') if cursor else None,
        'reused': cursor.get('reused', False) if cursor else False,
        'automatic_model_calls': 0}


def tick(*, now, session_guard, ledger_path):
    """Capture at most one due slot under current authority, at zero model calls.

    Guard must check actual runtime metadata and return its aware expiry. Failures
    propagate without replacing an uncertain reservation or retrying inference.
    """
    now = utc(now)
    deadline = expiry(session_guard)
    with locked(ledger_path) as path:
        cursor = read_cursor(path)
        next_due = (datetime.fromisoformat(cursor['due_utc'].replace('Z', '+00:00')) + WEEK
                    if cursor and cursor['phase'] == 'completed' else FIRST_DUE)
        if now >= deadline:
            return result('expired', cursor, next_due)
        if cursor and cursor['phase'] == 'completed' and next_due >= deadline:
            return result('expired', cursor, next_due)
        if now < FIRST_DUE or (cursor and cursor['phase'] == 'completed' and now < next_due):
            return result('not_due', cursor, next_due)
        actor = owner()
        if not cursor or cursor['phase'] == 'completed':
            due = FIRST_DUE + ((now - FIRST_DUE) // WEEK) * WEEK
            if due >= deadline or now >= expiry(session_guard):
                return result('expired', cursor, due)
            current = synthesis.snapshot(actor, 1)
            cursor = {'version': 1, 'renewal': RENEWAL, 'due_utc': stamp(due),
                'phase': 'reserved', 'input_digest': current['input_digest'], 'snapshot': current}
            write_cursor(path, cursor)
        # Recovery deliberately uses the reserved snapshot, never current inputs.
        if now >= expiry(session_guard):
            return result('expired', cursor, next_due)
        actor = owner()
        run, created = SynthesisRun.objects.get_or_create(actor=actor, profile_id=1,
            input_digest=cursor['input_digest'], defaults={'input_snapshot': cursor['snapshot'],
                'state': 'queued', 'evidence': {'scheduled_review': {
                    'due_utc': cursor['due_utc'], 'cadence': CADENCE,
                    'approved': False, 'automatic_model_call_cap': 0}}})
        cursor = {k: v for k, v in cursor.items() if k != 'snapshot'}
        cursor.update(phase='completed', run_id=str(run.pk), run_state=run.state, reused=not created)
        write_cursor(path, cursor)
        next_due = datetime.fromisoformat(cursor['due_utc'].replace('Z', '+00:00')) + WEEK
        review = run.evidence.get('scheduled_review')
        pending = run.state == 'queued' and type(review) is dict and review.get('approved') is not True
        return result('PENDING_REVIEW' if pending else 'reused', cursor, next_due)
