"""Private, unverified entry storage. No runtime reader or release authority.

The lock coordinates cooperating writers; inode checks are not hostile-owner CAS.
Unlink is neither erasure nor provider revocation. Deployment must separately
establish stopped service, mount isolation, and audit/backup exclusions.
"""
import fcntl
import json
import os
import re
import stat
import uuid
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

STORE_DIRECTORY = '/home/tang/.local/state/culture-cabinet-secrets/hardcover-backend'
OWNER_UID = 1000
OWNER_GID = 1000
SCOPES = ('read:catalog:search', 'read:catalog:data', 'read:library', 'write:library')
# A local token-type/application-use contract, never an OAuth scope grant.
TMDB_SCOPES = ('tmdb-application-read-access',)
# Local application purpose only, never a provider OAuth scope.
LIBRARYTHING_SCOPES = ('read-multirecommendations',)


@dataclass(frozen=True)
class ProviderContext:
    provider: str
    purpose: str
    directory: str
    scopes: tuple


_HARDCOVER = ProviderContext('hardcover', 'culture-cabinet-hardcover-backend', STORE_DIRECTORY, SCOPES)
_TMDB = ProviderContext('tmdb', 'culture-cabinet-tmdb-backend',
                        '/home/tang/.local/state/culture-cabinet-secrets/tmdb-backend', TMDB_SCOPES)

_LIBRARYTHING = ProviderContext('librarything', 'culture-cabinet-librarything-backend',
    '/home/tang/.local/state/culture-cabinet-secrets/librarything-backend', LIBRARYTHING_SCOPES)


HYDRA_SCOPES = ('hydra-external-search',)
SAB_SCOPES = ('sab-addfile-only',)
_HYDRA = ProviderContext('hydra', 'culture-cabinet-hydra-search',
    '/home/tang/.local/state/culture-cabinet-secrets/hydra-search', HYDRA_SCOPES)
_SAB = ProviderContext('sab', 'culture-cabinet-sab-add-only',
    '/home/tang/.local/state/culture-cabinet-secrets/sab-add-only', SAB_SCOPES)


def _provider_context(provider):
    if type(provider) is str:
        if provider == 'hydra':
            return _HYDRA
        if provider == 'sab':
            return _SAB
        if provider == 'hardcover':
            return _HARDCOVER
        if provider == 'tmdb':
            return _TMDB
        if provider == 'librarything':
            return _LIBRARYTHING
    raise Refused('invalid_input')


RECORD = 'credential.json'
LOCK = 'control.lock'
PENDING = 'transaction.pending'
MAX_RECORD = 8192
_TOKEN = re.compile(r'[A-Za-z0-9._~+/=\-]{1,4096}', re.ASCII)
_TEMP = re.compile(r'\.entry-[0-9a-f]{32}\.tmp', re.ASCII)
_OPERATIONS = frozenset(('enter', 'replace', 'remove', 'reconcile'))


class Refused(Exception):
    """Fixed diagnostics only; never carry a private value or raw OS error."""
    def __init__(self, reason='unsafe_store'):
        self.reason = reason
        super().__init__(reason)


class _Private:
    __slots__ = ()

    def __repr__(self):
        return '<private credential state>'

    def __reduce_ex__(self, protocol):
        raise TypeError('private credential state')


@dataclass(frozen=True, repr=False)
class Binding(_Private):
    account_id: str
    actor_id: str
    profile_id: str
    provider: str = 'hardcover'
    purpose: str = 'culture-cabinet-hardcover-backend'


@dataclass(frozen=True, repr=False)
class EntryMetadata(_Private):
    binding: Binding
    entered_at: datetime
    expires_at: datetime
    declared_provider_expires_at: datetime
    scopes: tuple = SCOPES


@dataclass(frozen=True)
class EntryReceipt:
    operation: str
    outcome: str
    reason: str

    def __post_init__(self):
        if (self.operation not in _OPERATIONS or self.outcome not in
                {'stored', 'removed', 'reconciled', 'refused', 'commit_unknown', 'durable_cleanup_unknown'}
                or self.reason not in {'complete', 'invalid_input', 'unsafe_store', 'dirty_store',
                                      'conflict', 'busy', 'expired', 'io_failure', 'interrupted'}):
            raise ValueError('invalid receipt')


@dataclass(frozen=True, repr=False)
class StoreIdentity(_Private):
    values: tuple


@dataclass(repr=False)
class PrivateRecord(_Private):
    metadata: EntryMetadata
    secret: bytearray
    generation: str
    identity: StoreIdentity

    @property
    def verified(self):
        return False

    def close(self):
        self.secret[:] = b'\0' * len(self.secret)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value, re.ASCII):
        raise Refused('invalid_input')
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        raise Refused('invalid_input') from None


def _time_text(value):
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


def validate_binding(binding, *, provider='hardcover'):
    context = _provider_context(provider)
    if type(binding) is not Binding or binding.provider != context.provider or binding.purpose != context.purpose:
        raise Refused('invalid_input')
    if not isinstance(binding.account_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', binding.account_id, re.ASCII):
        raise Refused('invalid_input')
    for value in (binding.actor_id, binding.profile_id):
        if (type(value) is not str or not re.fullmatch(r'[0-9]{1,18}', value, re.ASCII)
                or not 0 < int(value) <= 999999999999999999):
            raise Refused('invalid_input')


def validate_metadata(metadata, *, now, allow_expired=False, provider='hardcover'):
    if type(metadata) is not EntryMetadata:
        raise Refused('invalid_input')
    validate_binding(metadata.binding, provider=provider)
    if type(metadata.scopes) is not tuple or metadata.scopes != _provider_context(provider).scopes:
        raise Refused('invalid_input')
    for value in (metadata.entered_at, metadata.expires_at, metadata.declared_provider_expires_at):
        if type(value) is not datetime or value.tzinfo != timezone.utc or value.microsecond:
            raise Refused('invalid_input')
    if not (metadata.entered_at <= now and metadata.entered_at < metadata.expires_at
            <= metadata.entered_at + timedelta(days=89 if provider in ('tmdb', 'librarything') else 90)
            and metadata.expires_at <= metadata.declared_provider_expires_at):
        raise Refused('invalid_input')
    if not allow_expired and metadata.expires_at <= now:
        raise Refused('expired')


def _secret_text(secret, *, provider='hardcover'):
    if type(secret) is not bytearray or not 1 <= len(secret) <= 4096:
        raise Refused('invalid_input')
    if provider in ('hydra', 'sab'):
        try:
            value = secret.decode('utf-8')
        except UnicodeError:
            raise Refused('invalid_input') from None
        if (not value.strip() or any(unicodedata.category(c) == 'Cc' for c in value)
                or value.casefold() in {'changeme', 'change_me', 'replace_me',
                    'your_api_key', 'your-api-key', '<api_key>', '<apikey>', '${api_key}',
                    'redacted', '********', '__unchanged__', '***unchanged***'}
                or value.startswith('{OBF}')):
            raise Refused('invalid_input')
        return value
    try:
        value = secret.decode('ascii')
    except UnicodeError:
        raise Refused('invalid_input') from None
    if not _TOKEN.fullmatch(value):
        raise Refused('invalid_input')
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused('invalid_input')
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Refused('invalid_input')))
    except (ValueError, UnicodeError, RecursionError):
        raise Refused('invalid_input') from None


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise Refused('invalid_input')


def _encode(metadata, secret):
    binding = metadata.binding
    body = {'schema': 1, 'generation': uuid.uuid4().hex,
            'binding': {'provider': binding.provider, 'purpose': binding.purpose,
                        'account_id': binding.account_id, 'actor_id': binding.actor_id,
                        'profile_id': binding.profile_id},
            'scopes': list(metadata.scopes), 'entered_at': _time_text(metadata.entered_at),
            'expires_at': _time_text(metadata.expires_at),
            'declared_provider_expires_at': _time_text(metadata.declared_provider_expires_at),
            'token': _secret_text(secret, provider=binding.provider)}
    raw = json.dumps(body, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    if len(raw) > MAX_RECORD:
        raise Refused('invalid_input')
    return raw


def _decode(raw, identity, *, allow_expired=False, provider='hardcover'):
    body = _json(raw)
    _keys(body, ('schema', 'generation', 'binding', 'scopes', 'entered_at', 'expires_at',
                 'declared_provider_expires_at', 'token'))
    _keys(body['binding'], ('provider', 'purpose', 'account_id', 'actor_id', 'profile_id'))
    if type(body['schema']) is not int or body['schema'] != 1:
        raise Refused('invalid_input')
    generation = body['generation']
    if not isinstance(generation, str) or not re.fullmatch(r'[0-9a-f]{32}', generation):
        raise Refused('invalid_input')
    if uuid.UUID(hex=generation).version != 4 or uuid.UUID(hex=generation).variant != uuid.RFC_4122:
        raise Refused('invalid_input')
    if type(body['scopes']) is not list or type(body['token']) is not str:
        raise Refused('invalid_input')
    metadata = EntryMetadata(Binding(**body['binding']), parse_time(body['entered_at']),
                             parse_time(body['expires_at']), parse_time(body['declared_provider_expires_at']),
                             tuple(body['scopes']))
    validate_metadata(metadata, now=utc_now(), allow_expired=allow_expired, provider=provider)
    try:
        secret = bytearray(body['token'], 'utf-8' if provider in ('hydra', 'sab') else 'ascii')
    except UnicodeError:
        raise Refused('invalid_input') from None
    _secret_text(secret, provider=provider)
    return PrivateRecord(metadata, secret, generation, identity)


def _identity(info):
    return StoreIdentity((info.st_dev, info.st_ino, info.st_uid, info.st_gid,
                          info.st_mode, info.st_nlink, info.st_size))


def _inode(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode, info.st_nlink)


def _no_acl(fd):
    if any(name in ('system.posix_acl_access', 'system.posix_acl_default') for name in os.listxattr(fd)):
        raise Refused()


def _mount(fd):
    with open('/proc/self/fdinfo/' + str(fd), encoding='ascii') as stream:
        for line in stream:
            if line.startswith('mnt_id:'):
                return int(line.split()[1])
    raise Refused()


def _directory(fd, uid, gid, private):
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_gid != gid
            or (mode != 0o700 if private else bool(mode & 0o022))):
        raise Refused()
    _no_acl(fd)
    return info


class StoreHandle(_Private):
    def __init__(self, fd, uid, gid, chain, *, provider='hardcover'):
        self._context = _provider_context(provider)
        self.fd, self.uid, self.gid, self._chain = fd, uid, gid, chain
        self._lock = None
        self._directories = {descriptor: _inode(os.fstat(descriptor))[:-1]
                             for parent, _, child in chain for descriptor in (parent, child)}

    @property
    def context(self):
        return self._context

    def close(self):
        if self._lock is not None:
            self._lock.close()
        for fd, _, _ in reversed(self._chain):
            os.close(fd)
        self._chain = []
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _check(self):
        if self.fd < 0:
            raise Refused()
        for descriptor, expected in self._directories.items():
            if _inode(os.fstat(descriptor))[:-1] != expected:
                raise Refused()
        for parent, name, child in self._chain:
            _no_acl(parent)
            _no_acl(child)
            if _inode(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _inode(os.fstat(child)):
                raise Refused()
        _directory(self.fd, self.uid, self.gid, True)
        if self._lock is None or self._lock.fd < 0:
            raise Refused('busy')
        self._lock._check()
        names = _names(self)
        if len(names) > 4 or any(name not in (RECORD, LOCK, PENDING) and not _TEMP.fullmatch(name) for name in names):
            raise Refused()
        if sum(bool(_TEMP.fullmatch(name)) for name in names) > 1:
            raise Refused()
        return names


class LockHandle(_Private):
    def __init__(self, store, fd):
        self.store, self.fd = store, fd

    def _check(self):
        _validate_file(self.store, self.fd, LOCK, 0)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.store._lock is self:
            self.store._lock = None


def _names(store):
    # A fresh file description avoids reusing enumeration offsets after a
    # transaction mutates this directory. Bound enumeration before allocation.
    fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=store.fd)
    try:
        if _inode(os.fstat(fd)) != _inode(os.fstat(store.fd)):
            raise Refused()
        names = []
        with os.scandir(fd) as entries:
            for entry in entries:
                if len(names) == 4:
                    raise Refused()
                names.append(entry.name)
        return names
    finally:
        os.close(fd)


def _walk(parts, contracts, *, provider='hardcover'):
    _provider_context(provider)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    chain = []
    opened = [fd]
    try:
        anchor = _directory(fd, 0, 0, False)
        mount = _mount(fd)
        with open('/proc/self/mountinfo', encoding='ascii') as stream:
            mounts = [line.split() for line in stream]
        if not any(int(fields[0]) == mount and fields[fields.index('-') + 1] in ('btrfs', 'ext4', 'xfs') for fields in mounts):
            raise Refused()
        # A standard root-owned /home subvolume may use a separate local mount.
        # Retain its checked descriptor and anchor the remaining fixed walk to
        # its device and mount, rejecting later transitions including bind mounts.
        for name, (uid, gid, private) in zip(parts, contracts, strict=True):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
            opened.append(child)
            chain.append((fd, name, child))
            info = _directory(child, uid, gid, private)
            child_mount = _mount(child)
            if len(chain) == 1 and name == 'home':
                if not any(int(fields[0]) == child_mount and fields[fields.index('-') + 1] in ('btrfs', 'ext4', 'xfs') for fields in mounts):
                    raise Refused()
                anchor, mount = info, child_mount
            elif info.st_dev != anchor.st_dev or child_mount != mount:
                raise Refused()
            if _inode(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _inode(info):
                raise Refused()
            fd = child
        return StoreHandle(fd, OWNER_UID, OWNER_GID, chain, provider=provider)
    except BaseException:
        for descriptor in reversed(opened):
            os.close(descriptor)
        raise


def open_store(*, provider='hardcover'):
    """Fixed provisioned path only. Never creates or chmods any ancestor."""
    context = _provider_context(provider)
    if os.getuid() != OWNER_UID or os.geteuid() != OWNER_UID or OWNER_UID == 0:
        raise Refused()
    parts = context.directory.strip('/').split('/')
    return _walk(parts, [(0, 0, False), (OWNER_UID, OWNER_GID, False),
                         (OWNER_UID, OWNER_GID, False), (OWNER_UID, OWNER_GID, False),
                         (OWNER_UID, OWNER_GID, True), (OWNER_UID, OWNER_GID, True)], provider=provider)


def _fixture_store(directory, *, provider='hardcover'):
    """Internal owned-fake constructor; unreachable from CLI or environment."""
    _provider_context(provider)
    parent_path, name = os.path.split(os.fspath(directory))
    parent = os.open(parent_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fd = None
    try:
        _directory(parent, os.getuid(), os.getgid(), True)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        _directory(fd, os.getuid(), os.getgid(), True)
        if _mount(fd) != _mount(parent):
            raise Refused()
        return StoreHandle(fd, os.getuid(), os.getgid(), [(parent, name, fd)], provider=provider)
    except BaseException:
        if fd is not None:
            os.close(fd)
        os.close(parent)
        raise


def _validate_file(store, fd, name, cap):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != store.uid or info.st_gid != store.gid or info.st_nlink != 1
            or info.st_dev != os.fstat(store.fd).st_dev or info.st_size > cap
            or _mount(fd) != _mount(store.fd)):
        raise Refused()
    _no_acl(fd)
    if _identity(info) != _identity(os.stat(name, dir_fd=store.fd, follow_symlinks=False)):
        raise Refused()
    return _identity(info)


def _open_file(store, name, cap, flags=os.O_RDONLY):
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=store.fd)
    except OSError:
        raise Refused('unsafe_store') from None
    try:
        identity = _validate_file(store, fd, name, cap)
        return fd, identity
    except BaseException:
        os.close(fd)
        raise


def lock_store(store, *, exclusive=True):
    if not exclusive or store._lock is not None:
        raise Refused('busy')
    fd, _ = _open_file(store, LOCK, 0, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _validate_file(store, fd, LOCK, 0)
    except BlockingIOError:
        os.close(fd)
        raise Refused('busy') from None
    except BaseException:
        os.close(fd)
        raise
    store._lock = LockHandle(store, fd)
    return store._lock


def _read_file(store, name, cap):
    fd, identity = _open_file(store, name, cap)
    try:
        raw = os.read(fd, cap + 1)
        if len(raw) > cap or _validate_file(store, fd, name, cap) != identity or len(raw) != identity.values[-1]:
            raise Refused()
        return raw, identity
    finally:
        os.close(fd)


def _match_file(store, name, cap, expected):
    fd, identity = _open_file(store, name, cap)
    try:
        if identity != expected:
            raise Refused('conflict')
    finally:
        os.close(fd)


def _clean(store):
    names = store._check()
    if PENDING in names or any(_TEMP.fullmatch(name) for name in names):
        raise Refused('dirty_store')
    return names


def _read_record(store, *, allow_expired=False):
    raw, identity = _read_file(store, RECORD, MAX_RECORD)
    return _decode(raw, identity, allow_expired=allow_expired, provider=store.context.provider)


def read_record(store):
    _clean(store)
    return _read_record(store)


def _create(store, name, raw):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=store.fd)
    try:
        _validate_file(store, fd, name, MAX_RECORD)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('short write')
            view = view[written:]
        _validate_file(store, fd, name, MAX_RECORD)
        os.fsync(fd)
        return _validate_file(store, fd, name, MAX_RECORD)
    finally:
        os.close(fd)


def _marker(store, operation):
    identity = _create(store, PENDING, json.dumps({'schema': 1, 'operation': operation}, separators=(',', ':')).encode('ascii'))
    os.fsync(store.fd)
    return identity


def _finish(store, expected):
    store._check()
    _match_file(store, PENDING, 128, expected)
    os.unlink(PENDING, dir_fd=store.fd)
    os.fsync(store.fd)


def _receipt(operation, durable, error):
    if durable:
        return EntryReceipt(operation, 'durable_cleanup_unknown', 'io_failure')
    return EntryReceipt(operation, 'commit_unknown', 'interrupted' if not isinstance(error, OSError) else 'io_failure')


def commit_entry(store, metadata, secret, *, expected=None):
    operation = 'enter' if expected is None else 'replace'
    durable = started = False
    old = None
    try:
        _clean(store)
        now = utc_now()
        # entered_at is a local assertion, never imported from a token or caller.
        if type(metadata) is not EntryMetadata:
            raise Refused('invalid_input')
        metadata = EntryMetadata(metadata.binding, now, metadata.expires_at,
                                 metadata.declared_provider_expires_at, metadata.scopes)
        validate_metadata(metadata, now=now, provider=store.context.provider)
        raw = _encode(metadata, secret)
        if expected is None:
            if RECORD in _names(store):
                raise Refused('conflict')
        else:
            old = _read_record(store, allow_expired=True)
            if old.identity != expected or old.metadata.binding != metadata.binding:
                raise Refused('conflict')
        started = True
        marker_identity = _marker(store, operation)
        temporary = '.entry-' + uuid.uuid4().hex + '.tmp'
        temporary_identity = _create(store, temporary, raw)
        store._check()
        _match_file(store, temporary, MAX_RECORD, temporary_identity)
        _match_file(store, PENDING, 128, marker_identity)
        if expected is not None:
            _, current = _read_file(store, RECORD, MAX_RECORD)
            if current != expected:
                raise Refused('conflict')
        elif RECORD in _names(store):
            raise Refused('conflict')
        os.replace(temporary, RECORD, src_dir_fd=store.fd, dst_dir_fd=store.fd)
        os.fsync(store.fd)
        durable = True
        _finish(store, marker_identity)
        return EntryReceipt(operation, 'stored', 'complete')
    except Refused as error:
        return _receipt(operation, durable, error) if started else EntryReceipt(operation, 'refused', error.reason)
    except (OSError, KeyboardInterrupt) as error:
        return _receipt(operation, durable, error) if started else EntryReceipt(operation, 'refused', 'io_failure')
    finally:
        if old is not None:
            old.close()


def remove_entry(store, *, expected):
    durable = started = False
    try:
        _clean(store)
        _, identity = _read_file(store, RECORD, MAX_RECORD)
        if identity != expected:
            raise Refused('conflict')
        started = True
        marker_identity = _marker(store, 'remove')
        store._check()
        _match_file(store, PENDING, 128, marker_identity)
        _match_file(store, RECORD, MAX_RECORD, expected)
        os.unlink(RECORD, dir_fd=store.fd)
        os.fsync(store.fd)
        durable = True
        _finish(store, marker_identity)
        return EntryReceipt('remove', 'removed', 'complete')
    except Refused as error:
        return _receipt('remove', durable, error) if started else EntryReceipt('remove', 'refused', error.reason)
    except (OSError, KeyboardInterrupt) as error:
        return _receipt('remove', durable, error) if started else EntryReceipt('remove', 'refused', 'io_failure')


def _discard_entry(store):
    """Retire local artifacts without reading credential or temporary contents.

    A recognized marker, even an interrupted prefix, is a blocker rather than
    a replay instruction. Sync it and its name before any removal. Never replace
    it: a failed write must not open a clean-looking gap in restart protection.
    Only explicit discard accepts incomplete artifacts; present/absent stay strict.
    """
    durable = started = False
    try:
        names = store._check()
        identities = {}
        for name in names:
            if name == LOCK:
                continue
            cap = 128 if name == PENDING else MAX_RECORD
            fd, identity = _open_file(store, name, cap)
            os.close(fd)
            identities[name] = identity
        if PENDING in names:
            raw, marker_identity = _read_file(store, PENDING, 128)
            # These are exactly the fixed writer's possible short-write prefixes,
            # including an empty newly-created marker. Arbitrary JSON is refused.
            markers = [json.dumps({'schema': 1, 'operation': operation}, separators=(',', ':')).encode('ascii')
                       for operation in (*_OPERATIONS, 'discard')]
            if not any(marker.startswith(raw) for marker in markers):
                raise Refused()
            if marker_identity != identities[PENDING]:
                raise Refused('conflict')
        if set(store._check()) != set(names):
            raise Refused('conflict')
        started = True
        if PENDING not in names:
            marker_identity = _marker(store, 'discard')
        else:
            fd, identity = _open_file(store, PENDING, 128)
            try:
                if identity != marker_identity:
                    raise Refused('conflict')
                os.fsync(fd)
                _match_file(store, PENDING, 128, marker_identity)
            finally:
                os.close(fd)
            os.fsync(store.fd)
        remaining = set(names) | {PENDING}
        for name in (RECORD, *(name for name in names if _TEMP.fullmatch(name))):
            if name not in identities:
                continue
            if set(store._check()) != remaining:
                raise Refused('conflict')
            _match_file(store, PENDING, 128, marker_identity)
            _match_file(store, name, MAX_RECORD, identities[name])
            os.unlink(name, dir_fd=store.fd)
            remaining.remove(name)
        if set(store._check()) != {LOCK, PENDING}:
            raise Refused('conflict')
        os.fsync(store.fd)
        durable = True
        _finish(store, marker_identity)
        return EntryReceipt('reconcile', 'reconciled', 'complete')
    except Refused as error:
        return _receipt('reconcile', durable, error) if started else EntryReceipt('reconcile', 'refused', error.reason)
    except (OSError, KeyboardInterrupt) as error:
        return _receipt('reconcile', durable, error) if started else EntryReceipt('reconcile', 'refused', 'io_failure')


def reconcile_entry(store, *, intended, metadata=None, secret=None):
    """Explicit private confirmation, never automatic adoption of dirty state.

    Present requires re-entry of binding, token and expiry. Absent only confirms
    an already absent record. Discard explicitly retires bounded local artifacts
    without a token; it neither revokes at the provider nor erases copies.
    """
    if intended == 'discard':
        return _discard_entry(store)
    durable = started = False
    records = []
    try:
        names = store._check()
        if intended not in ('present', 'absent'):
            raise Refused('invalid_input')
        temporary = next((name for name in names if _TEMP.fullmatch(name)), None)
        if PENDING in names:
            raw, marker_identity = _read_file(store, PENDING, 128)
            marker = _json(raw)
            _keys(marker, ('schema', 'operation'))
            if type(marker['schema']) is not int or marker['schema'] != 1 or marker['operation'] not in (*_OPERATIONS, 'discard'):
                raise Refused()
            operation = marker['operation']
            if ((operation in ('remove', 'reconcile') and temporary)
                    or (operation == 'discard' and (temporary or RECORD in names))
                    or (operation == 'enter' and temporary and RECORD in names)
                    or (operation == 'replace' and RECORD not in names)):
                raise Refused('conflict')
        elif temporary:
            raise Refused('conflict')
        if intended == 'present':
            validate_metadata(metadata, now=utc_now(), allow_expired=True, provider=store.context.provider)
            _secret_text(secret, provider=store.context.provider)
            record = _read_record(store, allow_expired=True)
            records.append(record)
            if (record.metadata.binding != metadata.binding or record.secret != secret
                    or record.metadata.expires_at != metadata.expires_at
                    or record.metadata.declared_provider_expires_at != metadata.declared_provider_expires_at):
                raise Refused('conflict')
        elif RECORD in names:
            raise Refused('conflict')
        if temporary:
            raw, identity = _read_file(store, temporary, MAX_RECORD)
            pending = _decode(raw, identity, allow_expired=True, provider=store.context.provider)
            records.append(pending)
            if intended == 'present' and pending.metadata.binding != metadata.binding:
                raise Refused('conflict')
        store._check()
        started = True
        if PENDING not in names:
            marker_identity = _marker(store, 'reconcile')
        _match_file(store, PENDING, 128, marker_identity)
        if intended == 'present':
            fd, identity = _open_file(store, RECORD, MAX_RECORD)
            try:
                if identity != records[0].identity:
                    raise Refused('conflict')
                os.fsync(fd)
                _match_file(store, RECORD, MAX_RECORD, identity)
            finally:
                os.close(fd)
        if temporary:
            _match_file(store, temporary, MAX_RECORD, pending.identity)
            os.unlink(temporary, dir_fd=store.fd)
        os.fsync(store.fd)
        durable = True
        _finish(store, marker_identity)
        return EntryReceipt('reconcile', 'reconciled', 'complete')
    except Refused as error:
        return _receipt('reconcile', durable, error) if started else EntryReceipt('reconcile', 'refused', error.reason)
    except (OSError, KeyboardInterrupt) as error:
        return _receipt('reconcile', durable, error) if started else EntryReceipt('reconcile', 'refused', 'io_failure')
    finally:
        for record in records:
            record.close()
