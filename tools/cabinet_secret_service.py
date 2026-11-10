#!/usr/bin/python3
"""Private, bounded Culture Cabinet Secret Service bridge.

The importable codec/client has no GI, crypto, keyring or secret side effects.
Only the isolated system-Python worker imports the installed OS libraries. Key
bytes travel through inherited pipes, never argv, environment, stdout or stderr.
Same-UID hostile processes and OS administrators are outside this boundary.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import selectors
import stat
import struct
import subprocess
import sys
import time
import uuid

VERSION = 'cabinet-secret-service/v1'
MAX_FRAME = 65536
MAX_SECRET = 8192
TIMEOUT = 10.0
UID = 1000
RUNTIME = '/run/user/1000/culture-cabinet-key-store'
SELECTION = '/home/tang/.local/state/culture-cabinet-key-store/selection.json'
HELPER = str(Path(__file__).resolve())
STATUS = frozenset(('AVAILABLE', 'LOCKED', 'MISSING', 'UNAVAILABLE', 'CONFLICT',
                    'CORRUPT', 'UNKNOWN', 'REMOVED'))
REQUEST_FIELDS = frozenset(('version', 'operation', 'record_kind', 'binding',
                           'expected_revision', 'operation_id', 'record_if_put'))
RESPONSE_FIELDS = frozenset(('version', 'operation_id', 'status', 'revision', 'record'))
CREDENTIAL_FIELDS = frozenset(('schema', 'binding', 'config_revision_or_null', 'generation',
    'operation_id', 'entered_at', 'updated_at', 'expires_at_or_null', 'scopes', 'secret'))
ANCHOR_FIELDS = frozenset(('schema', 'installation_id', 'store_id', 'root_identity',
    'database_identity', 'checkpoint_id', 'database_digest', 'journal_path',
    'journal_length', 'journal_digest', 'journal_head', 'state'))
PURPOSES = {
    'openai': ('inference',),
    'tmdb': ('catalogue_lookup', 'culture-cabinet-tmdb-backend'),
    'hardcover': ('catalogue_lookup', 'culture-cabinet-hardcover-backend'),
    'librarything': ('culture-cabinet-librarything-backend',),
    'hydra': ('culture-cabinet-hydra-search',),
    'sab': ('culture-cabinet-sab-add-only',),
}


class Refused(Exception):
    """Only closed status codes; never retain an OS/library error or secret."""
    def __init__(self, status='UNAVAILABLE'):
        self.status = status if status in STATUS else 'UNAVAILABLE'
        super().__init__(self.status)


def require(value, status='UNAVAILABLE'):
    if not value:
        raise Refused(status)


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Refused() from None


def _shape(value, fields):
    require(type(value) is dict and set(value) == set(fields))


def _uuid(value):
    try:
        require(type(value) is str and str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError):
        raise Refused() from None
    return value


def _digest(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None)
    return value


def _text(value, maximum=256):
    try:
        require(type(value) is str and 0 < len(value.encode('utf-8')) <= maximum)
    except UnicodeError:
        raise Refused() from None
    return value


def _positive(value):
    require(type(value) is int and 1 <= value < 2**53)


def _time(value):
    try:
        require(type(value) is str and value.endswith('Z'))
        parsed = datetime.fromisoformat(value[:-1] + '+00:00')
        require(parsed.isoformat().replace('+00:00', 'Z') == value)
    except (ValueError, TypeError):
        raise Refused() from None
    return parsed


def strict_json(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_FRAME)
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    def nonfinite(_):
        raise Refused()
    def bounded(value, depth=0):
        require(depth <= 16)
        if type(value) is dict:
            require(len(value) <= 64)
            for key, item in value.items():
                _text(key, 128)
                bounded(item, depth + 1)
        elif type(value) is list:
            require(len(value) <= 128)
            for item in value:
                bounded(item, depth + 1)
        else:
            require(value is None or type(value) in (str, int, bool))
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=nonfinite)
        bounded(value)
        canonical(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise Refused() from None


def validate_binding(kind, binding):
    if kind == 'clean_restart_anchor':
        _shape(binding, ('os_uid', 'installation_id', 'store_id', 'root_identity'))
        require(type(binding['os_uid']) is int and binding['os_uid'] == UID)
        _uuid(binding['installation_id']); _uuid(binding['store_id'])
        _digest(binding['root_identity'])
        return
    require(kind == 'credential')
    _shape(binding, ('installation_id', 'owner_id', 'owner_revision', 'provider', 'purpose',
                     'connection_id_or_null', 'legacy_account_actor_profile_or_null'))
    _uuid(binding['installation_id'])
    _positive(binding['owner_id']); _positive(binding['owner_revision'])
    provider = binding['provider']
    require(type(provider) is str and provider in PURPOSES
            and binding['purpose'] in PURPOSES[provider])
    connection, legacy = binding['connection_id_or_null'], binding['legacy_account_actor_profile_or_null']
    if connection is not None:
        _uuid(connection)
        require(legacy is None and provider in ('openai', 'tmdb', 'hardcover'))
        require(binding['purpose'] == ('inference' if provider == 'openai' else 'catalogue_lookup'))
    else:
        require(provider != 'openai' and binding['purpose'] != 'catalogue_lookup')
        _shape(legacy, ('account_id', 'actor_id', 'profile_id'))
        require(type(legacy['account_id']) is str
                and re.fullmatch('[A-Za-z0-9_-]{1,64}', legacy['account_id']) is not None)
        for key in ('actor_id', 'profile_id'):
            require(type(legacy[key]) is str and re.fullmatch('[1-9][0-9]{0,17}', legacy[key]) is not None)


def validate_record(kind, binding, operation_id, record):
    if kind == 'clean_restart_anchor':
        _shape(record, ANCHOR_FIELDS)
        require(record['schema'] == 'cabinet-clean-restart/v1')
        for key in ('installation_id', 'store_id', 'root_identity'):
            require(record[key] == binding[key])
        _uuid(record['checkpoint_id'])
        for key in ('database_identity', 'database_digest', 'journal_digest', 'journal_head'):
            _digest(record[key])
        _text(record['journal_path'], 4096)
        require(record['journal_path'].startswith('/') and '\x00' not in record['journal_path'])
        require(type(record['journal_length']) is int and 0 <= record['journal_length'] < 2**53)
        require(record['state'] in ('ACTIVE', 'CLEAN'))
        return
    _shape(record, CREDENTIAL_FIELDS)
    require(record['schema'] == 'cabinet-credential/v1' and record['binding'] == binding
            and record['operation_id'] == operation_id)
    revision = record['config_revision_or_null']
    if binding['connection_id_or_null'] is not None:
        _positive(revision)
    else:
        require(revision is None)
    _uuid(record['generation']); _uuid(record['operation_id'])
    entered, updated = _time(record['entered_at']), _time(record['updated_at'])
    require(entered <= updated)
    expiry = record['expires_at_or_null']
    if expiry is not None:
        require(_time(expiry) > entered)
    scopes = record['scopes']
    require(type(scopes) is list and len(scopes) <= 64 and all(type(s) is str for s in scopes))
    require(len(scopes) == len(set(scopes)))
    for scope in scopes:
        _text(scope, 128)
    _text(record['secret'], MAX_SECRET)


def validate_request(request):
    _shape(request, REQUEST_FIELDS)
    require(request['version'] == VERSION and request['operation'] in
            ('availability', 'put', 'read', 'delete', 'reconcile'))
    validate_binding(request['record_kind'], request['binding'])
    _uuid(request['operation_id'])
    expected = request['expected_revision']
    if expected is not None:
        _uuid(expected)
    else:
        # Reconciliation of a first create has no previous store revision.
        require(request['operation'] in ('availability', 'put', 'reconcile'))
    if request['operation'] == 'put':
        validate_record(request['record_kind'], request['binding'], request['operation_id'], request['record_if_put'])
    else:
        require(request['record_if_put'] is None)
    require(len(canonical(request)) <= MAX_FRAME)
    return request


def response(request, status, revision=None, record=None):
    require(status in STATUS)
    if revision is not None:
        _uuid(revision)
    require(record is None or request['operation'] == 'read' and status == 'AVAILABLE')
    return {'version': VERSION, 'operation_id': request['operation_id'], 'status': status,
            'revision': revision, 'record': record}


def encode_frame(value):
    raw = canonical(value)
    require(0 < len(raw) <= MAX_FRAME)
    return struct.pack('!I', len(raw)) + raw


def decode_frame(raw):
    require(type(raw) is bytes and len(raw) >= 4)
    length, = struct.unpack('!I', raw[:4])
    require(0 < length <= MAX_FRAME and len(raw) == length + 4)
    return strict_json(raw[4:])


def _exchange(write_fd, read_fd, outgoing, deadline, open_fds):
    """Drain and fill concurrently so a full private pipe cannot bypass deadline."""
    os.set_blocking(write_fd, False); os.set_blocking(read_fd, False)
    incoming, offset, writer_open = bytearray(), 0, True
    with selectors.DefaultSelector() as selector:
        selector.register(write_fd, selectors.EVENT_WRITE)
        selector.register(read_fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'UNKNOWN')
            for key, _ in selector.select(remaining):
                if key.fd == write_fd:
                    try:
                        offset += os.write(write_fd, outgoing[offset:])
                    except BlockingIOError:
                        continue
                    if offset == len(outgoing):
                        selector.unregister(write_fd); os.close(write_fd); open_fds.remove(write_fd)
                        writer_open = False
                else:
                    try:
                        piece = os.read(read_fd, 8192)
                    except BlockingIOError:
                        continue
                    if not piece:
                        require(not writer_open, 'UNKNOWN')
                        return bytes(incoming)
                    incoming.extend(piece)
                    require(len(incoming) <= MAX_FRAME + 4, 'UNKNOWN')
                    if len(incoming) >= 4:
                        length, = struct.unpack('!I', incoming[:4])
                        require(0 < length <= MAX_FRAME and len(incoming) <= length + 4, 'UNKNOWN')


def invoke(request, *, timeout=TIMEOUT):
    """Native private client. A missing mutation acknowledgement is UNKNOWN.

    The helper path/interpreter and environment are fixed by this packaged module,
    not caller fields. No retries, implicit unlock or generic executable selector.
    """
    validate_request(request)
    require(type(timeout) in (int, float) and 0 < timeout <= TIMEOUT)
    process = None
    fds = []
    deadline = time.monotonic() + timeout
    failure = 'UNKNOWN' if request['operation'] in ('put', 'delete', 'reconcile') else 'UNAVAILABLE'
    try:
        require(os.getuid() == UID)
        request_read, request_write = os.pipe()
        response_read, response_write = os.pipe()
        fds = [request_read, request_write, response_read, response_write]
        process = subprocess.Popen(['/usr/bin/python3', '-I', '-B', HELPER,
            '--request-fd', str(request_read), '--response-fd', str(response_write)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, pass_fds=(request_read, response_write),
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'})
        os.close(request_read); fds.remove(request_read)
        os.close(response_write); fds.remove(response_write)
        raw = _exchange(request_write, response_read, encode_frame(request), deadline, fds)
        result = decode_frame(raw)
        _shape(result, RESPONSE_FIELDS)
        require(result['version'] == VERSION and result['operation_id'] == request['operation_id']
                and result['status'] in STATUS)
        require(result['record'] is None or request['operation'] == 'read' and result['status'] == 'AVAILABLE')
        if result['revision'] is not None:
            _uuid(result['revision'])
        if result['record'] is not None:
            require(type(result['record']) is dict)
            # A read is bound to the record's original write UUID, not read UUID.
            record_op = result['record'].get('operation_id') if request['record_kind'] == 'credential' else request['operation_id']
            validate_record(request['record_kind'], request['binding'], record_op, result['record'])
        process.wait(timeout=max(.001, deadline - time.monotonic()))
        require(process.returncode == 0)
        return result
    except (Refused, OSError, ValueError, TypeError, subprocess.SubprocessError):
        return response(request, failure)
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        if process is not None and process.poll() is None:
            process.kill(); process.wait()


@contextmanager
def mutation_lock(deadline):
    """Only a lock file, never secret material, goes in the checked runtime dir."""
    parent = directory = lock = None
    try:
        require(os.getuid() == UID)
        parent = os.open('/run/user/1000', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(parent)
        require(info.st_uid == UID and stat.S_IMODE(info.st_mode) == 0o700)
        try:
            os.mkdir('culture-cabinet-key-store', 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        directory = os.open('culture-cabinet-key-store', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        info = os.fstat(directory)
        require(info.st_uid == UID and stat.S_IMODE(info.st_mode) == 0o700)
        lock = os.open('control.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        info = os.fstat(lock)
        require(info.st_uid == UID and info.st_nlink == 1 and stat.S_ISREG(info.st_mode)
                and stat.S_IMODE(info.st_mode) == 0o600)
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, 'UNKNOWN')
                time.sleep(.01)
        yield
    finally:
        for fd in (lock, directory, parent):
            if fd is not None:
                os.close(fd)


class Store:
    """Authenticated record engine over the one selected collection backend.

    Backend.get/set/delete use an exact (kind,binding_digest,slot) selector. They
    must not unlock, discover other collections or initialize a missing master.
    An encrypted intent receipt disables old data before replacement/deletion;
    interrupted writes are resolved only by observing their exact UUID/revision.
    """
    def __init__(self, backend):
        self.backend = backend

    def _crypt(self):
        from cryptography.fernet import Fernet
        key = self.backend.master_key()
        try:
            return Fernet(key)
        except (ValueError, TypeError):
            raise Refused('CORRUPT') from None

    def _read(self, kind, binding_digest, slot, cipher):
        from cryptography.fernet import InvalidToken
        value = self.backend.get(kind, binding_digest, slot)
        if value is None:
            return None
        try:
            require(type(value) is bytes and len(value) <= MAX_FRAME * 2, 'CORRUPT')
            return strict_json(cipher.decrypt(value))
        except (InvalidToken, Refused, ValueError, TypeError):
            raise Refused('CORRUPT') from None

    def _write(self, kind, binding_digest, slot, cipher, value):
        token = cipher.encrypt(canonical(value))
        self.backend.set(kind, binding_digest, slot, token)
        require(self._read(kind, binding_digest, slot, cipher) == value, 'UNKNOWN')

    def execute(self, request):
        validate_request(request)
        try:
            return self._execute(request)
        except Refused as error:
            return response(request, error.status)

    def _execute(self, request):
        kind, binding, operation = (request[key] for key in ('record_kind', 'binding', 'operation'))
        binding_digest = hashlib.sha256(canonical(binding)).hexdigest()
        self.backend.available()
        if operation == 'availability':
            # Presence/status only: do not load a secret or decrypt on passive GET.
            found = self.backend.exists(kind, binding_digest, 'record')
            return response(request, 'AVAILABLE' if found else 'MISSING')
        cipher = self._crypt()
        receipt = self._read(kind, binding_digest, 'receipt', cipher)
        stored = self._read(kind, binding_digest, 'record', cipher)
        if stored is not None:
            _shape(stored, ('schema', 'kind', 'binding', 'revision', 'operation_id', 'record'))
            require(stored['schema'] == 'cabinet-record/v1' and stored['kind'] == kind
                    and stored['binding'] == binding, 'CORRUPT')
            _uuid(stored['revision']); _uuid(stored['operation_id'])
            validate_record(kind, binding, stored['operation_id'], stored['record'])
        if receipt is not None:
            _shape(receipt, ('schema', 'kind', 'binding', 'operation', 'operation_id', 'revision',
                            'previous_revision', 'request_digest', 'status'))
            require(receipt['schema'] == 'cabinet-receipt/v1' and receipt['kind'] == kind
                    and receipt['binding'] == binding and receipt['operation'] in ('put', 'delete')
                    and receipt['status'] in ('PENDING', 'ACTIVE', 'REMOVED'), 'CORRUPT')
            _uuid(receipt['operation_id']); _uuid(receipt['revision']); _digest(receipt['request_digest'])
            if receipt['previous_revision'] is not None:
                _uuid(receipt['previous_revision'])
        if operation == 'reconcile':
            return self._reconcile(request, kind, binding_digest, receipt, stored, cipher)
        if receipt is not None and receipt['status'] == 'PENDING':
            return response(request, 'UNKNOWN')
        if stored is not None:
            require(receipt is not None and receipt['status'] == 'ACTIVE'
                    and receipt['revision'] == stored['revision']
                    and receipt['operation_id'] == stored['operation_id'], 'CORRUPT')
        if operation == 'read':
            if stored is None:
                return response(request, 'MISSING')
            require(stored['revision'] == request['expected_revision'], 'CONFLICT')
            record = stored['record']
            if kind == 'credential' and record['expires_at_or_null'] is not None:
                require(datetime.now(timezone.utc) < _time(record['expires_at_or_null']))
            return response(request, 'AVAILABLE', stored['revision'], record)
        request_digest = hashlib.sha256(canonical(request)).hexdigest()
        if receipt is not None and receipt['operation_id'] == request['operation_id']:
            require(receipt['request_digest'] == request_digest, 'CONFLICT')
            return response(request, 'REMOVED' if receipt['status'] == 'REMOVED' else 'AVAILABLE', receipt['revision'])
        current = stored['revision'] if stored else None
        # A tombstone's revision prevents a first-create from erasing deletion
        # history. Re-enrollment is an explicit CAS against that tombstone.
        if current is None and receipt is not None:
            current = receipt['revision']
        require(current == request['expected_revision'], 'CONFLICT')
        if operation == 'delete':
            require(stored is not None, 'MISSING')
        if kind == 'credential' and operation == 'put':
            expiry = request['record_if_put']['expires_at_or_null']
            require(expiry is None or datetime.now(timezone.utc) < _time(expiry))
        revision = str(uuid.uuid4())
        intent = {'schema': 'cabinet-receipt/v1', 'kind': kind, 'binding': binding,
            'operation': operation, 'operation_id': request['operation_id'], 'revision': revision,
            'previous_revision': current, 'request_digest': request_digest, 'status': 'PENDING'}
        try:
            self._write(kind, binding_digest, 'receipt', cipher, intent)
            if operation == 'put':
                target = {'schema': 'cabinet-record/v1', 'kind': kind, 'binding': binding,
                    'revision': revision, 'operation_id': request['operation_id'], 'record': request['record_if_put']}
                self._write(kind, binding_digest, 'record', cipher, target)
            else:
                self.backend.delete(kind, binding_digest, 'record')
                require(not self.backend.exists(kind, binding_digest, 'record'), 'UNKNOWN')
            final = {**intent, 'status': 'ACTIVE' if operation == 'put' else 'REMOVED'}
            self._write(kind, binding_digest, 'receipt', cipher, final)
        except (Refused, OSError):
            # Any error after the mutation starts is ambiguous, even if a
            # lower-level API labels the error unavailable. Never retry blindly.
            return response(request, 'UNKNOWN')
        return response(request, 'AVAILABLE' if operation == 'put' else 'REMOVED', revision)

    def _reconcile(self, request, kind, binding_digest, receipt, stored, cipher):
        if receipt is None or receipt['operation_id'] != request['operation_id']:
            return response(request, 'UNKNOWN')
        require(receipt['previous_revision'] == request['expected_revision'], 'CONFLICT')
        if receipt['operation'] == 'put':
            complete = (stored is not None and stored['revision'] == receipt['revision']
                        and stored['operation_id'] == receipt['operation_id'])
            state, status = 'ACTIVE', 'AVAILABLE'
        else:
            complete = stored is None
            state, status = 'REMOVED', 'REMOVED'
        if not complete:
            return response(request, 'UNKNOWN')
        if receipt['status'] == 'PENDING':
            self._write(kind, binding_digest, 'receipt', cipher, {**receipt, 'status': state})
        else:
            require(receipt['status'] == state, 'CORRUPT')
        return response(request, status, receipt['revision'])


class _Async:
    """Cancellable GI work on a private main context, one absolute deadline."""
    def __init__(self, deadline):
        import gi
        gi.require_version('Secret', '1')
        from gi.repository import Gio, GLib, Secret
        self.Gio, self.GLib, self.Secret, self.deadline = Gio, GLib, Secret, deadline

    def call(self, start, finish):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0)
        context = self.GLib.MainContext.new()
        context.push_thread_default()
        loop = self.GLib.MainLoop.new(context, False)
        cancellable = self.Gio.Cancellable.new()
        outcome = {}
        def done(source, result, *_):
            try:
                outcome['value'] = finish(source, result)
            except Exception:
                outcome['error'] = True
            loop.quit()
        def expired(*_):
            outcome['error'] = True
            cancellable.cancel()
            loop.quit()
            return False
        timer = self.GLib.timeout_source_new(max(1, int(remaining * 1000)))
        timer.set_callback(expired)
        timer.attach(context)
        try:
            start(cancellable, done)
            if not outcome:
                loop.run()
            require('error' not in outcome and 'value' in outcome)
            return outcome['value']
        except Exception:
            cancellable.cancel()
            raise Refused() from None
        finally:
            timer.destroy()
            context.pop_thread_default()


def _read_selection():
    """Read only the fixed app selection, never a default/session alias.

    This contains nonsecret collection/protection references, not keys. A
    separately admitted provisioning step is required to publish this record.
    """
    fds = []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(fd)
        parts = SELECTION.strip('/').split('/')
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            fds.append(fd)
            info = os.fstat(fd)
            require(info.st_uid in (0, UID) and not stat.S_IMODE(info.st_mode) & 0o022)
        info = os.fstat(fd)
        require(info.st_uid == UID and stat.S_IMODE(info.st_mode) == 0o700)
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        fds.append(leaf)
        info = os.fstat(leaf)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == UID and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 4096)
        value = strict_json(os.read(leaf, 4097))
        _shape(value, ('schema', 'collection_ref', 'protection'))
        require(value['schema'] == 'cabinet-key-store-selection/v1')
        require(type(value['collection_ref']) is str and re.fullmatch(
            '/org/freedesktop/secrets/collection/[A-Za-z0-9_]+', value['collection_ref']) is not None)
        require(value['collection_ref'].rsplit('/', 1)[-1] not in ('session', 'default'))
        # This value is not inferred from Locked=false. Only the explicit host
        # provisioning path may publish protection evidence for this selection.
        require(value['protection'] == 'nonempty-password-verified')
        return value
    except (OSError, ValueError):
        raise Refused() from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


class _SecretBackend:
    """GI/libsecret adapter for exactly the host-selected application collection.

    No service/collection discovery, aliases, unlock flags or implicit creation.
    Metadata searches are confined to exact application selectors. Secret loads
    happen only for explicit read/mutation/reconciliation, never availability.
    """
    def __init__(self, deadline):
        self.async_ = _Async(deadline)
        self.Gio, self.GLib, self.Secret = self.async_.Gio, self.async_.GLib, self.async_.Secret
        require(os.getuid() == UID)
        selection = _read_selection()
        self.path = selection['collection_ref']
        # An explicit existing local user bus avoids inheriting a remote bus or
        # having libsecret launch a second daemon after an SSH terminal closes.
        info = os.stat('/run/user/1000', follow_symlinks=False)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == UID
                and stat.S_IMODE(info.st_mode) == 0o700)
        info = os.stat('/run/user/1000/bus', follow_symlinks=False)
        require(stat.S_ISSOCK(info.st_mode) and info.st_uid == UID)
        self.connection = self.async_.call(lambda cancel, done: self.Gio.DBusConnection.new_for_address(
            'unix:path=/run/user/1000/bus', self.Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
            self.Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, cancel, done, None),
            lambda source, result: self.Gio.DBusConnection.new_for_address_finish(result))
        owner = self._dbus('org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'GetNameOwner', self.GLib.Variant('(s)', ('org.freedesktop.secrets',)))
        self.owner = owner.unpack()[0]
        require(self.owner.startswith(':'))
        # The unique bus name and DO_NOT_AUTO_START also close the race where
        # the existing provider exits between NameHasOwner and proxy creation.
        self.service = self.Secret.Service(g_connection=self.connection, g_name=self.owner,
            g_object_path='/org/freedesktop/secrets',
            g_flags=self.Gio.DBusProxyFlags.DO_NOT_AUTO_START, flags=self.Secret.ServiceFlags.NONE)
        self._initialize(self.service)
        self.collection = self.Secret.Collection(service=self.service,
            g_connection=self.connection, g_name=self.owner, g_object_path=self.path,
            g_flags=self.Gio.DBusProxyFlags.DO_NOT_AUTO_START, flags=self.Secret.CollectionFlags.NONE)
        self._initialize(self.collection)
        require(self.collection.get_label() == 'Culture Cabinet')

    def _initialize(self, proxy):
        self.async_.call(lambda cancel, done: proxy.init_async(0, cancel, done, None),
                         lambda source, result: source.init_finish(result))

    def _dbus(self, bus_name, path, interface, method, parameters):
        return self.async_.call(lambda cancel, done: self.connection.call(bus_name, path, interface,
            method, parameters, None, self.Gio.DBusCallFlags.NO_AUTO_START,
            max(1, int((self.async_.deadline - time.monotonic()) * 1000)), cancel, done, None),
            lambda source, result: source.call_finish(result))

    def _unlocked(self):
        result = self._dbus(self.owner, self.path, 'org.freedesktop.DBus.Properties', 'Get',
                           self.GLib.Variant('(ss)', ('org.freedesktop.Secret.Collection', 'Locked')))
        require(result.unpack()[0] is False, 'LOCKED')

    @staticmethod
    def _attrs(kind, binding_digest, slot):
        require(slot in ('record', 'receipt', 'master'))
        return {'application': 'culture-cabinet', 'schema': 'cabinet-key-store/' + slot + '-v1',
                'kind': kind, 'binding': binding_digest}

    def _item(self, kind, binding_digest, slot):
        self._unlocked()
        attrs = self._attrs(kind, binding_digest, slot)
        items = self.async_.call(lambda cancel, done: self.collection.search(None, attrs,
            self.Secret.SearchFlags.ALL, cancel, done, None),
            lambda source, result: source.search_finish(result))
        require(len(items) <= 1, 'CORRUPT')
        if not items:
            return None
        item = items[0]
        require(item.get_attributes() == attrs, 'CORRUPT')
        require(not item.get_locked(), 'LOCKED')
        return item

    def available(self):
        self._unlocked()
        require(self._item('master', '0' * 64, 'master') is not None)

    def exists(self, kind, binding_digest, slot):
        return self._item(kind, binding_digest, slot) is not None

    def get(self, kind, binding_digest, slot):
        item = self._item(kind, binding_digest, slot)
        if item is None:
            return None
        self.async_.call(lambda cancel, done: item.load_secret(cancel, done, None),
                         lambda source, result: source.load_secret_finish(result))
        self._unlocked()
        value = item.get_secret()
        require(value is not None, 'LOCKED')
        text = value.get_text()
        require(type(text) is str, 'CORRUPT')
        try:
            return text.encode('ascii')
        except UnicodeError:
            raise Refused('CORRUPT') from None

    def master_key(self):
        key = self.get('master', '0' * 64, 'master')
        # Ordinary operations must never initialize or replace a missing key.
        require(key is not None)
        return key

    def set(self, kind, binding_digest, slot, value):
        require(slot in ('record', 'receipt') and type(value) is bytes)
        item = self._item(kind, binding_digest, slot)
        secret = self.Secret.Value.new(value.decode('ascii'), -1, 'text/plain')
        if item is None:
            self.async_.call(lambda cancel, done: self.Secret.Item.create(self.collection, None,
                self._attrs(kind, binding_digest, slot), 'Culture Cabinet protected record', secret,
                self.Secret.ItemCreateFlags.NONE, cancel, done, None),
                lambda source, result: self.Secret.Item.create_finish(result))
        else:
            self.async_.call(lambda cancel, done: item.set_secret(secret, cancel, done, None),
                             lambda source, result: source.set_secret_finish(result))
        self._unlocked()

    def delete(self, kind, binding_digest, slot):
        require(slot == 'record')
        item = self._item(kind, binding_digest, slot)
        require(item is not None, 'MISSING')
        self.async_.call(lambda cancel, done: item.delete(cancel, done, None),
                         lambda source, result: source.delete_finish(result))
        self._unlocked()


def _pipe_read(fd, deadline):
    info = os.fstat(fd)
    require(stat.S_ISFIFO(info.st_mode) and info.st_uid == UID and fd > 2)
    os.set_blocking(fd, False)
    raw = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0)
            for _ in selector.select(remaining):
                piece = os.read(fd, 8192)
                if not piece:
                    return decode_frame(bytes(raw))
                raw.extend(piece)
                require(len(raw) <= MAX_FRAME + 4)
                if len(raw) >= 4:
                    length, = struct.unpack('!I', raw[:4])
                    require(0 < length <= MAX_FRAME and len(raw) <= length + 4)


def _pipe_write(fd, value, deadline):
    info = os.fstat(fd)
    require(stat.S_ISFIFO(info.st_mode) and info.st_uid == UID and fd > 2)
    os.set_blocking(fd, False)
    raw, offset = encode_frame(value), 0
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while offset < len(raw):
            remaining = deadline - time.monotonic()
            require(remaining > 0)
            for _ in selector.select(remaining):
                offset += os.write(fd, raw[offset:])


def _main(argv):
    # No argparse/traceback can accidentally echo input: argv contains only pipe
    # numbers. Malformed input returns no plaintext diagnostics on standard fds.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    require(os.getuid() == UID and len(argv) == 5 and argv[1] == '--request-fd'
            and argv[3] == '--response-fd')
    request_fd, response_fd = int(argv[2]), int(argv[4])
    require(request_fd != response_fd)
    deadline = time.monotonic() + TIMEOUT
    request = _pipe_read(request_fd, deadline)
    os.close(request_fd)
    validate_request(request)
    try:
        with mutation_lock(deadline):
            result = Store(_SecretBackend(deadline)).execute(request)
    except Refused as error:
        result = response(request, error.status)
    except Exception:
        # An unforeseen provider failure during mutation is not evidence of
        # non-commit. Keep the pending native intent and reconcile later.
        status = 'UNKNOWN' if request['operation'] in ('put', 'delete', 'reconcile') else 'UNAVAILABLE'
        result = response(request, status)
    _pipe_write(response_fd, result, deadline)
    os.close(response_fd)


if __name__ == '__main__':
    try:
        _main(sys.argv)
    except BaseException:
        os._exit(1)
