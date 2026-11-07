#!/usr/bin/env python3
"""Bounded remote remux, with durable reservation and no modification of originals.

A reservation is never automatically rerun. Interrupted/failed work needs explicit
operator reconciliation. A validated result is persisted before atomic publication,
so a crash between publication and the ready receipt can be recovered by exact hash.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from apple_tv_media import MediaError, require, plan_media, ffmpeg_argv, validate_media, validate_contract, joc_extension, BLOCKED_REASONS

PROJECT_ROOT = Path('/home/tang/Projects/culture-cabinet')
COMPLETE = Path('/home/tang/Downloads/Usenet/Complete')
EXPIRY = '2026-11-06T04:17:29Z'
MAX_INPUT = 1024 * 1024


def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def check_deadline(expires, clock=time.time):
    try:
        approved = datetime.fromisoformat(expires.replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError):
        raise MediaError('Approved UTC remux expiry required.', 'session_expired') from None
    require(approved.tzinfo is not None and approved.utcoffset() == timedelta(0)
            and approved.timestamp() == epoch(EXPIRY) and clock() < approved.timestamp(),
            'Approved remux session expired.', 'session_expired')


@contextmanager
def open_pinned(path):
    """Walk every component without following symlinks, retaining the final fd."""
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'Absolute safe path required.')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            require(stat.S_ISREG(os.fstat(child).st_mode), 'Regular media file required.')
            yield child
        finally:
            os.close(child)
    finally:
        os.close(fd)


def signature(fd):
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def hash_fd(fd, expires, clock):
    before = signature(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    value = hashlib.sha256()
    while True:
        check_deadline(expires, clock)
        chunk = os.read(fd, 8 * 1024**2)
        if not chunk:
            break
        value.update(chunk)
    require(signature(fd) == before, 'Media changed while hashing.', 'source_verification_failed')
    return value.hexdigest(), before[2]


def verify_source_tree(complete, relative):
    """The completion contract registers one file, with no unregistered siblings."""
    parts = PurePosixPath(relative).parts
    with open_directory(Path(complete)) as root:
        fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            for index, part in enumerate(parts[1:], start=1):
                with os.scandir(fd) as entries:
                    names = []
                    for entry in entries:
                        names.append(entry.name)
                        require(len(names) <= 1, 'Unexpected entry in registered completion tree.', 'source_verification_failed')
                require(names == [part], 'Registered completion tree differs from exact source.', 'source_verification_failed')
                if index < len(parts) - 1:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
        finally:
            os.close(fd)


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, exist_ok=True)
    with open_directory(path) as fd:
        info = os.fstat(fd)
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077, 'Private work directory required.')
    return path


@contextmanager
def open_directory(path):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def write_state(path, value):
    with open_directory(path.parent) as directory:
        name = '.receipt-' + uuid.uuid4().hex
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, sort_keys=True, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass


def read_state(path):
    try:
        with open_pinned(path) as fd:
            require(os.fstat(fd).st_size <= MAX_INPUT, 'Oversized remux receipt.')
            return json.loads(os.read(fd, MAX_INPUT + 1))
    except FileNotFoundError:
        return None


def run_bounded(argv, *, expires, clock=time.time, pass_fds=(), max_output=1024 * 1024):
    """Use temporary capture files to bound memory, terminate the whole child group."""
    check_deadline(expires, clock)
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                start_new_session=True, pass_fds=pass_fds)
        def stop(signum=None, frame=None):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            raise MediaError('Remux interrupted or expired; reservation preserved.', 'remux_reconciliation_required')
        old = {}
        deadline = min(epoch(expires), clock() + 3600)
        try:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                old[sig] = signal.signal(sig, stop)
            while proc.poll() is None:
                if clock() >= deadline or output.tell() > max_output or errors.tell() > max_output:
                    stop()
                time.sleep(0.1)
            require(proc.returncode == 0, 'Media subprocess failed; durable reservation preserved.', 'remux_reconciliation_required')
            require(output.tell() <= max_output and errors.tell() <= max_output, 'Media subprocess output exceeded limit.')
            check_deadline(expires, clock)
            output.seek(0)
            return output.read(max_output + 1)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            for sig, handler in old.items():
                signal.signal(sig, handler)


def probe(path, runner, expires, clock, fds=()):
    return json.loads(runner(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-show_chapters',
                             '-of', 'json', str(path)], expires=expires, clock=clock, pass_fds=fds))


def packet_fingerprint(path, index, runner, expires, clock, fds=()):
    raw = runner(['ffprobe', '-v', 'error', '-select_streams', str(index), '-show_packets',
                  '-show_data_hash', 'sha256', '-show_entries', 'packet=data_hash', '-of', 'csv=p=0', str(path)],
                 expires=expires, clock=clock, pass_fds=fds, max_output=128 * 1024**2)
    hashes = re.findall(rb'SHA256:([0-9a-fA-F]{64})', raw)
    require(hashes, 'Packet payload evidence unavailable.')
    return len(hashes), hashlib.sha256(b'\n'.join(hashes)).hexdigest()


def inspect_atoms(fd):
    """Bounded structural traversal; never searches arbitrary media payload bytes."""
    size = os.fstat(fd).st_size
    result = {}
    positions = {}
    containers = {b'moov', b'trak', b'mdia', b'minf', b'stbl'}
    def walk(start, end, depth=0):
        require(depth <= 12, 'Excessive MP4 nesting.')
        offset = start
        while offset < end:
            require(end - offset >= 8, 'Truncated MP4 atom header.')
            header = os.pread(fd, 16, offset)
            length, kind = int.from_bytes(header[:4], 'big'), header[4:8]
            head = 8
            if length == 1:
                length, head = int.from_bytes(header[8:16], 'big'), 16
            elif length == 0:
                length = end - offset
            require(head <= length <= end - offset, 'Invalid MP4 atom length.')
            payload = offset + head
            if depth == 0:
                positions[kind] = offset
            if kind in containers:
                walk(payload, offset + length, depth + 1)
            elif kind == b'stsd':
                require(length >= head + 8, 'Truncated sample description.')
                walk(payload + 8, offset + length, depth + 1)
            elif kind in (b'hvc1', b'avc1'):
                require('video_sample_entry' not in result, 'Ambiguous video sample entries.')
                result['video_sample_entry'] = kind.decode()
                walk(payload + 78, offset + length, depth + 1)
            elif kind in (b'ec-3', b'ac-3', b'mp4a'):
                require('audio_sample_entry' not in result, 'Ambiguous audio sample entries.')
                result['audio_sample_entry'] = kind.decode()
                if kind == b'ec-3':
                    walk(payload + 28, offset + length, depth + 1)
            elif kind in (b'dec3', b'dvvC', b'dvcC', b'colr'):
                require(length <= 65536, 'Oversized codec atom.')
                name = kind.decode()
                require(name not in result, 'Ambiguous codec configuration atom.')
                data = os.pread(fd, length, offset)
                require(len(data) == length, 'Truncated codec atom.')
                result[name] = data.hex()
                if kind == b'dec3':
                    result['joc_extension_retained'] = joc_extension(data[head:])
            offset += length
    walk(0, size)
    result['faststart'] = b'moov' in positions and b'mdat' in positions and positions[b'moov'] < positions[b'mdat']
    return result


def recover(state, final, staged, expires, clock):
    require(state.get('status') in ('validated', 'ready'), 'Remux already reserved or blocked; reconcile before retry.', 'remux_reconciliation_required')
    result = state['result']
    candidate = final if final.exists() or final.is_symlink() else staged
    with open_pinned(candidate) as fd:
        value, size = hash_fd(fd, expires, clock)
    require(value == result['sha256'] and size == result['bytes'] and result['remote_path'] == str(final),
            'Durable remux evidence does not match output; reconcile manually.')
    if candidate == staged:
        require(not final.exists(), 'Final derivative already exists.')
        os.link(staged, final, follow_symlinks=False)
        staged.unlink()
    elif staged.exists() or staged.is_symlink():
        with open_pinned(final) as published, open_pinned(staged) as pending:
            require(signature(published)[:2] == signature(pending)[:2],
                    'Unexpected staged file after publication; reconcile manually.')
        staged.unlink()
    return result


def run_remux(contract, *, expires=EXPIRY, clock=time.time, runner=run_bounded,
              project_root=PROJECT_ROOT, complete=COMPLETE):
    check_deadline(expires, clock)
    validate_contract(contract)
    work = private_directory(Path(project_root) / '.apple-tv-work')
    folder = private_directory(work / contract['contract_sha256'])
    with open_directory(folder) as directory:
        lock = os.open('lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            receipt, final, staged = folder / 'receipt.json', folder / 'episode.mp4', folder / 'episode.partial'
            previous = read_state(receipt)
            if previous is not None:
                require(previous.get('contract_sha256') == contract['contract_sha256'], 'Remux reservation binding mismatch.')
                if previous.get('status') in ('validated', 'ready'):
                    saved = previous.get('result', {})
                    require(saved.get('version') == 1 and saved.get('contract_sha256') == contract['contract_sha256']
                            and saved.get('source_sha256') == contract['source']['sha256']
                            and saved.get('validation', {}).get('valid') is True
                            and saved['validation'].get('plan_sha256') == previous.get('plan_sha256'),
                            'Durable remux result binding mismatch.')
                if previous.get('status') == 'blocked' and previous.get('reason') in BLOCKED_REASONS:
                    code = previous['reason']
                    raise MediaError(BLOCKED_REASONS[code], code)
                result = recover(previous, final, staged, expires, clock)
                write_state(receipt, dict(previous, status='ready'))
                return result
            require(not final.exists() and not final.is_symlink() and not staged.exists() and not staged.is_symlink(),
                    'Unregistered remux output exists; reconcile manually.')
            verify_source_tree(complete, contract['source']['relative_path'])
            source = Path(complete) / contract['source']['relative_path']
            state = {'version': 1, 'contract_sha256': contract['contract_sha256'], 'status': 'reserved'}
            with open_pinned(source) as source_fd:
                value, size = hash_fd(source_fd, expires, clock)
                require((value, size) == (contract['source']['sha256'], contract['source']['bytes']), 'Source bytes/hash mismatch.', 'source_verification_failed')
                source_signature = signature(source_fd)
                pinned = '/proc/self/fd/' + str(source_fd)
                before = probe(pinned, runner, expires, clock, (source_fd,))
                try:
                    plan = plan_media(before, contract['episode'])
                    require(shutil.disk_usage(folder).free >= size * 2 + 256 * 1024**2,
                            'Insufficient private remux disk space.', 'insufficient_disk_space')
                except MediaError as error:
                    state.update(status='blocked', reason=error.code)
                    write_state(receipt, state)
                    raise
                state['plan_sha256'] = plan['plan_sha256']
                write_state(receipt, state)
                try:
                    # Exclusive reservation plus -n prevents overwriting any staged file.
                    runner(ffmpeg_argv(pinned, staged, plan), expires=expires, clock=clock, pass_fds=(source_fd,))
                    after = probe(staged, runner, expires, clock)
                    packets = {}
                    for label, source_index, output_index in (('video', plan['video'], 0), ('audio', plan['audio'], 1)):
                        first = packet_fingerprint(pinned, source_index, runner, expires, clock, (source_fd,))
                        second = packet_fingerprint(staged, output_index, runner, expires, clock)
                        packets[label] = {'source_packets': first[0], 'derivative_packets': second[0],
                                          'identical_packet_payload_hashes': first == second}
                    with open_pinned(staged) as fd:
                        validation = validate_media(before, after, plan, packets, inspect_atoms(fd))
                        output_hash, output_size = hash_fd(fd, expires, clock)
                        os.fsync(fd)
                    verify_source_tree(complete, contract['source']['relative_path'])
                    require(signature(source_fd) == source_signature, 'Original changed during remux.', 'source_verification_failed')
                    with open_pinned(source) as current:
                        require(signature(current) == source_signature, 'Original path changed during remux.', 'source_verification_failed')
                    check_deadline(expires, clock)
                    result = {'version': 1, 'contract_sha256': contract['contract_sha256'],
                              'source_sha256': value, 'remote_path': str(final), 'sha256': output_hash,
                              'bytes': output_size, 'validation': validation}
                    state.update(status='validated', result=result)
                    write_state(receipt, state)
                    os.link(staged, final, follow_symlinks=False)
                    staged.unlink()
                    os.fsync(directory)
                    state['status'] = 'ready'
                    write_state(receipt, state)
                    return result
                except (Exception, KeyboardInterrupt) as error:
                    # Keep validated evidence recoverable if publication was interrupted.
                    if state['status'] != 'validated':
                        state.update(status='blocked', reason=error.code if isinstance(error, MediaError)
                                     else 'remux_reconciliation_required')
                        write_state(receipt, state)
                    raise
        finally:
            os.close(lock)


def blocked_envelope(contract, error):
    """Expose only fixed policy vocabulary; never subprocess errors or source paths."""
    return {'version': 1, 'status': 'blocked', 'contract_sha256': contract['contract_sha256'],
            'source_sha256': contract['source']['sha256'], 'reason': error.code,
            'message': BLOCKED_REASONS[error.code]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expires', required=True)
    args = parser.parse_args()
    contract = None
    admitted = False
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        require(len(raw) <= MAX_INPUT, 'Oversized Apple TV contract.', 'invalid_contract')
        contract = json.loads(raw)
        validate_contract(contract)
        admitted = True
        result = run_remux(contract, expires=args.expires)
        print(json.dumps(result, sort_keys=True))
    except MediaError as error:
        if admitted:
            print(json.dumps(blocked_envelope(contract, error), sort_keys=True))
            return 0
        print(json.dumps({'status': 'blocked', 'reason': 'invalid_contract',
                          'message': BLOCKED_REASONS['invalid_contract']}), file=sys.stderr)
        return 1
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({'status': 'blocked', 'reason': 'remux_reconciliation_required',
                          'message': BLOCKED_REASONS['remux_reconciliation_required']}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
