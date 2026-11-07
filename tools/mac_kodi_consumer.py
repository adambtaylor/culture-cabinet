#!/usr/bin/env python3
"""Owned, explicit Cabinet v2 movie completion consumer. Python 3.9+.

Default CLI behavior validates only. --execute enables the existing SSH copy and
Kodi adapters; development tests inject both. No playback or library edits.
"""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
OWNER = Path('/Users/adamtaylor/Documents/Codex/2026-10-29/task')
MOVIES = Path('/Users/adamtaylor/Movies/Omarchy')
MAX_BYTES = 20 * 1024**3
MAX_RECEIPTS = 256
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.webm'}
READ_PROPERTIES = ['file', 'title', 'year', 'uniqueid', 'playcount', 'lastplayed', 'resume']


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def helpers():
    """Import the existing owner helpers without writing bytecode beside them."""
    original = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(OWNER))
    try:
        album = importlib.import_module('import_completed_album')
        sync = importlib.import_module('sync_completed_media')
        require(Path(album.__file__) == OWNER / 'import_completed_album.py'
                and Path(sync.__file__) == OWNER / 'sync_completed_media.py', 'Unexpected helper module.')
        return album, sync
    finally:
        sys.path.pop(0)
        sys.dont_write_bytecode = original


def encoded(value, newline=False):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
            + ('\n' if newline else '')).encode('utf-8')


def digest(value, newline=False):
    return hashlib.sha256(encoded(value, newline)).hexdigest()


def is_hash(value):
    return type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def timestamp(value):
    require(type(value) is str, 'Timestamp must be text.')
    return helpers()[0].date(value)


def validate(handoff, selection, now=None):
    """The caller supplies trusted export + independently selected binding, not a filename guess."""
    now = now or datetime.now(timezone.utc)
    require(type(handoff) is dict and handoff.get('version') == 2
            and handoff.get('state') == 'COMPLETED_FILES_VERIFIED'
            and handoff.get('sab_history_completed') is True, 'Final Cabinet v2 completion required.')
    require(set(selection) == {'source_identity', 'provenance', 'nzo_id', 'completion_evidence_sha256'},
            'Exact selected movie binding required.')
    identity = selection['source_identity']
    require(set(identity) == {'provider', 'medium', 'source_id', 'scope', 'title', 'edition', 'basis'}
            and handoff.get('source_identity') == identity, 'Producer and selected catalogue identity differ.')
    require(identity['provider'] == 'tmdb' and identity['medium'] == 'movie'
            and identity['scope'] == 'film', 'This consumer supports movie/film only; TV is unavailable.')
    require(type(identity['source_id']) is int and 0 < identity['source_id'] <= 2147483647,
            'Stable TMDB movie ID required.')
    require(identity['basis'] == 'selected_catalogue_identity_not_content_verification',
            'Catalogue identity must not claim content verification.')
    require(type(identity['edition']) is str and 0 < len(identity['edition']) <= 500
            and not any(ord(c) < 32 for c in identity['edition']), 'Exact selected edition required.')
    title = identity['title']
    require(type(title) is str and 0 < len(title.strip()) <= 300
            and not any(ord(c) < 32 for c in title), 'Selected movie title required.')
    auth = handoff['authorization']
    require(auth.get('scope') == 'film' and auth.get('authorized_content') is True,
            'Authorized film scope required.')
    files = handoff['files']
    require(type(files) is list and len(files) == 1, 'Exactly one completed video file is supported.')
    item = files[0]
    provenance = item['provenance']
    require(set(provenance) == {'request_id', 'result_id', 'result_digest', 'nzb_sha256'},
            'Exact request/result/NZB provenance required.')
    for key in ('request_id', 'result_id'):
        require(type(provenance[key]) is str and str(uuid.UUID(provenance[key])) == provenance[key],
                'Canonical request and result UUIDs required.')
    for key in ('result_digest', 'nzb_sha256'):
        require(is_hash(provenance[key]), 'Invalid provenance digest.')
    require(provenance == selection['provenance'] and item['nzo_id'] == selection['nzo_id'],
            'Selected provenance does not match completed file.')
    require(type(item['nzo_id']) is str and re.fullmatch(
        r'(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|SABnzbd_nzo_[A-Za-z0-9]+)', item['nzo_id']),
        'Invalid SAB job identity.')
    relative = helpers()[1].validate_relative(item['relative_path'])
    require(relative.suffix.lower() in VIDEO_EXTENSIONS
            and relative.parts[0] == 'CC-' + provenance['request_id']
            and 2 <= len(relative.parts) <= 16 and len(str(relative)) <= 4096,
            'Video must be inside the exact registered request directory.')
    require(is_hash(item['sha256']) and item['media_identity'] == 'sha256:' + item['sha256'],
            'Invalid completed file hash identity.')
    require(type(item['size']) is int and 0 < item['size'] <= MAX_BYTES, 'Invalid video size.')
    require(item['selected_scope'] == 'film' and item['observed_media'] == 'video'
            and item['observed_media_basis'] == 'filename_extension_only', 'Unexpected media scope.')
    require(item['content_identity'] == 'unverified' and item['full_work_verified'] is False
            and item['playback_started'] is False and item['indexed'] is False
            and item['kodi_music_catalog_eligible'] is False and item['reverify_before_playback'] is True
            and type(item['test_only']) is bool, 'Unexpected completion verification semantics.')
    checked, verified, acquired = (timestamp(handoff['checked_at']), timestamp(item['verified_at']),
                                   timestamp(item['acquired_at']))
    require(acquired.microsecond == 0 and int(verified.timestamp()) <= acquired.timestamp()
            and verified <= checked <= now and acquired <= checked
            and item['acquired_at_basis'] == 'sab_history_completed_at',
            'Authoritative SAB completion time required; hook/import/file times are insufficient.')
    evidence = {'source': 'sab_history', 'nzo_id': item['nzo_id'], 'acquired_at': item['acquired_at'],
        'acquired_at_basis': 'sab_history_completed_at', 'provenance': provenance,
        'files': [{key: item[key] for key in ('relative_path', 'sha256', 'size')}],
        'source_identity': identity}
    require(handoff['completion_evidence'] == evidence
            and digest(evidence, newline=True) == handoff['completion_evidence_sha256']
            == selection['completion_evidence_sha256'], 'Completion evidence binding mismatch.')
    hook_file = dict(item, acquired_at=None, acquired_at_basis='unavailable')
    hook_digest = digest({'authorization': auth, 'files': [hook_file]})
    return {'request_id': provenance['request_id'], 'file': deepcopy(item), 'selection': deepcopy(selection),
        'hook_sha256': hook_digest, 'completion_evidence_sha256': handoff['completion_evidence_sha256'],
        'signature': digest([selection, hook_digest]), 'checked_at': handoff['checked_at']}


def safe_directory(path, create=False, private=False):
    path = Path(path).absolute()
    require('..' not in path.parts, 'Parent traversal is not allowed.')
    for part in [*reversed(path.parents), path]:
        require(not part.is_symlink(), 'Symlink directory is not allowed.')
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and info.st_mode & 0o022 == 0, 'Directory must be owned and not writable by others.')
    if private:
        require(stat.S_IMODE(info.st_mode) == 0o700, 'State directory must have mode 0700.')
    return path


def read_json(path, private=False):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 1024**2,
                'Unsafe or oversized JSON file.')
        if private:
            require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600,
                    'Receipt must be private and owned.')
        raw = stream.read(1024**2 + 1)
    require(len(raw) <= 1024**2, 'JSON exceeds limit.')
    return json.loads(raw, object_pairs_hook=helpers()[0].unique_object)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_receipt(path, receipt):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.receipt-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded(receipt, newline=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def file_signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def verify_stream(stream, item):
    before = os.fstat(stream.fileno())
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size == item['size'],
            'Local media differs or is unsafe; file preserved.')
    result, remaining = hashlib.sha256(), item['size']
    while remaining:
        block = stream.read(min(1024**2, remaining))
        require(bool(block), 'Local media ended early.')
        result.update(block)
        remaining -= len(block)
    require(stream.read(1) == b'', 'Local media grew during verification.')
    require(file_signature(before) == file_signature(os.fstat(stream.fileno()))
            and result.hexdigest() == item['sha256'], 'Local media hash changed; file preserved.')
    return file_signature(before)


def verify_file(path, item):
    safe_directory(path.parent)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        verify_stream(stream, item)


def open_directory(path, stack):
    """Open every absolute path component without following symlinks."""
    require(path.is_absolute() and '..' not in path.parts, 'Absolute owned directory required.')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open('/', flags)
    stack.callback(os.close, descriptor)
    for name in path.parts[1:]:
        descriptor = os.open(name, flags, dir_fd=descriptor)
        stack.callback(os.close, descriptor)
    return descriptor


def verify_import_tree(directory, target, item, selection):
    """Bounded exact-tree inventory: only two files and their required parents.

    Unknown entries are rejected before opening them. Keep descriptors open and
    compare directory/file identity after hashing to detect changes during the
    observation. Kodi still consumes a pathname, so this is not an atomic lock
    against a same-user external writer after the final observation.
    """
    relative = target.relative_to(directory)
    require(1 <= len(relative.parts) <= 15, 'Import directory depth exceeds the bound.')
    nfo = nfo_bytes(selection)
    files = {str(relative): item, str(relative.with_suffix('.nfo')):
             {'size': len(nfo), 'sha256': hashlib.sha256(nfo).hexdigest()}}
    children = {'': set()}
    for name in files:
        parts = Path(name).parts
        for index, part in enumerate(parts):
            parent = '/'.join(parts[:index])
            children.setdefault(parent, set()).add(part)
            if index < len(parts) - 1:
                children.setdefault('/'.join(parts[:index + 1]), set())
    safe_directory(directory)
    with ExitStack() as stack:
        root = open_directory(directory, stack)
        directories, snapshots = {'': root}, []
        for name in sorted(children, key=lambda value: (value.count('/'), value)):
            if name:
                parent, _, leaf = name.rpartition('/')
                descriptor = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=directories[parent])
                stack.callback(os.close, descriptor)
                directories[name] = descriptor
            descriptor = directories[name]
            info = os.fstat(descriptor)
            require(info.st_uid == os.getuid() and info.st_mode & 0o022 == 0,
                    'Import directory must be owned and not writable by others.')
            snapshots.append((descriptor, file_signature(info)))
            seen = set()
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    require(entry.name in children[name] and len(seen) < len(children[name]),
                            'Unexpected import directory entry; all files preserved.')
                    seen.add(entry.name)
            require(seen == children[name], 'Required import file or directory is missing.')
        for name, expected in files.items():
            parent, _, leaf = name.rpartition('/')
            descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directories[parent])
            stream = stack.enter_context(os.fdopen(descriptor, 'rb'))
            snapshot = verify_stream(stream, expected)
            snapshots.append((descriptor, snapshot))
            require(file_signature(os.stat(leaf, dir_fd=directories[parent], follow_symlinks=False)) == snapshot,
                    'Import file was replaced during inventory.')
        for descriptor, snapshot in snapshots:
            require(file_signature(os.fstat(descriptor)) == snapshot, 'Import tree changed during inventory.')
        reopened = open_directory(directory, stack)
        require(file_signature(os.fstat(reopened)) == snapshots[0][1], 'Import root changed during inventory.')


def live_transfer(relative, staging, **expected):
    def bounded_run(command, **kwargs):
        kwargs['timeout'] = min(kwargs.get('timeout', 300), 300)
        return subprocess.run(command, **kwargs)
    return helpers()[1].fetch(relative, staging, runner=bounded_run, max_bytes=MAX_BYTES, **expected)


def probe_summary(probe, expected_hash):
    videos = [s for s in probe.get('streams', []) if s.get('codec_type') == 'video'
              and s.get('disposition', {}).get('attached_pic', 0) != 1 and s.get('codec_name')]
    duration = float(probe.get('format', {}).get('duration', 0))
    require(probe.get('validated_sha256') == expected_hash and videos and math.isfinite(duration)
            and 0 < duration <= 7 * 86400, 'Hash-bound playable video probe required.')
    return {'validated_sha256': expected_hash, 'video_codec': videos[0]['codec_name'],
            'duration': duration}


def nfo_bytes(selection):
    identity = selection['source_identity']
    movie = ET.Element('movie')
    ET.SubElement(movie, 'title').text = identity['title']
    ET.SubElement(movie, 'uniqueid', {'type': 'tmdb', 'default': 'true'}).text = str(identity['source_id'])
    # No playcount, lastplayed, resume or Kodi dateadded is supplied or overwritten.
    return ET.tostring(movie, encoding='utf-8', xml_declaration=True) + b'\n'


def publish(source, target):
    """Atomic no-overwrite publication within the owned per-request directory."""
    safe_directory(target.parent, create=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.install-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with open(source, 'rb') as incoming:
                shutil.copyfileobj(incoming, stream, 1024**2)
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    sync_directory(target.parent)


def readback(kodi, directory, target, selection):
    result = kodi.call('VideoLibrary.GetMovies', {
        'filter': {'field': 'path', 'operator': 'startswith', 'value': str(directory) + '/'},
        'properties': READ_PROPERTIES, 'limits': {'start': 0, 'end': 2}})
    require(type(result) is dict and type(result.get('movies', [])) is list, 'Invalid Kodi readback.')
    movies = result.get('movies', [])
    require(len(movies) <= 1 and result.get('limits', {}).get('total', len(movies)) <= 1,
            'Ambiguous Kodi entries in the exact request directory.')
    if not movies:
        return None
    movie = movies[0]
    require(movie.get('file') == str(target) and type(movie.get('movieid')) is int
            and str(movie.get('uniqueid', {}).get('tmdb')) == str(selection['source_identity']['source_id']),
            'Kodi file or stable TMDB identity differs; existing metadata preserved.')
    return {key: movie[key] for key in ('movieid', *READ_PROPERTIES) if key in movie}


def consume(handoff, selection, *, cache=MOVIES, state_dir=ROOT / 'state', execute=False,
            transfer=None, kodi=None, timeout=30, pause=time.sleep, now=None):
    job = validate(handoff, selection, now)
    selection = job['selection']
    require(type(execute) is bool, 'Execution authorization must be explicit.')
    require(type(timeout) in (int, float) and 0 <= timeout <= 60, 'Readback timeout must be 0–60 seconds.')
    if not execute:
        return {'status': 'blocked', 'reason': 'execution_not_authorized',
                'request_id': job['request_id'], 'playback_started': False}
    require(not job['file']['test_only'] or (transfer is not None and kodi is not None),
            'Synthetic handoffs require both injected adapters; live access refused.')
    state_dir = safe_directory(state_dir, create=True, private=True)
    cache = safe_directory(cache, create=True)
    lockfd = os.open(state_dir / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(), 'Unsafe lock.')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = state_dir / (job['request_id'] + '.json')
        previous = read_json(path, private=True) if path.exists() or path.is_symlink() else None
        signature = digest([job['signature'], str(cache)])
        require(not previous or (previous['signature'] == signature and previous['file'] == job['file']
                and previous['selection'] == selection and previous['hook_sha256'] == job['hook_sha256']
                and previous['completion_evidence_sha256'] == job['completion_evidence_sha256']
                and type(previous['scan_attempts']) is int and previous['scan_attempts'] in (0, 1)
                and previous['scan_key'] == 'video-' + job['request_id']
                and previous['status'] in ('queued', 'scanning', 'imported', 'unknown', 'blocked')),
                'Conflicting existing request; receipt preserved.')
        receipts = list(state_dir.glob('*.json'))
        require(len(receipts) < MAX_RECEIPTS or previous is not None, 'Receipt capacity reached; existing imports retained.')
        for other in receipts:
            if other != path:
                saved = read_json(other, private=True)
                require(saved['file']['sha256'] != job['file']['sha256']
                        and saved['selection']['source_identity']['source_id'] != selection['source_identity']['source_id'],
                        'Movie bytes or stable identity already belong to another request.')
        receipt = previous or {'version': 1, 'request_id': job['request_id'], 'signature': signature,
            'selection': job['selection'], 'file': job['file'], 'hook_sha256': job['hook_sha256'],
            'completion_evidence_sha256': job['completion_evidence_sha256'], 'status': 'queued',
            'scan_attempts': 0, 'scan_key': 'video-' + job['request_id'], 'probe': None,
            'library': None, 'created_at': datetime.now(timezone.utc).isoformat(), 'playback_started': False}
        # This is the Mac observation clock; the handoff retains its producer check.
        receipt['checked_at'] = datetime.now(timezone.utc).isoformat()
        save_receipt(path, receipt)
        item = job['file']
        target = cache.joinpath(*Path(item['relative_path']).parts)
        directory = cache / ('CC-' + job['request_id'])
        try:
            if receipt['scan_attempts'] or receipt['library'] is not None:
                # Ambiguous scans are read-only on every restart, even if files
                # disappear. Never repair/recopy their directory and rescan it.
                verify_file(target, item)
            else:
                if target.exists() or target.is_symlink():
                    verify_file(target, item)
                if not target.exists() or not receipt['probe']:
                    with tempfile.TemporaryDirectory(prefix='video-stage-', dir=state_dir) as folder:
                        result = (transfer or live_transfer)(item['relative_path'], Path(folder),
                            expected_sha256=item['sha256'], expected_bytes=item['size'])
                        staged = Path(result['local_path'])
                        require(staged.is_relative_to(Path(folder)) and result.get('sha256') == item['sha256']
                                and result.get('bytes') == item['size'], 'Transfer binding mismatch.')
                        verify_file(staged, item)
                        probe = probe_summary(result['media_probe'], item['sha256'])
                        if not target.exists():
                            publish(staged, target)
                        verify_file(target, item)
                        receipt['probe'] = probe
                safe_directory(target.parent)
                nfo = target.with_suffix('.nfo')
                wanted = nfo_bytes(selection)
                if nfo.exists() or nfo.is_symlink():
                    verify_file(nfo, {'size': len(wanted), 'sha256': hashlib.sha256(wanted).hexdigest()})
                else:
                    with tempfile.NamedTemporaryFile(dir=state_dir, prefix='.nfo-') as source:
                        source.write(wanted)
                        source.flush()
                        publish(Path(source.name), nfo)
                receipt['local_path'] = str(target)
                save_receipt(path, receipt)
            verify_import_tree(directory, target, item, selection)
            file_verified_at = datetime.now(timezone.utc).isoformat()
            kodi = kodi or helpers()[0].Kodi()
            library = readback(kodi, directory, target, selection)
            if library is None and not receipt['scan_attempts'] and receipt['library'] is None:
                verify_file(target, item)
                receipt.update(status='scanning', scan_attempts=1,
                               scan_reserved_at=datetime.now(timezone.utc).isoformat())
                save_receipt(path, receipt)
                # Recheck the whole tree after readback and the durable checkpoint,
                # immediately before the only scan call. A failure stays unknown.
                verify_import_tree(directory, target, item, selection)
                kodi.call('VideoLibrary.Scan', {'directory': str(directory), 'showdialogs': False})
            deadline = time.monotonic() + timeout
            while library is None:
                library = readback(kodi, directory, target, selection)
                if library is not None or time.monotonic() >= deadline:
                    break
                pause(min(.25, max(0, deadline - time.monotonic())))
            receipt.update(status='imported' if library is not None else 'unknown', reason=None)
            if library is not None:
                receipt['library'] = library
                receipt['readiness'] = {
                    'file_verified_at': file_verified_at,
                    'library_observed_at': datetime.now(timezone.utc).isoformat(),
                    **receipt['probe']}
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired):
            # No adapter error payload is persisted. A reserved scan remains
            # uncertain; immutable scan_attempts prohibits another scan forever.
            uncertain = receipt['scan_attempts'] or receipt['library'] is not None
            receipt.update(status='unknown' if uncertain else 'blocked',
                           reason='scan_reconciliation_required' if uncertain else 'verification_or_adapter_blocked')
        if receipt['status'] != 'imported':
            receipt.pop('readiness', None)
        receipt['checked_at'] = datetime.now(timezone.utc).isoformat()
        save_receipt(path, receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--handoff', type=Path, required=True)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--cache', type=Path, default=MOVIES)
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'state')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        result = consume(read_json(args.handoff), read_json(args.selection), cache=args.cache,
                         state_dir=args.state_dir, execute=args.execute)
        print(json.dumps(result))
        return 0 if result['status'] == 'imported' else 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        print(json.dumps({'status': 'blocked', 'reason': 'invalid_or_unavailable_owned_input',
                          'playback_started': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
