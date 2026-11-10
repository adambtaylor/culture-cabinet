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
KEYRING_DIRECTORY = '/home/tang/.local/share/keyrings'
ENCRYPTED_PREFIX = bytes.fromhex('476e6f6d654b657972696e670a0d000a00000000')
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


def _protect_process():
    """Private worker only: refuse secrets unless native crashes cannot dump it.

    Linux can route cores to a pipe collector independently of RLIMIT_CORE.
    PR_SET_DUMPABLE=0 closes that path before GI/Fernet/request bytes are loaded;
    this changes only this process, never global coredump or system settings.
    """
    import ctypes
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
    prctl.restype = ctypes.c_int
    require(prctl(4, 0, 0, 0, 0) == 0 and prctl(3, 0, 0, 0, 0) == 0)
    require(resource.getrlimit(resource.RLIMIT_CORE) == (0, 0))


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
    request = validate_request(strict_json(canonical(request)))
    require(type(timeout) in (int, float) and 0 < timeout <= TIMEOUT)
    process = None
    fds = []
    deadline = time.monotonic() + timeout
    failure = 'UNKNOWN' if request['operation'] in ('put', 'delete', 'reconcile') else 'UNAVAILABLE'
    try:
        require(os.getuid() == UID)
        request_read, request_write = os.pipe()
        fds.extend((request_read, request_write))
        response_read, response_write = os.pipe()
        fds.extend((response_read, response_write))
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
        if result['status'] == 'AVAILABLE' and request['operation'] != 'availability':
            require(result['revision'] is not None)
        if request['operation'] == 'read' and result['status'] == 'AVAILABLE':
            require(result['record'] is not None and result['revision'] == request['expected_revision'])
        if result['status'] == 'REMOVED':
            require(request['operation'] in ('delete', 'reconcile') and result['revision'] is not None)
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
            try:
                self._write(kind, binding_digest, 'receipt', cipher, {**receipt, 'status': state})
            except (Refused, OSError):
                return response(request, 'UNKNOWN')
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


def _selection_shape(value, *, allow_pending=False):
    _shape(value, ('schema', 'identifier', 'collection_ref', 'keyring_directory', 'provisioning', 'state'))
    require(value['schema'] == 'cabinet-key-store-selection/v1')
    require(value['state'] == 'ACTIVE' or allow_pending and value['state'] == 'PROVISIONING')
    identifier = value['identifier']
    require(type(identifier) is str and re.fullmatch('CultureCabinet[0-9a-f]{32}', identifier) is not None)
    require(value['collection_ref'] == '/org/freedesktop/secrets/collection/' + identifier)
    require(value['keyring_directory'] == KEYRING_DIRECTORY)
    proof = value['provisioning']
    _shape(proof, ('operation_id', 'service_owner', 'service_uid', 'implementation', 'absent_before_creation'))
    _uuid(proof['operation_id'])
    require(type(proof['service_uid']) is int and proof['service_uid'] == UID
            and proof['implementation'] == 'gnome-keyring-51.1'
            and proof['absent_before_creation'] is True)
    require(type(proof['service_owner']) is str and re.fullmatch(r':[0-9]+\.[0-9]+', proof['service_owner']) is not None)


def _open_checked_directory(path):
    """Walk fixed host paths without following any directory symlink."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.strip('/').split('/'):
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = new
            info = os.fstat(fd)
            require(info.st_uid in (0, UID) and not stat.S_IMODE(info.st_mode) & 0o022)
        require(os.fstat(fd).st_uid == UID)
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _probe_protection(selection):
    """Read exactly <=20 public format bytes of the one registered new file.

    The prefix establishes the supported GNOME51.1 nonempty-password writer
    branch, not password strength/full-file authenticity. A permanent marker or
    Locked=false is not protection evidence. Atomic replacement between separate
    observations is normal; replacement during this observation is refused.
    """
    _selection_shape(selection, allow_pending=True)
    directory = leaf = None
    try:
        directory = _open_checked_directory(KEYRING_DIRECTORY)
        name = selection['identifier'] + '.keyring'
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == UID and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == 0o600)
        leaf = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        opened = os.fstat(leaf)
        require(_file_identity(before) == _file_identity(opened))
        prefix = os.pread(leaf, 20, 0)
        after = os.fstat(leaf)
        path_after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        require(_file_identity(opened) == _file_identity(after) == _file_identity(path_after)
                and prefix == ENCRYPTED_PREFIX)
    except OSError:
        raise Refused() from None
    finally:
        if leaf is not None:
            os.close(leaf)
        if directory is not None:
            os.close(directory)


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
        _selection_shape(value)
        return value
    except (OSError, ValueError):
        raise Refused() from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _publish_selection(value, *, previous=None):
    """One nonsecret setup intent, then exact-CAS publication. No secret file.

    A cancelled/uncertain OS creation leaves PROVISIONING: ordinary operations
    refuse it, and another creation cannot silently adopt or replace it.
    """
    _selection_shape(value, allow_pending=True)
    parent = directory = temporary = None
    name = '.selection-' + uuid.uuid4().hex
    try:
        parent = _open_checked_directory('/home/tang/.local/state')
        try:
            os.mkdir('culture-cabinet-key-store', 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        directory = os.open('culture-cabinet-key-store', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        info = os.fstat(directory)
        require(info.st_uid == UID and stat.S_IMODE(info.st_mode) == 0o700)
        if previous is not None:
            current = os.stat('selection.json', dir_fd=directory, follow_symlinks=False)
            require(_file_identity(current) == previous)
        temporary = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        raw, offset = canonical(value), 0
        require(len(raw) <= 4096)
        while offset < len(raw):
            offset += os.write(temporary, raw[offset:])
        os.fsync(temporary)
        os.close(temporary); temporary = None
        if previous is None:
            # link() refuses an existing selection, including a symlink. This
            # is new provisioning, never an automatic migration/adoption.
            os.link(name, 'selection.json', src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            os.unlink(name, dir_fd=directory)
        else:
            require(_file_identity(os.stat('selection.json', dir_fd=directory,
                                          follow_symlinks=False)) == previous)
            os.replace(name, 'selection.json', src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        return _file_identity(os.stat('selection.json', dir_fd=directory, follow_symlinks=False))
    except OSError:
        raise Refused() from None
    finally:
        if temporary is not None:
            os.close(temporary)
        if directory is not None:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)
        if parent is not None:
            os.close(parent)


class _SecretBackend:
    """GI/libsecret adapter for exactly the host-selected application collection.

    No service/collection discovery, aliases, unlock flags or implicit creation.
    Metadata searches are confined to exact application selectors. Secret loads
    happen only for explicit read/mutation/reconciliation, never availability.
    """
    def __init__(self, deadline):
        self.selection = _read_selection()
        self._connect(deadline)
        self.path = self.selection['collection_ref']
        self._open_collection()

    def _connect(self, deadline):
        self.async_ = _Async(deadline)
        self.Gio, self.GLib, self.Secret = self.async_.Gio, self.async_.GLib, self.async_.Secret
        require(os.getuid() == UID)
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
        # libsecret's C constructor sets its own session-bus properties; passing
        # a ready g-connection then calling init_async aborts inside GDBusProxy.
        # Use its supported asynchronous factory instead. The installed client
        # resolves its bus-name setting during construction, so pin that
        # NONSECRET setting to the already observed unique owner. Unique names
        # cannot activate a daemon, including if this owner exits during open.
        # These two values are constructed here, never inherited or user input.
        os.environ['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=/run/user/1000/bus'
        os.environ['SECRET_SERVICE_BUS_NAME'] = self.owner
        self.service = self.async_.call(lambda cancel, done: self.Secret.Service.open(
            self.Secret.Service.__gtype__, self.owner, self.Secret.ServiceFlags.NONE,
            cancel, done, None), lambda source, result: self.Secret.Service.open_finish(result))
        require(self.service.get_name() == self.owner and self.service.get_name_owner() == self.owner
                and self.service.get_connection().get_guid() == self.connection.get_guid())
        self._verify_service()

    def _open_collection(self):
        self.collection = self.Secret.Collection(service=self.service,
            g_connection=self.connection, g_name=self.owner, g_object_path=self.path,
            g_flags=self.Gio.DBusProxyFlags.DO_NOT_AUTO_START, flags=self.Secret.CollectionFlags.NONE)
        self._initialize(self.collection)
        require(self.collection.get_label() == self.selection['identifier'])
        self._prove()

    def _verify_service(self):
        owner = self._dbus('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'GetNameOwner', self.GLib.Variant('(s)', ('org.freedesktop.secrets',))).unpack()[0]
        require(owner == self.owner)
        uid = self._dbus('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'GetConnectionUnixUser', self.GLib.Variant('(s)', (self.owner,))).unpack()[0]
        pid = self._dbus('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'GetConnectionUnixProcessID', self.GLib.Variant('(s)', (self.owner,))).unpack()[0]
        require(type(uid) is int and uid == UID and type(pid) is int and pid > 0)
        path = '/proc/' + str(pid) + '/exe'
        require(os.readlink(path) == '/usr/bin/gnome-keyring-daemon')
        running, installed = os.stat(path), os.stat('/usr/bin/gnome-keyring-daemon', follow_symlinks=False)
        require(stat.S_ISREG(installed.st_mode) and installed.st_uid == 0
                and not stat.S_IMODE(installed.st_mode) & 0o022
                and _file_identity(running) == _file_identity(installed))
        # Public package identity only, never credential content. A different
        # upstream format/version needs a new supported implementation decision.
        result = subprocess.run(['/usr/bin/pacman', '-Q', 'gnome-keyring'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=max(.001, self.async_.deadline - time.monotonic()), check=False,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        require(result.returncode == 0 and re.fullmatch(rb'gnome-keyring (?:[0-9]+:)?51\.1-[0-9]+\n', result.stdout) is not None)

    def _prove(self):
        self._verify_service()
        _probe_protection(self.selection)

    def _initialize(self, proxy):
        self.async_.call(lambda cancel, done: proxy.init_async(0, cancel, done, None),
                         lambda source, result: source.init_finish(result))

    def _dbus(self, bus_name, path, interface, method, parameters):
        return self.async_.call(lambda cancel, done: self.connection.call(bus_name, path, interface,
            method, parameters, None, self.Gio.DBusCallFlags.NO_AUTO_START,
            max(1, int((self.async_.deadline - time.monotonic()) * 1000)), cancel, done, None),
            lambda source, result: source.call_finish(result))

    def _unlocked(self):
        self._prove()
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
        self._prove()

    def exists(self, kind, binding_digest, slot):
        found = self._item(kind, binding_digest, slot) is not None
        self._prove()
        return found

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


def _provision_worker(operation_id, deadline):
    """Explicit host-only GNOME prompt; never read the collection password.

    Do not retry uncertain collection creation. Its nonsecret exact selection
    intent survives for the custodian, rather than losing which object we made.
    Master generation follows actual file-format proof, never prompt success.
    """
    _uuid(operation_id)
    require(os.getuid() == UID and os.isatty(0))
    with mutation_lock(deadline):
        try:
            os.stat(SELECTION, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            # Explicit host re-entry may unlock only the already registered,
            # protected ACTIVE selection. It cannot adopt an arbitrary existing
            # collection, finish an uncertain creation or regenerate a lost key.
            backend = _SecretBackend(deadline)
            backend._prove()
            backend.async_.call(lambda cancel, done: backend.service.unlock(
                [backend.collection], cancel, done, None),
                lambda source, result: source.unlock_finish(result))
            backend.available()
            Store(backend)._crypt()
            backend._prove()
            return
        backend = object.__new__(_SecretBackend)
        backend._connect(deadline)
        backend._verify_service()
        identifier = 'CultureCabinet' + uuid.uuid4().hex
        directory = _open_checked_directory(KEYRING_DIRECTORY)
        try:
            try:
                os.stat(identifier + '.keyring', dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise Refused()
        finally:
            os.close(directory)
        selection = {'schema': 'cabinet-key-store-selection/v1', 'state': 'PROVISIONING',
            'identifier': identifier, 'collection_ref': '/org/freedesktop/secrets/collection/' + identifier,
            'keyring_directory': KEYRING_DIRECTORY,
            'provisioning': {'operation_id': operation_id, 'service_owner': backend.owner,
                'service_uid': UID, 'implementation': 'gnome-keyring-51.1', 'absent_before_creation': True}}
        intent_identity = _publish_selection(selection)
        backend.selection, backend.path = selection, selection['collection_ref']
        collection = backend.async_.call(lambda cancel, done: backend.Secret.Collection.create(
            backend.service, identifier, None, backend.Secret.CollectionCreateFlags.NONE, cancel, done, None),
            lambda source, result: backend.Secret.Collection.create_finish(result))
        require(collection is not None and collection.get_object_path() == backend.path
                and collection.get_label() == identifier)
        backend.collection = collection
        canary = None
        directory = _open_checked_directory(KEYRING_DIRECTORY)
        try:
            try:
                os.stat(identifier + '.keyring', dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                # The only value permitted before protection proof is a fixed
                # public canary, solely to materialize this exact new file.
                attrs = {'application': 'culture-cabinet', 'schema': 'cabinet-key-store/canary-v1',
                         'kind': 'canary', 'binding': '0' * 64}
                value = backend.Secret.Value.new('Culture Cabinet public provisioning canary/v1', -1, 'text/plain')
                canary = backend.async_.call(lambda cancel, done: backend.Secret.Item.create(collection,
                    None, attrs, 'Culture Cabinet public provisioning canary', value,
                    backend.Secret.ItemCreateFlags.NONE, cancel, done, None),
                    lambda source, result: backend.Secret.Item.create_finish(result))
        finally:
            os.close(directory)
        backend._prove()
        backend._unlocked()
        require(backend._item('master', '0' * 64, 'master') is None)
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        value = backend.Secret.Value.new(key.decode('ascii'), -1, 'text/plain')
        backend.async_.call(lambda cancel, done: backend.Secret.Item.create(collection, None,
            backend._attrs('master', '0' * 64, 'master'), 'Culture Cabinet record encryption key', value,
            backend.Secret.ItemCreateFlags.NONE, cancel, done, None),
            lambda source, result: backend.Secret.Item.create_finish(result))
        backend._prove()
        require(backend.master_key() == key)
        if canary is not None:
            backend.async_.call(lambda cancel, done: canary.delete(cancel, done, None),
                               lambda source, result: source.delete_finish(result))
        backend._prove()
        _publish_selection({**selection, 'state': 'ACTIVE'}, previous=intent_identity)
        backend._prove()


def provision(*, timeout=120.0):
    """Trusted terminal custodian only; no normal web command can request this.

    GNOME alone collects the password. This function returns only status and the
    nonsecret selected reference; it never returns a collection/Fernet password.
    A pending setup intent is not implicitly retried or adopted.
    """
    require(os.isatty(0) and type(timeout) in (int, float) and 0 < timeout <= 120)
    operation_id = str(uuid.uuid4())
    result = {'status': 'UNAVAILABLE', 'collection_ref': None}
    read_fd, write_fd = os.pipe()
    process = None
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(['/usr/bin/python3', '-I', '-B', HELPER, '--provision',
            '--operation-id', operation_id, '--response-fd', str(write_fd)],
            stdin=0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
            pass_fds=(write_fd,), env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                                     'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'})
        os.close(write_fd); write_fd = None
        reply = _pipe_read(read_fd, deadline)
        _shape(reply, RESPONSE_FIELDS)
        require(reply['version'] == VERSION and reply['operation_id'] == operation_id
                and reply['status'] in STATUS and reply['revision'] is None and reply['record'] is None)
        process.wait(timeout=max(.001, deadline - time.monotonic()))
        require(process.returncode == 0)
        if reply['status'] == 'AVAILABLE':
            result = {'status': 'AVAILABLE', 'collection_ref': _read_selection()['collection_ref']}
        else:
            result['status'] = reply['status']
        return result
    except (Refused, OSError, ValueError, subprocess.SubprocessError):
        return result
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        if process is not None and process.poll() is None:
            process.kill(); process.wait()


def _check_pipe(fd, access):
    info = os.fstat(fd)
    require(stat.S_ISFIFO(info.st_mode) and info.st_uid == UID and fd > 2)
    require(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == access)


def _pipe_read(fd, deadline):
    _check_pipe(fd, os.O_RDONLY)
    os.set_blocking(fd, False)
    raw = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0)
            for _ in selector.select(remaining):
                try:
                    piece = os.read(fd, 8192)
                except BlockingIOError:
                    continue
                if not piece:
                    return decode_frame(bytes(raw))
                raw.extend(piece)
                require(len(raw) <= MAX_FRAME + 4)
                if len(raw) >= 4:
                    length, = struct.unpack('!I', raw[:4])
                    require(0 < length <= MAX_FRAME and len(raw) <= length + 4)


def _pipe_write(fd, value, deadline):
    _check_pipe(fd, os.O_WRONLY)
    os.set_blocking(fd, False)
    raw, offset = encode_frame(value), 0
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while offset < len(raw):
            remaining = deadline - time.monotonic()
            require(remaining > 0)
            for _ in selector.select(remaining):
                try:
                    offset += os.write(fd, raw[offset:])
                except BlockingIOError:
                    continue


def _main(argv):
    # No argparse/traceback can accidentally echo input: argv contains only pipe
    # numbers. Malformed input returns no plaintext diagnostics on standard fds.
    _protect_process()
    if len(argv) == 6 and argv[1:3] == ['--provision', '--operation-id'] and argv[4] == '--response-fd':
        operation_id, response_fd = _uuid(argv[3]), int(argv[5])
        _check_pipe(response_fd, os.O_WRONLY)
        deadline = time.monotonic() + 120
        try:
            _provision_worker(operation_id, deadline)
            status = 'AVAILABLE'
        except Exception:
            status = 'UNAVAILABLE'
        _pipe_write(response_fd, {'version': VERSION, 'operation_id': operation_id,
            'status': status, 'revision': None, 'record': None}, deadline)
        os.close(response_fd)
        return
    require(os.getuid() == UID and len(argv) == 5 and argv[1] == '--request-fd'
            and argv[3] == '--response-fd')
    request_fd, response_fd = int(argv[2]), int(argv[4])
    require(request_fd != response_fd)
    _check_pipe(request_fd, os.O_RDONLY); _check_pipe(response_fd, os.O_WRONLY)
    deadline = time.monotonic() + TIMEOUT
    request = _pipe_read(request_fd, deadline)
    os.close(request_fd)
    validate_request(request)
    try:
        with mutation_lock(deadline):
            backend = _SecretBackend(deadline)
            result = Store(backend).execute(request)
            if result['status'] in ('AVAILABLE', 'REMOVED'):
                try:
                    backend._prove()
                except Exception:
                    result = response(request, 'UNKNOWN' if request['operation'] in
                        ('put', 'delete', 'reconcile') else 'UNAVAILABLE')
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
