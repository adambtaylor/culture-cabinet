"""Real read-only SQLite behavior; no household database or credential access."""
from pathlib import Path
import sqlite3
import tempfile
from unittest import TestCase
from unittest.mock import patch
from shelf.completion_observer import read_history


class LocalHistoryTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.db'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE history(nzo_id,name,status,storage,completed,script,fail_message,password)')
            db.execute('INSERT INTO history VALUES(?,?,?,?,?,?,?,?)',
                ('SABnzbd_nzo_fixture', 'CC-fixture', 'Completed', '/fixture', 1,
                 'culture-cabinet-completion', '', 'private unrelated value'))

    def test_only_exact_job_fields_returned_and_database_is_unchanged(self):
        before = self.path.read_bytes()
        row = read_history('SABnzbd_nzo_fixture', database=self.path)
        self.assertEqual(row['status'], 'Completed')
        self.assertNotIn('password', row)
        self.assertIsNone(read_history('SABnzbd_nzo_absent', database=self.path))
        self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_ids_symlinks_and_world_writable_database_refuse(self):
        link = self.path.with_name('link.db'); link.symlink_to(self.path)
        with self.assertRaises((ValueError, OSError)): read_history('SABnzbd_nzo_fixture', database=link)
        self.path.chmod(0o666)
        with self.assertRaises(ValueError): read_history('SABnzbd_nzo_fixture', database=self.path)
        self.path.chmod(0o600)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO history SELECT * FROM history')
        with self.assertRaises(ValueError): read_history('SABnzbd_nzo_fixture', database=self.path)

    def test_sqlite_connect_time_path_swap_cannot_substitute_completed_history(self):
        # Reproduces review F1: original code reopened this mutable pathname.
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE history SET status='Running'")
        foreign = self.path.with_name('foreign.db')
        foreign.write_bytes(self.path.read_bytes())
        with sqlite3.connect(foreign) as db:
            db.execute("UPDATE history SET status='Completed'")
        original = sqlite3.connect
        def swap(target, *args, **kwargs):
            held = self.path.with_name('held.db')
            self.path.rename(held)
            self.path.symlink_to(foreign)
            try:
                return original(target, *args, **kwargs)
            finally:
                self.path.unlink()
                held.rename(self.path)
        with patch('shelf.completion_observer.sqlite3.connect', side_effect=swap):
            self.assertEqual(read_history('SABnzbd_nzo_fixture', database=self.path)['status'], 'Running')

    def test_active_journal_or_wal_format_is_unavailable_not_silently_ignored(self):
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = self.path.with_name(self.path.name + suffix)
            sidecar.write_bytes(b'')
            with self.assertRaises(ValueError): read_history('SABnzbd_nzo_fixture', database=self.path)
            sidecar.unlink()
        raw = bytearray(self.path.read_bytes()); raw[18:20] = b'\2\2'
        self.path.write_bytes(raw)
        with self.assertRaises(ValueError): read_history('SABnzbd_nzo_fixture', database=self.path)

    def test_write_or_journal_appearing_during_snapshot_refuses(self):
        import os
        original = os.stat
        for change in ('write', 'journal'):
            before = self.path.read_bytes()
            def change_before_path_check(path, *args, **kwargs):
                if path == self.path.name and kwargs.get('dir_fd') is not None:
                    if change == 'write':
                        with self.path.open('ab') as stream: stream.write(b'changed')
                    else:
                        self.path.with_name(self.path.name + '-journal').write_bytes(b'active')
                return original(path, *args, **kwargs)
            with self.subTest(change=change), patch('shelf.completion_observer.os.stat', side_effect=change_before_path_check):
                with self.assertRaises(ValueError): read_history('SABnzbd_nzo_fixture', database=self.path)
            self.path.write_bytes(before)
            self.path.with_name(self.path.name + '-journal').unlink(missing_ok=True)
