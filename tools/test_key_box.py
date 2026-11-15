"""Real encryption with disposable fixtures; no owner keyring or provider calls."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import key_box

DUMMY = 'public-fictional-openai-key-for-key-box-test'


class MemoryKeyring:
    def __init__(self):
        self.key = None
        self.writes = 0

    def read(self):
        return self.key

    def write(self, value):
        self.key = value
        self.writes += 1


class KeyBoxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.keyring = MemoryKeyring()
        self.box = key_box.KeyBox(Path(self.temp.name) / 'box', self.keyring)

    def test_save_encrypts_and_new_instance_loads_without_reentry(self):
        self.box.save(DUMMY)
        self.assertNotIn(DUMMY.encode(), self.box.path.read_bytes())
        self.assertNotIn(self.keyring.key, self.box.path.read_bytes())
        self.assertEqual(self.box.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.box.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key_box.KeyBox(self.box.directory, self.keyring).load(), DUMMY)
        self.box.save(DUMMY + '-replacement')
        self.assertEqual(self.box.load(), DUMMY + '-replacement')
        self.assertEqual(self.keyring.writes, 1)

    def test_missing_unlock_key_never_regenerates_over_existing_ciphertext(self):
        self.box.save(DUMMY)
        before = self.box.path.read_bytes()
        self.keyring.key = None
        with self.assertRaises(key_box.BoxError):
            self.box.load()
        with self.assertRaises(key_box.BoxError):
            self.box.save('replacement')
        self.assertEqual(self.box.path.read_bytes(), before)
        self.assertEqual(self.keyring.writes, 1)

    def test_tampering_or_wrong_key_cannot_decrypt_or_replace_box(self):
        self.box.save(DUMMY)
        self.box.path.write_bytes(b'corrupted-ciphertext')
        with self.assertRaises(key_box.InvalidToken):
            self.box.load()
        with self.assertRaises(key_box.InvalidToken):
            self.box.save('replacement')
        self.assertEqual(self.box.path.read_bytes(), b'corrupted-ciphertext')

    def test_failed_atomic_replace_preserves_previous_box_and_cleans_tempfile(self):
        self.box.save(DUMMY)
        before = self.box.path.read_bytes()
        with patch.object(key_box.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.box.save('replacement')
        self.assertEqual(self.box.path.read_bytes(), before)
        self.assertEqual({p.name for p in self.box.directory.iterdir()}, {'.lock', 'openai.fernet'})

    def test_symlink_and_public_ciphertext_refuse(self):
        self.box.save(DUMMY)
        self.box.path.chmod(0o644)
        with self.assertRaises(key_box.BoxError):
            self.box.load()
        self.box.path.chmod(0o600)
        target = self.box.path.with_name('original')
        self.box.path.rename(target)
        self.box.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.box.load()
        self.assertNotIn(DUMMY.encode(), target.read_bytes())

    def test_private_entry_and_error_output_never_echo_the_key(self):
        with patch.object(key_box, 'protect_memory'), patch.object(key_box, 'LoginKeyring'), \
                patch.object(key_box.KeyBox, 'save', side_effect=RuntimeError(DUMMY)), \
                patch.object(key_box.sys.stdin, 'isatty', return_value=True), \
                patch.object(key_box.sys.stderr, 'isatty', return_value=True), \
                patch.object(key_box.getpass, 'getpass', return_value=DUMMY), \
                patch.object(key_box.sys.stderr, 'write') as output:
            self.assertEqual(key_box.main(['save']), 1)
        self.assertNotIn(DUMMY, ''.join(call.args[0] for call in output.call_args_list))

    def test_fresh_process_loads_real_ciphertext_with_test_only_key_custody(self):
        self.box.save(DUMMY)
        # Fixture key is passed through a private pipe, not argv/environment.
        # This proves process independence and encryption, not actual OS custody.
        program = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from key_box import KeyBox, protect_memory
protect_memory()
class FixtureKeyring:
    def read(self): return sys.stdin.buffer.read(100)
value = KeyBox(Path(sys.argv[2]), FixtureKeyring()).load()
assert value == 'public-fictional-openai-key-for-key-box-test'
print('FRESH_PROCESS_LOAD_PASS')
"""
        result = subprocess.run([sys.executable, '-I', '-B', '-c', program,
            str(Path(key_box.__file__).parent), str(self.box.directory)], input=self.keyring.key,
            capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'FRESH_PROCESS_LOAD_PASS\n')


class ApplicationBridgeTests(unittest.TestCase):
    def test_app_uses_private_pipes_and_fixed_system_helper(self):
        path = Path(__file__).resolve().parents[1] / 'backend/shelf/key_box.py'
        spec = importlib.util.spec_from_file_location('app_key_box', path)
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        with patch.object(app.resource, 'setrlimit'), patch.object(app.ctypes, 'CDLL') as libc, \
                patch.object(app.subprocess, 'run') as run:
            libc.return_value.prctl.return_value = 0
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b'')
            app.save_openai_key(DUMMY)
            args, kwargs = run.call_args
            self.assertEqual(args[0][:3], ['/usr/bin/python3', '-I', '-B'])
            self.assertEqual(args[0][-1], '_save')
            self.assertNotIn(DUMMY, repr(args))
            self.assertEqual(kwargs['input'], DUMMY.encode())
            run.return_value = subprocess.CompletedProcess([], 0, stdout=DUMMY.encode())
            self.assertEqual(app.load_openai_key(), DUMMY)
            run.return_value = subprocess.CompletedProcess([], 1, stdout=DUMMY.encode())
            with self.assertRaises(app.KeyBoxUnavailable) as error:
                app.load_openai_key()
            self.assertNotIn(DUMMY, str(error.exception))


class LoginKeyringTests(unittest.TestCase):
    def test_plaintext_or_missing_login_keyring_refuses_before_any_secret_call(self):
        keyring = key_box.LoginKeyring()  # Imports the library; does not connect.
        with patch.object(keyring, 'secret') as secret, \
                patch.object(key_box, 'private_read', return_value=b'[keyring]\n'):
            with self.assertRaises(key_box.BoxError):
                keyring.read()
            secret.password_lookup_sync.assert_not_called()
        with patch.object(keyring, 'secret') as secret, \
                patch.object(key_box, 'private_read', side_effect=FileNotFoundError):
            with self.assertRaises(key_box.BoxError):
                keyring.write(b'fictional')
            secret.password_store_sync.assert_not_called()

    def test_unlock_key_goes_to_login_collection_with_nonsecret_attributes(self):
        keyring = key_box.LoginKeyring()
        key = key_box.Fernet.generate_key()
        with patch.object(keyring, 'secret') as secret, \
                patch.object(key_box, 'private_read', return_value=key_box.ENCRYPTED_KEYRING_PREFIX):
            secret.password_lookup_sync.return_value = key.decode('ascii')
            secret.password_store_sync.return_value = True
            keyring.write(key)
            args = secret.password_store_sync.call_args.args
            self.assertEqual(args[2], '/org/freedesktop/secrets/collection/login')
            self.assertNotIn(key.decode('ascii'), repr(args[1]))
            self.assertEqual(args[4], key.decode('ascii'))


if __name__ == '__main__':
    unittest.main()
