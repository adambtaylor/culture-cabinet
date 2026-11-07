"""Operator-only exact-job observer of the existing local SAB history database.

Uses no API/admin key. This is deliberately absent from HTTP routes: the bounded
Mac worker invokes it over the existing SSH session with a trusted actor/profile.
Only known acknowledged jobs are queried; unrelated history is never exported.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat

from django.db.models import F

from shelf import completion_history, completion_registry as registry, obtain_acquisition as acquire
from shelf.acquisition_contract import Blocked
from shelf.models import ObtainTransfer

HISTORY = Path('/home/tang/.config/sabnzbd/admin/history1.db')


def read_history(nzo, *, database=HISTORY):
    """Read a stable rollback-journal snapshot, never reopen a mutable pathname.

    Installed SAB uses rollback format with no live journal. WAL or a journal in
    progress is unavailable, not a reason to ignore unapplied transactions. The
    no-follow descriptor is copied into bounded memory, checked for concurrent
    changes, then queried in an in-memory SQLite connection. No history copy is
    written to disk and SQLite cannot follow a swapped source pathname.
    """
    registry.nzo_id(nzo)
    database = Path(database)
    parts = registry.path_parts(str(database), absolute=True)
    parent_path, name = '/' + '/'.join(parts[:-1]), parts[-1]
    with registry.directory(parent_path) as parent:
        def no_sidecars():
            for suffix in ('-wal', '-shm', '-journal'):
                try:
                    os.stat(name + suffix, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise ValueError('Active or unsupported history journal')
        no_sidecars()
        fd = os.open(name, registry.READ_FLAGS, dir_fd=parent)
        with os.fdopen(fd, 'rb', buffering=0) as source:
            before = os.fstat(source.fileno())
            registry.require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                             and before.st_nlink == 1 and not before.st_mode & 0o022
                             and 0 < before.st_size <= 64 * 1024**2)
            payload = source.read(before.st_size + 1)
            registry.require(len(payload) == before.st_size
                and registry.signature(before) == registry.signature(os.fstat(source.fileno()))
                and registry.signature(before) == registry.signature(os.stat(
                    name, dir_fd=parent, follow_symlinks=False)))
        no_sidecars()
        with registry.directory(parent_path) as current:
            registry.require(registry.signature(os.fstat(parent)) ==
                             registry.signature(os.fstat(current)))
    registry.require(payload[:16] == b'SQLite format 3\0' and payload[18:20] == b'\1\1')
    with closing(sqlite3.connect(':memory:')) as db:
        db.deserialize(payload)
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT nzo_id, name, status, storage, completed, script, '
                          'fail_message FROM history WHERE nzo_id=? LIMIT 2', (nzo,)).fetchall()
    registry.require(len(rows) <= 1)
    return dict(rows[0]) if rows else None


def observe(*, actor, profile_id, job_id, context, reader=read_history):
    """Pending/failed/unknown never become completed merely because files exist."""
    if type(context) is not acquire.OperatorContext:
        raise Blocked('Trusted completion operator context required')
    context.require(actor, profile_id)
    entry = acquire.completion_registration(actor=actor, profile_id=profile_id,
                                            job_id=job_id, context=context, include_failed=True)
    transfer = ObtainTransfer.objects.get(job_id=job_id)
    binding = {key: entry[key] for key in ('request_id', 'nzo_id', 'result_id',
                                         'result_digest', 'nzb_sha256')}
    if transfer.state == 'failed':
        return {'state': 'failed', 'reason': 'sab_reported_failure', **binding}
    try:
        slot = reader(entry['nzo_id'])
        context.require(actor, profile_id)
        registry.require(acquire.completion_registration(actor=actor, profile_id=profile_id,
                         job_id=job_id, context=context, include_failed=True) == entry)
        if slot is None:
            return {'state': 'pending', 'reason': 'final_history_not_present', **binding}
        registry.require(slot['nzo_id'] == entry['nzo_id']
                         and slot['name'] == transfer.binding['completion_intent']['nzbname'])
        if slot['status'] == 'Failed':
            # Preserve this terminal observation in the same receipt shown by
            # Obtain. CAS prevents a late read overwriting cancellation/handoff.
            changed = ObtainTransfer.objects.filter(pk=transfer.pk,
                revision=transfer.revision, state='queued', provider_job=entry['nzo_id'],
                job__status='blocked_configuration', local_cancelled_at__isnull=True
            ).update(state='failed', reason='provider_failed', revision=F('revision') + 1)
            registry.require(changed == 1)
            return {'state': 'failed', 'reason': 'sab_reported_failure', **binding}
        if slot['status'] != 'Completed':
            return {'state': 'pending', 'reason': 'sab_not_completed', **binding}
        handoff = completion_history.export_completed(actor=actor, profile_id=profile_id,
            job_id=job_id, context=context, history={'history': {'slots': [slot]}})
        from shelf.completion_status import record_completion
        record_completion(actor=actor, profile_id=profile_id, job_id=job_id,
                          context=context, handoff=handoff)
        return {'state': 'completed', **binding, 'handoff': handoff}
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return {'state': 'unknown', 'reason': 'current_completion_unavailable', **binding}
