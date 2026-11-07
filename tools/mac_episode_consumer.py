#!/usr/bin/env python3
"""Exact selected-show episode completion consumer. No episode-title inference.

Only fresh v2 evidence and one hash-bound video are accepted. The local filename
is canonical for Kodi; the receipt retains the original remote file identity.
The reviewed movie module supplies filesystem, transfer, JSON and probe helpers.
Kodi NFO references: https://kodi.wiki/view/NFO_files/TV_shows and
https://kodi.wiki/view/NFO_files/Episodes . No scraper episode ID is invented.
"""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

import mac_kodi_consumer as video
from episode_scope import parse_scope, episode_code, is_episode_scope
from mac_kodi_consumer import (require, helpers, digest, is_hash, timestamp,
    safe_directory, read_json, save_receipt, file_signature, verify_stream,
    verify_file, open_directory, live_transfer, probe_summary, publish,
    MAX_BYTES, MAX_RECEIPTS, VIDEO_EXTENSIONS)

ROOT = Path(__file__).resolve().parent
TV_SHOWS = Path('/Users/adamtaylor/Movies/Omarchy/TV Shows')
READ_PROPERTIES = ['file', 'title', 'season', 'episode', 'tvshowid', 'uniqueid',
                   'playcount', 'lastplayed', 'resume']


def validate(handoff, selection, now=None):
    """The caller supplies trusted export + independently selected binding, not a filename guess."""
    now = now or datetime.now(timezone.utc)
    require(type(handoff) is dict and type(handoff.get('version')) is int and handoff['version'] == 2
            and handoff.get('state') == 'COMPLETED_FILES_VERIFIED'
            and handoff.get('sab_history_completed') is True, 'Final Cabinet v2 completion required.')
    require(type(selection) is dict and set(selection) == {'source_identity', 'provenance', 'nzo_id', 'completion_evidence_sha256'},
            'Exact selected episode binding required.')
    identity = selection['source_identity']
    require(type(identity) is dict and set(identity) == {'provider', 'medium', 'source_id', 'scope', 'title', 'edition', 'basis'}
            and handoff.get('source_identity') == identity, 'Producer and selected catalogue identity differ.')
    require(identity['provider'] == 'tmdb' and identity['medium'] == 'tv'
            and is_episode_scope(identity['scope']), 'Exact selected single episode required.')
    require(type(identity['source_id']) is int and 0 < identity['source_id'] <= 2147483647,
            'Stable TMDB show ID required.')
    require(identity['basis'] == 'selected_catalogue_identity_not_content_verification',
            'Catalogue identity must not claim content verification.')
    require(type(identity['edition']) is str and 0 < len(identity['edition'].strip()) <= 500
            and not any(ord(c) < 32 for c in identity['edition']), 'Exact selected edition required.')
    title = identity['title']
    require(type(title) is str and 0 < len(title.strip()) <= 300
            and not any(ord(c) < 32 for c in title), 'Selected show title required.')
    auth = handoff['authorization']
    require(type(auth) is dict and auth.get('edition') == identity['edition']
            and auth.get('scope') == identity['scope'] and auth.get('authorized_content') is True,
            'Authorized selected episode scope required.')
    files = handoff['files']
    require(type(files) is list and len(files) == 1, 'Exactly one completed video file is supported.')
    item = files[0]
    require(type(item) is dict, 'Completed video entry required.')
    provenance = item['provenance']
    require(type(provenance) is dict and set(provenance) == {'request_id', 'result_id', 'result_digest', 'nzb_sha256'},
            'Exact request/result/NZB provenance required.')
    for key in ('request_id', 'result_id'):
        require(type(provenance[key]) is str and str(uuid.UUID(provenance[key])) == provenance[key],
                'Canonical request and result UUIDs required.')
    for key in ('result_digest', 'nzb_sha256'):
        require(is_hash(provenance[key]), 'Invalid provenance digest.')
    require(provenance == selection['provenance'] and item['nzo_id'] == selection['nzo_id'],
            'Selected provenance does not match completed file.')
    require(type(item['nzo_id']) is str and re.fullmatch(
        r'(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|SABnzbd_nzo_[A-Za-z0-9_-]{1,100})', item['nzo_id']),
        'Invalid SAB job identity.')
    relative = helpers()[1].validate_relative(item['relative_path'])
    require(relative.suffix.lower() in VIDEO_EXTENSIONS
            and relative.parts[0] == 'CC-' + provenance['request_id']
            and 2 <= len(relative.parts) <= 16 and len(str(relative)) <= 4096,
            'Video must be inside the exact registered request directory.')
    require(is_hash(item['sha256']) and item['media_identity'] == 'sha256:' + item['sha256'],
            'Invalid completed file hash identity.')
    require(type(item['size']) is int and 0 < item['size'] <= MAX_BYTES, 'Invalid video size.')
    require(item['selected_scope'] == identity['scope'] and item['observed_media'] == 'video'
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


def episode_id(selection):
    return 'tmdb-show-' + str(selection['source_identity']['source_id']) + ':' + episode_code(selection['source_identity']['scope']).lower()


def nfo_files(selection):
    identity = selection['source_identity']
    show = ET.Element('tvshow')
    ET.SubElement(show, 'title').text = identity['title']
    ET.SubElement(show, 'uniqueid', {'type': 'tmdb', 'default': 'true'}).text = str(identity['source_id'])
    season, number = parse_scope(identity['scope'])
    code = episode_code(identity['scope'])
    episode = ET.Element('episodedetails')
    ET.SubElement(episode, 'title').text = code
    ET.SubElement(episode, 'season').text = str(season)
    ET.SubElement(episode, 'episode').text = str(number)
    ET.SubElement(episode, 'plot').text = 'Actual episode title unknown. ' + code + ' is the explicitly selected scope; content identity remains unverified.'
    ET.SubElement(episode, 'uniqueid', {'type': 'cabinet', 'default': 'true'}).text = episode_id(selection)
    # Never reuse the TMDB show ID as an episode scraper ID, or set watch history.
    return {name: ET.tostring(tree, encoding='utf-8', xml_declaration=True) + b'\n'
            for name, tree in (('tvshow.nfo', show), (code + '.nfo', episode))}


def verify_import_tree(directory, target, item, selection):
    """Exactly one canonical video and two NFOs; pin descriptors while hashing."""
    require(target.parent == directory and target.name == episode_code(selection['source_identity']['scope']) + Path(item['relative_path']).suffix.lower(),
            'Canonical selected episode filename required.')
    files = {target.name: item}
    for filename, raw in nfo_files(selection).items():
        files[filename] = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    safe_directory(directory)
    with ExitStack() as stack:
        descriptor = open_directory(directory, stack)
        info = os.fstat(descriptor)
        require(info.st_uid == os.getuid() and info.st_mode & 0o022 == 0, 'Unsafe episode directory.')
        before = file_signature(info)
        seen = set()
        with os.scandir(descriptor) as entries:
            for entry in entries:
                require(entry.name in files and len(seen) < 3, 'Unexpected episode import entry; files preserved.')
                seen.add(entry.name)
        require(seen == set(files), 'Required episode file missing.')
        snapshots = []
        for name, expected in files.items():
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            stream = stack.enter_context(os.fdopen(fd, 'rb'))
            signature = verify_stream(stream, expected)
            require(file_signature(os.stat(name, dir_fd=descriptor, follow_symlinks=False)) == signature,
                    'Episode import file replaced during inventory.')
            snapshots.append((fd, signature))
        require(all(file_signature(os.fstat(fd)) == saved for fd, saved in snapshots)
                and file_signature(os.fstat(descriptor)) == before, 'Episode tree changed during inventory.')
        reopened = open_directory(directory, stack)
        require(file_signature(os.fstat(reopened)) == before, 'Episode import root changed during inventory.')


def readback(kodi, directory, target, selection):
    result = kodi.call('VideoLibrary.GetEpisodes', {
        'filter': {'field': 'path', 'operator': 'startswith', 'value': str(directory) + '/'},
        'properties': READ_PROPERTIES, 'limits': {'start': 0, 'end': 2}})
    require(type(result) is dict and type(result.get('episodes', [])) is list, 'Invalid episode readback.')
    episodes = result.get('episodes', [])
    require(type(result.get('limits', {})) is dict, 'Invalid episode limits.')
    total = result.get('limits', {}).get('total', len(episodes))
    require(type(total) is int and len(episodes) <= 1 and total == len(episodes), 'Ambiguous episode directory.')
    if not episodes:
        return None
    episode = episodes[0]
    require(type(episode) is dict and episode.get('file') == str(target)
            and type(episode.get('episodeid')) is int and episode['episodeid'] > 0
            and type(episode.get('tvshowid')) is int and episode['tvshowid'] > 0
            and type(episode.get('season')) is int and episode['season'] == parse_scope(selection['source_identity']['scope'])[0]
            and type(episode.get('episode')) is int and episode['episode'] == parse_scope(selection['source_identity']['scope'])[1]
            and type(episode.get('uniqueid')) is dict
            and episode['uniqueid'].get('cabinet') == episode_id(selection), 'Wrong episode identity; metadata preserved.')
    result = kodi.call('VideoLibrary.GetTVShowDetails', {'tvshowid': episode['tvshowid'], 'properties': ['uniqueid']})
    require(type(result) is dict, 'Invalid show readback.')
    show = result.get('tvshowdetails', {})
    require(type(show) is dict and type(show.get('tvshowid')) is int
            and show['tvshowid'] == episode['tvshowid'] and type(show.get('uniqueid')) is dict
            and str(show['uniqueid'].get('tmdb')) == str(selection['source_identity']['source_id']),
            'Selected TMDB show identity differs.')
    return {key: episode[key] for key in ('episodeid', *READ_PROPERTIES) if key in episode} | {
        'show_uniqueid': {'tmdb': str(selection['source_identity']['source_id'])}}


def validate_receipt(receipt, handoff, selection, *, cache=TV_SHOWS, now=None):
    job = validate(handoff, selection, now)
    cache = Path(cache).absolute()
    require(type(receipt) is dict and type(receipt.get('version')) is int and receipt['version'] == 1
            and receipt.get('request_id') == job['request_id'] and receipt.get('selection') == selection
            and receipt.get('file') == job['file'] and receipt.get('hook_sha256') == job['hook_sha256']
            and receipt.get('completion_evidence_sha256') == job['completion_evidence_sha256']
            and receipt.get('signature') == digest([job['signature'], str(cache)])
            and receipt.get('scan_key') == 'episode-' + job['request_id']
            and type(receipt.get('scan_attempts')) is int and receipt['scan_attempts'] in (0, 1)
            and receipt.get('status') in ('queued', 'scanning', 'blocked', 'unknown', 'imported')
            and receipt.get('playback_started') is False, 'Foreign episode receipt.')
    current = now or datetime.now(timezone.utc)
    acquired = timestamp(job['file']['acquired_at'])
    require(acquired <= timestamp(receipt['created_at']) <= timestamp(receipt['checked_at']) <= current, 'Invalid episode receipt time.')
    if receipt['status'] in ('scanning', 'unknown'):
        require(receipt['scan_attempts'] == 1, 'Read-only reconciliation reservation required.')
    if receipt['status'] == 'imported':
        target = str(Path(cache) / ('CC-' + job['request_id']) / (episode_code(selection['source_identity']['scope']) + Path(job['file']['relative_path']).suffix.lower()))
        library = receipt['library']
        require(type(library) is dict and type(library.get('uniqueid')) is dict
                and type(library.get('show_uniqueid')) is dict
                and receipt.get('local_path') == target and library['file'] == target
                and type(library['episodeid']) is int and library['episodeid'] > 0
                and type(library['tvshowid']) is int and library['tvshowid'] > 0
                and type(library['season']) is int and library['season'] == parse_scope(selection['source_identity']['scope'])[0]
                and type(library['episode']) is int and library['episode'] == parse_scope(selection['source_identity']['scope'])[1]
                and library['uniqueid'].get('cabinet') == episode_id(selection)
                and str(library['show_uniqueid'].get('tmdb')) == str(selection['source_identity']['source_id']),
                'Episode receipt readback differs.')
        probe, readiness = receipt['probe'], receipt['readiness']
        require(type(probe) is dict and set(probe) == {'validated_sha256', 'video_codec', 'duration'}
                and probe['validated_sha256'] == job['file']['sha256']
                and type(probe['video_codec']) is str and bool(probe['video_codec'])
                and type(probe['duration']) in (int, float) and math.isfinite(probe['duration'])
                and 0 < probe['duration'] <= 7 * 86400, 'Invalid verified video probe.')
        require(type(readiness) is dict
                and set(readiness) == {'file_verified_at', 'library_observed_at', *probe}
                and all(readiness[key] == value for key, value in probe.items())
                and acquired <= timestamp(readiness['file_verified_at'])
                    <= timestamp(readiness['library_observed_at']) <= current, 'Invalid episode readiness.')
    else:
        require('readiness' not in receipt, 'Uncertain episode cannot claim readiness.')
    return receipt


def consume(handoff, selection, *, cache=TV_SHOWS, state_dir=ROOT / 'state', execute=False,
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
    cache = Path(cache).absolute()
    lockfd = os.open(state_dir / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, 'Unsafe lock.')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = state_dir / (job['request_id'] + '.json')
        previous = read_json(path, private=True) if path.exists() or path.is_symlink() else None
        signature = digest([job['signature'], str(cache)])
        if previous is not None:
            validate_receipt(previous, handoff, selection, cache=cache, now=now)
            if previous['status'] == 'imported':
                return previous  # A validated import is terminal; later outages cannot downgrade it.
        cache = safe_directory(cache, create=True)
        receipts = list(state_dir.glob('*.json'))
        require(len(receipts) < MAX_RECEIPTS or previous is not None, 'Receipt capacity reached; existing imports retained.')
        for other in receipts:
            if other != path:
                saved = read_json(other, private=True)
                require(saved['file']['sha256'] != job['file']['sha256']
                        and (saved['selection']['source_identity']['source_id'] != selection['source_identity']['source_id']
                             or parse_scope(saved['selection']['source_identity']['scope'])
                             != parse_scope(selection['source_identity']['scope'])),
                        'Episode bytes or selected show episode already belong to another request.')
        receipt = previous or {'version': 1, 'request_id': job['request_id'], 'signature': signature,
            'selection': job['selection'], 'file': job['file'], 'hook_sha256': job['hook_sha256'],
            'completion_evidence_sha256': job['completion_evidence_sha256'], 'status': 'queued',
            'scan_attempts': 0, 'scan_key': 'episode-' + job['request_id'], 'probe': None,
            'library': None, 'created_at': datetime.now(timezone.utc).isoformat(), 'playback_started': False}
        # Receipt observation time is local; producer export time stays in the handoff.
        receipt['checked_at'] = datetime.now(timezone.utc).isoformat()
        save_receipt(path, receipt)
        item = job['file']
        directory = cache / ('CC-' + job['request_id'])
        target = directory / (episode_code(selection['source_identity']['scope']) + Path(item['relative_path']).suffix.lower())
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
                for filename, wanted in nfo_files(selection).items():
                    nfo = directory / filename
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
    parser.add_argument('--cache', type=Path, default=TV_SHOWS)
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
