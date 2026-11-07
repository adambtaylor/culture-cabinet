#!/usr/bin/env python3
"""Bounded Apple TV delivery, with durable uncertainty and read-only reconciliation.

No player or UI is opened. A watchfolder handoff is an import attempt, not proof
of library ingestion. Once reserved, that attempt is never repeated, including
after a crash between reservation and rename.
"""
import argparse
from contextlib import contextmanager
import ctypes
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import time

from mac_kodi_consumer import (require, digest, encoded, is_hash, safe_directory,
    read_json, save_receipt, sync_directory, file_signature)
from mac_completion_dispatcher import bounded_run, decode

EXPIRY = '2026-11-06T04:17:29+00:00'
REMOTE = '/home/tang/Projects/culture-cabinet'
WATCHFOLDER = Path('/Users/adamtaylor/Movies/TV/Media.localized/Automatically Add to TV.localized')
LIBRARY = Path('/Users/adamtaylor/Movies/TV/Media.localized/TV Shows')
MAX_RECEIPTS = 10000
MAX_MEDIA_BYTES = 1024 ** 4
ERRORS = (OSError, ValueError, KeyError, TypeError, RuntimeError, TimeoutError, subprocess.TimeoutExpired)


class RemuxBlocked(ValueError):
    """An actionable policy category, never an untrusted child error payload."""
    def __init__(self, code):
        from apple_tv_media import BLOCKED_REASONS
        require(code in BLOCKED_REASONS, 'Unknown remux refusal category.')
        self.code = code
        super().__init__(code)


@contextmanager
def termination_signals():
    """Unwind owned children and journals before the dispatcher's hard deadline.

    Only the CLI installs handlers. After the first signal, further signals
    cannot interrupt child cleanup or the final durable receipt checkpoint.
    """
    previous, interrupted = {}, False
    def interrupt(signum, frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise InterruptedError('Apple TV consumer interrupted.')
    try:
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def utcnow():
    return datetime.now(timezone.utc)


def expiry(value, clock):
    end = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(end.tzinfo is not None and clock() < end <= datetime.fromisoformat(EXPIRY),
            'Current approved session required.')
    return end


def validate_contract(contract):
    from apple_tv_media import validate_contract as validate
    validate(contract)
    return contract


def verify_asset(path, derivative, *, timeout, clock=utcnow, expires=EXPIRY):
    """Hash an exact pinned regular file within the operation/session deadline."""
    safe_directory(Path(path).parent)
    end = expiry(expires, clock)
    deadline = time.monotonic() + timeout
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size == derivative['bytes'],
                'Media file differs or is unsafe.')
        hashed, count = hashlib.sha256(), 0
        while count < derivative['bytes']:
            require(clock() < end and time.monotonic() < deadline, 'Asset verification deadline reached.')
            block = stream.read(min(1024 ** 2, derivative['bytes'] - count))
            require(bool(block), 'Media ended early.')
            hashed.update(block)
            count += len(block)
        require(not stream.read(1) and hashed.hexdigest() == derivative['sha256']
                and file_signature(before) == file_signature(os.fstat(stream.fileno()))
                and file_signature(before) == file_signature(os.stat(path, follow_symlinks=False)),
                'Media changed during verification.')
    return file_signature(before)


def atomic_handoff(source, target):
    """An exclusive rename: never replace a watchfolder entry that raced us."""
    library = ctypes.CDLL(None, use_errno=True)
    if hasattr(library, 'renamex_np'):  # macOS RENAME_EXCL
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(target), 4)
    elif hasattr(library, 'renameat2'):  # Linux test/development host
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    else:
        raise ValueError('Exclusive atomic rename unavailable.')
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def validate_derivative(result, contract):
    if type(result) is dict and result.get('status') == 'blocked':
        require(type(result.get('version')) is int and result['version'] == 1
                and result.get('contract_sha256') == contract['contract_sha256']
                and result.get('source_sha256') == contract['source']['sha256'], 'Foreign blocked remux result.')
        raise RemuxBlocked(result.get('reason'))
    require(type(result) is dict and type(result.get('version')) is int and result['version'] == 1
            and result.get('contract_sha256') == contract['contract_sha256']
            and result.get('source_sha256') == contract['source']['sha256']
            and result.get('remote_path') == REMOTE + '/.apple-tv-work/' + contract['contract_sha256'] + '/episode.mp4'
            and is_hash(result.get('sha256')) and type(result.get('bytes')) is int
            and 0 < result['bytes'] <= MAX_MEDIA_BYTES, 'Derivative binding differs.')
    proof = result.get('validation')
    require(type(proof) is dict and proof.get('valid') is True
            and is_hash(proof.get('plan_sha256')), 'Validated remux proof required.')
    from apple_tv_media import episode_metadata
    require(proof.get('metadata') == episode_metadata(contract['episode'])
            and type(proof.get('duration_seconds')) in (int, float)
            and math.isfinite(proof['duration_seconds']) and 0 < proof['duration_seconds'] <= 7 * 86400
            and proof.get('hdr_display_verified') is False, 'Remux episode/duration proof differs.')
    for label in ('video', 'audio'):
        packets = proof.get('packet_integrity', {}).get(label, {})
        require(type(packets.get('source_packets')) is int and packets['source_packets'] > 0
                and packets.get('derivative_packets') == packets['source_packets']
                and packets.get('identical_packet_payload_hashes') is True, 'Remux packet evidence missing.')
    return result


def validate_native(value, derivative):
    require(type(value) is dict and value.get('is_playable') is True
            and value.get('playback_started') is False
            and type(value.get('duration_seconds')) in (int, float)
            and math.isfinite(value['duration_seconds']) and 0 < value['duration_seconds'] <= 7 * 86400,
            'Native playable asset proof required.')
    expected = derivative['validation']['duration_seconds']
    require(type(expected) in (int, float) and math.isfinite(expected)
            and abs(value['duration_seconds'] - expected) <= 1, 'Native duration differs.')
    tracks = value.get('tracks')
    require(type(tracks) is list and 2 <= len(tracks) <= 32, 'Native codec formats required.')
    codecs = {}
    for track in tracks:
        require(type(track) is dict and type(track.get('formats')) is list and track['formats'],
                'Native format descriptions missing.')
        require(all(type(f) is dict and type(f.get('codec')) is str for f in track['formats']),
                'Native codec description malformed.')
        codecs.setdefault(track.get('type'), set()).update(f.get('codec') for f in track['formats'])
    require(bool(codecs.get('vide')) and codecs['vide'] <= {'hvc1', 'avc1'}
            and bool(codecs.get('soun')) and codecs['soun'] <= {'ec-3', 'ac-3', 'mp4a'},
            'Native codec unsupported.')
    entry = derivative['validation'].get('atoms', {}).get('video_sample_entry')
    require(entry in ('hvc1', 'avc1') and codecs['vide'] == {entry}, 'Native video differs from validated remux.')
    audio_tracks = [track for track in tracks if track.get('type') == 'soun']
    require(len(audio_tracks) == 1 and audio_tracks[0].get('language') in ('eng', 'en'),
            'Native English audio required.')
    audio_entry = derivative['validation'].get('atoms', {}).get('audio_sample_entry')
    require(audio_entry in ('ec-3', 'ac-3', 'mp4a') and codecs['soun'] == {audio_entry},
            'Native audio differs from validated remux.')
    if derivative['validation'].get('atmos') is True:
        require(codecs['soun'] == {'ec-3'}, 'Native Atmos audio sample entry differs.')
    return value


def live_remux(contract, *, expires, timeout):
    command = [REMOTE + '/.venv/bin/python', '-B', REMOTE + '/tools/apple_tv_remux.py', '--expires', expires]
    code, raw = bounded_run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                             'omarchy', shlex.join(command)], encoded(contract), timeout=timeout,
                            expires=datetime.fromisoformat(expires.replace('Z', '+00:00')), clock=utcnow)
    require(code == 0, 'Remote remux unavailable.')
    return decode(raw)


def live_transfer(derivative, destination, *, expires, timeout):
    """Stream exactly the validated derivative; never scp a tree or buffer media."""
    expected_path = REMOTE + '/.apple-tv-work/' + derivative['contract_sha256'] + '/episode.mp4'
    require(derivative['remote_path'] == expected_path, 'Unexpected remote derivative.')
    end = expiry(expires, utcnow)
    deadline = time.monotonic() + min(timeout, (end - utcnow()).total_seconds())
    # The remote reader rejects symlinks in every component and limits bytes.
    reader = """import os,stat,sys
p=sys.argv[1]; count=int(sys.argv[2]); fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
try:
 for part in p.split('/')[1:-1]:
  nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
 stream=os.open(p.split('/')[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
 info=os.fstat(stream)
 assert stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size==count
 with os.fdopen(stream,'rb') as src:
  while count:
   block=src.read(min(1048576,count));assert block;sys.stdout.buffer.write(block);count-=len(block)
  assert not src.read(1)
finally: os.close(fd)
"""
    args = [REMOTE + '/.venv/bin/python', '-B', '-c', reader, expected_path, str(derivative['bytes'])]
    child = subprocess.Popen(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                              'omarchy', shlex.join(args)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output, selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            count = 0
            while selector.get_map():
                left = min(deadline - time.monotonic(), (end - utcnow()).total_seconds())
                require(left > 0, 'Transfer deadline reached.')
                for key, _ in selector.select(min(left, .25)):
                    block = os.read(key.fileobj.fileno(), min(1024 ** 2, derivative['bytes'] + 1 - count))
                    if not block:
                        selector.unregister(key.fileobj)
                    else:
                        count += len(block)
                        require(count <= derivative['bytes'], 'Remote media grew.')
                        output.write(block)
            output.flush()
            os.fsync(output.fileno())
        left = min(deadline - time.monotonic(), (end - utcnow()).total_seconds())
        require(left > 0 and child.wait(timeout=left) == 0 and count == derivative['bytes'],
                'Transfer incomplete.')
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        child.stdout.close()


def live_native_probe(path, *, timeout):
    # Compile cache belongs only to this invocation, never the user's HOME or
    # the readonly owner checkout. It contains no second media copy.
    with tempfile.TemporaryDirectory(prefix='cabinet-tv-native-') as cache:
        code, raw = bounded_run(['/usr/bin/swift', '-module-cache-path', cache,
                                 str(Path(__file__).with_name('apple_tv_native.swift')), str(path)],
                                timeout=min(timeout, 120), expires=datetime.fromisoformat(EXPIRY), clock=utcnow)
    require(code == 0, 'Native inspection unavailable.')
    return decode(raw)


def mp4_metadata(path):
    """Read the small moov metadata only; numeric TV tags use big-endian data."""
    found = {}
    def atoms(data, start=0):
        cursor = start
        while cursor < len(data):
            require(cursor + 8 <= len(data), 'Truncated metadata atom.')
            size, kind = struct.unpack('>I4s', data[cursor:cursor + 8])
            require(8 <= size <= len(data) - cursor, 'Invalid metadata atom.')
            yield kind, data[cursor + 8:cursor + size]
            cursor += size
    def visit(data):
        for kind, payload in atoms(data):
            if kind in (b'udta', b'ilst'):
                visit(payload)
            elif kind == b'meta':
                require(len(payload) >= 4, 'Invalid metadata header.')
                visit(payload[4:])
            elif kind in (b'tvsh', b'tvsn', b'tves', b'\xa9nam'):
                for child, value in atoms(payload):
                    if child == b'data':
                        require(len(value) >= 8 and kind not in found, 'Invalid or duplicate TV metadata.')
                        found[kind] = int.from_bytes(value[8:], 'big') if kind in (b'tvsn', b'tves') else value[8:].decode('utf-8')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'Unsafe library asset.')
        offset = 0
        while offset < before.st_size:
            head = stream.read(8)
            require(len(head) == 8, 'Truncated MP4.')
            size, kind = struct.unpack('>I4s', head)
            header = 8
            if size == 1:
                large = stream.read(8)
                require(len(large) == 8, 'Truncated MP4 size.')
                size, header = int.from_bytes(large, 'big'), 16
            if size == 0:
                size = before.st_size - offset
            require(header <= size <= before.st_size - offset, 'Invalid MP4 size.')
            if kind == b'moov':
                require(size - header <= 32 * 1024 ** 2, 'Metadata exceeds bound.')
                visit(stream.read(size - header))
                break
            stream.seek(size - header, 1)
            offset += size
        require(file_signature(before) == file_signature(os.fstat(stream.fileno())), 'Library metadata changed.')
    require(set(found) == {b'tvsh', b'tvsn', b'tves', b'\xa9nam'}, 'Exact episode metadata missing.')
    return dict(zip(('show', 'season', 'episode', 'title'), (found[k] for k in (b'tvsh', b'tvsn', b'tves', b'\xa9nam'))))


def observe_exact(path, episode, derivative, *, native_probe=live_native_probe, timeout=120):
    path = Path(path).absolute()
    if not path.exists() and not path.is_symlink():
        return None
    safe_directory(path.parent)
    signature = verify_asset(path, derivative, timeout=timeout)
    before = path.lstat()
    require(signature == file_signature(before), 'Library path changed after hashing.')
    metadata = mp4_metadata(path)
    require(all(metadata[k] == episode[k] for k in ('show', 'season', 'episode')), 'Library episode metadata differs.')
    title = derivative['validation'].get('metadata', {}).get('title')
    require(type(title) is str and metadata['title'] == title, 'Library title metadata differs.')
    native = validate_native(native_probe(path, timeout=timeout), derivative)
    require(file_signature(before) == file_signature(path.lstat()), 'Library file changed during observation.')
    return {'path': str(path), 'sha256': derivative['sha256'], 'bytes': derivative['bytes'],
            'metadata': metadata, 'native': native}


def live_library(episode, derivative, *, timeout):
    title = derivative['validation']['metadata']['title']
    require(all(type(v) is str and v not in ('', '.', '..') and '/' not in v and '\x00' not in v
                for v in (episode['show'], title)), 'Exact library path cannot be constructed.')
    return observe_exact(LIBRARY / episode['show'] / ('Season ' + str(episode['season'])) / (title + '.mp4'),
                         episode, derivative, timeout=timeout)


def validate_library(value, episode, derivative):
    if value is None:
        return None
    require(type(value) is dict and value.get('sha256') == derivative['sha256']
            and value.get('bytes') == derivative['bytes'] and type(value.get('path')) is str
            and Path(value['path']).is_absolute(), 'Exact library file proof required.')
    metadata = value['metadata']
    require(type(metadata) is dict and all(metadata.get(k) == episode[k] for k in ('show', 'season', 'episode'))
            and metadata.get('title') == derivative['validation']['metadata']['title'], 'Library metadata binding differs.')
    validate_native(value['native'], derivative)
    return value


def validate_receipt(value, contract):
    require(type(value) is dict and value.get('version') == 1 and value.get('destination') == 'apple_tv'
            and value.get('contract_sha256') == contract['contract_sha256']
            and value.get('request_id') == contract['selection']['provenance']['request_id']
            and value.get('source_sha256') == contract['source']['sha256']
            and value.get('source_bytes') == contract['source']['bytes'] and value.get('episode') == contract['episode']
            and value.get('playback_started') is False and type(value.get('import_attempts')) is int
            and value['import_attempts'] in (0, 1)
            and value.get('status') in ('blocked', 'remux_ready', 'handoff_pending', 'unknown', 'library_import_observed'),
            'Foreign Apple TV receipt.')
    created, checked = (datetime.fromisoformat(value[k].replace('Z', '+00:00')) for k in ('created_at', 'checked_at'))
    require(created.tzinfo is not None and checked.tzinfo is not None
            and created <= checked <= datetime.fromisoformat(EXPIRY), 'Invalid receipt chronology.')
    if value['derivative'] is not None:
        validate_derivative(value['derivative'], contract)
    if value['native'] is not None:
        validate_native(value['native'], value['derivative'])
    if value['status'] in ('handoff_pending', 'unknown'):
        require(value['import_attempts'] == 1 and type(value['handoff']) is dict, 'Durable handoff reservation required.')
    if value['import_attempts']:
        require(value['native'] is not None and value['derivative'] is not None and type(value['handoff']) is dict,
                'Validated handoff reservation required.')
        target = WATCHFOLDER / ('CC-' + value['request_id'] + '-S%02dE%02d-' %
                               (value['episode']['season'], value['episode']['episode']) + value['derivative']['sha256'] + '.mp4')
        require(value['handoff'].get('path') == str(target), 'Foreign watchfolder handoff.')
        reserved = datetime.fromisoformat(value['handoff']['reserved_at'].replace('Z', '+00:00'))
        require(reserved.tzinfo is not None and created <= reserved <= checked, 'Invalid handoff reservation chronology.')
        if value['handoff']['delivered_at'] is not None:
            delivered = datetime.fromisoformat(value['handoff']['delivered_at'].replace('Z', '+00:00'))
            require(delivered.tzinfo is not None and reserved <= delivered <= checked, 'Invalid handoff delivery chronology.')
    if value['status'] == 'library_import_observed':
        require(value['native'] is not None and value['library'] is not None, 'Observed library proof required.')
        validate_library(value['library'], value['episode'], value['derivative'])
        require(value['native'] == value['library']['native'], 'Native library proof differs.')
    return value


def consume(contract, state_dir, execute=False, remux=None, transfer=None, native_probe=None,
            library=None, expires=EXPIRY, clock=utcnow, timeout=3600):
    validate_contract(contract)
    require(type(execute) is bool and type(timeout) in (int, float) and 0 < timeout <= 7200, 'Invalid execution bounds.')
    expiry(expires, clock)
    deadline = time.monotonic() + timeout
    def budget():
        remaining = min(deadline - time.monotonic(), (expiry(expires, clock) - clock()).total_seconds())
        require(remaining > 0, 'Consumer deadline reached.')
        return remaining
    now = clock().isoformat()
    receipt = {'version': 1, 'destination': 'apple_tv', 'contract_sha256': contract['contract_sha256'],
        'request_id': contract['selection']['provenance']['request_id'], 'source_sha256': contract['source']['sha256'],
        'source_bytes': contract['source']['bytes'], 'episode': deepcopy(contract['episode']), 'status': 'blocked',
        'created_at': now, 'checked_at': now, 'playback_started': False, 'import_attempts': 0,
        'derivative': None, 'native': None, 'handoff': None, 'library': None, 'reason': 'execution_not_authorized'}
    if not execute:
        return receipt
    require(not contract['completion']['files'][0]['test_only'] or all((remux, transfer, native_probe, library)),
            'Synthetic contracts require all injected adapters.')
    state = safe_directory(state_dir, create=True, private=True)
    lockfd = os.open(state / 'consumer.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, 'Unsafe consumer lock.')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = state / (receipt['request_id'] + '.json')
        def checkpoint():
            # Every recoverable snapshot must be admissible at the backend,
            # including a crash immediately after reservation or delivery.
            receipt['checked_at'] = clock().isoformat()
            save_receipt(path, receipt)
        if path.exists() or path.is_symlink():
            receipt = validate_receipt(read_json(path, private=True), contract)
            if receipt['status'] == 'library_import_observed':
                return receipt
        journals = list(state.glob('*.json'))
        require(len(journals) < MAX_RECEIPTS or path.exists(), 'Receipt capacity reached.')
        # Same source and episode under another request must reconcile that
        # reservation, never launch a second derivative transfer/import.
        for other in journals:
            if other == path:
                continue
            saved = read_json(other, private=True)
            old_episode = saved.get('episode', {})
            same_episode = all(old_episode.get(k) == receipt['episode'][k] for k in ('show', 'source_id', 'season', 'episode'))
            if saved.get('source_sha256') == receipt['source_sha256'] and same_episode:
                if saved.get('import_attempts') or saved.get('library'):
                    # The existing derivative is bound to the first request;
                    # reuse only read-only exact library evidence, not remux state.
                    try:
                        proof = validate_library((library or live_library)(receipt['episode'], saved['derivative'], timeout=budget()),
                                                 receipt['episode'], saved['derivative'])
                    except ERRORS:
                        proof = None
                    receipt.update(status='blocked', reason='duplicate_source_episode_already_observed' if proof else
                                   'duplicate_source_episode_reconciliation_required', checked_at=clock().isoformat())
                    # Preserve the original binding; do not publish a foreign
                    # contract derivative as a new Cabinet receipt.
                    checkpoint()
                    return receipt
        checkpoint()
        try:
            expiry(expires, clock)
            if receipt['import_attempts']:
                proof = validate_library((library or live_library)(receipt['episode'], receipt['derivative'], timeout=budget()),
                                         receipt['episode'], receipt['derivative'])
                receipt.update(library=proof, status='library_import_observed' if proof else 'unknown',
                               reason=None if proof else 'handoff_reconciliation_required')
                if proof:
                    receipt['native'] = proof['native']
                else:
                    target = Path(receipt['handoff']['path'])
                    if target.exists() or target.is_symlink():
                        verify_asset(target, receipt['derivative'], timeout=budget(), clock=clock, expires=expires)
                        receipt.update(status='handoff_pending', reason='watchfolder_delivery_is_not_library_observation')
            else:
                if receipt['derivative'] is None:
                    receipt['derivative'] = validate_derivative((remux or live_remux)(contract, expires=expires, timeout=budget()), contract)
                    receipt.update(status='remux_ready', reason=None)
                    checkpoint()
                derivative = receipt['derivative']
                # Existing exact library evidence is checked before staging any
                # bytes, including a prior manual import of this derivative.
                proof = validate_library((library or live_library)(receipt['episode'], derivative, timeout=budget()), receipt['episode'], derivative)
                if proof:
                    receipt.update(status='library_import_observed', library=proof, native=proof['native'], reason=None)
                else:
                    watch = safe_directory(WATCHFOLDER)  # Never create/configure TV's watchfolder.
                    staging = safe_directory(watch.parent / '.cabinet-apple-tv-staging', create=True, private=True)
                    require(staging.stat().st_dev == watch.stat().st_dev, 'Cross-device handoff refused.')
                    staged = staging / (receipt['request_id'] + '-' + contract['contract_sha256'] + '.mp4')
                    if not staged.exists() and not staged.is_symlink():
                        if shutil.disk_usage(staging).free < derivative['bytes'] + 64 * 1024 ** 2:
                            raise RemuxBlocked('insufficient_disk_space')
                        expiry(expires, clock)
                        (transfer or live_transfer)(derivative, staged, expires=expires, timeout=budget())
                    signature = verify_asset(staged, derivative, timeout=budget(), clock=clock, expires=expires)
                    receipt['native'] = validate_native((native_probe or live_native_probe)(staged, timeout=budget()), derivative)
                    require(signature == file_signature(staged.lstat()), 'Staged asset changed during native inspection.')
                    target = watch / ('CC-' + receipt['request_id'] + '-S%02dE%02d-' % (receipt['episode']['season'], receipt['episode']['episode']) + derivative['sha256'] + '.mp4')
                    require(not target.exists() and not target.is_symlink(), 'Watchfolder target already exists; reconcile.')
                    expiry(expires, clock)
                    receipt.update(status='handoff_pending', import_attempts=1, reason=None,
                                   handoff={'path': str(target), 'reserved_at': clock().isoformat(), 'delivered_at': None})
                    checkpoint()
                    # Reservation is the point of no retry, even if rename raises
                    # or the process dies before the next journal checkpoint.
                    require(signature == file_signature(staged.lstat()), 'Staged asset changed before handoff.')
                    safe_directory(watch)
                    require(staged.stat().st_dev == watch.stat().st_dev, 'Cross-device handoff refused.')
                    require(not target.exists() and not target.is_symlink(), 'Watchfolder target appeared.')
                    atomic_handoff(staged, target)
                    sync_directory(staging)
                    sync_directory(watch)
                    receipt['handoff']['delivered_at'] = clock().isoformat()
                    checkpoint()
                    proof = validate_library((library or live_library)(receipt['episode'], derivative, timeout=budget()), receipt['episode'], derivative)
                    receipt.update(library=proof, status='library_import_observed' if proof else 'handoff_pending',
                                   reason=None if proof else 'watchfolder_delivery_is_not_library_observation')
                    if proof:
                        receipt['native'] = proof['native']
        except RemuxBlocked as error:
            receipt.update(status='unknown' if receipt['import_attempts'] else 'blocked',
                           reason='handoff_reconciliation_required' if receipt['import_attempts'] else error.code)
        except ERRORS:
            receipt.update(status='unknown' if receipt['import_attempts'] else 'blocked',
                           reason='handoff_reconciliation_required' if receipt['import_attempts'] else 'verification_or_adapter_blocked')
        checkpoint()
        return receipt


def reconcile_existing_import(record, *, native_probe=None, timeout=120):
    """Owner-confirmed manual basis only; never emit a Cabinet receipt or copy.

    The trusted record binds the original source hash to an exact already-known
    library path. Current hash, MP4 tags and native compatibility are rechecked.
    """
    require(type(timeout) in (int, float) and 0 < timeout <= 7200, 'Invalid observation timeout.')
    require(type(record) is dict and record.get('basis') == 'owner_confirmed_existing_import'
            and is_hash(record.get('source_sha256')) and is_hash(record.get('sha256'))
            and type(record.get('bytes')) is int and 0 < record['bytes'] <= MAX_MEDIA_BYTES,
            'Trusted manual import record required.')
    derivative = {'sha256': record['sha256'], 'bytes': record['bytes'],
                  'validation': {'duration_seconds': record['duration_seconds'], 'metadata': {'title': record['title']},
                                 'atoms': {'video_sample_entry': record['video_sample_entry']},
                                 'atmos': record.get('atmos') is True}}
    derivative['validation']['atoms']['audio_sample_entry'] = record['audio_sample_entry']
    proof = observe_exact(record['library_path'], record['episode'], derivative,
                          native_probe=native_probe or live_native_probe, timeout=timeout)
    return {'version': 1, 'basis': 'owner_confirmed_existing_import', 'source_sha256': record['source_sha256'],
            'episode': deepcopy(record['episode']), 'status': 'library_import_observed' if proof else 'unknown',
            'library': proof, 'checked_at': utcnow().isoformat(), 'playback_started': False,
            'import_attempts': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contract', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--expires', default=EXPIRY)
    args = parser.parse_args()
    with termination_signals():
        try:
            result = consume(read_json(args.contract), args.state_dir, execute=args.execute, expires=args.expires)
            print(json.dumps(result, allow_nan=False))
            return 0 if result['status'] == 'library_import_observed' else 2
        except ERRORS:
            print(json.dumps({'status': 'blocked', 'reason': 'invalid_or_unavailable_owned_input', 'playback_started': False}))
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
