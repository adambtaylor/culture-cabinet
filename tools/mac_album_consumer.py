#!/usr/bin/env python3
"""Explicit Cabinet v2 FLAC album consumer; no playback or metadata edits.

Only an exact, complete manifest can be scanned. A durable scan reservation or
prior readback makes every later attempt read-only. Credentials remain owned by
the existing Mac adapters. Default CLI operation validates without executing.
"""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import time
import unicodedata
import uuid
from urllib.parse import unquote

import mac_kodi_consumer as core

ROOT = Path(__file__).resolve().parent
CACHE = Path('/Users/adamtaylor/Music/Omarchy/Cabinet Albums')
MAX_FILES = 128
MAX_BYTES = 20 * 1024**3
ART_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
EXTENSIONS = {'.flac', '.cue'} | ART_EXTENSIONS
TRACK_FIELDS = {'relative_path', 'local_path', 'sha256', 'bytes', 'validated_sha256',
                'title', 'artist', 'album', 'disc', 'track', 'duration', 'embedded_artwork'}
SONG_FIELDS = ['file', 'title', 'album', 'artist', 'albumartist', 'track', 'disc',
               'duration', 'albumid', 'thumbnail', 'art', 'playcount', 'lastplayed',
               'musicbrainzartistid', 'musicbrainzalbumid']
ALBUM_FIELDS = ['title', 'artist', 'thumbnail', 'art', 'description', 'musicbrainzalbumid']
require = core.require


def text(value, limit=500):
    require(type(value) is str and 0 < len(value.strip()) <= limit
            and not any(ord(c) < 32 for c in value), 'Bounded plain text required.')
    return value.strip()


def normalized(value):
    return ' '.join(unicodedata.normalize('NFC', text(value)).split()).casefold()


def relative(value):
    require(type(value) is str and 0 < len(value) <= 4096 and not value.startswith('/')
            and '\\' not in value and not any(ord(c) < 32 for c in value)
            and all(part not in ('', '.', '..') for part in value.split('/')),
            'Exact relative album path required.')
    path = PurePosixPath(value)
    require(2 <= len(path.parts) <= 16 and path.suffix.lower() in EXTENSIONS,
            'Unsupported album manifest file.')
    return path


def validate(handoff, selection, now=None):
    now = now or datetime.now(timezone.utc)
    require(type(handoff) is dict and handoff.get('version') == 2
            and handoff.get('state') == 'COMPLETED_FILES_VERIFIED'
            and handoff.get('sab_history_completed') is True, 'Final Cabinet v2 completion required.')
    require(type(selection) is dict and set(selection) == {'source_identity', 'album_identity',
            'provenance', 'nzo_id', 'completion_evidence_sha256'}, 'Exact selected album binding required.')
    identity = selection['source_identity']
    require(type(identity) is dict and set(identity) == {'provider', 'medium', 'source_id',
            'scope', 'title', 'edition', 'basis'} and identity == handoff['source_identity'],
            'Selected catalogue identity differs.')
    require((identity['provider'], identity['medium'], identity['scope']) ==
            ('album_request', 'music', 'album_flac') and type(identity['source_id']) is int
            and 0 < identity['source_id'] <= 2147483647
            and identity['basis'] == 'selected_catalogue_identity_not_content_verification',
            'Exact internal AlbumRequest identity required.')
    text(identity['title']); text(identity['edition'])
    auth, album = handoff['authorization'], selection['album_identity']
    require(type(album) is dict and set(album) == {'namespace', 'artist_id', 'release_id',
            'artist', 'album', 'track_count', 'source'}
            and album == handoff['album_identity'] == auth['album_identity']
            and album['namespace'] in ('musicbrainz', 'licensed_catalogue'), 'Bound album identity required.')
    for field in ('artist_id', 'release_id', 'artist', 'album', 'source'):
        require(text(album[field]) == album[field], 'Canonical album identity required.')
    if album['namespace'] == 'musicbrainz':
        for field in ('artist_id', 'release_id'):
            require(str(uuid.UUID(album[field])) == album[field], 'Canonical MusicBrainz ID required.')
    require(type(album['track_count']) is int and 1 <= album['track_count'] <= MAX_FILES
            and auth['scope'] == 'album_flac' and auth['authorized_content'] is True
            and auth['edition'] == identity['edition'], 'Exact authorized album scope required.')
    files = handoff['files']
    require(type(files) is list and 0 < len(files) <= MAX_FILES, 'Bounded full manifest required.')
    provenance = selection['provenance']
    require(type(provenance) is dict and set(provenance) == {
        'request_id', 'result_id', 'result_digest', 'nzb_sha256'}, 'Exact provenance required.')
    for field in ('request_id', 'result_id'):
        require(type(provenance[field]) is str and str(uuid.UUID(provenance[field])) == provenance[field],
                'Canonical provenance UUID required.')
    for field in ('result_digest', 'nzb_sha256'):
        require(core.is_hash(provenance[field]), 'Invalid provenance digest.')
    require(type(selection['nzo_id']) is str and re.fullmatch(
        r'(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|SABnzbd_nzo_[A-Za-z0-9]+)', selection['nzo_id']),
        'Invalid SAB identity.')
    checked = core.timestamp(handoff['checked_at'])
    paths, hashes, audio, total = set(), set(), [], 0
    art_count = cue_count = 0
    acquired = files[0]['acquired_at']
    acquired_time = core.timestamp(acquired)
    test_only = files[0]['test_only']
    for item in files:
        path = relative(item['relative_path'])
        key = unicodedata.normalize('NFC', str(path)).casefold()
        require(path.parts[0] == 'CC-' + provenance['request_id'] and key not in paths,
                'Duplicate or out-of-request file path.')
        paths.add(key)
        require(item['provenance'] == provenance and item['nzo_id'] == selection['nzo_id']
                and item['test_only'] is test_only and type(test_only) is bool,
                'Mixed manifest provenance.')
        require(core.is_hash(item['sha256']) and item['media_identity'] == 'sha256:' + item['sha256']
                and type(item['size']) is int and 0 < item['size'] <= MAX_BYTES, 'Invalid file identity.')
        total += item['size']
        if path.suffix.lower() == '.flac':
            require(item['sha256'] not in hashes and item['observed_media'] == 'audio',
                    'Duplicate or incorrectly classified FLAC track.')
            hashes.add(item['sha256']); audio.append(item)
        else:
            if path.suffix.lower() == '.cue':
                cue_count += 1
            else:
                art_count += 1
            limit = 1024**2 if path.suffix.lower() == '.cue' else 20 * 1024**2
            require(item['size'] <= limit and item['observed_media'] in ('unknown', 'image'),
                    'Invalid bounded sidecar.')
        require(item['selected_scope'] == 'album_flac'
                and item['observed_media_basis'] == 'filename_extension_only'
                and item['content_identity'] == 'unverified' and item['full_work_verified'] is False
                and item['playback_started'] is False and item['indexed'] is False
                and item['kodi_music_catalog_eligible'] is False and item['reverify_before_playback'] is True,
                'Unexpected completion semantics.')
        verified = core.timestamp(item['verified_at'])
        require(item['acquired_at'] == acquired and acquired_time.microsecond == 0
                and int(verified.timestamp()) <= acquired_time.timestamp()
                and verified <= checked <= now and acquired_time <= checked
                and item['acquired_at_basis'] == 'sab_history_completed_at',
                'Authoritative final SAB timestamp required.')
    require(len(audio) == album['track_count'] and total <= MAX_BYTES
            and art_count <= 16 and cue_count <= 4,
            'FLAC count differs from selected album, or album too large.')
    evidence = {'source': 'sab_history', 'nzo_id': selection['nzo_id'], 'acquired_at': acquired,
        'acquired_at_basis': 'sab_history_completed_at', 'provenance': provenance,
        'files': [{key: item[key] for key in ('relative_path', 'sha256', 'size')} for item in files],
        'source_identity': identity, 'album_identity': album}
    require(handoff['completion_evidence'] == evidence
            and core.digest(evidence, newline=True) == handoff['completion_evidence_sha256']
            == selection['completion_evidence_sha256'], 'Full completion evidence mismatch.')
    hook = core.digest({'authorization': auth, 'files': [dict(item, acquired_at=None,
        acquired_at_basis='unavailable') for item in files]})
    return {'request_id': provenance['request_id'], 'file': deepcopy(audio[0]),
        'files': deepcopy(files), 'album_identity': deepcopy(album), 'selection': deepcopy(selection),
        'hook_sha256': hook, 'completion_evidence_sha256': handoff['completion_evidence_sha256'],
        'signature': core.digest([selection, hook]), 'checked_at': handoff['checked_at']}


def number(value, *, default=None):
    if value is None and default is not None:
        return default, None
    require(type(value) is str and re.fullmatch(r'[0-9]{1,3}(?:/[0-9]{1,3})?', value.strip()),
            'Explicit positive disc/track numbering required.')
    parts = [int(part) for part in value.strip().split('/')]
    require(1 <= parts[0] <= MAX_FILES and (len(parts) == 1 or parts[0] <= parts[1] <= MAX_FILES),
            'Invalid disc/track number or total.')
    return parts[0], parts[1] if len(parts) == 2 else None


def probe_track(probe, item, album, target):
    require(type(probe) is dict and probe.get('validated_sha256') == item['sha256'],
            'Hash-bound FLAC probe required.')
    streams = probe.get('streams')
    require(type(streams) is list and len(streams) <= 16, 'Bounded probe streams required.')
    audio = [s for s in streams if type(s) is dict and s.get('codec_type') == 'audio']
    require(len(audio) == 1 and audio[0].get('codec_name') == 'flac'
            and all(type(s) is dict and (s.get('codec_type') == 'audio' or
                s.get('codec_type') == 'video' and s.get('codec_name') in ('mjpeg', 'png')
                and type(s.get('disposition')) is dict and s['disposition'].get('attached_pic') == 1)
                for s in streams), 'Exactly one FLAC audio stream with optional embedded artwork required.')
    duration = float(probe.get('format', {}).get('duration', 0))
    require(math.isfinite(duration) and 0 < duration <= 86400, 'Positive finite track duration required.')
    raw = probe.get('format', {}).get('tags', {})
    require(type(raw) is dict and len(raw) <= 128, 'Bounded embedded tags required.')
    tags = {}
    for key, value in raw.items():
        require(type(key) is str and key.casefold() not in tags, 'Ambiguous embedded tag names.')
        tags[key.casefold()] = value
    title, artist, name = (text(tags.get(field)) for field in ('title', 'artist', 'album'))
    require(normalized(artist) == normalized(album['artist']) and normalized(name) == normalized(album['album']),
            'Embedded artist/album differs from the selected album.')
    if 'albumartist' in tags:
        require(normalized(tags['albumartist']) == normalized(album['artist']), 'Conflicting album artist.')
    for first, alternate in (('track', 'tracknumber'), ('disc', 'discnumber')):
        if first in tags and alternate in tags:
            require(number(tags[first]) == number(tags[alternate]), 'Conflicting numbering tags.')
    track, track_total = number(tags.get('track', tags.get('tracknumber')))
    disc, disc_total = number(tags.get('disc', tags.get('discnumber')), default=1)
    if album['namespace'] == 'musicbrainz':
        for tag, field in (('musicbrainz_albumid', 'release_id'), ('musicbrainz_artistid', 'artist_id')):
            if tag in tags:
                require(tags[tag] == album[field], 'Embedded MusicBrainz identity differs.')
    return {'relative_path': item['relative_path'], 'local_path': str(target), 'sha256': item['sha256'],
        'bytes': item['size'], 'validated_sha256': item['sha256'], 'title': title, 'artist': artist,
        'album': name, 'disc': disc, 'track': track, 'duration': duration,
        'embedded_artwork': any(s.get('codec_type') == 'video' for s in streams)}, (disc_total, track_total)


def complete_numbering(tracks, totals=None):
    pairs = {(t['disc'], t['track']) for t in tracks}
    require(len(pairs) == len(tracks), 'Duplicate disc/track numbering.')
    discs = {disc for disc, _ in pairs}
    require(discs == set(range(1, max(discs) + 1)), 'Incomplete disc numbering.')
    counts = {disc: len([p for p in pairs if p[0] == disc]) for disc in discs}
    require(all({track for d, track in pairs if d == disc} == set(range(1, count + 1))
                for disc, count in counts.items()), 'Incomplete track numbering.')
    for track, (disc_total, track_total) in zip(tracks, totals or []):
        require((disc_total is None or disc_total == len(discs))
                and (track_total is None or track_total == counts[track['disc']]), 'Declared tag totals differ.')


def covers(files):
    parents = [str(PurePosixPath(item['relative_path']).parent) for item in files
               if PurePosixPath(item['relative_path']).suffix.lower() == '.flac']
    common = PurePosixPath(os.path.commonpath(parents))
    choices = [item for item in files if PurePosixPath(item['relative_path']).parent == common
               and PurePosixPath(item['relative_path']).name.lower() in
               ('folder.jpg', 'folder.png', 'cover.jpg', 'cover.png')]
    return choices


def published(files):
    art = covers(files)
    return [item for item in files if PurePosixPath(item['relative_path']).suffix.lower() == '.flac'
            or item in art]


def verify_tree(cache, job):
    directory = cache / ('CC-' + job['request_id'])
    expected = {str(PurePosixPath(item['relative_path']).relative_to(directory.name)): item
                for item in published(job['files'])}
    children = {'': set()}
    for name in expected:
        parts = PurePosixPath(name).parts
        for index, part in enumerate(parts):
            children.setdefault('/'.join(parts[:index]), set()).add(part)
            if index < len(parts)-1:
                children.setdefault('/'.join(parts[:index+1]), set())
    core.safe_directory(directory)
    with ExitStack() as stack:
        root = core.open_directory(directory, stack)
        descriptors, snapshots = {'': root}, []
        for name in sorted(children, key=lambda value: (value.count('/'), value)):
            if name:
                parent, _, leaf = name.rpartition('/')
                fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[parent])
                stack.callback(os.close, fd); descriptors[name] = fd
            fd = descriptors[name]
            info = os.fstat(fd)
            require(info.st_uid == os.getuid() and info.st_mode & 0o022 == 0, 'Unsafe import directory.')
            snapshots.append((fd, core.file_signature(info)))
            seen = set()
            with os.scandir(fd) as entries:
                for entry in entries:
                    require(entry.name in children[name] and len(seen) < len(children[name]),
                            'Unexpected file in album tree; files preserved.')
                    seen.add(entry.name)
            require(seen == children[name], 'Missing expected album file.')
        for name, item in expected.items():
            parent, _, leaf = name.rpartition('/')
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptors[parent])
            stream = stack.enter_context(os.fdopen(fd, 'rb'))
            snap = core.verify_stream(stream, item); snapshots.append((fd, snap))
            require(core.file_signature(os.stat(leaf, dir_fd=descriptors[parent], follow_symlinks=False)) == snap,
                    'Album file replaced during inventory.')
        require(all(core.file_signature(os.fstat(fd)) == snap for fd, snap in snapshots),
                'Album tree changed during inventory.')
        reopened = core.open_directory(directory, stack)
        require(core.file_signature(os.fstat(reopened)) == snapshots[0][1], 'Album root changed during inventory.')


def artist_matches(value, expected):
    return type(value) is list and len(value) == 1 and normalized(value[0]) == normalized(expected)


def artwork_present(rows):
    return any(type(row.get('thumbnail')) is str and bool(row['thumbnail'])
               or type(row.get('art')) is dict and any(type(v) is str and bool(v) for v in row['art'].values())
               for row in rows)


def validate_artwork(rows, tracks, files):
    """Only preserved conventional art or hash-bound embedded pictures prove art."""
    conventional = covers(files)
    paths = {track['local_path'] for track in tracks if track['embedded_artwork']}
    expected = set()
    if conventional:
        first = tracks[0]
        root = Path(first['local_path'])
        for _ in relative(first['relative_path']).parts:
            root = root.parent
        expected.update(str(root.joinpath(*relative(item['relative_path']).parts)) for item in conventional)
    observed = False
    for row in rows:
        values = [row.get('thumbnail')]
        art = row.get('art', {})
        require(type(art) is dict and len(art) <= 32, 'Bounded Kodi artwork map required.')
        values.extend(art.values())
        for value in values:
            if value in (None, ''):
                continue
            require(type(value) is str and len(value) <= 16384, 'Bounded Kodi artwork path required.')
            candidate = value
            if candidate.startswith('image://'):
                candidate = unquote(candidate[len('image://'):]).rstrip('/')
            elif candidate.startswith('file://'):
                candidate = unquote(candidate[len('file://'):])
            valid = candidate in expected or (candidate.startswith('music@') and candidate[6:] in paths)
            require(valid, 'Kodi artwork does not prove preserved album artwork.')
            observed = True
    require(not (expected or paths) or observed, 'Preserved artwork not observed in Kodi.')
    return observed


def validate_library(library, tracks, album, files):
    require(type(library) is dict and set(library) == {'songs', 'albums', 'all_tracks_indexed_once', 'artwork_present'}
            and library['all_tracks_indexed_once'] is True, 'Exact album readback required.')
    songs, albums = library['songs'], library['albums']
    require(type(songs) is list and len(songs) == len(tracks) and type(albums) is list and len(albums) == 1,
            'Complete, single-album Kodi readback required.')
    details = albums[0]
    require(type(details) is dict and type(details.get('albumid')) is int and details['albumid'] > 0
            and normalized(details.get('title')) == normalized(album['album'])
            and artist_matches(details.get('artist'), album['artist']), 'Kodi album identity differs.')
    by_path, ids = {}, set()
    for song in songs:
        require(type(song) is dict and type(song.get('songid')) is int and song['songid'] > 0
                and song['songid'] not in ids and song.get('file') not in by_path, 'Duplicate Kodi song.')
        ids.add(song['songid']); by_path[song.get('file')] = song
    require(set(by_path) == {track['local_path'] for track in tracks}, 'Kodi paths differ from exact album tree.')
    for track in tracks:
        song = by_path[track['local_path']]
        require(song.get('title') == track['title'] and normalized(song.get('album')) == normalized(track['album'])
                and artist_matches(song.get('artist'), track['artist'])
                and (song.get('albumartist') in (None, []) or artist_matches(song['albumartist'], album['artist']))
                and song.get('albumid') == details['albumid']
                and type(song.get('track')) is int and song['track'] == track['track']
                and type(song.get('disc')) is int and song['disc'] == track['disc']
                and type(song.get('duration')) in (int, float) and math.isfinite(song['duration'])
                and 0 < song['duration'] <= 86400
                and abs(song['duration'] - track['duration']) <= 2,
                'Kodi track metadata differs; existing values preserved.')
        if album['namespace'] == 'musicbrainz':
            for row, key, expected in ((song, 'musicbrainzartistid', album['artist_id']),
                    (song, 'musicbrainzalbumid', album['release_id']),
                    (details, 'musicbrainzalbumid', album['release_id'])):
                value = row.get(key)
                require(value in (None, '', []) or value == expected or value == [expected],
                        'Kodi MusicBrainz identity differs.')
    require(type(library['artwork_present']) is bool and library['artwork_present'] ==
            validate_artwork(songs + albums, tracks, files), 'Artwork presence must be observed.')


def readback(kodi, directory, tracks, album, files, on_seen=None):
    reply = kodi.call('AudioLibrary.GetSongs', {'filter': {'field': 'path', 'operator': 'startswith',
        'value': str(directory) + '/'}, 'properties': SONG_FIELDS,
        'limits': {'start': 0, 'end': MAX_FILES + 1}})
    require(type(reply) is dict, 'Kodi song response required.')
    songs = reply.get('songs', [])
    require(type(songs) is list, 'Kodi song list required.')
    if songs and on_seen is not None:
        on_seen()
    require(len(songs) <= len(tracks)
            and reply.get('limits', {}).get('total', len(songs)) == len(songs), 'Ambiguous Kodi song window.')
    if len(songs) < len(tracks):
        require(all(type(song) is dict and song.get('file') in {t['local_path'] for t in tracks}
                    for song in songs), 'Unrelated song in album directory.')
        return None
    album_ids = {song.get('albumid') for song in songs}
    require(len(album_ids) == 1 and all(type(value) is int and value > 0 for value in album_ids),
            'Tracks belong to different Kodi albums.')
    details = kodi.call('AudioLibrary.GetAlbumDetails', {'albumid': next(iter(album_ids)),
                                                       'properties': ALBUM_FIELDS})['albumdetails']
    songs = [{key: song[key] for key in ['songid', *SONG_FIELDS] if key in song} for song in songs]
    details = {key: details[key] for key in ['albumid', *ALBUM_FIELDS] if key in details}
    result = {'songs': songs, 'albums': [details], 'all_tracks_indexed_once': True,
              'artwork_present': artwork_present(songs + [details])}
    validate_library(result, tracks, album, files)
    return result


def validate_receipt(receipt, handoff, selection, *, cache, now=None):
    job = validate(handoff, selection, now)
    cache = Path(cache).absolute()
    require(type(receipt) is dict and receipt.get('version') == 1
            and receipt['request_id'] == job['request_id'] and receipt['selection'] == selection
            and receipt['album_identity'] == job['album_identity'] and receipt['files'] == job['files']
            and receipt['hook_sha256'] == job['hook_sha256']
            and receipt['completion_evidence_sha256'] == job['completion_evidence_sha256']
            and receipt['signature'] == core.digest([job['signature'], str(cache)])
            and receipt['playback_started'] is False and receipt['scan_key'] == 'album-' + job['request_id']
            and type(receipt['scan_attempts']) is int and receipt['scan_attempts'] in (0, 1)
            and type(receipt['readback_seen']) is bool
            and receipt['status'] in ('queued', 'scanning', 'imported', 'unknown', 'blocked'), 'Conflicting album receipt.')
    current = now or datetime.now(timezone.utc)
    acquired = core.timestamp(job['files'][0]['acquired_at'])
    created = core.timestamp(receipt['created_at'])
    checked = core.timestamp(receipt['checked_at'])
    require(acquired <= created <= checked <= current, 'Invalid receipt time.')
    tracks = receipt['tracks']
    require(type(tracks) is list and len(tracks) <= job['album_identity']['track_count'], 'Invalid receipt tracks.')
    expected = {item['relative_path']: item for item in job['files'] if relative(item['relative_path']).suffix.lower() == '.flac'}
    seen = set()
    for track in tracks:
        require(type(track) is dict and set(track) == TRACK_FIELDS and track['relative_path'] in expected
                and track['relative_path'] not in seen, 'Conflicting track receipt.')
        seen.add(track['relative_path']); item = expected[track['relative_path']]
        require(track['sha256'] == track['validated_sha256'] == item['sha256'] and track['bytes'] == item['size']
                and track['local_path'] == str(cache.joinpath(*relative(item['relative_path']).parts))
                and normalized(track['artist']) == normalized(job['album_identity']['artist'])
                and normalized(track['album']) == normalized(job['album_identity']['album'])
                and type(track['embedded_artwork']) is bool
                and type(track['duration']) in (int, float) and math.isfinite(track['duration'])
                and 0 < track['duration'] <= 86400 and type(track['disc']) is int and 1 <= track['disc'] <= MAX_FILES
                and type(track['track']) is int and 1 <= track['track'] <= MAX_FILES, 'Invalid verified track binding.')
        text(track['title'])
    if tracks:
        require(len(tracks) == len(expected), 'Partial probe set cannot authorize an album scan.')
        complete_numbering(tracks)
    if receipt['status'] == 'imported':
        require(len(tracks) == len(expected) and receipt['readback_seen'], 'Imported album needs complete tracks.')
        validate_library(receipt['library'], tracks, job['album_identity'], job['files'])
        readiness = receipt['readiness']
        require(set(readiness) == {'file_verified_at', 'library_observed_at', 'track_count'}
                and readiness['track_count'] == len(tracks)
                and acquired <= core.timestamp(readiness['file_verified_at']) <=
                    core.timestamp(readiness['library_observed_at']) <= current, 'Invalid album readiness.')
    else:
        require(receipt['library'] is None and 'readiness' not in receipt, 'Uncertain receipt cannot claim readiness.')
    require(receipt['status'] != 'scanning' or receipt['scan_attempts'] == 1, 'Scan not durably reserved.')
    return receipt


def live_art(relative_path, staging, **expected):
    from mac_album_files import fetch_art
    return fetch_art(relative_path, staging, **expected)


def live_sidecar(relative_path, **expected):
    from mac_album_files import verify_sidecar
    return verify_sidecar(relative_path, **expected)


def consume(handoff, selection, *, cache=CACHE, state_dir=ROOT / 'state' / 'album', execute=False,
            transfer=None, art_transfer=None, sidecar_verify=None, kodi=None, timeout=30,
            pause=time.sleep, now=None):
    job = validate(handoff, selection, now)
    require(type(execute) is bool and type(timeout) in (int, float)
            and math.isfinite(timeout) and 0 <= timeout <= 60, 'Explicit bounded execution required.')
    if not execute:
        return {'status': 'blocked', 'reason': 'execution_not_authorized',
                'request_id': job['request_id'], 'playback_started': False}
    art = covers(job['files'])
    skipped = [item for item in job['files'] if relative(item['relative_path']).suffix.lower() != '.flac' and item not in art]
    require(not job['file']['test_only'] or (transfer is not None and kodi is not None
            and (not art or art_transfer is not None) and (not skipped or sidecar_verify is not None)),
            'Synthetic handoffs require injected adapters.')
    state_dir = core.safe_directory(state_dir, create=True, private=True)
    cache = core.safe_directory(cache, create=True)
    fd = os.open(state_dir / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, 'Unsafe album lock.')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = state_dir / (job['request_id'] + '.json')
        previous = core.read_json(path, private=True) if path.exists() or path.is_symlink() else None
        if previous:
            validate_receipt(previous, handoff, selection, cache=cache, now=now)
            if previous['status'] == 'imported':
                return previous
        receipts = list(state_dir.glob('*.json'))
        require(len(receipts) < core.MAX_RECEIPTS or previous is not None, 'Album receipt capacity reached.')
        hashes = {item['sha256'] for item in job['files'] if relative(item['relative_path']).suffix.lower() == '.flac'}
        for other in receipts:
            if other != path:
                saved = core.read_json(other, private=True)
                prior = saved['album_identity']
                require((prior['namespace'], prior['release_id']) !=
                        (job['album_identity']['namespace'], job['album_identity']['release_id'])
                        and not hashes.intersection(item['sha256'] for item in saved['files']
                            if relative(item['relative_path']).suffix.lower() == '.flac'),
                        'Album release or track bytes already belong to another request.')
        receipt = previous or {'version': 1, 'request_id': job['request_id'],
            'signature': core.digest([job['signature'], str(cache)]), 'selection': job['selection'],
            'album_identity': job['album_identity'], 'files': job['files'], 'hook_sha256': job['hook_sha256'],
            'completion_evidence_sha256': job['completion_evidence_sha256'], 'status': 'queued',
            'scan_attempts': 0, 'scan_key': 'album-' + job['request_id'], 'readback_seen': False,
            'tracks': [], 'library': None, 'created_at': datetime.now(timezone.utc).isoformat(),
            'playback_started': False}
        receipt['checked_at'] = datetime.now(timezone.utc).isoformat()
        core.save_receipt(path, receipt)
        directory = cache / ('CC-' + job['request_id'])
        try:
            if not receipt['scan_attempts'] and not receipt['readback_seen']:
                staged, tracks, totals = [], [], []
                with tempfile.TemporaryDirectory(prefix='album-stage-', dir=state_dir) as folder:
                    for item in job['files']:
                        rel = item['relative_path']; target = cache.joinpath(*relative(rel).parts)
                        if item in skipped:
                            result = (sidecar_verify or live_sidecar)(rel, expected_sha256=item['sha256'], expected_bytes=item['size'])
                            require(result.get('sha256') == item['sha256'] and result.get('bytes') == item['size'],
                                    'Skipped sidecar hash mismatch.')
                            continue
                        adapter = (art_transfer or live_art) if item in art else (transfer or core.live_transfer)
                        result = adapter(rel, Path(folder), expected_sha256=item['sha256'], expected_bytes=item['size'])
                        source = Path(result['local_path'])
                        require(source.is_relative_to(Path(folder)) and result.get('sha256') == item['sha256']
                                and result.get('bytes') == item['size'], 'Transfer binding mismatch.')
                        core.verify_file(source, item)
                        if item not in art:
                            track, total = probe_track(result['media_probe'], item, job['album_identity'], target)
                            tracks.append(track); totals.append(total)
                        staged.append((source, target, item))
                    complete_numbering(tracks, totals)
                    for source, target, item in staged:
                        if target.exists() or target.is_symlink():
                            core.verify_file(target, item)
                        else:
                            core.publish(source, target)
                        core.verify_file(target, item)
                receipt['tracks'] = tracks
                core.save_receipt(path, receipt)
            require(len(receipt['tracks']) == job['album_identity']['track_count'], 'Complete verified probe set required.')
            verify_tree(cache, job)
            file_verified_at = datetime.now(timezone.utc).isoformat()
            kodi = kodi or core.helpers()[0].Kodi()
            def remember_readback():
                receipt['readback_seen'] = True
                core.save_receipt(path, receipt)
            library = readback(kodi, directory, receipt['tracks'], job['album_identity'], job['files'], remember_readback)
            if library is None and not receipt['scan_attempts'] and not receipt['readback_seen']:
                setting = kodi.call('Settings.GetSettingValue', {'setting': 'musiclibrary.downloadinfo'})
                require(type(setting) is dict and setting.get('value') is False,
                        'Disable automatic online music information fetching before this bounded import.')
                receipt.update(status='scanning', scan_attempts=1,
                               scan_reserved_at=datetime.now(timezone.utc).isoformat())
                core.save_receipt(path, receipt)
                verify_tree(cache, job)
                kodi.call('AudioLibrary.Scan', {'directory': str(directory), 'showdialogs': False})
            deadline = time.monotonic() + timeout
            while library is None:
                library = readback(kodi, directory, receipt['tracks'], job['album_identity'], job['files'], remember_readback)
                if library is not None or time.monotonic() >= deadline:
                    break
                pause(min(.25, max(0, deadline-time.monotonic())))
            receipt.update(status='imported' if library is not None else 'unknown', library=library, reason=None)
            if library is not None:
                receipt['readback_seen'] = True
                receipt['readiness'] = {'file_verified_at': file_verified_at,
                    'library_observed_at': datetime.now(timezone.utc).isoformat(), 'track_count': len(receipt['tracks'])}
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired):
            uncertain = receipt['scan_attempts'] or receipt['readback_seen']
            receipt.update(status='unknown' if uncertain else 'blocked', library=None,
                reason='scan_reconciliation_required' if uncertain else 'verification_or_adapter_blocked')
        if receipt['status'] != 'imported':
            receipt.pop('readiness', None)
        receipt['checked_at'] = datetime.now(timezone.utc).isoformat()
        core.save_receipt(path, receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--handoff', type=Path, required=True)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--cache', type=Path, default=CACHE)
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'state' / 'album')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        result = consume(core.read_json(args.handoff), core.read_json(args.selection), cache=args.cache,
                         state_dir=args.state_dir, execute=args.execute)
        print(core.encoded(result).decode())
        return 0 if result['status'] == 'imported' else 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        print('{"status":"blocked","reason":"invalid_or_unavailable_owned_input","playback_started":false}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
