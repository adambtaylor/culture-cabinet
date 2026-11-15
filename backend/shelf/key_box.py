"""Small application API for the separate encrypted key box; no Django setup."""
import ctypes
from pathlib import Path
import resource
import subprocess


class KeyBoxUnavailable(Exception):
    pass


def _call(action, value=None):
    # Protect this process before receiving a plaintext key from the private
    # child pipe. The system Python supplies already-installed GI/cryptography.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None)
    if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(3, 0, 0, 0, 0) != 0:
        raise KeyBoxUnavailable('Secret memory protection is unavailable.')
    helper = Path(__file__).resolve().parents[2] / 'tools/key_box.py'
    try:
        result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(helper), action],
            input=value, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90, check=False)
        if result.returncode or len(result.stdout) > 8192:
            raise KeyBoxUnavailable('The encrypted key box is unavailable.')
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        raise KeyBoxUnavailable('The encrypted key box is unavailable.') from None


def save_openai_key(api_key):
    if type(api_key) is not str or not 0 < len(api_key.encode('utf-8')) <= 8192:
        raise ValueError('A nonempty API key is required.')
    _call('_save', api_key.encode('utf-8'))


def load_openai_key():
    value = _call('_load')
    if not value:
        raise KeyBoxUnavailable('The encrypted key box is empty.')
    return value.decode('utf-8')
