"""Owned disposable NONcredential records and child/PTY harness; never production paths."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from datetime import timedelta

PYTHON = '/home/tang/Projects/culture-cabinet/.venv/bin/python'
FIXTURE = str(Path(__file__).absolute())
CANARY = b'OBVIOUS-NONCREDENTIAL-FICTION-ONLY-7'


def load_component(name):
    # This internal harness is deliberately outside the production command.
    path = Path(__file__).absolute().parents[1] / (name + '.py')
    spec = importlib.util.spec_from_file_location('_fixture_' + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def metadata(store, **changes):
    now = store.utc_now()
    values = {'binding': store.Binding('FICTION_ACCOUNT', '17', '23'), 'entered_at': now,
              'expires_at': now + timedelta(days=1), 'declared_provider_expires_at': now + timedelta(days=2)}
    values.update(changes)
    return store.EntryMetadata(**values)


class FakeTree:
    def __init__(self, store, *, provider='hardcover'):
        self.temporary = tempfile.TemporaryDirectory(prefix='credentials-NONCREDENTIAL-')
        self.path = Path(self.temporary.name)
        self.path.chmod(0o700)
        fd = os.open(self.path / 'control.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        self.store = store._fixture_store(self.path, provider=provider)
        store.lock_store(self.store)

    def close(self):
        self.store.close()
        self.temporary.cleanup()


def bootstrap_child(directory, scenario):
    """Exercise production policy with owned code bytes, never a real opener.

    Deployment locations and the root dirfd are redirected to an owned tree.
    Root/home owner metadata models UID0 without chown (this worker namespace's
    root is UID1000). Code UID1000 is real; policy and sibling compilation run
    unchanged. Wrong-code-owner metadata is injected, never accepted as valid.
    """
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import patch
    entry = load_component('credential_entry')
    directory = Path(directory)
    code = directory / 'credential_store.py'
    target_inode = code.stat().st_ino
    virtual_root = directory.parent.parent
    virtual_code = '/home/' + directory.name
    root_inodes = {(p.stat().st_dev, p.stat().st_ino) for p in (virtual_root, directory.parent)}
    real_open, real_fstat, real_stat = os.open, os.fstat, os.stat
    def root_metadata(info):
        if (info.st_dev, info.st_ino) in root_inodes:
            fields = {name: getattr(info, name) for name in
                      ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink', 'st_size',
                       'st_mtime_ns', 'st_ctime_ns')}
            return SimpleNamespace(**{**fields, 'st_uid': 0, 'st_gid': 0})
        return info
    def open_root(name, *args, **kwargs):
        return real_open(str(virtual_root) if name == '/' else name, *args, **kwargs)
    def stat_root(*args, **kwargs):
        return root_metadata(real_stat(*args, **kwargs))
    def fstat(fd):
        info = root_metadata(real_fstat(fd))
        if info.st_ino == target_inode and scenario in ('wrong-owner', 'wrong-group'):
            values = list(info)
            values[4 if scenario == 'wrong-owner' else 5] = 0
            return os.stat_result(values)
        return info
    with ExitStack() as stack:
        stack.enter_context(patch.object(entry, '_CODE_DIRECTORY', virtual_code))
        stack.enter_context(patch.object(entry, 'ENTRY_SCRIPT', virtual_code + '/credential_entry.py'))
        stack.enter_context(patch.object(entry, '__file__', virtual_code + '/credential_entry.py'))
        stack.enter_context(patch.object(entry.os, 'open', open_root))
        stack.enter_context(patch.object(entry.os, 'stat', stat_root))
        stack.enter_context(patch.object(entry.os, 'fstat', fstat))
        if scenario in ('substitution', 'directory-substitution', 'mutation'):
            real_read = os.read
            def read(fd, size):
                raw = real_read(fd, size)
                if os.fstat(fd).st_ino == target_inode:
                    if scenario == 'substitution':
                        os.replace(directory / 'replacement.py', code)
                    elif scenario == 'directory-substitution':
                        directory.rename(directory.with_name('retired-code'))
                        directory.mkdir(mode=0o700)
                    else:
                        code.chmod(0o644)
                        code.write_bytes(raw)
                        code.chmod(0o444)
                return raw
            stack.enter_context(patch.object(entry.os, 'read', read))
        elif scenario in ('no-isolation', 'site-enabled', 'bytecode-enabled'):
            flags = SimpleNamespace(isolated=1, no_site=1, dont_write_bytecode=1)
            setattr(flags, {'no-isolation': 'isolated', 'site-enabled': 'no_site',
                            'bytecode-enabled': 'dont_write_bytecode'}[scenario], 0)
            stack.enter_context(patch.object(entry.sys, 'flags', flags))
        elif scenario == 'wrong-interpreter':
            stack.enter_context(patch.object(entry.sys, 'executable', '/OWNED-FAKE-WRONG-INTERPRETER'))
        elif scenario == 'wrong-script':
            stack.enter_context(patch.object(entry, 'ENTRY_SCRIPT', str(directory / 'wrong.py')))
        try:
            if scenario.startswith('librarything-smoke'):
                module, sender = entry._deployment_store(smoke=True, librarything=True)
                assert sender.__file__ == virtual_code + '/librarything_smoke.py'
                assert callable(sender.run_live)
            elif scenario.startswith('smoke'):
                module, sender = entry._deployment_store(smoke=True)
                assert sender.__file__ == virtual_code + '/provider_smoke.py'
                assert callable(sender.run_live)
            else:
                module = entry._deployment_store()
        except (entry.EntryFailure, OSError):
            print('BOOTSTRAP_REFUSED', flush=True)
            return 2
        # The checked sibling is the real store with this hard-fail appended by
        # the parent fixture, so even a mistaken valid verb cannot open storage.
        try:
            module.open_store()
        except AssertionError as error:
            assert str(error) == 'production store forbidden in bootstrap fixture'
        else:
            raise AssertionError('bootstrap fixture opener did not hard-fail')
        assert not any(name == 'django' or name.startswith(('django.', 'cabinet.')) for name in sys.modules)
        assert module.__file__ == virtual_code + '/credential_store.py'
        print('BOOTSTRAP_ACCEPTED_OPENER_BLOCKED', flush=True)
        return 0


def child(args):
    mode, directory, *options = args
    if mode == 'bootstrap':
        return bootstrap_child(directory, options[0])
    store = load_component('credential_store')
    if mode == 'pty':
        entry = load_component('credential_entry')
        def forbidden_production_open():
            raise AssertionError('production store forbidden in fake harness')
        store.open_store = forbidden_production_open
        if len(options) > 1 and options[1] == 'timeout':
            original_prompt = entry._prompt
            absolute_deadline = entry.time.monotonic() + 0.15
            def short_prompt(fd, text, cap, deadline):
                return original_prompt(fd, text, cap, min(deadline, absolute_deadline))
            entry._prompt = short_prompt
        provider = ('librarything' if options[0] == 'setup-librarything' else
                    'tmdb' if options[0] == 'setup-tmdb' else 'hardcover')
        return entry._run(options[0], store, lambda: store._fixture_store(directory, provider=provider))
    handle = store._fixture_store(directory)
    try:
        try:
            store.lock_store(handle)
        except store.Refused as error:
            print(error.reason, flush=True)
            return 2
        if mode == 'hold':
            print('LOCKED', flush=True)
            sys.stdin.buffer.read(1)
            return 0
        if mode == 'lock':
            print('LOCKED', flush=True)
            return 0
        if mode in ('crash', 'crash-remove', 'crash-discard', 'crash-partial'):
            barrier = options[0]
            original_sync = store.os.fsync
            original_replace = store.os.replace
            original_unlink = store.os.unlink
            original_write = store.os.write
            count = 0
            writes = 0
            def fsync(fd):
                nonlocal count
                count += 1
                original_sync(fd)
                if barrier == 'sync-' + str(count):
                    print('BARRIER', flush=True)
                    os.kill(os.getpid(), signal.SIGSTOP)
            def replace(*a, **kw):
                original_replace(*a, **kw)
                if barrier == 'rename':
                    print('BARRIER', flush=True)
                    os.kill(os.getpid(), signal.SIGSTOP)
            def unlink(name, **kw):
                original_unlink(name, **kw)
                if ((barrier == 'unlink' and name == store.RECORD)
                        or (barrier == 'unlink-temp' and store._TEMP.fullmatch(name))
                        or (barrier == 'unlink-marker' and name == store.PENDING)):
                    print('BARRIER', flush=True)
                    os.kill(os.getpid(), signal.SIGSTOP)
            def write(fd, raw):
                nonlocal writes
                writes += 1
                if mode == 'crash-partial' and writes == (1 if barrier == 'marker' else 2):
                    written = original_write(fd, raw[:8 if barrier == 'marker' else 13])
                    print('BARRIER', flush=True)
                    os.kill(os.getpid(), signal.SIGSTOP)
                    return written
                return original_write(fd, raw)
            store.os.fsync = fsync
            store.os.replace = replace
            store.os.unlink = unlink
            store.os.write = write
            if mode == 'crash-discard':
                receipt = store.reconcile_entry(handle, intended='discard')
            elif mode == 'crash-remove':
                record = store.read_record(handle)
                receipt = store.remove_entry(handle, expected=record.identity)
                record.close()
            else:
                receipt = store.commit_entry(handle, metadata(store), bytearray(CANARY))
            print(json.dumps(receipt.__dict__), flush=True)
            return 0
        if mode == 'commit':
            print(store.commit_entry(handle, metadata(store), bytearray(CANARY)).outcome, flush=True)
            return 0
        raise ValueError('invalid fake harness mode')
    finally:
        handle.close()


if __name__ == '__main__':
    raise SystemExit(child(sys.argv[1:]))
