"""S01/S02/S03/P01: actual fake files, competing processes and crash barriers."""
import errno
import builtins
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import pickle
import select
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from datetime import timedelta
from unittest.mock import patch

from shelf import credential_store as store
from shelf.tests.fixtures_credentials import CANARY, FIXTURE, PYTHON, FakeTree, metadata


class CredentialStoreWalkTests(unittest.TestCase):
    """Real dirfd walk in an owned tree; model only root ownership and mounts."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='walk-owned-')
        self.addCleanup(temporary.cleanup)
        self.parts = store.STORE_DIRECTORY.strip('/').split('/')
        self.paths = [Path(temporary.name)]
        for name in self.parts:
            child = self.paths[-1] / name
            child.mkdir(mode=0o700)
            self.paths.append(child)
        self.contracts = [(0, 0, False), (store.OWNER_UID, store.OWNER_GID, False),
                          (store.OWNER_UID, store.OWNER_GID, False),
                          (store.OWNER_UID, store.OWNER_GID, False),
                          (store.OWNER_UID, store.OWNER_GID, True),
                          (store.OWNER_UID, store.OWNER_GID, True)]
        self.keys = [(p.stat().st_dev, p.stat().st_ino) for p in self.paths]
        self.changes = {key: {'st_dev': 101} for key in self.keys}
        for key in self.keys[:2]:
            self.changes[key].update(st_uid=0, st_gid=0)
        self.mount_ids = dict.fromkeys(self.keys, 901)
        self.home_fs = 'btrfs'
        real_open, real_stat, real_fstat = os.open, os.stat, os.fstat
        real_stream = builtins.open

        def modeled(info):
            changes = self.changes.get((info.st_dev, info.st_ino))
            if changes is None:
                return info
            values = {name: getattr(info, name) for name in
                      ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink', 'st_size')}
            return SimpleNamespace(**{**values, **changes})

        def open_root(name, *args, **kwargs):
            return real_open(self.paths[0] if name == '/' else name, *args, **kwargs)

        def mount(fd):
            info = real_fstat(fd)
            return self.mount_ids[(info.st_dev, info.st_ino)]

        def stream(name, *args, **kwargs):
            if name == '/proc/self/mountinfo':
                return io.StringIO('901 0 0:1 / / rw - btrfs owned rw\n'
                                   '902 901 0:2 / /home rw - ' + self.home_fs + ' owned rw\n')
            return real_stream(name, *args, **kwargs)

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(store.os, 'open', open_root))
        stack.enter_context(patch.object(store.os, 'stat', lambda *a, **kw: modeled(real_stat(*a, **kw))))
        stack.enter_context(patch.object(store.os, 'fstat', lambda fd: modeled(real_fstat(fd))))
        stack.enter_context(patch.object(store, '_mount', mount))
        stack.enter_context(patch('builtins.open', stream))

    def separate_home(self):
        for key in self.keys[1:]:
            self.changes[key]['st_dev'] = 202
            self.mount_ids[key] = 902

    def walk(self):
        handle = store._walk(self.parts, self.contracts)
        self.addCleanup(handle.close)
        return handle

    def test_home_sharing_root_is_accepted(self):
        handle = self.walk()
        self.assertEqual(os.fstat(handle.fd).st_ino, self.keys[-1][1])
        self.assertEqual(len(handle._chain), len(self.parts))

    def test_separate_local_home_is_accepted(self):
        self.separate_home()
        for filesystem in ('btrfs', 'ext4', 'xfs'):
            with self.subTest(filesystem=filesystem):
                self.home_fs = filesystem
                handle = self.walk()
                self.assertEqual(os.fstat(handle.fd).st_dev, 202)
                self.assertEqual(store._mount(handle._chain[1][0]), 902)
                with self.assertRaises(store.Refused) as caught:
                    handle._check()
                self.assertEqual(caught.exception.reason, 'busy')
                handle.close()

    def test_nonlocal_or_unlisted_home_is_refused(self):
        self.separate_home()
        for filesystem in ('nfs', 'tmpfs', 'overlay', 'fuse.sshfs'):
            with self.subTest(filesystem=filesystem):
                self.home_fs = filesystem
                with self.assertRaises(store.Refused):
                    self.walk()
        self.home_fs = 'btrfs'
        self.mount_ids[self.keys[1]] = 999
        with self.assertRaises(store.Refused):
            self.walk()

    def test_home_owner_group_mode_and_acl_checks_are_preserved(self):
        self.separate_home()
        for field in ('st_uid', 'st_gid'):
            with self.subTest(field=field):
                self.changes[self.keys[1]][field] = 1000
                with self.assertRaises(store.Refused):
                    self.walk()
                self.changes[self.keys[1]][field] = 0
        for mode in (0o777, 0o775, 0o757):
            with self.subTest(mode=mode):
                self.paths[1].chmod(mode)
                with self.assertRaises(store.Refused):
                    self.walk()
        self.paths[1].chmod(0o755)
        real_xattrs = os.listxattr
        def xattrs(fd):
            return ['system.posix_acl_default'] if os.fstat(fd).st_ino == self.keys[1][1] else real_xattrs(fd)
        with patch.object(store.os, 'listxattr', xattrs):
            with self.assertRaises(store.Refused):
                self.walk()

    def test_home_symlink_and_nondirectory_are_refused(self):
        home = self.paths[1]
        retired = home.with_name('retired-owned-home')
        home.rename(retired)
        try:
            home.symlink_to(retired, target_is_directory=True)
            with self.assertRaises(OSError):
                self.walk()
            home.unlink()
            home.write_bytes(b'NONSECRET')
            with self.assertRaises(OSError):
                self.walk()
        finally:
            home.unlink()
            retired.rename(home)

    def test_each_later_device_or_mount_transition_is_refused(self):
        for separate in (False, True):
            if separate:
                self.separate_home()
            for key in self.keys[2:]:
                for kind in ('device', 'same-device-bind'):
                    with self.subTest(separate=separate, component=self.keys.index(key), kind=kind):
                        device, mount = self.changes[key]['st_dev'], self.mount_ids[key]
                        if kind == 'device':
                            self.changes[key]['st_dev'] = 303
                        else:
                            self.mount_ids[key] = 999
                        with self.assertRaises(store.Refused):
                            self.walk()
                        self.changes[key]['st_dev'], self.mount_ids[key] = device, mount

    def test_first_boundary_is_only_named_home(self):
        self.separate_home()
        self.paths[1].rename(self.paths[1].with_name('other-owned'))
        self.parts[0] = 'other-owned'
        with self.assertRaises(store.Refused):
            self.walk()

    def test_home_inode_substitution_during_walk_is_refused(self):
        self.separate_home()
        real_stat = os.stat
        def stat_name(name, *args, **kwargs):
            info = real_stat(name, *args, **kwargs)
            if name == 'home':
                return SimpleNamespace(**{**vars(info), 'st_ino': info.st_ino + 1})
            return info
        with patch.object(store.os, 'stat', stat_name):
            with self.assertRaises(store.Refused):
                self.walk()

    def test_retained_home_identity_owner_mode_and_acl_are_checked(self):
        self.separate_home()
        handle = self.walk()
        for field in ('st_ino', 'st_uid', 'st_gid'):
            with self.subTest(field=field):
                self.changes[self.keys[1]][field] = 12345
                with self.assertRaises(store.Refused) as caught:
                    handle._check()
                self.assertEqual(caught.exception.reason, 'unsafe_store')
                self.changes[self.keys[1]].pop(field)
                if field != 'st_ino':
                    self.changes[self.keys[1]][field] = 0
        self.paths[1].chmod(0o777)
        with self.assertRaises(store.Refused) as caught:
            handle._check()
        self.assertEqual(caught.exception.reason, 'unsafe_store')
        self.paths[1].chmod(0o700)
        real_xattrs = os.listxattr
        def xattrs(fd):
            return ['system.posix_acl_access'] if os.fstat(fd).st_ino == self.keys[1][1] else real_xattrs(fd)
        with patch.object(store.os, 'listxattr', xattrs):
            with self.assertRaises(store.Refused) as caught:
                handle._check()
            self.assertEqual(caught.exception.reason, 'unsafe_store')


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTree(store)
        self.addCleanup(self.fake.close)
        self.handle = self.fake.store
        self.path = self.fake.path

    def enter(self, **kwargs):
        return store.commit_entry(self.handle, metadata(store, **kwargs), bytearray(CANARY))

    def record(self):
        record = store.read_record(self.handle)
        self.addCleanup(record.close)
        return record

    def test_S01_entry_replace_remove_unverified_and_persistent_lock(self):
        lock = (self.path / store.LOCK).stat()
        self.assertEqual(self.enter().outcome, 'stored')
        old = self.record()
        self.assertFalse(old.verified)
        self.assertEqual(old.secret, CANARY)
        receipt = store.commit_entry(self.handle, metadata(store), bytearray(b'NEW-NONCREDENTIAL'), expected=old.identity)
        self.assertEqual(receipt.outcome, 'stored')
        current = self.record()
        self.assertNotEqual(old.generation, current.generation)
        self.assertEqual(store.remove_entry(self.handle, expected=current.identity).outcome, 'removed')
        self.assertEqual(os.listdir(self.path), [store.LOCK])
        self.assertEqual((self.path / store.LOCK).stat().st_ino, lock.st_ino)
        self.assertEqual((self.path / store.LOCK).stat().st_size, 0)

    def test_S01_binding_and_identity_conflicts_preserve_record(self):
        self.enter()
        old = self.record()
        changed = metadata(store, binding=store.Binding('OTHER_FICTION', '17', '23'))
        self.assertEqual(store.commit_entry(self.handle, changed, bytearray(CANARY), expected=old.identity).reason, 'conflict')
        self.assertEqual(self.enter().reason, 'conflict')
        stale = store.StoreIdentity((0,) * 7)
        self.assertEqual(store.remove_entry(self.handle, expected=stale).reason, 'conflict')
        self.assertFalse((self.path / store.PENDING).exists())
        self.assertEqual(self.record().secret, CANARY)

    def test_S01_strict_bounded_schema_and_duplicate_fields(self):
        self.enter()
        good = (self.path / store.RECORD).read_bytes()
        body = json.loads(good)
        variants = [good.replace(b'"schema":1', b'"schema":1,"schema":1'), b'[' * 2000,
                    b'x' * 8193, good + b'\xff']
        for field, value in [('schema', True), ('generation', '0' * 32), ('scopes', list(reversed(store.SCOPES))),
                             ('token', 'Bearer FICTION'), ('verified', True), ('entered_at', '2099-01-01T00:00:00Z')]:
            altered = dict(body)
            altered[field] = value
            variants.append(json.dumps(altered).encode())
        for raw in variants:
            with self.subTest(case=len(raw)):
                (self.path / store.RECORD).write_bytes(raw)
                with self.assertRaises(store.Refused):
                    store.read_record(self.handle)
        (self.path / store.RECORD).write_bytes(good)
        self.assertEqual(self.record().secret, CANARY)

    def test_S01_profile_ids_and_scope_expiry_bounds(self):
        for bad in ('0', '-1', '1' * 19, '１', '1.0', True, 23):
            with self.subTest(value=bad):
                receipt = self.enter(binding=store.Binding('FICTION', '17', bad))
                self.assertEqual(receipt.outcome, 'refused')
        for change in ({'expires_at': store.utc_now() + timedelta(days=91)},
                       {'expires_at': store.utc_now() - timedelta(seconds=1)},
                       {'declared_provider_expires_at': store.utc_now()}, {'scopes': tuple(reversed(store.SCOPES))}):
            self.assertEqual(self.enter(**change).outcome, 'refused')
        self.assertEqual(self.enter(binding=store.Binding('FICTION', '17', '9' * 18)).outcome, 'stored')

    def test_S01_entered_at_is_local_and_record_never_verifies_claims(self):
        self.assertEqual(self.enter(entered_at=store.utc_now() - timedelta(days=20)).outcome, 'stored')
        record = self.record()
        self.assertLess(abs((record.metadata.entered_at - store.utc_now()).total_seconds()), 2)
        self.assertFalse(record.verified)
        self.assertNotIn('verified', json.loads((self.path / store.RECORD).read_bytes()))

    def test_S01_expired_refuses_read_but_can_be_reconciled_and_removed(self):
        self.enter()
        body = json.loads((self.path / store.RECORD).read_bytes())
        now = store.utc_now()
        body['entered_at'] = store._time_text(now - timedelta(days=2))
        body['expires_at'] = store._time_text(now - timedelta(days=1))
        body['declared_provider_expires_at'] = body['expires_at']
        (self.path / store.RECORD).write_text(json.dumps(body))
        with self.assertRaises(store.Refused) as caught:
            store.read_record(self.handle)
        self.assertEqual(caught.exception.reason, 'expired')
        record = store._read_record(self.handle, allow_expired=True)
        self.addCleanup(record.close)
        receipt = store.reconcile_entry(self.handle, intended='present', metadata=record.metadata, secret=bytearray(CANARY))
        self.assertEqual(receipt.outcome, 'reconciled')
        with self.assertRaises(store.Refused):
            store.read_record(self.handle)
        self.assertEqual(store.remove_entry(self.handle, expected=record.identity).outcome, 'removed')

    def test_S01_alias_mode_link_unknown_entry_and_fifo_refusal(self):
        credential = self.path / store.RECORD
        target = self.path / 'owned-canary'
        target.write_bytes(CANARY)
        credential.symlink_to(target)
        self.assertEqual(self.enter().outcome, 'refused')
        with self.assertRaises(store.Refused):
            store._read_record(self.handle)
        credential.unlink()
        os.link(target, credential)
        with self.assertRaises(store.Refused):
            store._read_record(self.handle)
        credential.unlink()
        target.unlink()
        os.mkfifo(credential, 0o600)
        with self.assertRaises(store.Refused):
            store._read_record(self.handle)
        credential.unlink()
        self.enter()
        credential.chmod(0o640)
        with self.assertRaises(store.Refused):
            store.read_record(self.handle)
        credential.chmod(0o600)
        self.path.chmod(0o750)
        self.assertEqual(self.enter().outcome, 'refused')
        self.path.chmod(0o700)

    def test_S01_acl_refused_on_owned_fake_only(self):
        # A masked extended ACL is refused even when the visible mode is0600.
        # Use the mapped fixture UID; arbitrary UIDs may be unmapped here.
        import struct
        acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', tag, perm, ident) for tag, perm, ident in
                [(1, 6, 0xffffffff), (2, 6, os.getuid()), (4, 0, 0xffffffff), (16, 6, 0xffffffff), (32, 0, 0xffffffff)])
        os.setxattr(self.path / store.LOCK, 'system.posix_acl_access', acl)
        (self.path / store.LOCK).chmod(0o600)
        with self.assertRaises(store.Refused):
            self.handle._check()
        os.removexattr(self.path / store.LOCK, 'system.posix_acl_access')
        (self.path / store.LOCK).chmod(0o600)

    def test_S02_dirty_marker_and_unmarked_temporary_block(self):
        store._marker(self.handle, 'enter')
        self.assertEqual(self.enter().reason, 'dirty_store')
        with self.assertRaises(store.Refused):
            store.read_record(self.handle)
        self.assertEqual(store.reconcile_entry(self.handle, intended='absent').outcome, 'reconciled')
        temp = self.path / ('.entry-' + 'a' * 32 + '.tmp')
        temp.write_bytes(b'FICTION')
        temp.chmod(0o600)
        self.assertEqual(self.enter().reason, 'dirty_store')
        self.assertEqual(store.reconcile_entry(self.handle, intended='absent').reason, 'conflict')

    def test_S02_reconciliation_requires_exact_private_confirmation(self):
        self.enter()
        old = self.record()
        store._marker(self.handle, 'replace')
        for intended, meta, token in [('absent', None, None), ('present', old.metadata, bytearray(b'WRONG-FICTION')),
                                     ('present', metadata(store, binding=store.Binding('OTHER', '17', '23')), bytearray(CANARY))]:
            result = store.reconcile_entry(self.handle, intended=intended, metadata=meta, secret=token)
            self.assertEqual(result.outcome, 'refused')
            self.assertTrue((self.path / store.PENDING).exists())
        self.assertEqual(store.reconcile_entry(self.handle, intended='present', metadata=old.metadata, secret=bytearray(CANARY)).outcome, 'reconciled')
        self.assertEqual(self.record().secret, CANARY)

    def test_S02_marker_and_temporary_contradictions_refuse(self):
        self.enter()
        old = self.record()
        store._marker(self.handle, 'enter')
        temp = '.entry-' + 'b' * 32 + '.tmp'
        store._create(self.handle, temp, (self.path / store.RECORD).read_bytes())
        self.assertEqual(store.reconcile_entry(self.handle, intended='present', metadata=old.metadata, secret=bytearray(CANARY)).reason, 'conflict')
        (self.path / temp).unlink()
        (self.path / store.PENDING).write_bytes(b'{"schema":1,"operation":"remove","extra":1}')
        self.assertEqual(store.reconcile_entry(self.handle, intended='present', metadata=old.metadata, secret=bytearray(CANARY)).outcome, 'refused')

    def test_S03_every_fsync_boundary_reports_correct_uncertainty(self):
        real_sync = os.fsync
        for failed in range(1, 6):
            fake = FakeTree(store)
            try:
                count = 0
                def sync(fd):
                    nonlocal count
                    count += 1
                    if count == failed:
                        raise OSError(errno.EIO, 'PRIVATE-ERROR-NONCREDENTIAL')
                    real_sync(fd)
                with patch.object(store.os, 'fsync', sync):
                    result = store.commit_entry(fake.store, metadata(store), bytearray(CANARY))
                self.assertEqual(result.outcome, 'durable_cleanup_unknown' if failed == 5 else 'commit_unknown')
                self.assertEqual((fake.path / store.PENDING).exists(), failed != 5)
                self.assertEqual((fake.path / store.RECORD).exists(), failed >= 4)
                self.assertNotIn('PRIVATE-ERROR', repr(result))
            finally:
                fake.close()

    def test_S03_marker_durable_before_any_credential_mutation(self):
        events = []
        real_sync, real_replace = os.fsync, os.replace
        def sync(fd):
            events.append(('sync', os.fstat(fd).st_ino))
            real_sync(fd)
        def replace(*args, **kwargs):
            marker = self.path / store.PENDING
            self.assertTrue(marker.exists())
            self.assertEqual(json.loads(marker.read_bytes()), {'schema': 1, 'operation': 'enter'})
            self.assertEqual(events[0][1], marker.stat().st_ino)
            self.assertEqual(events[1][1], self.path.stat().st_ino)
            self.assertEqual(len(events), 3)
            events.append(('rename', 0))
            return real_replace(*args, **kwargs)
        with patch.object(store.os, 'fsync', sync), patch.object(store.os, 'replace', replace):
            self.assertEqual(self.enter().outcome, 'stored')
        self.assertEqual([kind for kind, _ in events], ['sync', 'sync', 'sync', 'rename', 'sync', 'sync'])

    def test_S03_remove_cleanup_uncertainty_and_precommit_error(self):
        self.enter()
        record = self.record()
        real_sync = os.fsync
        count = 0
        def sync(fd):
            nonlocal count
            count += 1
            if count == 4:
                raise OSError(errno.EIO, 'NONCREDENTIAL')
            real_sync(fd)
        with patch.object(store.os, 'fsync', sync):
            self.assertEqual(store.remove_entry(self.handle, expected=record.identity).outcome, 'durable_cleanup_unknown')
        self.assertFalse((self.path / store.RECORD).exists())
        self.assertFalse((self.path / store.PENDING).exists())
        self.assertEqual(store.reconcile_entry(self.handle, intended='absent').outcome, 'reconciled')

    def test_S03_concurrent_writers_and_lock_substitution(self):
        result = subprocess.run([PYTHON, '-I', '-S', '-B', FIXTURE, 'commit', str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b'busy\n')
        lock = self.path / store.LOCK
        lock.rename(self.path / 'old-lock')
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        self.assertEqual(self.enter().outcome, 'refused')
        self.assertEqual((self.path / 'old-lock').stat().st_size, 0)

    def test_S03_post_flock_identity_substitution_refuses(self):
        self.handle._lock.close()
        real_flock = store.fcntl.flock
        def flock(fd, flags):
            real_flock(fd, flags)
            (self.path / store.LOCK).rename(self.path / 'old-lock')
            newfd = os.open(self.path / store.LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(newfd)
        with patch.object(store.fcntl, 'flock', flock):
            with self.assertRaises(store.Refused):
                store.lock_store(self.handle)
        self.assertIsNone(self.handle._lock)

    def test_S03_real_subprocess_SIGKILL_at_each_crash_barrier(self):
        # Process death tests actual lock release/restart state, not power-loss
        # persistence. Directory fsync failure is exercised separately above.
        for barrier in ('sync-1', 'sync-2', 'sync-3', 'rename', 'sync-4', 'sync-5'):
            with self.subTest(barrier=barrier):
                fake = FakeTree(store)
                try:
                    fake.store._lock.close()
                    child = subprocess.Popen([PYTHON, '-I', '-S', '-B', FIXTURE, 'crash', str(fake.path), barrier], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        self.assertTrue(select.select([child.stdout], [], [], 10)[0], 'crash barrier timed out')
                        self.assertEqual(child.stdout.readline(), b'BARRIER\n')
                        child.kill()
                        child.communicate(timeout=10)
                        self.assertEqual(child.returncode, -signal.SIGKILL)
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.communicate(timeout=10)
                    store.lock_store(fake.store)
                    present = barrier in ('rename', 'sync-4', 'sync-5')
                    self.assertEqual((fake.path / store.RECORD).exists(), present)
                    if barrier != 'sync-5':
                        self.assertTrue((fake.path / store.PENDING).exists())
                        self.assertEqual(store.commit_entry(fake.store, metadata(store), bytearray(CANARY)).reason, 'dirty_store')
                    if present:
                        record = store._read_record(fake.store)
                        try:
                            receipt = store.reconcile_entry(fake.store, intended='present', metadata=record.metadata, secret=bytearray(CANARY))
                        finally:
                            record.close()
                    else:
                        receipt = store.reconcile_entry(fake.store, intended='absent')
                    self.assertEqual(receipt.outcome, 'reconciled')
                    self.assertEqual(sorted(os.listdir(fake.path)), sorted([store.LOCK, store.RECORD] if present else [store.LOCK]))
                finally:
                    fake.close()

    def test_S01_owned_ancestor_mode_and_leaf_substitution_refused(self):
        parent = self.path / 'fake-ancestor'
        parent.mkdir(mode=0o700)
        leaf = parent / 'fake-leaf'
        leaf.mkdir(mode=0o700)
        fd = os.open(leaf / store.LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        handle = store._fixture_store(leaf)
        self.addCleanup(handle.close)
        store.lock_store(handle)
        self.assertEqual(handle._check(), [store.LOCK])
        parent.chmod(0o750)
        with self.assertRaises(store.Refused):
            handle._check()
        parent.chmod(0o700)
        leaf.rename(parent / 'retired-owned-leaf')
        leaf.mkdir(mode=0o700)
        with self.assertRaises(store.Refused):
            handle._check()

    def test_S03_marker_substitution_after_data_durability_is_cleanup_unknown(self):
        real_sync = os.fsync
        count = 0
        def sync(fd):
            nonlocal count
            count += 1
            real_sync(fd)
            if count == 4:
                replacement = self.path / '.owned-swap'
                raw = (self.path / store.PENDING).read_bytes()
                newfd = os.open(replacement, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(newfd, raw)
                    real_sync(newfd)
                finally:
                    os.close(newfd)
                os.replace(replacement, self.path / store.PENDING)
        with patch.object(store.os, 'fsync', sync):
            result = self.enter()
        self.assertEqual(result.outcome, 'durable_cleanup_unknown')
        self.assertTrue((self.path / store.PENDING).exists())
        record = store._read_record(self.handle)
        self.addCleanup(record.close)
        self.assertEqual(record.secret, CANARY)
        self.assertEqual(store.reconcile_entry(self.handle, intended='present', metadata=record.metadata,
                                              secret=bytearray(CANARY)).outcome, 'reconciled')

    def test_S03_replace_error_and_signal_after_rename_preserve_dirty_evidence(self):
        self.enter()
        record = self.record()
        with patch.object(store.os, 'replace', side_effect=OSError(errno.EIO, 'PRIVATE-NONCREDENTIAL-ERROR')):
            receipt = store.commit_entry(self.handle, metadata(store), bytearray(b'NEW-NONCREDENTIAL'), expected=record.identity)
        self.assertEqual(receipt.outcome, 'commit_unknown')
        self.assertNotIn('PRIVATE-NONCREDENTIAL', repr(receipt))
        self.assertEqual(store.reconcile_entry(self.handle, intended='present', metadata=record.metadata,
                                              secret=bytearray(CANARY)).outcome, 'reconciled')
        real_replace = os.replace
        def replace(*args, **kwargs):
            real_replace(*args, **kwargs)
            raise KeyboardInterrupt()
        with patch.object(store.os, 'replace', replace):
            receipt = store.commit_entry(self.handle, metadata(store), bytearray(b'NEW-NONCREDENTIAL'), expected=record.identity)
        self.assertEqual(receipt.outcome, 'commit_unknown')
        self.assertTrue((self.path / store.PENDING).exists())
        self.assertEqual(store._read_record(self.handle).secret, b'NEW-NONCREDENTIAL')

    def test_S03_actual_removal_subprocess_crashes_and_reconciliation(self):
        for barrier in ('unlink', 'sync-3', 'sync-4'):
            with self.subTest(barrier=barrier):
                fake = FakeTree(store)
                try:
                    self.assertEqual(store.commit_entry(fake.store, metadata(store), bytearray(CANARY)).outcome, 'stored')
                    fake.store._lock.close()
                    child = subprocess.Popen([PYTHON, '-I', '-S', '-B', FIXTURE, 'crash-remove', str(fake.path), barrier],
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                        self.assertEqual(child.stdout.readline(), b'BARRIER\n')
                        child.kill()
                        child.communicate(timeout=10)
                        self.assertEqual(child.returncode, -signal.SIGKILL)
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.communicate(timeout=10)
                    store.lock_store(fake.store)
                    self.assertFalse((fake.path / store.RECORD).exists())
                    self.assertEqual((fake.path / store.PENDING).exists(), barrier != 'sync-4')
                    self.assertEqual(store.reconcile_entry(fake.store, intended='absent').outcome, 'reconciled')
                finally:
                    fake.close()

    def test_P01_private_repr_pickle_and_fixed_receipts(self):
        self.enter()
        record = self.record()
        for private in (record, record.metadata, record.metadata.binding, record.identity, self.handle, self.handle._lock):
            self.assertEqual(repr(private), '<private credential state>')
            self.assertNotIn(CANARY.decode(), str(private))
            with self.assertRaises(TypeError):
                pickle.dumps(private)
        receipt = store.EntryReceipt('enter', 'stored', 'complete')
        self.assertEqual(set(receipt.__dict__), {'operation', 'outcome', 'reason'})
        with self.assertRaises(ValueError):
            store.EntryReceipt('enter', 'stored', CANARY.decode())
        record.close()
        self.assertFalse(any(record.secret))

    def restart(self):
        self.handle.close()
        self.handle = store._fixture_store(self.path)
        store.lock_store(self.handle)
        self.fake.store = self.handle

    def test_CV1_lost_token_dirty_remove_discard_and_fresh_entry(self):
        self.enter()
        record = self.record()
        real_unlink = os.unlink
        def unlink(name, **kw):
            if name == store.RECORD:
                raise OSError(errno.EIO, 'OWNED_FAKE')
            return real_unlink(name, **kw)
        with patch.object(store.os, 'unlink', unlink):
            self.assertEqual(store.remove_entry(self.handle, expected=record.identity).outcome, 'commit_unknown')
        self.restart()
        self.assertEqual(store.remove_entry(self.handle, expected=record.identity).reason, 'dirty_store')
        self.assertEqual(store.reconcile_entry(self.handle, intended='absent').reason, 'conflict')
        self.assertEqual(store.reconcile_entry(self.handle, intended='present', metadata=record.metadata,
                                              secret=bytearray(b'LOST-TOKEN-REPLACEMENT')).reason, 'conflict')
        # Discard must never decode/read the record or temporary token contents.
        real_read = store._read_file
        def read(handle, name, cap):
            self.assertEqual(name, store.PENDING)
            return real_read(handle, name, cap)
        with patch.object(store, '_read_record', side_effect=AssertionError('no token read')), \
                patch.object(store, '_decode', side_effect=AssertionError('no token decode')), \
                patch.object(store, '_read_file', read):
            self.assertEqual(store.reconcile_entry(self.handle, intended='discard').outcome, 'reconciled')
        self.assertEqual(os.listdir(self.path), [store.LOCK])
        self.assertEqual(self.enter().outcome, 'stored')

    def test_CV1_dirty_old_new_present_absent_and_orphan_temp(self):
        for record_present, pending, temporary in ((True, 'replace', True), (True, 'enter', False),
                                                   (False, 'remove', False), (False, 'enter', True),
                                                   (False, None, True), (False, None, False)):
            with self.subTest(state=(record_present, pending, temporary)):
                fake = FakeTree(store)
                try:
                    if record_present:
                        self.assertEqual(store.commit_entry(fake.store, metadata(store), bytearray(CANARY)).outcome, 'stored')
                    if pending:
                        store._marker(fake.store, pending)
                    if temporary:
                        store._create(fake.store, '.entry-' + 'c' * 32 + '.tmp', b'INCOMPLETE-NONCREDENTIAL')
                    lock_inode = (fake.path / store.LOCK).stat().st_ino
                    self.assertEqual(store.reconcile_entry(fake.store, intended='discard').outcome, 'reconciled')
                    self.assertEqual(os.listdir(fake.path), [store.LOCK])
                    self.assertEqual((fake.path / store.LOCK).stat().st_ino, lock_inode)
                finally:
                    fake.close()

    def test_CV1_short_write_ENOSPC_partial_marker_and_temp(self):
        real_write = os.write
        for target in (1, 2):
            with self.subTest(write=target):
                fake = FakeTree(store)
                try:
                    calls = 0
                    def write(fd, raw):
                        nonlocal calls
                        calls += 1
                        if calls == target:
                            return real_write(fd, raw[:8 if target == 1 else 13])
                        if calls == target + 1:
                            raise OSError(errno.ENOSPC, 'OWNED_FAKE')
                        return real_write(fd, raw)
                    with patch.object(store.os, 'write', write):
                        self.assertEqual(store.commit_entry(fake.store, metadata(store), bytearray(CANARY)).outcome, 'commit_unknown')
                    self.assertEqual(store.reconcile_entry(fake.store, intended='absent').outcome, 'refused')
                    fake.store.close()
                    fake.store = store._fixture_store(fake.path)
                    store.lock_store(fake.store)
                    self.assertEqual(store.reconcile_entry(fake.store, intended='discard').outcome, 'reconciled')
                    self.assertEqual(os.listdir(fake.path), [store.LOCK])
                finally:
                    fake.close()

    def test_CV1_unknown_marker_and_unsafe_artifacts_refuse_without_mutation(self):
        for kind in ('unknown', 'marker', 'oversize', 'mode', 'symlink', 'hardlink', 'fifo', 'acl'):
            with self.subTest(kind=kind):
                fake = FakeTree(store)
                try:
                    path = fake.path / store.RECORD
                    store._create(fake.store, store.RECORD, b'INCOMPLETE-NONCREDENTIAL')
                    if kind == 'unknown':
                        (fake.path / 'unrecognized').write_bytes(b'FICTION')
                    elif kind == 'marker':
                        store._create(fake.store, store.PENDING, b'{"schema":1,"operation":"unknown"}')
                    elif kind == 'oversize':
                        path.write_bytes(b'x' * (store.MAX_RECORD + 1))
                    elif kind == 'mode':
                        path.chmod(0o640)
                    elif kind == 'symlink':
                        path.unlink(); path.symlink_to(fake.path / 'missing-fiction')
                    elif kind == 'hardlink':
                        # Alias outside the enumerated leaf, still wholly owned.
                        alias = fake.path.parent / (fake.path.name + '-alias')
                        os.link(path, alias)
                        self.addCleanup(alias.unlink)
                    elif kind == 'fifo':
                        path.unlink(); os.mkfifo(path, 0o600)
                    elif kind == 'acl':
                        import struct
                        acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', tag, perm, ident) for tag, perm, ident in
                            [(1, 6, 0xffffffff), (2, 6, os.getuid()), (4, 0, 0xffffffff), (16, 6, 0xffffffff), (32, 0, 0xffffffff)])
                        os.setxattr(path, 'system.posix_acl_access', acl); path.chmod(0o600)
                    before = set(os.listdir(fake.path))
                    self.assertEqual(store.reconcile_entry(fake.store, intended='discard').outcome, 'refused')
                    self.assertEqual(set(os.listdir(fake.path)), before)
                finally:
                    fake.close()

    def test_CV1_discard_requires_lock_and_unchanged_directory(self):
        self.enter()
        self.handle._lock.close()
        self.assertEqual(store.reconcile_entry(self.handle, intended='discard').reason, 'busy')
        store.lock_store(self.handle)
        lock = self.path / store.LOCK
        lock.rename(self.path / 'retired-lock')
        store._create(self.handle, store.LOCK, b'')
        self.assertEqual(store.reconcile_entry(self.handle, intended='discard').outcome, 'refused')
        self.assertTrue((self.path / store.RECORD).exists())
        (self.path / store.LOCK).unlink(); (self.path / 'retired-lock').rename(lock)
        retired = self.path.with_name(self.path.name + '-retired')
        self.path.rename(retired)
        self.path.mkdir(mode=0o700)
        try:
            self.assertEqual(store.reconcile_entry(self.handle, intended='discard').outcome, 'refused')
            self.assertTrue((retired / store.RECORD).exists())
        finally:
            self.path.rmdir(); retired.rename(self.path)

    def test_CV1_discard_substitution_between_validation_and_unlink(self):
        self.enter()
        original = (self.path / store.RECORD).read_bytes()
        real_sync = os.fsync
        calls = 0
        def sync(fd):
            nonlocal calls
            calls += 1
            real_sync(fd)
            if calls == 2:
                replacement = self.path / '.replacement'
                replacement.write_bytes(original); replacement.chmod(0o600)
                os.replace(replacement, self.path / store.RECORD)
        with patch.object(store.os, 'fsync', sync):
            result = store.reconcile_entry(self.handle, intended='discard')
        self.assertEqual(result.outcome, 'commit_unknown')
        self.assertEqual((self.path / store.RECORD).read_bytes(), original)
        self.assertTrue((self.path / store.PENDING).exists())
        self.assertEqual(store.reconcile_entry(self.handle, intended='discard').outcome, 'reconciled')

    def test_CV1_discard_every_fsync_and_unlink_failure_then_restart(self):
        for existing_marker in (False, True):
            for fault in ('sync-1', 'sync-2', 'unlink-record', 'unlink-temp', 'sync-3', 'unlink-marker', 'sync-4'):
                with self.subTest(marker=existing_marker, fault=fault):
                    fake = FakeTree(store)
                    try:
                        store._create(fake.store, store.RECORD, b'INCOMPLETE-OLD')
                        store._create(fake.store, '.entry-' + 'd' * 32 + '.tmp', b'INCOMPLETE-NEW')
                        if existing_marker:
                            store._create(fake.store, store.PENDING, b'{"schema')
                        real_sync, real_unlink = os.fsync, os.unlink
                        calls = 0
                        events = []
                        def sync(fd):
                            nonlocal calls
                            calls += 1
                            events.append('sync-' + str(calls))
                            if fault == events[-1]:
                                raise OSError(errno.EIO, 'OWNED_FAKE')
                            real_sync(fd)
                        def unlink(name, **kw):
                            kind = 'marker' if name == store.PENDING else 'record' if name == store.RECORD else 'temp'
                            events.append('unlink-' + kind)
                            self.assertGreaterEqual(calls, 2)
                            if fault == events[-1]:
                                raise OSError(errno.EIO, 'OWNED_FAKE')
                            return real_unlink(name, **kw)
                        with patch.object(store.os, 'fsync', sync), patch.object(store.os, 'unlink', unlink):
                            result = store.reconcile_entry(fake.store, intended='discard')
                        self.assertEqual(result.outcome, 'durable_cleanup_unknown' if fault in ('unlink-marker', 'sync-4') else 'commit_unknown')
                        if fault in ('sync-1', 'sync-2'):
                            self.assertTrue((fake.path / store.RECORD).exists())
                        fake.store.close(); fake.store = store._fixture_store(fake.path); store.lock_store(fake.store)
                        self.assertEqual(store.reconcile_entry(fake.store, intended='discard').outcome, 'reconciled')
                        self.assertEqual(os.listdir(fake.path), [store.LOCK])
                    finally:
                        fake.close()

    def test_CV1_discard_and_partial_write_process_death_each_barrier(self):
        scenarios = [('crash-discard', barrier) for barrier in
                     ('sync-1', 'sync-2', 'unlink', 'unlink-temp', 'sync-3', 'unlink-marker', 'sync-4')]
        scenarios += [('crash-partial', 'marker'), ('crash-partial', 'temp')]
        for mode, barrier in scenarios:
            with self.subTest(mode=mode, barrier=barrier):
                fake = FakeTree(store)
                try:
                    if mode == 'crash-discard':
                        store._create(fake.store, store.RECORD, b'INCOMPLETE-OLD')
                        store._create(fake.store, '.entry-' + 'e' * 32 + '.tmp', b'INCOMPLETE-NEW')
                    fake.store._lock.close()
                    child = subprocess.Popen([PYTHON, '-I', '-S', '-B', FIXTURE, mode, str(fake.path), barrier],
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        self.assertTrue(select.select([child.stdout], [], [], 10)[0], 'crash barrier timeout')
                        self.assertEqual(child.stdout.readline(), b'BARRIER\n')
                        child.kill(); child.communicate(timeout=10)
                        self.assertEqual(child.returncode, -signal.SIGKILL)
                    finally:
                        if child.poll() is None:
                            child.kill(); child.communicate(timeout=10)
                    fake.store.close(); fake.store = store._fixture_store(fake.path); store.lock_store(fake.store)
                    self.assertEqual(store.reconcile_entry(fake.store, intended='discard').outcome, 'reconciled')
                    self.assertEqual(os.listdir(fake.path), [store.LOCK])
                finally:
                    fake.close()

    def test_CV1_discard_marker_short_write_ENOSPC_preserves_old_then_recovers(self):
        self.enter()
        original = (self.path / store.RECORD).read_bytes()
        real_write = os.write
        calls = 0
        def write(fd, raw):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, raw[:8])
            raise OSError(errno.ENOSPC, 'OWNED_FAKE')
        with patch.object(store.os, 'write', write):
            result = store.reconcile_entry(self.handle, intended='discard')
        self.assertEqual(result.outcome, 'commit_unknown')
        self.assertEqual((self.path / store.RECORD).read_bytes(), original)
        self.assertEqual((self.path / store.PENDING).stat().st_size, 8)
        self.restart()
        self.assertEqual(store.reconcile_entry(self.handle, intended='discard').outcome, 'reconciled')
        self.assertEqual(os.listdir(self.path), [store.LOCK])

    def test_CV1_discard_marker_and_temp_metadata_are_not_relaxed(self):
        for name in (store.PENDING, '.entry-' + 'f' * 32 + '.tmp'):
            for kind in ('mode', 'oversize', 'symlink', 'multiple-temp'):
                with self.subTest(name=name, kind=kind):
                    fake = FakeTree(store)
                    try:
                        store._create(fake.store, name, b'')
                        path = fake.path / name
                        if kind == 'mode':
                            path.chmod(0o640)
                        elif kind == 'oversize':
                            path.write_bytes(b'x' * (129 if name == store.PENDING else store.MAX_RECORD + 1))
                        elif kind == 'symlink':
                            path.unlink(); path.symlink_to(fake.path / 'missing-owned-fixture')
                        else:
                            store._create(fake.store, '.entry-' + '0' * 32 + '.tmp', b'')
                            if name == store.PENDING:
                                store._create(fake.store, '.entry-' + '1' * 32 + '.tmp', b'')
                        before = set(os.listdir(fake.path))
                        self.assertEqual(store.reconcile_entry(fake.store, intended='discard').outcome, 'refused')
                        self.assertEqual(set(os.listdir(fake.path)), before)
                    finally:
                        fake.close()


class ProviderContextTests(unittest.TestCase):
    """Both directions use owned NONcredential fakes; no private path is opened."""
    def setUp(self):
        self.fakes = {}
        for provider in ('hardcover', 'tmdb'):
            fake = FakeTree(store, provider=provider)
            self.addCleanup(fake.close)
            self.fakes[provider] = fake

    def values(self, provider):
        context = self.fakes[provider].store.context
        return metadata(store, binding=store.Binding('unverified', '1', '1', context.provider, context.purpose),
                        scopes=context.scopes)

    def stored(self, provider):
        fake = self.fakes[provider]
        self.assertEqual(store.commit_entry(fake.store, self.values(provider), bytearray(CANARY)).outcome, 'stored')
        return fake

    def test_fixed_provider_paths_and_unknown_or_path_overrides_refused_before_open(self):
        with patch.object(store, '_walk') as walk:
            store.open_store()
            self.assertEqual(walk.call_args.args[0], store.STORE_DIRECTORY.strip('/').split('/'))
            self.assertEqual(walk.call_args.kwargs, {'provider': 'hardcover'})
            store.open_store(provider='tmdb')
            self.assertEqual(walk.call_args.args[0],
                             ['home', 'tang', '.local', 'state', 'culture-cabinet-secrets', 'tmdb-backend'])
            self.assertEqual(walk.call_args.kwargs, {'provider': 'tmdb'})
        with patch.object(store.os, 'open', side_effect=AssertionError('must not open')):
            for provider in ('unknown', '../tmdb', '/tmp/owned', None, {}, store._TMDB):
                with self.subTest(provider=provider), self.assertRaises(store.Refused):
                    store.open_store(provider=provider)
            with self.assertRaises(TypeError):
                store.open_store(directory=str(self.fakes['tmdb'].path))
            with self.assertRaises(TypeError):
                store.open_store(path=str(self.fakes['tmdb'].path))

    def test_matching_contexts_preserve_schema1_and_unverified_roundtrip(self):
        for provider in self.fakes:
            with self.subTest(provider=provider):
                fake = self.stored(provider)
                record = store.read_record(fake.store)
                self.addCleanup(record.close)
                self.assertEqual(record.secret, CANARY)
                self.assertEqual(record.metadata.binding, self.values(provider).binding)
                self.assertEqual(record.metadata.scopes, fake.store.context.scopes)
                self.assertFalse(record.verified)
                self.assertEqual(json.loads((fake.path / store.RECORD).read_bytes())['schema'], 1)
                self.assertEqual(fake.store.context.scopes, record.metadata.scopes)

    def test_cross_provider_commit_and_each_metadata_component_refused_without_mutation(self):
        from dataclasses import replace
        for provider, other in (('hardcover', 'tmdb'), ('tmdb', 'hardcover')):
            fake = self.fakes[provider]
            own, foreign = self.values(provider), self.values(other)
            variants = (foreign, replace(own, binding=replace(own.binding, provider=foreign.binding.provider)),
                        replace(own, binding=replace(own.binding, purpose=foreign.binding.purpose)),
                        replace(own, scopes=foreign.scopes))
            for values in variants:
                with self.subTest(provider=provider, values=values):
                    receipt = store.commit_entry(fake.store, values, bytearray(CANARY))
                    self.assertEqual((receipt.outcome, receipt.reason), ('refused', 'invalid_input'))
                    self.assertEqual(os.listdir(fake.path), [store.LOCK])

    def test_cross_provider_record_swap_read_and_replacement_refused(self):
        originals = {}
        for provider in self.fakes:
            fake = self.stored(provider)
            originals[provider] = (fake.path / store.RECORD).read_bytes()
        for provider, other in (('hardcover', 'tmdb'), ('tmdb', 'hardcover')):
            fake = self.fakes[provider]
            own = json.loads(originals[provider])
            foreign = json.loads(originals[other])
            variants = [foreign]
            for field in ('provider', 'purpose'):
                changed = json.loads(originals[provider])
                changed['binding'][field] = foreign['binding'][field]
                variants.append(changed)
            variants.append({**own, 'scopes': foreign['scopes']})
            for body in variants:
                with self.subTest(provider=provider, body=body):
                    raw = json.dumps(body).encode()
                    (fake.path / store.RECORD).write_bytes(raw)
                    with self.assertRaises(store.Refused):
                        store.read_record(fake.store)
                    _, identity = store._read_file(fake.store, store.RECORD, store.MAX_RECORD)
                    receipt = store.commit_entry(fake.store, self.values(provider), bytearray(CANARY), expected=identity)
                    self.assertEqual(receipt.outcome, 'refused')
                    self.assertEqual((fake.path / store.RECORD).read_bytes(), raw)
                    self.assertFalse((fake.path / store.PENDING).exists())

    def test_tmdb_record_requires_explicit_context_never_default_hardcover(self):
        fake = self.stored('tmdb')
        raw, identity = store._read_file(fake.store, store.RECORD, store.MAX_RECORD)
        with self.assertRaises(store.Refused):
            store._decode(raw, identity)
        explicit = store._decode(raw, identity, provider='tmdb')
        self.addCleanup(explicit.close)
        self.assertEqual(explicit.secret, CANARY)
        fake.store._lock.close()
        default = store._fixture_store(fake.path)
        self.addCleanup(default.close)
        store.lock_store(default)
        with self.assertRaises(store.Refused):
            store.read_record(default)

    def test_cross_provider_reconcile_metadata_record_and_temporary_refused(self):
        for provider, other in (('hardcover', 'tmdb'), ('tmdb', 'hardcover')):
            fake = self.stored(provider)
            store._marker(fake.store, 'replace')
            before = {p.name: p.read_bytes() for p in fake.path.iterdir()}
            receipt = store.reconcile_entry(fake.store, intended='present', metadata=self.values(other), secret=bytearray(CANARY))
            self.assertEqual((receipt.outcome, receipt.reason), ('refused', 'invalid_input'))
            self.assertEqual({p.name: p.read_bytes() for p in fake.path.iterdir()}, before)
            foreign = store._encode(self.values(other), bytearray(CANARY))
            temporary = '.entry-' + 'a' * 32 + '.tmp'
            store._create(fake.store, temporary, foreign)
            receipt = store.reconcile_entry(fake.store, intended='present', metadata=self.values(provider), secret=bytearray(CANARY))
            self.assertEqual(receipt.outcome, 'refused')
            self.assertEqual((fake.path / temporary).read_bytes(), foreign)
            (fake.path / temporary).unlink()
            (fake.path / store.RECORD).write_bytes(foreign)
            receipt = store.reconcile_entry(fake.store, intended='present', metadata=self.values(provider), secret=bytearray(CANARY))
            self.assertEqual(receipt.outcome, 'refused')
            self.assertTrue((fake.path / store.PENDING).exists())

    def test_cross_provider_absent_reconcile_rejects_foreign_temporary(self):
        for provider, other in (('hardcover', 'tmdb'), ('tmdb', 'hardcover')):
            fake = self.fakes[provider]
            store._marker(fake.store, 'enter')
            temporary = '.entry-' + 'b' * 32 + '.tmp'
            store._create(fake.store, temporary, store._encode(self.values(other), bytearray(CANARY)))
            before = {p.name: p.read_bytes() for p in fake.path.iterdir()}
            receipt = store.reconcile_entry(fake.store, intended='absent')
            self.assertEqual(receipt.outcome, 'refused')
            self.assertEqual({p.name: p.read_bytes() for p in fake.path.iterdir()}, before)

    def test_tmdb_local_contract_refuses_user_write_scopes_and_overlong_cap(self):
        from dataclasses import replace
        values = self.values('tmdb')
        for scopes in (store.SCOPES, ('read',), ('write',), ('user-session',), ()):
            receipt = store.commit_entry(self.fakes['tmdb'].store, replace(values, scopes=scopes), bytearray(CANARY))
            self.assertEqual(receipt.outcome, 'refused')
        too_late = values.entered_at + timedelta(days=90)
        receipt = store.commit_entry(self.fakes['tmdb'].store,
                                     replace(values, expires_at=too_late, declared_provider_expires_at=too_late), bytearray(CANARY))
        self.assertEqual(receipt.outcome, 'refused')
