#!/usr/bin/python3
"""One encrypted OpenAI key; its Fernet key belongs to the OS login keyring."""
import argparse
from contextlib import contextmanager
import ctypes
import fcntl
import getpass
import os
from pathlib import Path
import resource
import stat
import sys
import tempfile

from cryptography.fernet import Fernet, InvalidToken

MAX_KEY_BYTES = 8192
BOX_DIRECTORY = Path.home() / '.local/share/culture-cabinet-key-box'
ENCRYPTED_KEYRING_PREFIX = bytes.fromhex('476e6f6d654b657972696e670a0d000a00000000')


class BoxError(Exception):
    """Only fixed messages cross the terminal/process boundary."""


def protect_memory():
    # A piped core collector can ignore the ordinary core-size limit on Linux.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None)
    if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(3, 0, 0, 0, 0) != 0:
        raise BoxError('Secret memory protection is unavailable.')


def private_read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise BoxError('Storage must be a private regular file.')
        return os.read(fd, limit)
    finally:
        os.close(fd)


class LoginKeyring:
    """Standard libsecret storage, isolated from Cabinet's older collection."""
    def __init__(self):
        import gi
        gi.require_version('Secret', '1')
        from gi.repository import Secret
        self.secret = Secret
        self.schema = Secret.Schema.new('org.culturecabinet.KeyBox', Secret.SchemaFlags.NONE,
            {'application': Secret.SchemaAttributeType.STRING})
        self.attributes = {'application': 'culture-cabinet-key-box'}

    def _protected(self):
        # GNOME allows an empty-password keyring. Refuse its plaintext format;
        # inspect only the public header, never the keyring's contents.
        path = Path.home() / '.local/share/keyrings/login.keyring'
        try:
            prefix = private_read(path, 20)
        except FileNotFoundError:
            raise BoxError('Create a password-protected GNOME Login keyring first.') from None
        if prefix != ENCRYPTED_KEYRING_PREFIX:
            raise BoxError('Set a password on the GNOME Login keyring first.')

    def read(self):
        self._protected()
        value = self.secret.password_lookup_sync(self.schema, self.attributes, None)
        self._protected()
        return value.encode('ascii') if value is not None else None

    def write(self, key):
        self._protected()
        if not self.secret.password_store_sync(self.schema, self.attributes,
                '/org/freedesktop/secrets/collection/login', 'Culture Cabinet box unlock key',
                key.decode('ascii'), None):
            raise BoxError('The OS keyring did not save the unlock key.')
        self._protected()
        if self.read() != key:
            raise BoxError('The OS keyring could not confirm the unlock key.')


class KeyBox:
    def __init__(self, directory, keyring):
        self.directory, self.keyring = Path(directory), keyring
        self.path = self.directory / 'openai.fernet'

    @contextmanager
    def _locked(self, *, create=False):
        if create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise BoxError('The key box folder must be private.')
        # A live lock prevents concurrent first saves from replacing each other's
        # unlock key. No device/inode identity is persisted across launches.
        fd = os.open(self.directory / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            lock = os.fstat(fd)
            if (not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.getuid()
                    or stat.S_IMODE(lock.st_mode) != 0o600 or lock.st_nlink != 1):
                raise BoxError('The key box lock must be private.')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def save(self, api_key):
        if (type(api_key) is not str or not api_key or api_key != api_key.strip()
                or any(c.isspace() for c in api_key)
                or len(api_key.encode('utf-8')) > MAX_KEY_BYTES):
            raise BoxError('Enter a nonempty API key without whitespace.')
        with self._locked(create=True):
            key = self.keyring.read()
            if key is None:
                if self.path.exists() or self.path.is_symlink():
                    raise BoxError('The existing box unlock key is missing. The box was preserved.')
                key = Fernet.generate_key()
                self.keyring.write(key)
            cipher = Fernet(key)
            if self.path.exists() or self.path.is_symlink():
                # A damaged or mismatched box is not silently replaced by Save.
                cipher.decrypt(private_read(self.path, 16385))
            token = cipher.encrypt(api_key.encode('utf-8'))
            fd, name = tempfile.mkstemp(prefix='.openai-', dir=self.directory)
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(token)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.path)
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(name):
                    os.unlink(name)

    def load(self):
        with self._locked():
            token = private_read(self.path, 16385)
            key = self.keyring.read()
            if key is None:
                raise BoxError('The box unlock key is missing from the OS keyring.')
            value = Fernet(key).decrypt(token).decode('utf-8')
            if not value or len(value.encode('utf-8')) > MAX_KEY_BYTES:
                raise BoxError('The key box contents are invalid.')
            return value


def main(argv=None):
    parser = argparse.ArgumentParser(description='Save an OpenAI key in Culture Cabinet’s encrypted box.')
    parser.add_argument('action', choices=('save', 'check', '_save', '_load'))
    args = parser.parse_args(argv)
    try:
        protect_memory()
        box = KeyBox(BOX_DIRECTORY, LoginKeyring())
        if args.action == 'save':
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise BoxError('Run Save in your terminal for hidden key entry.')
            box.save(getpass.getpass('OpenAI API key (hidden): '))
            print('API key saved in the encrypted box.')
        elif args.action == 'check':
            box.load()
            print('The encrypted box opened successfully. No API request was made.')
        elif args.action == '_save':
            if not stat.S_ISFIFO(os.fstat(0).st_mode):
                raise BoxError('Private application input is required.')
            box.save(sys.stdin.buffer.read(MAX_KEY_BYTES + 1).decode('utf-8'))
        else:
            if not stat.S_ISFIFO(os.fstat(1).st_mode):
                raise BoxError('Private application output is required.')
            # The app captures this pipe; the public terminal commands never
            # print the API key. Keys never enter arguments or the environment.
            sys.stdout.buffer.write(box.load().encode('utf-8'))
        return 0
    except BoxError as error:
        print(str(error), file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print('Key entry cancelled.', file=sys.stderr)
    except InvalidToken:
        print('The box could not be decrypted. Existing files were preserved.', file=sys.stderr)
    except Exception:
        print('The key box is unavailable. Check the OS keyring and private folder.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
