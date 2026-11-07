"""Standalone private human entry; no Django, provider, runtime or path option.

Real deployment additionally needs reviewed immutable code and an unrecorded
human terminal. Python/kernel copies are not erased; SIGKILL cannot restore TTY.
"""
import array
import fcntl
import os
import select
import signal
import stat
import sys
import termios
import time
import types
from contextlib import contextmanager
from datetime import timedelta

PYTHON = '/home/tang/Projects/culture-cabinet/.venv/bin/python'
ENTRY_SCRIPT = '/home/tang/Projects/culture-cabinet/backend/shelf/credential_entry.py'
_CODE_DIRECTORY = '/home/tang/Projects/culture-cabinet/backend/shelf'
_TIOCGSID = 0x5429  # Linux tty ioctl; not exported by every Python termios build.
_OWNER = 1000
_GROUP = 1000
_VERBS = ('enter', 'replace', 'remove', 'reconcile')
_SETUPS = ('setup-hardcover', 'setup-tmdb')
_LIBRARYTHING_SETUP = 'setup-librarything'
_SMOKES = ('smoke-hardcover', 'smoke-tmdb', 'smoke-librarything')
_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGTSTP)


class EntryFailure(Exception):
    pass


def _interrupted(signum, frame):
    raise KeyboardInterrupt()


def _code_identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _checked_code(fd, name, *, owner=_OWNER):
    # O freezes these pinned-owner 0444 bytes on read-only worker mounts;
    # owner validation is not a root-owned installation or a hostile-owner CAS.
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(source)
        named = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_gid != _GROUP or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o444 or info.st_size > 65536
                or _code_identity(info) != _code_identity(named)
                or any(n.startswith('system.posix_acl_') for n in os.listxattr(source))):
            raise EntryFailure()
        raw = os.read(source, 65537)
        after = os.fstat(source)
        if (len(raw) != info.st_size or _code_identity(after) != _code_identity(info)
                or _code_identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _code_identity(info)
                or any(n.startswith('system.posix_acl_') for n in os.listxattr(source))):
            raise EntryFailure()
        return raw
    finally:
        os.close(source)


def _deployment_store(*, smoke=False, librarything=False):
    # -I -S -B removes cwd, env imports and site hooks before this code runs.
    # The only application module is loaded from checked fixed bytes, not sys.path.
    if (sys.executable != PYTHON or os.path.abspath(__file__) != ENTRY_SCRIPT
            or not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode):
        raise EntryFailure()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    chain = []
    opened = [fd]
    try:
        root = os.fstat(fd)
        if (root.st_uid != 0 or root.st_mode & 0o022
                or any(n.startswith('system.posix_acl_') for n in os.listxattr(fd))):
            raise EntryFailure()
        for index, part in enumerate(_CODE_DIRECTORY.strip('/').split('/')):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            opened.append(child)
            info = os.fstat(child)
            named = os.stat(part, dir_fd=fd, follow_symlinks=False)
            owner = 0 if index == 0 else _OWNER
            if (info.st_uid not in (0, owner) or info.st_mode & 0o022
                    or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
                    or any(n.startswith('system.posix_acl_') for n in os.listxattr(child))):
                raise EntryFailure()
            chain.append((fd, part, child, _code_identity(info)))
            fd = child
        _checked_code(fd, 'credential_entry.py')
        raw = _checked_code(fd, 'credential_store.py')
        smoke_name = 'librarything_smoke.py' if librarything else 'provider_smoke.py'
        smoke_raw = _checked_code(fd, smoke_name) if smoke else None
        # Keep ancestry descriptors until both reads finish, so a renamed or
        # substituted code directory cannot validate a detached sibling pair.
        for parent, name, child, expected in chain:
            if (_code_identity(os.fstat(child)) != expected
                    or _code_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected
                    or any(n.startswith('system.posix_acl_') for n in os.listxattr(child))):
                raise EntryFailure()
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
    module = types.ModuleType('_culture_cabinet_credential_store')
    module.__file__ = _CODE_DIRECTORY + '/credential_store.py'
    sys.modules[module.__name__] = module
    exec(compile(raw, module.__file__, 'exec'), module.__dict__)
    if smoke:
        sender = types.ModuleType('_culture_cabinet_' + smoke_name[:-3])
        sender.__file__ = _CODE_DIRECTORY + '/' + smoke_name
        sys.modules[sender.__name__] = sender
        exec(compile(smoke_raw, sender.__file__, 'exec'), sender.__dict__)
        return module, sender
    return module


def _terminal_identity():
    identities = []
    for fd in (0, 1, 2):
        if not os.isatty(fd) or os.tcgetpgrp(fd) != os.getpgrp():
            raise EntryFailure()
        info = os.fstat(fd)
        identities.append((info.st_dev, info.st_ino, info.st_rdev))
        session = array.array('i', [0])
        fcntl.ioctl(fd, _TIOCGSID, session, True)
        if session[0] != os.getsid(0):
            raise EntryFailure()
    if len(set(identities)) != 1:
        raise EntryFailure()
    fd = os.open('/dev/tty', os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        if os.tcgetpgrp(fd) != os.getpgrp():
            raise EntryFailure()
    finally:
        os.close(fd)


@contextmanager
def _hidden_terminal():
    _terminal_identity()
    saved = termios.tcgetattr(0)
    handlers = {}
    try:
        for signum in _SIGNALS:
            handlers[signum] = signal.signal(signum, _interrupted)
        hidden = termios.tcgetattr(0)
        hidden[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.IEXTEN)
        hidden[0] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR)
        hidden[6][termios.VMIN] = 0
        hidden[6][termios.VTIME] = 0
        termios.tcsetattr(0, termios.TCSAFLUSH, hidden)
        observed = termios.tcgetattr(0)
        if observed[3] & (termios.ECHO | termios.ECHONL | termios.ICANON):
            raise EntryFailure()
        yield
    finally:
        # Flush unread/overflow bytes while echo is still disabled. Blocking the
        # handled signals makes restoration itself a single catchable-exit step.
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, _SIGNALS)
        try:
            try:
                termios.tcsetattr(0, termios.TCSAFLUSH, saved)
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def _prompt(fd, text, cap, deadline):
    os.write(2, text.encode('ascii'))
    value = bytearray()
    overflow = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise EntryFailure()
            char = os.read(fd, 1)
            if not char:
                raise EntryFailure()
            if char in (b'\n', b'\r'):
                os.write(2, b'\n')
                if overflow:
                    raise EntryFailure()
                return value
            if len(value) < cap:
                value.extend(char)
            else:
                # Drain to delimiter (or the same absolute deadline), hidden.
                # Never accept the truncated prefix of a canonical input line.
                overflow = True
    except BaseException:
        value[:] = b'\0' * len(value)
        raise


def _text(fd, prompt, cap, deadline):
    value = _prompt(fd, prompt, cap, deadline)
    try:
        return value.decode('ascii')
    except UnicodeError:
        raise EntryFailure() from None
    finally:
        value[:] = b'\0' * len(value)


def _binding(store, fd, deadline):
    binding = store.Binding(_text(fd, 'Account ID: ', 64, deadline),
                            _text(fd, 'Actor ID: ', 18, deadline),
                            _text(fd, 'Profile ID: ', 18, deadline))
    store.validate_binding(binding)
    return binding


def collect_entry(tty_fd, *, now, deadline, _store=None):
    if _store is None:
        from . import credential_store as _store
    binding = _binding(_store, tty_fd, deadline)
    expires = _store.parse_time(_text(tty_fd, 'Local expiry UTC (YYYY-MM-DDTHH:MM:SSZ): ', 20, deadline))
    provider = _store.parse_time(_text(tty_fd, 'Declared provider expiry UTC: ', 20, deadline))
    metadata = _store.EntryMetadata(binding, now, expires, provider)
    _store.validate_metadata(metadata, now=now)
    secret = _prompt(tty_fd, 'Token: ', 4096, deadline)
    try:
        _store._secret_text(secret)
    except BaseException:
        secret[:] = b'\0' * len(secret)
        raise
    return metadata, secret


def _setup_metadata(store, provider):
    # O supplied the actual installation IDs; these defaults confer no grants.
    # Authentication does not prove account identity; this remains unverified.
    context = store._provider_context(provider)
    now = store.utc_now()
    cap = now + timedelta(days=89)
    return store.EntryMetadata(store.Binding('unverified', '1', '1', context.provider, context.purpose),
                               now, cap, cap, context.scopes)


def _setup_instruction(provider):
    if provider == 'librarything':
        return (b'LibraryThing: paste a private API key from https://www.librarything.com/developer/tokens (account/sign-in required). '
                b'read-multirecommendations is a LOCAL purpose, not an OAuth scope; read-only recommendations only. '
                b'89-day local cap and declared expiry, UNVERIFIED/not provider-attested; provider expiry is unchanged. '
                b'Owner/profile fixed locally; authentication cannot prove account identity. Source off. All inputs hidden.\n')
    if provider == 'hardcover':
        return (b'Hardcover: paste a fresh 90-day key declaring read:catalog:search read:catalog:data read:library write:library; '
                b'89-day local cap and declared expiry, UNVERIFIED/not provider-attested; source off, identity verification required before activation. All inputs hidden.\n')
    return (b'TMDB: paste an API Read Access Token (application Bearer); tmdb-application-read-access is a local token-type contract, '
            b'GET authentication/catalogue only, no user/session/account writes; 89-day local cap and declared expiry, '
            b'UNVERIFIED/not provider-attested; source off, identity verification required before activation. All inputs hidden.\n')


def _emit(message):
    try:
        os.write(2, message)
        return True
    except OSError:
        # A lost terminal has no alternate diagnostic route. No raw traceback.
        return False


def _receipt(receipt):
    acknowledged = _emit(('credential-entry ' + receipt.operation + ' ' + receipt.outcome + ' ' + receipt.reason + '\n').encode('ascii'))
    if not acknowledged:
        return 3
    if receipt.outcome in ('commit_unknown', 'durable_cleanup_unknown'):
        return 3
    return 2 if receipt.outcome == 'refused' else 0


def _run(operation, store_module, opener):
    store = record = secret = None
    setup = operation in (*_SETUPS, _LIBRARYTHING_SETUP)
    provider = 'tmdb' if operation == 'setup-tmdb' else 'hardcover'
    if operation == _LIBRARYTHING_SETUP:
        provider = 'librarything'
    if setup:
        operation = 'enter'
    deadline = time.monotonic() + 120
    try:
        if (os.getuid() != _OWNER or os.geteuid() != _OWNER or _OWNER == 0
                or os.getgid() != _GROUP or os.getegid() != _GROUP):
            raise EntryFailure()
        with _hidden_terminal():
            store = opener()
            if store.context.provider != provider:
                raise store_module.Refused('invalid_input')
            store_module.lock_store(store)
            if setup:
                os.write(2, _setup_instruction(provider))
            else:
                os.write(2, b'Hardcover backend only; stored UNVERIFIED/source off.\nScopes: read:catalog:search read:catalog:data read:library write:library\nRemoval does not revoke at provider or erase copies. All inputs hidden.\n')
            if operation in ('enter', 'replace'):
                if setup:
                    secret = _prompt(0, 'Token: ', 4096, deadline)
                    store_module._secret_text(secret)
                else:
                    metadata, secret = collect_entry(0, now=store_module.utc_now(), deadline=deadline, _store=store_module)
                if _text(0, 'Type STORE to confirm: ', 5, deadline) != 'STORE':
                    raise EntryFailure()
                if setup:
                    metadata = _setup_metadata(store_module, provider)
                expected = None
                if operation == 'replace':
                    store_module._clean(store)
                    record = store_module._read_record(store, allow_expired=True)
                    expected = record.identity
                # The entry time belongs to the local commit, not the beginning
                # of a potentially two-minute private conversation.
                metadata = store_module.EntryMetadata(metadata.binding, store_module.utc_now(), metadata.expires_at,
                                                       metadata.declared_provider_expires_at, metadata.scopes)
                if time.monotonic() >= deadline:
                    raise EntryFailure()
                receipt = store_module.commit_entry(store, metadata, secret, expected=expected)
            elif operation == 'remove':
                binding = _binding(store_module, 0, deadline)
                if _text(0, 'Type REMOVE to confirm: ', 6, deadline) != 'REMOVE':
                    raise EntryFailure()
                store_module._clean(store)
                record = store_module._read_record(store, allow_expired=True)
                if record.metadata.binding != binding:
                    raise store_module.Refused('conflict')
                if time.monotonic() >= deadline:
                    raise EntryFailure()
                receipt = store_module.remove_entry(store, expected=record.identity)
            else:
                intended = _text(0, 'Intended state (present/absent/discard): ', 7, deadline)
                metadata = None
                if intended == 'discard':
                    os.write(2, b'DISCARD retires the fixed local Hardcover backend record and recognized interrupted transaction artifacts without the old token.\nNo provider revocation or erasure of copies. A lost provider key must be revoked separately by the human.\n')
                if intended == 'present':
                    # Expired data remains explicitly reconcilable/removable.
                    binding = _binding(store_module, 0, deadline)
                    expires = store_module.parse_time(_text(0, 'Local expiry UTC (YYYY-MM-DDTHH:MM:SSZ): ', 20, deadline))
                    provider = store_module.parse_time(_text(0, 'Declared provider expiry UTC: ', 20, deadline))
                    secret = _prompt(0, 'Token: ', 4096, deadline)
                    # Read only after the human has explicitly chosen present;
                    # reuse the record's trusted entry time for expired cleanup.
                    store._check()
                    record = store_module._read_record(store, allow_expired=True)
                    metadata = store_module.EntryMetadata(binding, record.metadata.entered_at, expires, provider)
                confirmation = 'DISCARD' if intended == 'discard' else 'RECONCILE'
                if _text(0, 'Type ' + confirmation + ' to confirm: ', len(confirmation), deadline) != confirmation:
                    raise EntryFailure()
                if time.monotonic() >= deadline:
                    raise EntryFailure()
                receipt = store_module.reconcile_entry(store, intended=intended, metadata=metadata, secret=secret)
            return _receipt(receipt)
    except store_module.Refused as error:
        return _receipt(store_module.EntryReceipt(operation, 'refused', error.reason))
    except (EntryFailure, OSError, termios.error, ValueError, KeyboardInterrupt):
        _emit(('credential-entry ' + operation + ' refused interrupted\n').encode('ascii'))
        return 2
    finally:
        if secret is not None:
            secret[:] = b'\0' * len(secret)
        if record is not None:
            record.close()
        if store is not None:
            store.close()


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] in _SMOKES:
        if args != [args[0], '--live']:
            _emit(b'credential-entry refused explicit_live_required\n')
            return 2
        try:
            if args[0] == 'smoke-librarything':
                module, sender = _deployment_store(smoke=True, librarything=True)
            else:
                module, sender = _deployment_store(smoke=True)
        except (EntryFailure, OSError, ValueError, ImportError, SyntaxError):
            _emit(b'credential-entry refused deployment\n')
            return 2
        provider = args[0].removeprefix('smoke-')
        return sender.run_live(module, provider)
    if len(args) != 1 or args[0] not in (*_VERBS, *_SETUPS, _LIBRARYTHING_SETUP):
        _emit(b'credential-entry refused invalid_command\n')
        return 2
    try:
        module = _deployment_store()
    except (EntryFailure, OSError, ValueError, ImportError, SyntaxError):
        _emit(b'credential-entry refused deployment\n')
        return 2
    if args[0] in (*_SETUPS, _LIBRARYTHING_SETUP):
        provider = args[0].removeprefix('setup-')
        return _run(args[0], module, lambda: module.open_store(provider=provider))
    return _run(args[0], module, module.open_store)


if __name__ == '__main__':
    raise SystemExit(main())
