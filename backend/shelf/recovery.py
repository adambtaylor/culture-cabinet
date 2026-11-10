"""Recovery through a retained witness or one authenticated native CLEAN anchor.

The baseline constructor never opens existing disk. The separate native path
consumes an independent keyring checkpoint before SQL writers reopen. Disk
checksums alone never establish freshness. Lock order is coordinator then SQLite.
The separate trusted_restart_fixture has an explicit synthetic root-trust gate;
it verifies disk consistency, never rollback freshness or host-loss recovery.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone as utc_timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import threading
import uuid
import weakref

from django.db import connection
from django.http import Http404
from shelf.feedback import FeedbackError, Busy, Conflict, canonical, uid, SIGNALS, bounded_preflight

_CURRENT = ContextVar('feedback_recovery', default=None)
_BARRIER_STACK = ContextVar('feedback_barrier_stack', default=())
_NATIVE_FIRST_STARTS = weakref.WeakKeyDictionary()
CODES = {'erase_feedback': 'erase', 'deny_view': 'revoke', 'deny_select': 'revoke',
         'deny_mutate': 'revoke', 'deny_export': 'revoke', 'disable_profile': 'revoke',
         'disable_actor': 'revoke', 'deny_source_content': 'source_restrict'}
RESTRICTION_FIELDS = ('kind', 'profile_id', 'target_id', 'signal', 'scope', 'subject_id', 'restriction_code')


class RecoveryBlocked(FeedbackError):
    status = 503
    def __init__(self, message='Recovery continuity is unavailable.'):
        super().__init__(message)


def installed_coordinator():
    return _CURRENT.get()


@contextmanager
def using_recovery(coordinator):
    if not isinstance(coordinator, CurrentRecoveryCoordinator): raise RecoveryBlocked()
    token = _CURRENT.set(coordinator)
    try: yield coordinator
    finally: _CURRENT.reset(token)


def native_first_start_matches(coordinator, boundary):
    """Identity check only; the enclosing barrier still verifies the witness."""
    if type(coordinator) is not CurrentRecoveryCoordinator:
        return False
    issued = _NATIVE_FIRST_STARTS.get(coordinator)
    return (issued is not None and issued[0] is boundary
            and not coordinator.closed and not coordinator.blocked and boundary.active)


_NATIVE_CLEAN_STARTS = weakref.WeakKeyDictionary()


def native_clean_start_matches(coordinator, boundary):
    # An absent witness is a refusal, not a WeakKeyDictionary lookup of None.
    if type(coordinator) is not CurrentRecoveryCoordinator:
        return False
    issued = _NATIVE_CLEAN_STARTS.get(coordinator)
    return (type(coordinator) is CurrentRecoveryCoordinator and issued is not None
            and issued[0] is boundary and boundary.active
            and not coordinator.closed and not coordinator.blocked)


def _verify_native_first_start(coordinator):
    boundary, raw, paths, fds, identities = _NATIVE_FIRST_STARTS[coordinator]
    _trusted_boundary(boundary)
    for index, path in enumerate(paths):
        if _store_identity(path, fds[index], directory=index != 0, private=index != 2) != identities[index]:
            raise RecoveryBlocked()
    if (coordinator.bound_db is not raw or raw_store(raw) != coordinator.store_id
            or raw.execute('SELECT 1').fetchone() != (1,) or not coordinator.matches_connection(raw)):
        raise RecoveryBlocked()


@contextmanager
def native_first_start(boundary):
    """Retain continuity from exclusive fresh creation; never reopen old history."""
    from cabinet import portable_runtime
    if installed_coordinator() is not None:
        raise RecoveryBlocked()
    root = _trusted_boundary(boundary)
    # The launcher issues this one-use proof only after O_EXCL database creation.
    # Neither an empty restriction table nor a request flag can recreate it.
    root_identity, database_identity = portable_runtime._consume_native_first_start(boundary)
    portable_runtime.validate_existing(boundary)
    paths = (Path(boundary.database_name), root, root.parent)
    fds, raw, coordinator = {}, None, None
    try:
        for index, path in enumerate(paths):
            fds[index] = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if index else 0))
        identities = tuple(_store_identity(path, fds[index], directory=index != 0, private=index != 2)
                           for index, path in enumerate(paths))
        if (identities[1], identities[0]) != (root_identity, database_identity):
            raise RecoveryBlocked()
        raw = sqlite3.connect(paths[0].as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=0.05)
        _closed_sql_guards(raw)
        raw.execute('PRAGMA foreign_keys=ON')
        raw.execute('PRAGMA synchronous=FULL')
        _effective_guards(raw)
        journal = root.parent / ('.native-recovery-' + boundary.installation_id.hex
                                 + '-' + boundary.process_generation.hex)
        coordinator = CurrentRecoveryCoordinator.provision_empty(store_id=raw_store(raw),
            journal_directory=journal, trusted_empty_db=raw)
        _NATIVE_FIRST_STARTS[coordinator] = (boundary, raw, paths, fds, identities)
        coordinator.verify_current()
        # This same ContextVar scope encloses service construction and requests.
        # Close journal/control handles before the launcher closes the BoundRoot.
        with using_recovery(coordinator):
            yield coordinator
    finally:
        if coordinator is not None:
            _NATIVE_FIRST_STARTS.pop(coordinator, None)
            coordinator.close()
        if raw is not None:
            raw.close()
        for fd in fds.values():
            os.close(fd)



def _closed_sql_guards(raw):
    from shelf.models import register_closed_sql_guards
    register_closed_sql_guards(raw)


def raw_store(raw):
    try:
        row = raw.execute('SELECT store_id FROM shelf_recoveryanchor WHERE id=1').fetchone()
        return str(uuid.UUID(row[0]))
    except (sqlite3.Error, ValueError, TypeError): raise RecoveryBlocked() from None


def dbid(value):
    return uuid.UUID(str(value)).hex if value is not None else None


def rows(raw, sql, args=()):
    cursor = raw.execute(sql, args)
    return [dict(zip([d[0] for d in cursor.description], row)) for row in cursor.fetchall()]


def validate_restriction(r):
    if not isinstance(r, dict) or set(r) != set(RESTRICTION_FIELDS): raise RecoveryBlocked()
    code = r['restriction_code']
    if CODES.get(code) != r['kind']: raise RecoveryBlocked()
    if r['profile_id'] is not None and (type(r['profile_id']) is not int or r['profile_id'] <= 0): raise RecoveryBlocked()
    if code == 'erase_feedback':
        if r['profile_id'] is None or (r['signal'] is not None and r['signal'] not in SIGNALS) or r['scope'] not in ('item','session','profile') or r['subject_id'] is not None: raise RecoveryBlocked()
        if (r['scope']=='profile') != (r['target_id'] is None): raise RecoveryBlocked()
        if r['target_id'] is not None: uid(r['target_id'])
        return
    if any(r[k] is not None for k in ('target_id','signal','scope')): raise RecoveryBlocked()
    if code in ('deny_view','deny_select','deny_mutate','deny_export','disable_profile'):
        if r['profile_id'] is None: raise RecoveryBlocked()
        if code == 'disable_profile' and r['subject_id'] is not None: raise RecoveryBlocked()
        if r['subject_id'] is not None and (not isinstance(r['subject_id'],str) or not r['subject_id'].isascii() or not r['subject_id'].isdigit() or len(r['subject_id'])>18): raise RecoveryBlocked()
    elif code == 'disable_actor':
        if r['profile_id'] is not None or not isinstance(r['subject_id'],str) or not r['subject_id'].isascii() or not r['subject_id'].isdigit() or len(r['subject_id'])>18: raise RecoveryBlocked()
    elif code == 'deny_source_content':
        if r['profile_id'] is not None or not isinstance(r['subject_id'],str) or not r['subject_id'] or len(r['subject_id'])>80 or '\0' in r['subject_id']: raise RecoveryBlocked()


def identity(path, fd):
    a = os.fstat(fd)
    b = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(a.st_mode) or a.st_nlink != 1 or b.st_nlink != 1 or stat.S_ISLNK(b.st_mode) or (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
        raise RecoveryBlocked()
    if stat.S_IMODE(a.st_mode) != 0o600: raise RecoveryBlocked()
    return a.st_dev, a.st_ino


def safe_directory(path, *, empty=False):
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or path.resolve() != path.absolute(): raise RecoveryBlocked()
    if not path.is_dir() or (empty and any(path.iterdir())): raise RecoveryBlocked()
    return path


class CurrentRecoveryCoordinator:
    def __init__(self, *args, **kwargs):
        raise RecoveryBlocked('Existing disk cannot establish a current witness.')

    @classmethod
    def provision_empty(cls, *, store_id, journal_directory, trusted_empty_db):
        store_id = uid(store_id)
        raw = trusted_empty_db
        if raw.in_transaction or raw_store(raw) != store_id: raise RecoveryBlocked()
        for table in ('feedbackevent', 'feedbackprojection', 'feedbacksubject', 'mutationreceipt', 'recoveryrestriction'):
            if raw.execute(f'SELECT count(*) FROM shelf_{table}').fetchone()[0]: raise RecoveryBlocked()
        directory = Path(journal_directory).absolute()
        safe_directory(directory.parent)
        if directory.exists() or directory.is_symlink(): raise RecoveryBlocked()
        database_path = raw.execute('PRAGMA database_list').fetchone()[2]
        if database_path and (directory == Path(database_path).parent or directory.is_relative_to(Path(database_path).parent)):
            raise RecoveryBlocked()
        directory.mkdir(mode=0o700)
        obj = object.__new__(cls)
        obj.store_id = store_id; obj.directory = directory; obj.mutex = threading.RLock()
        obj.blocked = False; obj.closed = False; obj.pending = None; obj.audit = None
        obj.frames = []; obj.head_id = None; obj.sequence = 0
        obj.fds = {}; obj.identities = {}; obj.directory_fd = None
        obj.bound_db = raw; obj.owns_control = False
        obj.bound_path = Path(database_path).resolve() if database_path else None
        obj.bound_identity = ((obj.bound_path.stat().st_dev,obj.bound_path.stat().st_ino) if obj.bound_path else None)
        try:
            obj.directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(obj.directory_fd)
            obj.directory_identity = (info.st_dev, info.st_ino)
            for name in ('lock', 'journal'):
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=obj.directory_fd)
                obj.fds[name] = fd; obj.identities[name] = identity(directory / name, fd)
            fcntl.flock(obj.fds['lock'], fcntl.LOCK_EX | fcntl.LOCK_NB)
            obj.length = 0; obj.digest = hashlib.sha256(b'').hexdigest()
            for fd in obj.fds.values(): os.fsync(fd)
            os.fsync(obj.directory_fd)
            parent_fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(parent_fd)
            finally: os.close(parent_fd)
            obj.verify_current()
            return obj
        except BaseException:
            obj.close()
            raise

    def matches_connection(self, raw):
        try:
            if raw_store(raw) != self.store_id: return False
            if self.bound_path is None: return raw is self.bound_db
            path = raw.execute('PRAGMA database_list').fetchone()[2]
            if not path or Path(path).absolute() != self.bound_path: return False
            if not raw.in_transaction:
                info = self.bound_path.stat()
                if (info.st_dev,info.st_ino) != self.bound_identity: self._block()
            return True
        except (OSError,sqlite3.Error): self._block()

    def close(self):
        if self.closed: return
        self.closed = True; self.blocked = True
        for fd in self.fds.values():
            try: os.close(fd)
            except OSError: pass
        if self.directory_fd is not None:
            try: os.close(self.directory_fd)
            except OSError: pass
        if getattr(self,'owns_control',False): self.bound_db.close()
        for fd in getattr(self,'store_fds',{}).values():
            try: os.close(fd)
            except OSError: pass

    def _block(self):
        self.blocked = True
        raise RecoveryBlocked()

    def verify_current(self):
        if self.blocked or self.closed: raise RecoveryBlocked()
        try:
            if self in _NATIVE_FIRST_STARTS:
                _verify_native_first_start(self)
            if self in _NATIVE_CLEAN_STARTS:
                boundary, raw, paths, fds, identities = _NATIVE_CLEAN_STARTS[self]
                _trusted_boundary(boundary)
                for index, path in enumerate(paths):
                    if _store_identity(path, fds[index], directory=index != 0, private=index != 2) != identities[index]:
                        self._block()
                if self.bound_db is not raw or not self.matches_connection(raw):
                    self._block()
            if getattr(self, '_trusted_restart_seal', None) is _TRUSTED_RESTART_SEAL:
                _trusted_boundary(self.trusted_boundary)
                _trusted_journal_binding(self.trusted_boundary, self.bound_db, self.store_fds, self)
            if getattr(self,'owns_control',False):
                _validate_store_identities(self.validated_store, self.store_fds)
                if raw_store(self.bound_db)!=self.store_id or self.bound_db.execute('SELECT 1').fetchone()!=(1,): self._block()
                if self.bound_db.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID: self._block()
            info = self.directory.stat()
            if self.directory.is_symlink() or (info.st_dev,info.st_ino) != self.directory_identity or stat.S_IMODE(info.st_mode) != 0o700: self._block()
            held = os.fstat(self.directory_fd)
            if (held.st_dev,held.st_ino)!=self.directory_identity or info.st_uid!=os.getuid(): self._block()
            for name, fd in self.fds.items():
                if os.fstat(fd).st_uid!=os.getuid(): self._block()
                if identity(self.directory/name, fd) != self.identities[name]: self._block()
            # Retained descriptor and lock ownership are part of the live witness.
            lock_info = Path(f"/proc/self/fdinfo/{self.fds['lock']}").read_text()
            lock_rows = [line.split() for line in lock_info.splitlines() if line.startswith('lock:')]
            if not any(len(row)>=9 and row[2:5]==['FLOCK','ADVISORY','WRITE']
                       and row[5]==str(os.getpid()) and row[6].endswith(':'+str(self.identities['lock'][1])) for row in lock_rows):
                self._block()
            data = os.pread(self.fds['journal'], self.length + 1, 0)
            if len(data) != self.length or hashlib.sha256(data).hexdigest() != self.digest: self._block()
            offset = 0; previous = None; parsed = []
            while offset < len(data):
                if offset+4 > len(data): self._block()
                size = struct.unpack('>I', data[offset:offset+4])[0]; offset += 4
                if size > 16*1024*1024 or offset+size > len(data): self._block()
                frame = json.loads(data[offset:offset+size]); offset += size
                checksum = frame.pop('checksum')
                if hashlib.sha256(canonical(frame)).hexdigest() != checksum or frame['previous'] != previous or frame['sequence'] != len(parsed)+1 or frame['store_id'] != self.store_id: self._block()
                previous = checksum; frame['checksum'] = checksum; parsed.append(frame)
            if parsed != self.frames: self._block()
            return {'head_id': self.head_id, 'restriction_sequence': self.sequence,
                    'journal_bytes': self.length, 'journal_digest': self.digest}
        except (RecoveryBlocked, OSError, sqlite3.Error, ValueError, KeyError, TypeError, struct.error): self._block()

    @contextmanager
    def barrier(self, expected_store_id):
        with self.mutex:
            if str(expected_store_id) != self.store_id or self.blocked or self.closed: raise RecoveryBlocked()
            stack = _BARRIER_STACK.get()
            nested = self in stack
            if not nested: self.verify_current()
            token = _BARRIER_STACK.set(stack + (self,))
            try:
                yield self
                # Nested service authority checks use the verified live checkpoint
                # under this same mutex, with no journal I/O during IMMEDIATE.
                if not nested: self.verify_current()
            finally: _BARRIER_STACK.reset(token)

    def _append(self, phase, operation, body):
        self.verify_current()
        if len(self.frames) >= 100000: raise RecoveryBlocked()
        frame = {'sequence': len(self.frames)+1, 'store_id': self.store_id,
                 'operation_uuid': operation, 'phase': phase,
                 'previous': self.frames[-1]['checksum'] if self.frames else None, **body}
        frame['checksum'] = hashlib.sha256(canonical(frame)).hexdigest()
        encoded = canonical(frame)
        if len(encoded) > 16*1024*1024 or self.length+4+len(encoded)>512*1024*1024: raise RecoveryBlocked()
        data = struct.pack('>I',len(encoded))+encoded
        try:
            os.lseek(self.fds['journal'],0,os.SEEK_END)
            view = memoryview(data)
            while view:
                n = os.write(self.fds['journal'], view)
                if n <= 0: raise OSError
                view = view[n:]
            os.fsync(self.fds['journal'])
        except OSError: self._block()
        self.frames.append(frame); self.length += len(data)
        self.digest = hashlib.sha256(os.pread(self.fds['journal'],self.length,0)).hexdigest()
        return frame

    def prepare(self, restriction_operation_uuid, minimal_restrictions, *, audit=None):
        uid(restriction_operation_uuid)
        if self.pending: raise RecoveryBlocked()
        for r in minimal_restrictions: validate_restriction(r)
        if not minimal_restrictions: raise RecoveryBlocked()
        if any(f['operation_uuid'] == restriction_operation_uuid for f in self.frames): raise Conflict()
        pending = {'restrictions': [dict(r, sequence=self.sequence+i+1) for i,r in enumerate(minimal_restrictions)],
                   'local_commit_uuid': str(uuid.uuid4()), 'head_id': str(uuid.uuid4())}
        self._append('prepare', restriction_operation_uuid, pending)
        self.pending = dict(pending, operation_uuid=restriction_operation_uuid)
        self.audit = audit
        self.audits = getattr(self, "audits", {})
        self.audits[restriction_operation_uuid] = audit
        return self.pending

    def pending_denies(self, profile_id, target_id=None, action=None, actor_id=None):
        if not self.pending: return False
        return any(r['profile_id'] == profile_id or (r['restriction_code']=='disable_actor' and str(actor_id)==r['subject_id']) or r['kind']=='source_restrict' for r in self.pending['restrictions'])

    def committed_restrictions(self):
        committed = {f['operation_uuid'] for f in self.frames if f['phase']=='commit'}
        return [(f,r) for f in self.frames if f['phase']=='prepare' and f['operation_uuid'] in committed for r in f['restrictions']]

    def verify_mirror(self, raw):
        if raw_store(raw) != self.store_id: raise RecoveryBlocked()
        local = rows(raw, 'SELECT * FROM shelf_recoveryrestriction ORDER BY sequence')
        expected = self.committed_restrictions()
        if self.pending:
            # A committed local pending batch is allowed only for trusted resume;
            # ordinary profile access is denied by pending_denies first.
            expected += [(self.pending,r) for r in self.pending['restrictions'] if any(x['sequence']==r['sequence'] for x in local)]
        if len(local) != len(expected): raise RecoveryBlocked()
        for entry,(frame,r) in zip(local,expected):
            if entry != mirror_values(frame,r): raise RecoveryBlocked()

    def assert_import_source_policy(self, *, package, binding):
        # Called only under the coordinator mutex, after fresh action authority,
        # and again inside each write attempt (including owned erase prepares).
        self.verify_mirror(connection.connection)
        if self.pending and any(r['kind']=='source_restrict' for r in self.pending['restrictions']):
            raise FeedbackError()
        banned={r['subject_id'] for _,r in self.committed_restrictions()
                if r['restriction_code']=='deny_source_content'}
        targets={t['id']:t for t in package['targets']}
        approvals={e['id']:e['sha256'] for e in binding.independent_record_digests}
        for target in targets.values():
            if target['source_namespace'] in banned:
                _fictional_target(target)
        for record in package['records']:
            denied=targets[record['target_id']]['source_namespace'] in banned
            if record.get('source_redacted',False):
                if not denied: raise FeedbackError()
                continue  # Strict structural shape was already validated.
            if not denied or record['erased']: continue
            if record['original_origin']!='direct': raise FeedbackError()
            if any(record[k] is not None for k in ('source_record_id','source_time','source_metadata')):
                raise FeedbackError()
            statement={k:v for k,v in record.items() if k!='source_redacted'}
            if approvals.get(record['id'])!=hashlib.sha256(canonical(statement)).hexdigest():
                raise FeedbackError()

    def assert_scope_open(self, profile, target, signal, scope):
        for _,r in self.committed_restrictions():
            if r['kind']=='erase' and r['profile_id']==profile and (r['target_id'] is None or (r['target_id']==str(target) and r['scope']==scope)) and (r['signal'] is None or r['signal']==signal):
                from shelf.feedback import Gone
                raise Gone('This feedback scope is erased.')

    def validate_pending_authority(self, raw):
        if not self.audit: return
        actor, grant = self.audit
        for r in self.pending['restrictions']:
            if r['profile_id'] is not None and not raw.execute(
                'SELECT 1 FROM shelf_grant g JOIN shelf_profile p ON p.id=g.profile_id '
                'JOIN auth_user u ON u.id=g.user_id WHERE g.id=? AND g.user_id=? AND g.profile_id=? '
                'AND g.can_view=1 AND g.can_mutate=1 AND p.active=1 AND u.is_active=1',
                (grant,actor,r['profile_id'])).fetchone(): raise RecoveryBlocked()

    def apply_pending(self, raw, *, require_current_authority=False, operator=None):
        if not self.pending: raise RecoveryBlocked()
        if raw.in_transaction: raise RecoveryBlocked()
        if raw_store(raw)!=self.store_id: raise RecoveryBlocked()
        timeout = raw.execute('PRAGMA busy_timeout').fetchone()[0]
        raw.execute('PRAGMA busy_timeout=50')
        try:
            for attempt in range(3):
                try:
                    raw.execute('BEGIN IMMEDIATE')
                    if require_current_authority: self.validate_pending_authority(raw)
                    if operator is not None: self.validate_operator(raw,operator)
                    raw.create_function('cabinet_disclosure_write', 0, lambda: 1)
                    for r in self.pending['restrictions']:
                        apply_restriction(raw, self.pending, r, audit=self.audit)
                    if require_current_authority: self.validate_pending_authority(raw)
                    if operator is not None: self.validate_operator(raw,operator)
                    raw.execute('COMMIT')
                    break
                except sqlite3.OperationalError as error:
                    if raw.in_transaction: raw.execute('ROLLBACK')
                    if getattr(error,'sqlite_errorcode',0)&255 not in (5,6): raise
                    if attempt==2: raise Busy()
                except BaseException:
                    if raw.in_transaction: raw.execute('ROLLBACK')
                    raise
        finally:
            raw.create_function('cabinet_disclosure_write', 0, lambda: 0)
            raw.execute(f'PRAGMA busy_timeout={timeout}')

    def validate_operator(self, raw, operator):
        if operator is not getattr(self,'operator_capability',None) or not raw.execute(
            'SELECT 1 FROM auth_user WHERE id=? AND is_active=1',(operator.actor_id,)).fetchone():
            raise Http404

    def finalize(self, pending, local_commit_uuid):
        if pending is not self.pending or local_commit_uuid != pending['local_commit_uuid']: raise RecoveryBlocked()
        self.verify_current()
        self.verify_mirror(self.bound_db)
        for r in pending['restrictions']:
            if not self.bound_db.execute('SELECT 1 FROM shelf_recoveryrestriction WHERE sequence=?',(r['sequence'],)).fetchone(): raise RecoveryBlocked()
        self._append('commit',pending['operation_uuid'], {'prepare_sequence': len(self.frames),
            'local_commit_uuid': local_commit_uuid, 'head_id': pending['head_id']})
        self.head_id = pending['head_id']; self.sequence = pending['restrictions'][-1]['sequence']
        self.pending = None; self.audit = None
        return self.verify_current()

    def resume_pending(self, local_connection):
        with self.barrier(self.store_id):
            if local_connection is not self.bound_db: raise RecoveryBlocked()
            if not self.pending: return self.verify_current()
            self.apply_pending(local_connection)
            return self.finalize(self.pending,self.pending['local_commit_uuid'])


def mirror_values(frame,r):
    return {'sequence':r['sequence'], **{k: (dbid(r[k]) if k=='target_id' else r[k]) for k in RESTRICTION_FIELDS},
            'operation_uuid':dbid(frame['operation_uuid']), 'local_commit_uuid':dbid(frame['local_commit_uuid']),
            'external_head_id':dbid(frame['head_id'])}


def apply_restriction(raw, frame, r, *, audit=None):
    validate_restriction({k:r[k] for k in RESTRICTION_FIELDS})
    existing = rows(raw,'SELECT * FROM shelf_recoveryrestriction WHERE sequence=?',(r['sequence'],))
    values = mirror_values(frame,r)
    if existing:
        if existing != [values]: raise RecoveryBlocked()
        return
    code = r['restriction_code']; profile = r['profile_id']
    if code == 'erase_feedback':
        args = (profile,dbid(r['target_id']),r['signal'],r['scope'])
        key = 'profile_id=? AND target_id=? AND signal=? AND scope=?'
        events = rows(raw, f'SELECT * FROM shelf_feedbackevent WHERE {key} ORDER BY revision',args)
        if events:
            raw.execute(f'UPDATE shelf_feedbackevent SET body=NULL,erased=1,attribution=NULL,original_recorded_at=NULL,source_time=NULL,source_record_id=NULL,source_metadata=NULL WHERE {key}',args)
            last=events[-1]
            if last['operation']!='erase':
                if not audit: raise RecoveryBlocked('Missing live audit context.')
                actor,grant=audit
                if not raw.execute('SELECT 1 FROM auth_user WHERE id=?',(actor,)).fetchone(): raise RecoveryBlocked()
                from datetime import datetime, timezone
                fields={'id':uuid.uuid4().hex, 'profile_id':profile,'target_id':args[1], 'signal':r['signal'], 'scope':r['scope'],
                        'revision':last['revision']+1,'operation':'erase','actor_id':actor,'authority_grant_id':grant,
                        'recorded_at':datetime.now(timezone.utc).isoformat(),'origin':'direct','original_origin':'direct','erased':1}
                raw.execute(f"INSERT INTO shelf_feedbackevent ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",tuple(fields.values()))
                revision=last['revision']+1
            else: revision=last['revision']
            raw.execute(f'UPDATE shelf_feedbackprojection SET value=NULL,effective_event_id=NULL,erased=1,revision=? WHERE {key}',(revision,*args))
        raw.execute("UPDATE shelf_mutationreceipt SET state='retired',payload_sha256=NULL WHERE profile_id=?",(profile,))
        if audit:
            from datetime import datetime, timezone
            raw.execute("INSERT OR IGNORE INTO shelf_mutationreceipt (actor_id,mutation_id,profile_id,operation_family,state,created_at) VALUES (?,?,?,'feedback.erase','retired',?)",
                        (audit[0],dbid(frame['operation_uuid']),profile,datetime.now(timezone.utc).isoformat()))
    elif code.startswith('deny_') and code != 'deny_source_content':
        field='can_'+code[5:]
        raw.execute(f'UPDATE shelf_grant SET {field}=0 WHERE profile_id=?'+(' AND user_id=?' if r['subject_id'] else ''),
                    (profile,int(r['subject_id'])) if r['subject_id'] else (profile,))
    elif code == 'disable_profile': raw.execute('UPDATE shelf_profile SET active=0 WHERE id=?',(profile,))
    elif code == 'disable_actor': raw.execute('UPDATE auth_user SET is_active=0 WHERE id=?',(int(r['subject_id']),))
    elif code == 'deny_source_content':
        targets = [x[0] for x in raw.execute('SELECT id FROM shelf_feedbacktarget WHERE source_namespace=?',(r['subject_id'],))]
        if not targets: raise RecoveryBlocked('Unknown source mapping.')
        for target in targets:
            affected=rows(raw,"SELECT DISTINCT profile_id,signal,scope FROM shelf_feedbackevent WHERE target_id=? AND original_origin!='direct'",(target,))
            raw.execute("UPDATE shelf_feedbackevent SET body=NULL,source_record_id=NULL,source_metadata=NULL,source_time=NULL,attribution=NULL,original_recorded_at=NULL WHERE target_id=? AND original_origin!='direct'",(target,))
            for k in affected: raw_recompute(raw,k['profile_id'],target,k['signal'],k['scope'])
    else: raise RecoveryBlocked()
    raw.execute(f"INSERT INTO shelf_recoveryrestriction ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",tuple(values.values()))


def raw_recompute(raw,profile,target,signal,scope):
    args=(profile,target,signal,scope)
    events=rows(raw,'SELECT * FROM shelf_feedbackevent WHERE profile_id=? AND target_id=? AND signal=? AND scope=? ORDER BY revision',args)
    reversed_ids={e['reverses_id'] for e in events if e['operation']=='reverse'}
    erased=any(e['erased'] or e['operation']=='erase' for e in events)
    effective=next((e for e in reversed(events) if e['operation']=='set' and e['id'] not in reversed_ids),None) if not erased else None
    raw.execute('UPDATE shelf_feedbackprojection SET revision=?,effective_event_id=?,value=?,erased=? WHERE profile_id=? AND target_id=? AND signal=? AND scope=?',
                (events[-1]['revision'],effective['id'] if effective else None,effective['body'] if effective else None,int(erased),*args))


@contextmanager
def access_barrier():
    current = installed_coordinator()
    if current:
        with current.barrier(current.store_id) as barrier:
            # Verify retained storage before Django can open/create a request DB.
            connection.ensure_connection()
            if not current.matches_connection(connection.connection): raise RecoveryBlocked()
            yield barrier
        return
    # This narrow baseline exists only for the unchanged verification tests.
    from cabinet import boundary
    from shelf.models import FeedbackEvent, FeedbackSubject, MutationReceipt, RecoveryRestriction
    if boundary._demo or boundary._secret is None or 'memory' not in str(connection.settings_dict['NAME']): raise RecoveryBlocked()
    if any(model.objects.exists() for model in (FeedbackEvent,FeedbackSubject,MutationReceipt,RecoveryRestriction)): raise RecoveryBlocked()
    yield _EmptyVerificationBarrier()


class _EmptyVerificationBarrier:
    pending_denies = lambda *args, **kwargs: False
    def verify_mirror(self, raw): pass
    def assert_scope_open(self,*args): raise RecoveryBlocked('Install the trusted synthetic coordinator before feedback writes.')


# An unforgeable in-process capability is issued only to the trusted harness.
# Neither a superuser nor any request/import field confers this capability.
@dataclass(frozen=True)
class TrustedOperator:
    coordinator: object
    actor_id: int
    nonce: object


def trusted_operator(coordinator, actor):
    if coordinator is not installed_coordinator(): raise RecoveryBlocked()
    from django.contrib.auth import get_user_model
    if not get_user_model().objects.filter(pk=actor.pk,is_active=True).exists(): raise Http404
    cap=TrustedOperator(coordinator,actor.pk,object())
    coordinator.operator_capability=cap
    return cap


@bounded_preflight
def restrict_authority(*, actor, command, recovery):
    """Trusted internal denial only. No grants, additions or HTTP authority editor."""
    if recovery is not installed_coordinator() or actor is not getattr(recovery,'operator_capability',None): raise Http404
    if set(command)!= {'mutation_id','base_sequence','restrictions'}: raise FeedbackError()
    uid(command['mutation_id'])
    if type(command['base_sequence']) is not int or command['base_sequence']<0 or not isinstance(command['restrictions'],list) or not command['restrictions']: raise FeedbackError()
    if len({canonical(r) for r in command['restrictions']}) != len(command['restrictions']): raise FeedbackError()
    for r in command['restrictions']:
        validate_restriction(r)
        if r['restriction_code']=='erase_feedback': raise FeedbackError()
        if r['restriction_code']=='disable_actor' and r['subject_id']==str(actor.actor_id): raise FeedbackError()
    with recovery.barrier(recovery.store_id) as barrier:
        from django.contrib.auth import get_user_model
        if not get_user_model().objects.filter(pk=actor.actor_id,is_active=True).exists(): raise Http404
        previous=[f for f in recovery.frames if f['operation_uuid']==command['mutation_id'] and f['phase']=='prepare']
        if previous:
            old=[{k:r[k] for k in RESTRICTION_FIELDS} for r in previous[0]['restrictions']]
            if old != command['restrictions']: raise Conflict()
            if recovery.pending: raise RecoveryBlocked()
            return barrier.verify_current()
        if command['base_sequence'] != recovery.sequence: raise Conflict()
        with __import__('shelf.feedback',fromlist=['immediate']).immediate():
            barrier.verify_mirror(connection.connection)
            for r in command['restrictions']:
                if r['profile_id'] is not None and not connection.connection.execute('SELECT 1 FROM shelf_profile WHERE id=?',(r['profile_id'],)).fetchone(): raise FeedbackError()
                if r['subject_id'] is not None and r['kind']=='revoke' and not get_user_model().objects.filter(pk=int(r['subject_id'])).exists(): raise FeedbackError()
                if r['kind']=='source_restrict' and not connection.connection.execute('SELECT 1 FROM shelf_feedbacktarget WHERE source_namespace=?',(r['subject_id'],)).fetchone(): raise FeedbackError()
            if not get_user_model().objects.filter(pk=actor.actor_id,is_active=True).exists(): raise Http404
        pending=barrier.prepare(command['mutation_id'],command['restrictions'])
        barrier.apply_pending(connection.connection,operator=actor)
        return barrier.finalize(pending,pending['local_commit_uuid'])


@dataclass(frozen=True)
class ExclusiveScratchFile:
    path: Path


@dataclass(frozen=True)
class BackupManifest:
    version: int
    backup_id: str
    store_id: str
    schema_version: str
    database_sha256: str
    database_length: int
    snapshot_sequence: int
    snapshot_head_id: str | None
    counts: dict
    semantics_version: int = 1


@dataclass(frozen=True)
class ReadOnlyBackup:
    path: Path
    manifest: BackupManifest | None


@dataclass(frozen=True)
class RestoreReport:
    state: str
    backup_id: str | None
    snapshot_head: str | None
    current_head: str | None
    applied_restriction_ids: tuple
    checks: tuple
    block_reasons: tuple
    isolated_database: str | None = None


def semantic_check(raw):
    """Integrity is necessary but cannot detect an incomplete main-file WAL copy.

    Independently reduce event rows and compare every projection. Check known
    migration, constraints, full references and statement/provenance structure.
    """
    from shelf.feedback import validate_body, ORIGINS
    from shelf.models import (Profile,Grant,FeedbackTarget,FeedbackSubject,FeedbackEvent,
        FeedbackProjection,MutationReceipt,RecoveryAnchor,RecoveryRestriction)
    for model in (Profile,Grant,FeedbackTarget,FeedbackSubject,FeedbackEvent,FeedbackProjection,MutationReceipt,RecoveryAnchor,RecoveryRestriction):
        columns={r[1] for r in raw.execute('PRAGMA table_info('+model._meta.db_table+')')}
        if columns != {f.column for f in model._meta.local_fields}: raise RecoveryBlocked('Unknown schema columns.')
    if raw.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or raw.execute('PRAGMA foreign_key_check').fetchall(): raise RecoveryBlocked('Database integrity failed.')
    from .portable_obtain_origin import validate_obtain_history
    try:
        validate_obtain_history(raw)
    except (ValueError, TypeError, KeyError):
        raise RecoveryBlocked('Obtain origin semantics failed.') from None
    if not raw.execute("SELECT 1 FROM django_migrations WHERE app='shelf' AND name='0002_feedback'").fetchone(): raise RecoveryBlocked('Unknown schema.')
    events=rows(raw,'SELECT * FROM shelf_feedbackevent ORDER BY profile_id,target_id,signal,scope,revision')
    targets={r['id']:r for r in rows(raw,'SELECT * FROM shelf_feedbacktarget')}
    groups={}; ids={e['id']:e for e in events}
    subjects={r['profile_id']:str(uuid.UUID(r['subject_id'])) for r in rows(raw,'SELECT * FROM shelf_feedbacksubject')}
    def sql_utc(value):
        if value is None: return None
        parsed=datetime.fromisoformat(value) if isinstance(value,str) else value
        if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=utc_timezone.utc)
        return parsed.isoformat()
    if len(ids)!=len(events): raise RecoveryBlocked()
    for target in targets.values():
        if target['kind']=='item':
            if target['parent_id'] is not None: raise RecoveryBlocked()
        elif target['kind']=='session':
            if target['parent_id'] not in targets or targets[target['parent_id']]['kind']!='item': raise RecoveryBlocked()
        else: raise RecoveryBlocked()
    for e in events:
        if e['target_id'] not in targets or e['scope']!=targets[e['target_id']]['kind'] or e['signal'] not in SIGNALS or e['operation'] not in ('set','reverse','erase') or e['original_origin'] not in ORIGINS: raise RecoveryBlocked()
        key=(e['profile_id'],e['target_id'],e['signal'],e['scope']); group=groups.setdefault(key,[])
        if e['revision']!=len(group)+1: raise RecoveryBlocked()
        if e['profile_id'] not in subjects or e['origin'] not in ORIGINS: raise RecoveryBlocked()
        from shelf.portability import validate_provenance
        provenance={k:(json.loads(e[k]) if e[k] is not None and k in ('attribution','source_metadata') else sql_utc(e[k]) if k in ('original_recorded_at','source_time') else e[k]) for k in ('original_origin','attribution','original_recorded_at','source_time','source_metadata','source_record_id')}
        provenance.update(erased=bool(e['erased']),value=json.loads(e['body']) if e['body'] is not None else None)
        validate_provenance(provenance,subjects[e['profile_id']])
        if (e['operation']=='reverse') != (e['reverses_id'] is not None) or (e['operation']!='set' and e['body'] is not None): raise RecoveryBlocked()
        if e['body'] is not None:
            validate_body(e['signal'],json.loads(e['body']),imported=True)
        if e['erased'] and any(e[k] is not None for k in ('body','source_record_id','source_metadata','source_time','attribution','original_recorded_at')): raise RecoveryBlocked()
        for field in ('reverses_id','corrects_id'):
            if e[field]:
                ref=ids.get(e[field])
                if not ref or ref['operation']!='set' or tuple(ref[k] for k in ('profile_id','target_id','signal','scope'))!=key or ref['revision']>=e['revision']: raise RecoveryBlocked()
        group.append(e)
    projections=rows(raw,'SELECT * FROM shelf_feedbackprojection')
    if len(projections)!=len(groups): raise RecoveryBlocked('Projection count mismatch.')
    for p in projections:
        key=tuple(p[k] for k in ('profile_id','target_id','signal','scope'))
        if key not in groups: raise RecoveryBlocked()
        g=groups[key]; reversed_ids={e['reverses_id'] for e in g if e['operation']=='reverse'}
        erased=any(e['erased'] or e['operation']=='erase' for e in g)
        effective=next((e for e in reversed(g) if e['operation']=='set' and e['id'] not in reversed_ids),None) if not erased else None
        if (p['revision'],p['effective_event_id'],p['erased'])!=(g[-1]['revision'],effective['id'] if effective else None,int(erased)): raise RecoveryBlocked('Projection reducer mismatch.')
        if (json.loads(p['value']) if p['value'] is not None else None)!=(json.loads(effective['body']) if effective and effective['body'] is not None else None): raise RecoveryBlocked('Projection value mismatch.')
    for receipt in rows(raw,'SELECT * FROM shelf_mutationreceipt'):
        if receipt['state'] not in ('accepted','retired') or receipt['operation_family'] not in ('feedback.set','feedback.reverse','feedback.erase','import.portable','import.legacy'): raise RecoveryBlocked()
        if receipt['state']=='retired':
            if receipt['payload_sha256'] is not None: raise RecoveryBlocked()
        elif not isinstance(receipt['payload_sha256'],str) or len(receipt['payload_sha256'])!=64: raise RecoveryBlocked()
        if receipt['event_id'] is not None:
            event=ids.get(receipt['event_id'])
            if not event or event['profile_id']!=receipt['profile_id'] or event['revision']!=receipt['accepted_revision'] or event['target_id']!=receipt['target_id']: raise RecoveryBlocked()
    counts={t:raw.execute(f'SELECT count(*) FROM shelf_{t}').fetchone()[0] for t in ('feedbacktarget','feedbackevent','feedbackprojection','mutationreceipt','recoveryrestriction')}
    counts['signals']={r['signal']:r['n'] for r in rows(raw,'SELECT signal,count(*) n FROM shelf_feedbackevent GROUP BY signal')}
    counts['rating_basis']={}
    for e in events:
        if e['signal'] in ('enjoyment','legacy_rating') and e['body'] is not None:
            basis=json.loads(e['body'])['basis']; counts['rating_basis'][basis]=counts['rating_basis'].get(basis,0)+1
    return counts


def exclusive_file(path):
    path=Path(path).absolute(); safe_directory(path.parent)
    if stat.S_IMODE(path.parent.stat().st_mode)!=0o700: raise RecoveryBlocked('Scratch directory must be private.')
    try: return path,os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except OSError: raise RecoveryBlocked('Destination must be exclusive.') from None


def consistent_backup(*, source, destination, recovery, progress=None):
    if recovery is not installed_coordinator() or not isinstance(destination,ExclusiveScratchFile): raise RecoveryBlocked()
    if source is recovery.bound_db or source.in_transaction or not source.execute('PRAGMA query_only').fetchone()[0]: raise RecoveryBlocked('Dedicated read-only connection required.')
    destination_parent=Path(destination.path).absolute().parent
    if destination_parent==recovery.directory or destination_parent.is_relative_to(recovery.directory): raise RecoveryBlocked('Journal isolation required.')
    path=None; dst=None; success=False
    try:
        with recovery.barrier(recovery.store_id):
            if recovery.pending: raise RecoveryBlocked()
            if raw_store(source)!=recovery.store_id: raise RecoveryBlocked()
            source.execute('BEGIN')
            source.execute('SELECT count(*) FROM shelf_feedbackevent').fetchone()  # pin snapshot
            recovery.verify_mirror(source)
            checkpoint=recovery.verify_current()
        path,fd=exclusive_file(destination.path); expected_identity=identity(path,fd); os.close(fd)
        dst=sqlite3.connect(path); _closed_sql_guards(dst)
        source.backup(dst,pages=16,progress=progress,sleep=0.01)
        counts=semantic_check(dst)
        if raw_store(dst)!=recovery.store_id: raise RecoveryBlocked()
        dst.close(); dst=None
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            if identity(path,fd)!=expected_identity: raise RecoveryBlocked()
            os.fsync(fd)
            with os.fdopen(os.dup(fd),'rb') as handle: data=handle.read()
        finally: os.close(fd)
        manifest=BackupManifest(1,str(uuid.uuid4()),recovery.store_id,'shelf.0002_feedback',
            hashlib.sha256(data).hexdigest(),len(data),checkpoint['restriction_sequence'],checkpoint['head_id'],counts)
        # Service-issued manifests are admitted only by the still-live witness.
        recovery.backups=getattr(recovery,'backups',{})
        recovery.backups[manifest.backup_id]=manifest
        success=True
        return manifest
    except sqlite3.Error: raise RecoveryBlocked('Backup failed.') from None
    finally:
        if source.in_transaction: source.execute('ROLLBACK')
        if dst: dst.close()
        if path and not success: path.unlink(missing_ok=True)


def isolated_restore(*, backup, sandbox, recovery):
    applied=[]; checks=[]; path=None; raw=None
    manifest=backup.manifest if isinstance(backup,ReadOnlyBackup) else None
    def blocked(reason):
        return RestoreReport('BLOCKED',manifest.backup_id if manifest else None,
            manifest.snapshot_head_id if manifest else None,recovery.head_id,tuple(applied),tuple(checks),(reason,),str(path) if path else None)
    try:
        if recovery is not installed_coordinator(): return blocked('MISSING_CURRENT_WITNESS')
        with recovery.barrier(recovery.store_id):
            if recovery.pending: return blocked('PENDING_RECONCILIATION')
            if not manifest: return blocked('UNSAFE_MAIN_FILE_COPY')
            if getattr(recovery,'backups',{}).get(manifest.backup_id)!=manifest: return blocked('UNVERIFIED_MANIFEST')
            if manifest.store_id!=recovery.store_id: return blocked('WRONG_STORE')
            source=Path(backup.path).absolute()
            safe_directory(source.parent)
            fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                identity(source,fd)
                with os.fdopen(os.dup(fd),'rb') as f: data=f.read()
            finally: os.close(fd)
            if len(data)!=manifest.database_length or hashlib.sha256(data).hexdigest()!=manifest.database_sha256: return blocked('BACKUP_HASH_OR_LENGTH')
            directory=safe_directory(sandbox,empty=True)
            if directory==recovery.directory or directory.is_relative_to(recovery.directory): return blocked('JOURNAL_ISOLATION')
            if stat.S_IMODE(directory.stat().st_mode)!=0o700: return blocked('PRIVATE_SCRATCH_REQUIRED')
            path,fd=exclusive_file(directory/'isolated.sqlite3')
            with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
            raw=sqlite3.connect(path); _closed_sql_guards(raw); raw.execute('PRAGMA foreign_keys=ON')
            if raw_store(raw)!=recovery.store_id or semantic_check(raw)!=manifest.counts: return blocked('SCHEMA_OR_SEMANTICS')
            checks.extend(('integrity','foreign_keys','schema','snapshot_reducer','snapshot_counts'))
            local=rows(raw,'SELECT * FROM shelf_recoveryrestriction ORDER BY sequence')
            prefix=[(f,r) for f,r in recovery.committed_restrictions() if r['sequence']<=manifest.snapshot_sequence]
            if local!=[mirror_values(f,r) for f,r in prefix] or len(local)!=manifest.snapshot_sequence: return blocked('INVALID_RESTRICTION_PREFIX')
            if prefix and prefix[-1][0]['head_id']!=manifest.snapshot_head_id: return blocked('INVALID_SNAPSHOT_HEAD')
            raw.execute('BEGIN IMMEDIATE')
            for frame,r in recovery.committed_restrictions():
                if r['sequence']>manifest.snapshot_sequence:
                    apply_restriction(raw,frame,r,audit=getattr(recovery,'audits',{}).get(frame['operation_uuid']))
                    applied.append(r['sequence'])
            # A backup contains auth rows but restored rows cannot authorize access.
            raw.execute('DELETE FROM django_session')
            raw.execute("UPDATE auth_user SET is_active=0,password='!isolated-restore'")
            raw.execute('UPDATE shelf_grant SET can_view=0,can_select=0,can_mutate=0,can_export=0')
            raw.commit()
            semantic_check(raw); recovery.verify_mirror(raw); recovery.verify_current()
            checks.extend(('current_restrictions','reconciled_reducer','no_sessions','neutralized_authority','current_witness'))
            return RestoreReport('ISOLATED_READY',manifest.backup_id,manifest.snapshot_head_id,recovery.head_id,
                tuple(applied),tuple(checks),(),str(path))
    except (FeedbackError,OSError,sqlite3.Error,ValueError,KeyError): return blocked('UNVERIFIABLE_OR_INVALID')
    finally:
        if raw: raw.close()


# This header tag prevents reuse of even an empty first session. It is never a
# substitute for the uninterrupted coordinator or proof of current restrictions.
APPLICATION_ID = 0x43434631
_STORE_SEAL = object()
_STORE_DESCRIPTORS = {}


@dataclass(frozen=True, eq=False)
class ValidatedSyntheticStore:
    mode: str
    database_path: Path
    journal_directory: Path
    store_id: str
    identities: tuple
    _seal: object


def _fictional_target(target):
    """Source metadata is constrained by independently specified fiction."""
    from shelf.catalogue import CARDS, TARGET_IDS
    from shelf.portability import NAMESPACE
    if target['source_namespace']=='fiction-p1101':
        keys=('paper-observatory','mosslight-station','clockwork-lagoon')
        manifest={identifier:dict(id=identifier,kind='item',parent_id=None,
            title=card.title,category=card.category,source_namespace='fiction-p1101',source_key=key)
            for identifier,card,key in zip(TARGET_IDS,CARDS,keys)}
        if target!=manifest.get(target['id']): raise FeedbackError()
    elif target['source_namespace']=='legacy-fixture-v1':
        import re
        match=re.fullmatch(r'(film|book):([1-9][0-9]*)',target['source_key'])
        if not match: raise FeedbackError()
        media,identifier=match[1],int(match[2])
        expected=str(uuid.uuid5(NAMESPACE,canonical(['legacy-fixture-v1',media,identifier]).decode()))
        if target!=dict(id=expected,kind='item',parent_id=None,title=f'Fictional {media} {identifier}',
                category=f'Fictional {media}',source_namespace='legacy-fixture-v1',source_key=f'{media}:{identifier}'):
            raise FeedbackError()
    else: raise FeedbackError()



def _effective_guards(raw):
    from importlib import import_module
    import re
    current = import_module('shelf.migrations.0028_guided_owner_journey')
    try:
        definitions = current.effective_inventory(raw)
    except RuntimeError:
        raise RecoveryBlocked() from None
    normalize = lambda sql: sql.strip().removesuffix(';').rstrip()
    expected = set()
    for sql in definitions:
        match = re.match(r'CREATE TRIGGER (\w+)\s+(?:BEFORE|AFTER) (?:INSERT|UPDATE|DELETE) ON (\w+)\b', sql)
        if match is None:
            raise RecoveryBlocked()
        expected.add((match[1], match[2], normalize(sql)))
    actual = {(name, table, normalize(sql)) for name, table, sql in
              raw.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'")}
    if (len(expected) != len(definitions) or len({row[0] for row in expected}) != len(expected)
            or actual != expected or raw.execute("SELECT 1 FROM sqlite_master WHERE type='view'").fetchone()):
        raise RecoveryBlocked()


def _empty_synthetic_store(raw):
    from django.apps import apps
    known={m._meta.db_table for m in apps.get_models()}
    # Auto-created many-to-many tables are absent from get_models().
    for model in apps.get_models():
        known.update(f.remote_field.through._meta.db_table for f in model._meta.local_many_to_many)
    actual={r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if actual - (known | {'django_migrations','sqlite_sequence'}): raise RecoveryBlocked()
    _effective_guards(raw)
    for table in ('portabledisclosuregrant', 'portabledisclosurepreview', 'profilecabinetstate', 'portablecabinetmanifest'):
        if raw.execute(f'SELECT count(*) FROM shelf_{table}').fetchone()[0]: raise RecoveryBlocked()
    if raw.execute("SELECT 1 FROM shelf_synthesisrun WHERE contract_version='enhance/v1'").fetchone(): raise RecoveryBlocked()
    models=list(apps.get_models())
    models += [f.remote_field.through for m in apps.get_models() for f in m._meta.local_many_to_many]
    for model in models:
        columns={r[1] for r in raw.execute('PRAGMA table_info('+model._meta.db_table+')')}
        if columns!={f.column for f in model._meta.local_fields}: raise RecoveryBlocked()
    semantic_check(raw)
    for table in ('feedbackevent','feedbackprojection','feedbacksubject','mutationreceipt','recoveryrestriction'):
        if raw.execute(f'SELECT count(*) FROM shelf_{table}').fetchone()[0]: raise RecoveryBlocked()
    if raw.execute('PRAGMA application_id').fetchone()[0]!=0: raise RecoveryBlocked()
    for target in rows(raw,'SELECT * FROM shelf_feedbacktarget'):
        if target['deleted']: raise RecoveryBlocked()
        portable={k:target[k] for k in ('kind','title','category','source_namespace','source_key')}
        portable.update(id=str(uuid.UUID(target['id'])),parent_id=str(uuid.UUID(target['parent_id'])) if target['parent_id'] else None)
        try: _fictional_target(portable)
        except FeedbackError: raise RecoveryBlocked() from None


def _store_paths(store):
    return (store.database_path, store.database_path.parent, store.journal_directory.parent)


def _store_identity(path, fd, *, directory, private=True):
    info=os.fstat(fd); current=os.stat(path,follow_symlinks=False)
    if (info.st_dev,info.st_ino)!=(current.st_dev,current.st_ino) or info.st_uid!=os.getuid(): raise RecoveryBlocked()
    if directory:
        safe_directory(path)
        if not stat.S_ISDIR(info.st_mode) or (stat.S_IMODE(info.st_mode)!=0o700 if private else info.st_mode & 0o022): raise RecoveryBlocked()
        return info.st_dev,info.st_ino
    return identity(path,fd)


def _validate_store_identities(store, fds):
    for index,path in enumerate(_store_paths(store)):
        if _store_identity(path,fds[index],directory=index!=0,private=store.mode=='fixture' or index!=2)!=store.identities[index]: raise RecoveryBlocked()


def _store_handles(store):
    fds={}
    try:
        for index,path in enumerate(_store_paths(store)):
            flags=os.O_RDONLY|os.O_NOFOLLOW|(os.O_DIRECTORY if index else 0)
            fds[index]=os.open(path,flags)
        return fds
    except BaseException:
        for fd in fds.values(): os.close(fd)
        raise


def _issue_store(mode, database, journal, expected_store_id=None):
    provisional=ValidatedSyntheticStore(mode,Path(database),Path(journal),'',(),_STORE_SEAL)
    fds={}; raw=None
    try:
        # No symlink at any ancestor; no relative/canonical alias admitted.
        safe_directory(provisional.database_path.parent)
        safe_directory(provisional.journal_directory.parent)
        fds=_store_handles(provisional)
        identities=tuple(_store_identity(path,fds[i],directory=i!=0,private=mode=='fixture' or i!=2)
                         for i,path in enumerate(_store_paths(provisional)))
        if journal.exists() or journal.is_symlink(): raise RecoveryBlocked()
        raw=sqlite3.connect(database.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=0.05)
        _closed_sql_guards(raw)
        raw.execute('PRAGMA foreign_keys=ON')
        _empty_synthetic_store(raw)
        store_id=raw_store(raw)
        if expected_store_id is not None and store_id!=uid(expected_store_id): raise RecoveryBlocked()
        descriptor=ValidatedSyntheticStore(mode,database,journal,store_id,identities,_STORE_SEAL)
        _validate_store_identities(descriptor,fds)
        _STORE_DESCRIPTORS[id(descriptor)]=(weakref.ref(descriptor),fds)
        weakref.finalize(descriptor,_release_unconsumed_store,id(descriptor))
        fds={}  # Factory identities remain pinned until lifecycle consumes them.
        return descriptor
    except (OSError,sqlite3.Error,ValueError): raise RecoveryBlocked() from None
    finally:
        if raw: raw.close()
        for fd in fds.values(): os.close(fd)


def validated_demo_store():
    from cabinet import boundary
    if not boundary._demo or boundary._secret is None: raise RecoveryBlocked()
    try: boundary.validate_paths(demo=True)
    except boundary.BoundaryError: raise RecoveryBlocked() from None
    if boundary.ROOT!=boundary.DEPLOYMENT_ROOT: raise RecoveryBlocked()
    return _issue_store('runtime',boundary.DATABASE,boundary.ROOT/'.demo-recovery')


def validated_fixture_store(*, root, expected_store_id):
    from cabinet import boundary
    import tempfile
    if boundary._demo or boundary._secret is None or 'memory' not in str(connection.settings_dict['NAME']):
        raise RecoveryBlocked()
    root=Path(root).absolute(); temporary=Path(tempfile.gettempdir()).resolve()
    if root==temporary or not root.is_relative_to(temporary): raise RecoveryBlocked()
    safe_directory(root)
    # A fresh private fixture has only the explicitly copied migrated DB.
    if {p.name for p in root.iterdir()}!={'db'}: raise RecoveryBlocked()
    if {p.name for p in (root/'db').iterdir()}!={'synthetic.sqlite3'}: raise RecoveryBlocked()
    return _issue_store('fixture',root/'db/synthetic.sqlite3',root/'journal',expected_store_id)


def _release_unconsumed_store(identifier):
    entry=_STORE_DESCRIPTORS.pop(identifier,None)
    if entry:
        for fd in entry[1].values(): os.close(fd)


def _sealed_store(store):
    return (type(store) is ValidatedSyntheticStore and store._seal is _STORE_SEAL
            and id(store) in _STORE_DESCRIPTORS and _STORE_DESCRIPTORS[id(store)][0]() is store)


@contextmanager
def synthetic_demo_lifecycle(*, validated_store):
    from cabinet import boundary
    store=validated_store
    if installed_coordinator() is not None or not _sealed_store(store): raise RecoveryBlocked()
    # Consume even a failed attempt; retain the factory's descriptors so an
    # unlinked inode cannot be recycled between issuance and lifecycle entry.
    _,fds=_STORE_DESCRIPTORS.pop(id(store))
    raw=None; coordinator=None; token=None
    try:
        if store.mode=='fixture':
            if boundary._demo or boundary._secret is None: raise RecoveryBlocked()
        elif store.mode=='runtime':
            if not boundary._demo or boundary._secret is None: raise RecoveryBlocked()
            try: boundary.validate_paths(demo=True)
            except boundary.BoundaryError: raise RecoveryBlocked() from None
            if (boundary.ROOT!=boundary.DEPLOYMENT_ROOT or store.database_path!=boundary.DATABASE
                    or store.journal_directory!=boundary.ROOT/'.demo-recovery'): raise RecoveryBlocked()
        else: raise RecoveryBlocked()
        _validate_store_identities(store,fds)
        raw=sqlite3.connect(store.database_path.as_uri()+'?mode=rw',uri=True,
                            isolation_level=None,timeout=0.05)
        _closed_sql_guards(raw)
        raw.execute('PRAGMA foreign_keys=ON'); raw.execute('PRAGMA synchronous=FULL')
        _validate_store_identities(store,fds)
        if raw_store(raw)!=store.store_id: raise RecoveryBlocked()
        _empty_synthetic_store(raw)
        coordinator=CurrentRecoveryCoordinator.provision_empty(store_id=store.store_id,
            journal_directory=store.journal_directory,trusted_empty_db=raw)
        with coordinator.mutex:
            coordinator.verify_current()
            raw.execute('BEGIN IMMEDIATE')
            try:
                _validate_store_identities(store,fds)
                _empty_synthetic_store(raw)
                raw.execute(f'PRAGMA application_id={APPLICATION_ID}')
                raw.execute('COMMIT')
            except BaseException:
                if raw.in_transaction: raw.execute('ROLLBACK')
                raise
            if raw.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID: raise RecoveryBlocked()
            os.fsync(fds[0]); os.fsync(fds[1])
            coordinator.validated_store=store; coordinator.store_fds=fds
            coordinator.owns_control=True
            coordinator.verify_current()
            token=_CURRENT.set(coordinator)
        yield coordinator
    except (OSError,sqlite3.Error,ValueError): raise RecoveryBlocked() from None
    finally:
        if token is not None: _CURRENT.reset(token)
        if coordinator: coordinator.close()
        if raw: raw.close()
        # Coordinator closes these after ownership transfer; close only once.
        if not coordinator or not coordinator.owns_control:
            for fd in fds.values(): os.close(fd)


_TRUSTED_RESTART_SEAL = object()


def _trusted_boundary(boundary):
    """Original locked root identity; consistency is explicitly NOT freshness."""
    from cabinet.portable_boundary import BoundRoot
    if type(boundary) is not BoundRoot or not boundary.active:
        raise RecoveryBlocked()
    root = safe_directory(boundary.root)
    info = root.stat()
    if (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            or hashlib.sha256(f'{info.st_dev}:{info.st_ino}'.encode()).hexdigest() != boundary.root_identity):
        raise RecoveryBlocked()
    if identity(root / '.portable-lock', boundary._lock) != (
            os.stat(root / '.portable-lock').st_dev, os.stat(root / '.portable-lock').st_ino):
        raise RecoveryBlocked()
    if 'FLOCK  ADVISORY  WRITE' not in Path(f'/proc/self/fdinfo/{boundary._lock}').read_text():
        # Kernel whitespace differs; check the fields rather than infer a lock
        # from merely being able to reopen its pathname.
        entries = [line.split() for line in Path(f'/proc/self/fdinfo/{boundary._lock}').read_text().splitlines()
                   if line.startswith('lock:')]
        if not any(row[2:5] == ['FLOCK', 'ADVISORY', 'WRITE'] and row[5] == str(os.getpid()) for row in entries):
            raise RecoveryBlocked()
    marker = root / '.portable-installation.json'
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        identity(marker, fd)
        value = json.loads(os.read(fd, 512))
        if value != {'schema': 1, 'installation_id': str(boundary.installation_id)}:
            raise RecoveryBlocked()
    finally:
        os.close(fd)
    return root



def _trusted_journal_binding(boundary, raw, fds, obj, *, create=False):
    """Bind separate owned directories/inodes, not a claim of disk freshness."""
    parent = boundary.root.parent
    safe_directory(parent)
    if parent.stat().st_uid != os.geteuid() or stat.S_IMODE(parent.stat().st_mode) != 0o700:
        raise RecoveryBlocked()
    journal = obj.directory
    if (journal != parent / ('.portable-recovery-' + boundary.installation_id.hex)
            or {p.name for p in journal.iterdir()} != ({'lock', 'journal'} if create else {'lock', 'journal', 'binding.json'})):
        raise RecoveryBlocked()
    expected = {'schema': 1, 'installation_id': str(boundary.installation_id),
        'root_identity': boundary.root_identity, 'store_id': raw_store(raw),
        'database': list(_store_identity(boundary.root / 'portable.sqlite3', fds[0], directory=False)),
        'root': list(_store_identity(boundary.root, fds[1], directory=True)),
        'parent': list(_store_identity(parent, fds[2], directory=True)),
        'journal': list(_store_identity(journal, obj.directory_fd, directory=True))}
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY
    fd = os.open('binding.json', flags | os.O_NOFOLLOW, 0o600, dir_fd=obj.directory_fd)
    try:
        binding_identity = identity(journal / 'binding.json', fd)
        if ('binding.json' in obj.identities and
                obj.identities['binding.json'] != binding_identity):
            raise RecoveryBlocked()
        if os.fstat(fd).st_uid != os.geteuid():
            raise RecoveryBlocked()
        if create:
            encoded = canonical(expected)
            if os.write(fd, encoded) != len(encoded):
                raise RecoveryBlocked()
            os.fsync(fd); os.fsync(obj.directory_fd)
        else:
            data = os.read(fd, 4097)
            if len(data) > 4096 or json.loads(data) != expected:
                raise RecoveryBlocked()
        if 'binding.json' not in obj.fds:
            # Keep the first descriptor: an equal-byte pathname replacement is
            # still a loss of this uninterrupted witness, not a new authority.
            obj.fds['binding.json'] = fd
            obj.identities['binding.json'] = binding_identity
            fd = None
    finally:
        if fd is not None:
            os.close(fd)


def _load_trusted_journal(obj):
    """Reconstruct the original protocol, refusing unknown/incomplete evidence."""
    obj.length = os.fstat(obj.fds['journal']).st_size
    if not 0 <= obj.length <= 512 * 1024 * 1024:
        raise RecoveryBlocked()
    data = os.pread(obj.fds['journal'], obj.length + 1, 0)
    obj.digest = hashlib.sha256(data).hexdigest()
    offset, previous, pending = 0, None, None
    obj.frames, obj.head_id, obj.sequence = [], None, 0
    while offset < len(data):
        if offset + 4 > len(data):
            raise RecoveryBlocked()
        size = struct.unpack('>I', data[offset:offset + 4])[0]; offset += 4
        if not 0 < size <= 16 * 1024 * 1024 or offset + size > len(data):
            raise RecoveryBlocked()
        encoded = data[offset:offset + size]; offset += size
        frame = json.loads(encoded)
        checksum = frame.pop('checksum')
        if (hashlib.sha256(canonical(frame)).hexdigest() != checksum or frame['previous'] != previous
                or frame['sequence'] != len(obj.frames) + 1 or frame['store_id'] != obj.store_id):
            raise RecoveryBlocked()
        uid(frame['operation_uuid'])
        if frame['phase'] == 'prepare':
            if pending is not None or any(old['operation_uuid'] == frame['operation_uuid'] for old in obj.frames):
                raise RecoveryBlocked()
            if set(frame) != {'sequence', 'store_id', 'operation_uuid', 'phase', 'previous',
                              'restrictions', 'local_commit_uuid', 'head_id'}:
                raise RecoveryBlocked()
            uid(frame['head_id']); uid(frame['local_commit_uuid'])
            if not frame['restrictions']:
                raise RecoveryBlocked()
            for index, restriction in enumerate(frame['restrictions']):
                if set(restriction) != set(RESTRICTION_FIELDS) | {'sequence'} or restriction['sequence'] != obj.sequence + index + 1:
                    raise RecoveryBlocked()
                validate_restriction({key: restriction[key] for key in RESTRICTION_FIELDS})
            pending = {key: frame[key] for key in ('operation_uuid', 'restrictions', 'local_commit_uuid', 'head_id')}
        elif frame['phase'] == 'commit':
            if (pending is None or set(frame) != {'sequence', 'store_id', 'operation_uuid', 'phase', 'previous',
                        'prepare_sequence', 'local_commit_uuid', 'head_id'}
                    or frame['operation_uuid'] != pending['operation_uuid']
                    or frame['local_commit_uuid'] != pending['local_commit_uuid'] or frame['head_id'] != pending['head_id']
                    or frame['prepare_sequence'] != len(obj.frames)):
                raise RecoveryBlocked()
            obj.head_id, obj.sequence = pending['head_id'], pending['restrictions'][-1]['sequence']
            pending = None
        else:
            raise RecoveryBlocked()
        frame['checksum'] = checksum; previous = checksum; obj.frames.append(frame)
    obj.pending, obj.audit = pending, None
    obj.verify_current(); obj.verify_mirror(obj.bound_db)


@contextmanager
def trusted_restart_fixture(boundary, *, confirmed, fresh=False):
    """Explicit synthetic same-installation restart; never an anti-rollback proof.

    The real activation gate is intentionally absent: this fixture requires the
    supported ephemeral verification environment and a private temporary root.
    Whole-root replay on the original inode is outside this named trust mode.
    Uninterrupted-witness constructors and their refusal tests remain unchanged.
    """
    from cabinet import boundary as verification
    from django.db.migrations.loader import MigrationLoader
    import tempfile
    if (confirmed is not True or installed_coordinator() is not None or verification._secret is None
            or verification._demo or 'memory' not in str(connection.settings_dict['NAME'])):
        raise RecoveryBlocked()
    root = _trusted_boundary(boundary)
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise RecoveryBlocked()
    parent = safe_directory(root.parent)
    if parent.stat().st_uid != os.geteuid() or stat.S_IMODE(parent.stat().st_mode) != 0o700:
        raise RecoveryBlocked()
    path, journal = root / 'portable.sqlite3', parent / ('.portable-recovery-' + boundary.installation_id.hex)
    raw = None; obj = None; fds = {}
    try:
        # Keep descriptor identities pinned throughout schema/journal inspection.
        for index, target in enumerate((path, root, parent)):
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if index else 0))
            fds[index] = fd
            _store_identity(target, fd, directory=index != 0)
        raw = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=0.05)
        _closed_sql_guards(raw)
        raw.execute('PRAGMA foreign_keys=ON'); raw.execute('PRAGMA synchronous=FULL')
        if set(raw.execute('SELECT app,name FROM django_migrations')) != set(MigrationLoader(None).disk_migrations):
            raise RecoveryBlocked()
        installation = raw.execute('SELECT id,root_identity,data_schema_version FROM shelf_portableinstallation').fetchall()
        if installation != [(boundary.installation_id.hex, boundary.root_identity, 1)]:
            raise RecoveryBlocked()
        _effective_guards(raw)
        semantic_check(raw)
        if fresh:
            if journal.exists() or raw.execute('PRAGMA application_id').fetchone()[0] != 0:
                raise RecoveryBlocked()
            obj = CurrentRecoveryCoordinator.provision_empty(store_id=raw_store(raw),
                journal_directory=journal, trusted_empty_db=raw)
            # The exclusive binding becomes durable before the application marker.
            # Interrupted initialization is retained and cannot be re-provisioned.
            _trusted_journal_binding(boundary, raw, fds, obj, create=True)
            raw.execute(f'PRAGMA application_id={APPLICATION_ID}')
            os.fsync(fds[0])
        else:
            if raw.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
                raise RecoveryBlocked()
            safe_directory(journal)
            obj = object.__new__(CurrentRecoveryCoordinator)
            obj.store_id, obj.directory, obj.mutex = raw_store(raw), journal, threading.RLock()
            obj.closed = obj.blocked = False; obj.owns_control = False
            obj.bound_db, obj.bound_path = raw, path
            obj.bound_identity = (path.stat().st_dev, path.stat().st_ino)
            obj.fds, obj.identities, obj.store_fds = {}, {}, {}
            obj.directory_fd = None
            obj.directory_fd = os.open(journal, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(obj.directory_fd); obj.directory_identity = (info.st_dev, info.st_ino)
            for name in ('lock', 'journal'):
                fd = os.open(journal / name, os.O_RDWR | os.O_NOFOLLOW)
                obj.fds[name] = fd; obj.identities[name] = identity(journal / name, fd)
            fcntl.flock(obj.fds['lock'], fcntl.LOCK_EX | fcntl.LOCK_NB)
            _trusted_journal_binding(boundary, raw, fds, obj)
            _load_trusted_journal(obj)
        descriptor = ValidatedSyntheticStore('fixture', path, journal, obj.store_id,
            tuple(_store_identity(target, fds[index], directory=index != 0)
                for index, target in enumerate((path, root, parent))), _STORE_SEAL)
        obj.validated_store, obj.store_fds, obj.owns_control = descriptor, fds, True
        obj._trusted_restart_seal, obj.trusted_boundary = _TRUSTED_RESTART_SEAL, boundary
        obj.verify_current()
        # Pending erase without its original audit authority cannot be guessed.
        # Other complete pending restrictions settle using the original protocol.
        if obj.pending:
            obj.resume_pending(raw)
        # Expire actual in-process sessions/leases/previews too; an old authority
        # object cannot survive a synthetic restart merely because SQL is clean.
        for close in tuple(boundary._closing):
            close()
        from shelf.models import _content_digest
        raw.create_function('cabinet_disclosure_write', 0, lambda: 1)
        raw.create_function('cabinet_content_digest', -1, _content_digest)
        raw.execute('BEGIN IMMEDIATE')
        try:
            raw.execute('DELETE FROM django_session')
            raw.execute("UPDATE shelf_portabledisclosurepreview SET state='stale' WHERE state='preview'")
            raw.execute('DELETE FROM shelf_portablevalidation')
            raw.execute("UPDATE shelf_portableprobeattempt SET state='unknown',outcome='unknown' WHERE state IN ('prepared','sending')")
            raw.execute("UPDATE shelf_synthesisrun SET state='unknown',portable_stage='unknown',revision=revision+1 WHERE contract_version='enhance/v1' AND attempts=1 AND state='running'")
            now = datetime.now(utc_timezone.utc).isoformat()
            affected = {row[0] for row in raw.execute("SELECT profile_id FROM shelf_synthesisrun WHERE contract_version='enhance/v1' AND content_redacted_at IS NULL AND julianday(portable_expires_at)<=julianday(?)", (now,))}
            affected.update(row[0] for row in raw.execute("SELECT cabinet_id FROM shelf_portablecabinetmanifest WHERE content_redacted_at IS NULL AND julianday(expires_at)<=julianday(?)", (now,)))
            raw.execute("UPDATE shelf_portablecabinetmanifest SET content_digest=cabinet_content_digest(manifest),manifest='{}',content_redacted_at=? WHERE content_redacted_at IS NULL AND julianday(expires_at)<=julianday(?)", (now, now))
            raw.execute("UPDATE shelf_synthesisrun SET content_digest=cabinet_content_digest(output,evidence),output='{}',evidence='{}',content_redacted_at=?,revision=revision+1 WHERE contract_version='enhance/v1' AND content_redacted_at IS NULL AND julianday(portable_expires_at)<=julianday(?)", (now, now))
            for profile in affected:
                raw.execute('UPDATE shelf_profilecabinetstate SET policy_epoch=policy_epoch+1,restriction_watermark=restriction_watermark+1 WHERE profile_id=?', (profile,))
            raw.execute('COMMIT')
        except BaseException:
            if raw.in_transaction: raw.execute('ROLLBACK')
            raise
        finally:
            raw.create_function('cabinet_disclosure_write', 0, lambda: 0)
        with using_recovery(obj):
            yield obj
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        raise RecoveryBlocked() from None
    finally:
        if obj is not None:
            obj.close()
        if raw is not None:
            raw.close()
        if obj is None or not obj.owns_control:
            for fd in fds.values():
                os.close(fd)


class NativeCleanCheckpoint:
    """Independent authenticated CLEAN/ACTIVE record for one native installation.

    The local locator contains only a store UUID and opaque revision. It cannot
    bless a database: the protected keyring record and original root lock must
    agree before any native authority writer is constructed.
    """
    def __init__(self, boundary, request):
        self.boundary, self.request = boundary, request
        self.locator = boundary.root / '.portable-clean-restart.json'
        self.revision = self.binding = self.coordinator = None
        self.enrolled = self.clean_stop = False
        self.pending_operation = self.pending_record = None
        self.writers_stopped = None

    def _call(self, operation, *, record=None, operation_id=None):
        identifier = str(operation_id or uuid.uuid4())
        value = {'version': 'cabinet-secret-service/v1', 'operation': operation,
            'record_kind': 'clean_restart_anchor', 'binding': self.binding,
            'expected_revision': self.revision, 'operation_id': identifier, 'record_if_put': record}
        try:
            reply = self.request(value)
            if (type(reply) is not dict or set(reply) != {'version', 'operation_id', 'status', 'revision', 'record'}
                    or reply['version'] != value['version'] or reply['operation_id'] != identifier
                    or reply['status'] != 'AVAILABLE' or type(reply['revision']) is not str
                    or str(uuid.UUID(reply['revision'])) != reply['revision']):
                raise ValueError
            return reply
        except Exception:
            raise RecoveryBlocked() from None

    def _locator_write(self):
        value = canonical({'schema': 1, 'store_id': self.binding['store_id'], 'revision': self.revision})
        name = self.locator.name + '.' + uuid.uuid4().hex + '.tmp'
        directory = os.open(self.boundary.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = None
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            if os.write(fd, value) != len(value):
                raise RecoveryBlocked()
            os.fsync(fd)
            os.close(fd); fd = None
            os.replace(name, self.locator.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def _put(self, record):
        # Keep the exact operation in RAM through a lost acknowledgement. No
        # subsequent call repeats its mutation; reconciliation observes it only.
        if self.pending_operation is None:
            self.pending_operation, self.pending_record = str(uuid.uuid4()), record
            reply = self._call('put', record=record, operation_id=self.pending_operation)
        else:
            if self.pending_record != record:
                raise RecoveryBlocked()
            reply = self._call('reconcile', operation_id=self.pending_operation)
        revision = reply['revision']
        old = self.revision
        self.revision = revision
        try:
            read = self._call('read')
            if read['revision'] != revision or read['record'] != record:
                raise RecoveryBlocked()
            self._locator_write()
        except BaseException:
            self.revision = old
            raise
        self.pending_operation = self.pending_record = None

    def _bind(self, store_id):
        self.binding = {'os_uid': os.getuid(), 'installation_id': str(self.boundary.installation_id),
            'store_id': store_id, 'root_identity': self.boundary.root_identity}

    def _database_identity(self):
        fd = os.open(self.boundary.database_name, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            return hashlib.sha256(canonical(identity(Path(self.boundary.database_name), fd))).hexdigest()
        finally:
            os.close(fd)

    def _database_digest(self):
        fd = os.open(self.boundary.database_name, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            identity(Path(self.boundary.database_name), fd)
            value = hashlib.sha256()
            while data := os.read(fd, 1024 * 1024):
                value.update(data)
            return value.hexdigest()
        finally:
            os.close(fd)

    def _record(self, state):
        obj = self.coordinator
        return {'schema': 'cabinet-clean-restart/v1', **{key: self.binding[key] for key in
            ('installation_id', 'store_id', 'root_identity')}, 'database_identity': self._database_identity(),
            'checkpoint_id': str(uuid.uuid4()), 'database_digest': self._database_digest(),
            'journal_path': str(obj.directory), 'journal_length': obj.length, 'journal_digest': obj.digest,
            'journal_head': hashlib.sha256(canonical({'head': obj.head_id, 'sequence': obj.sequence})).hexdigest(),
            'state': state}

    def enroll(self):
        if self.enrolled:
            return
        obj = installed_coordinator()
        if not native_first_start_matches(obj, self.boundary):
            raise RecoveryBlocked()
        obj.verify_current(); obj.verify_mirror(obj.bound_db)
        if obj.pending is not None or self.locator.exists():
            raise RecoveryBlocked()
        self.coordinator = obj
        self._bind(obj.store_id)
        record = self.pending_record or self._record('ACTIVE')
        self._put(record)
        self.enrolled = True

    def seal(self):
        if not self.enrolled or not self.clean_stop:
            return
        if self.writers_stopped is None or self.writers_stopped() is not True:
            raise RecoveryBlocked()
        obj = self.coordinator
        # ExitStack has already stopped the selected workers and accepting
        # socket. Close every Django writer before sealing the last raw handle.
        from django.db import connections
        connections.close_all()
        obj.verify_current(); obj.verify_mirror(obj.bound_db)
        if obj.pending is not None or obj.bound_db.in_transaction:
            raise RecoveryBlocked()
        semantic_check(obj.bound_db)
        if obj.bound_db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
            raise RecoveryBlocked()
        obj.bound_db.close()
        database = os.open(self.boundary.database_name, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(database)
        finally:
            os.close(database)
        directory = os.open(self.boundary.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        # CLEAN is last. A failed/uncertain store update leaves no admitted
        # restart and never trades database continuity for key availability.
        self._put(self._record('CLEAN'))

    def load_clean(self, raw):
        _trusted_boundary(self.boundary)
        self._bind(raw_store(raw))
        fd = os.open(self.locator, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            identity(self.locator, fd)
            value = json.loads(os.read(fd, 1025))
            if (set(value) != {'schema', 'store_id', 'revision'} or value['schema'] != 1
                    or value['store_id'] != self.binding['store_id']
                    or str(uuid.UUID(value['revision'])) != value['revision']):
                raise RecoveryBlocked()
            self.revision = value['revision']
        finally:
            os.close(fd)
        reply = self._call('read')
        record = reply['record']
        required = {'schema','installation_id','store_id','root_identity','database_identity','checkpoint_id',
                    'database_digest','journal_path','journal_length','journal_digest','journal_head','state'}
        if (reply['revision'] != self.revision or type(record) is not dict or set(record) != required
                or record['schema'] != 'cabinet-clean-restart/v1' or record['state'] != 'CLEAN'
                or any(record[key] != self.binding[key] for key in ('installation_id','store_id','root_identity'))
                or record['database_identity'] != self._database_identity()
                or record['database_digest'] != self._database_digest()):
            raise RecoveryBlocked()
        # WAL must have been fully checkpointed by the prior owned writer.
        wal = Path(self.boundary.database_name + '-wal')
        if wal.exists() and wal.stat().st_size:
            raise RecoveryBlocked()
        return record


def _invalidate_native_restart(raw):
    """Retain data/reactions, expire all process-old sessions and sending proofs."""
    from shelf.models import _content_digest
    raw.create_function('cabinet_disclosure_write', 0, lambda: 1)
    raw.create_function('cabinet_content_digest', -1, _content_digest)
    raw.execute('BEGIN IMMEDIATE')
    try:
        raw.execute('DELETE FROM django_session')
        raw.execute("UPDATE shelf_portabledisclosurepreview SET state='stale' WHERE state='preview'")
        raw.execute('DELETE FROM shelf_portablevalidation')
        raw.execute('UPDATE shelf_portableconnection SET credential_generation=credential_generation+1,validation_generation=validation_generation+1')
        raw.execute("UPDATE shelf_portableprobeattempt SET state='unknown',outcome='unknown' WHERE state IN ('prepared','sending')")
        raw.execute("UPDATE shelf_synthesisrun SET state='unknown',portable_stage='unknown',revision=revision+1 WHERE contract_version='enhance/v1' AND attempts=1 AND state='running'")
        now = datetime.now(utc_timezone.utc).isoformat()
        affected = {row[0] for row in raw.execute("SELECT profile_id FROM shelf_synthesisrun WHERE contract_version='enhance/v1' AND content_redacted_at IS NULL AND julianday(portable_expires_at)<=julianday(?)", (now,))}
        affected.update(row[0] for row in raw.execute("SELECT cabinet_id FROM shelf_portablecabinetmanifest WHERE content_redacted_at IS NULL AND julianday(expires_at)<=julianday(?)", (now,)))
        raw.execute("UPDATE shelf_portablecabinetmanifest SET content_digest=cabinet_content_digest(manifest),manifest='{}',content_redacted_at=? WHERE content_redacted_at IS NULL AND julianday(expires_at)<=julianday(?)", (now, now))
        raw.execute("UPDATE shelf_synthesisrun SET content_digest=cabinet_content_digest(output,evidence),output='{}',evidence='{}',content_redacted_at=?,revision=revision+1 WHERE contract_version='enhance/v1' AND content_redacted_at IS NULL AND julianday(portable_expires_at)<=julianday(?)", (now, now))
        for profile in affected:
            raw.execute('UPDATE shelf_profilecabinetstate SET policy_epoch=policy_epoch+1,restriction_watermark=restriction_watermark+1 WHERE profile_id=?', (profile,))
        raw.execute('COMMIT')
    except BaseException:
        if raw.in_transaction:
            raw.execute('ROLLBACK')
        raise
    finally:
        raw.create_function('cabinet_disclosure_write', 0, lambda: 0)


@contextmanager
def native_clean_restart(boundary, checkpoint):
    """Consume one authenticated CLEAN checkpoint before any authority DB write."""
    if installed_coordinator() is not None or checkpoint.boundary is not boundary:
        raise RecoveryBlocked()
    root = _trusted_boundary(boundary)
    raw = obj = None
    fds = {}
    try:
        paths = (Path(boundary.database_name), root, root.parent)
        for index, path in enumerate(paths):
            fds[index] = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if index else 0))
        identities = tuple(_store_identity(path, fds[index], directory=index != 0, private=index != 2)
                           for index, path in enumerate(paths))
        raw = sqlite3.connect(paths[0].as_uri() + '?mode=ro', uri=True, isolation_level=None, timeout=0.05)
        _closed_sql_guards(raw)
        record = checkpoint.load_clean(raw)
        _effective_guards(raw); semantic_check(raw)
        journal = Path(record['journal_path'])
        prefix = '.native-recovery-' + boundary.installation_id.hex + '-'
        if journal.parent != root.parent or not journal.name.startswith(prefix):
            raise RecoveryBlocked()
        if len(journal.name[len(prefix):]) != 32 or str(uuid.UUID(journal.name[len(prefix):])).replace('-', '') != journal.name[len(prefix):]:
            raise RecoveryBlocked()
        safe_directory(journal)
        obj = object.__new__(CurrentRecoveryCoordinator)
        obj.store_id, obj.directory, obj.mutex = raw_store(raw), journal, threading.RLock()
        obj.closed = obj.blocked = False
        obj.owns_control = False
        obj.bound_db, obj.bound_path, obj.bound_identity = raw, paths[0], identities[0]
        obj.fds, obj.identities, obj.store_fds = {}, {}, {}
        obj.directory_fd = None
        obj.directory_fd = os.open(journal, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(obj.directory_fd)
        obj.directory_identity = (info.st_dev, info.st_ino)
        for name in ('lock', 'journal'):
            fd = os.open(journal / name, os.O_RDWR | os.O_NOFOLLOW)
            obj.fds[name], obj.identities[name] = fd, identity(journal / name, fd)
        fcntl.flock(obj.fds['lock'], fcntl.LOCK_EX | fcntl.LOCK_NB)
        _load_trusted_journal(obj)
        if (obj.pending is not None or obj.length != record['journal_length'] or obj.digest != record['journal_digest']
                or hashlib.sha256(canonical({'head':obj.head_id,'sequence':obj.sequence})).hexdigest() != record['journal_head']):
            raise RecoveryBlocked()
        # Atomically consume CLEAN in the independent store before opening the
        # write connection. Replaying old DB/locator bytes cannot regain it.
        checkpoint.coordinator = obj
        checkpoint._put({**record, 'state':'ACTIVE', 'checkpoint_id':str(uuid.uuid4())})
        checkpoint.enrolled = True
        raw.close()
        raw = sqlite3.connect(paths[0].as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=0.05)
        _closed_sql_guards(raw)
        raw.execute('PRAGMA foreign_keys=ON'); raw.execute('PRAGMA synchronous=FULL')
        obj.bound_db = raw
        _NATIVE_CLEAN_STARTS[obj] = (boundary, raw, paths, fds, identities)
        obj.verify_current(); obj.verify_mirror(raw)
        _invalidate_native_restart(raw)
        with using_recovery(obj):
            yield obj
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        raise RecoveryBlocked() from None
    finally:
        if obj is not None:
            _NATIVE_CLEAN_STARTS.pop(obj, None)
            obj.close()
        if raw is not None:
            raw.close()
        for fd in fds.values():
            os.close(fd)
