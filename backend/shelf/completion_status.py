"""Persist operator observations, not browser assertions or live availability.

The sole live import writer is the bounded SSH CLI. Its authenticated operator
must retrieve the actual existing media consumer receipt; JSON has no independent
authority. No provider, filesystem/media, Kodi, or messaging calls occur here.
"""
from copy import deepcopy
from shelf.episode_scope import parse_scope, episode_code
from datetime import datetime
import hashlib
import json
import math
import unicodedata
from pathlib import PurePosixPath
from posixpath import commonpath
from urllib.parse import unquote

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from shelf import completion_registry as registry, obtain_acquisition as acquire
from shelf.acquisition_contract import Blocked, _text
from shelf import completion_media as media
from shelf.models import ObtainCompletionStatus, ObtainTransfer

MAX_RECEIPT_BYTES = 256 * 1024


def digest(value, *, newline=False):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw + (b'\n' if newline else b'')).hexdigest()


def _authority(actor, profile_id, job_id, context):
    if type(context) is not acquire.OperatorContext:
        raise Blocked('Trusted completion operator required')
    context.require(actor, profile_id)
    entry = acquire.completion_registration(actor=actor, profile_id=profile_id,
        job_id=job_id, context=context)
    registry.require(context.purpose == 'offline_test' or entry['test_only'] is False)
    return entry


def _lock(actor, profile_id, job_id, context, entry):
    transfer = ObtainTransfer.objects.get(job_id=job_id)
    # A no-value-change UPDATE acquires the SQLite write lock too. This avoids
    # races with cancellation while keeping identical receipt replays no-ops.
    changed = ObtainTransfer.objects.filter(pk=transfer.pk, revision=transfer.revision,
        binding=transfer.binding, provider_job=entry['nzo_id'], state='queued',
        local_cancelled_at__isnull=True, job__status='blocked_configuration').update(
            revision=F('revision'))
    registry.require(changed == 1)
    registry.require(_authority(actor, profile_id, job_id, context) == entry)
    return transfer


def _completion(handoff, entry, job):
    registry.require(type(handoff) is dict and handoff.get('version') == 2
        and handoff.get('state') == 'COMPLETED_FILES_VERIFIED'
        and handoff.get('sab_history_completed') is True)
    registry.require(len(registry.encode_json(handoff)) <= MAX_RECEIPT_BYTES)
    evidence = handoff['completion_evidence']
    registry.require(digest(evidence, newline=True) == handoff['completion_evidence_sha256'])
    provenance = {key: entry[key] for key in ('request_id', 'result_id', 'result_digest', 'nzb_sha256')}
    identity = media.source_identity(job)
    registry.require(handoff['source_identity'] == identity == evidence['source_identity'])
    if (job.provider, job.medium, job.scope) == ('album_request', 'music', 'album_flac'):
        album = media.album_identity(job)
        registry.require(handoff['album_identity'] == evidence['album_identity']
            == handoff['authorization']['album_identity'] == album)
    files = handoff['files']
    registry.require(type(files) is list and bool(files)
        and evidence['source'] == 'sab_history' and evidence['nzo_id'] == entry['nzo_id']
        and evidence['provenance'] == provenance
        and evidence['acquired_at_basis'] == 'sab_history_completed_at'
        and evidence['files'] == [{key: item[key] for key in ('relative_path', 'sha256', 'size')}
                                  for item in files])
    registry.require(all(item['provenance'] == provenance and item['nzo_id'] == entry['nzo_id']
        and item['test_only'] is entry['test_only'] and item['acquired_at'] == evidence['acquired_at']
        and item['acquired_at_basis'] == evidence['acquired_at_basis'] for item in files))
    checked, acquired = (datetime.fromisoformat(handoff['checked_at']),
                         datetime.fromisoformat(evidence['acquired_at']))
    registry.require(checked.utcoffset() is not None and acquired.utcoffset() is not None
        and acquired <= checked <= timezone.now())
    # checked_at refreshes with every export; immutable completion evidence and
    # hook file observations retain their original versions.
    return {key: deepcopy(value) for key, value in handoff.items() if key != 'checked_at'}


def record_completion(*, actor, profile_id, job_id, context, handoff):
    """Called only after observer validates final SAB history plus current files."""
    entry = _authority(actor, profile_id, job_id, context)
    with transaction.atomic():
        transfer = _lock(actor, profile_id, job_id, context, entry)
        completion = _completion(handoff, entry, transfer.job)
        row, created = ObtainCompletionStatus.objects.get_or_create(transfer=transfer, defaults={
            'completion': completion, 'completion_sha256': completion['completion_evidence_sha256']})
        registry.require(row.completion == completion and row.completion_sha256 ==
                         completion['completion_evidence_sha256'])
        return row


def _video_receipt(receipt, row, entry, *, kind):
    registry.require(type(receipt) is dict
        and len(registry.encode_json(receipt)) <= MAX_RECEIPT_BYTES
        and type(receipt.get('version')) is int and receipt['version'] == 1)
    complete = row.completion
    identity = complete['source_identity']
    registry.require(media.kind(identity) == kind and len(complete['files']) == 1)
    item = complete['files'][0]
    registry.require(item['observed_media'] == 'video' and item['selected_scope'] == identity['scope']
        and item['playback_started'] is False and item['content_identity'] == 'unverified')
    selection = {'source_identity': identity, 'provenance': item['provenance'],
        'nzo_id': entry['nzo_id'], 'completion_evidence_sha256': row.completion_sha256}
    registry.require(receipt['request_id'] == entry['request_id']
        and receipt['selection'] == selection and receipt['file'] == item
        and receipt['completion_evidence_sha256'] == row.completion_sha256
        and receipt['playback_started'] is False
        and receipt['scan_key'] == ('video-' if kind == 'movie' else 'episode-') + entry['request_id']
        and type(receipt['scan_attempts']) is int and receipt['scan_attempts'] in (0, 1))
    hook_file = dict(item, acquired_at=None, acquired_at_basis='unavailable')
    registry.require(receipt['hook_sha256'] == digest({
        'authorization': complete['authorization'], 'files': [hook_file]}))
    state = receipt['status']
    registry.require(state in ('blocked', 'unknown', 'imported'))
    created, checked = (datetime.fromisoformat(receipt[key]) for key in ('created_at', 'checked_at'))
    registry.require(created.utcoffset() is not None and checked.utcoffset() is not None
        and datetime.fromisoformat(item['acquired_at']) <= created <= checked <= timezone.now())
    library = receipt['library']
    if state == 'imported':
        local = receipt['local_path']
        relative = PurePosixPath(item['relative_path'])
        path = PurePosixPath(local)
        registry.require(type(local) is str and len(local) <= 4096 and path.is_absolute()
            and str(path) == local and '..' not in path.parts
            and type(library) is dict and library['file'] == local)
        if kind == 'movie':
            registry.require(path.parts[-len(relative.parts):] == relative.parts
                and type(library['movieid']) is int and library['movieid'] > 0
                and str(library['uniqueid']['tmdb']) == str(identity['source_id']))
        else:
            extension = relative.suffix.lower()
            season, episode = parse_scope(identity['scope'])
            code = episode_code(identity['scope'])
            registry.require(extension in {'.mp4', '.mkv', '.webm'}
                and path.parts[-2:] == ('CC-' + entry['request_id'], code + extension)
                and type(library['episodeid']) is int and library['episodeid'] > 0
                and type(library['tvshowid']) is int and library['tvshowid'] > 0
                and type(library['season']) is int and library['season'] == season
                and type(library['episode']) is int and library['episode'] == episode
                and str(library['show_uniqueid']['tmdb']) == str(identity['source_id'])
                and library['uniqueid']['cabinet'] == 'tmdb-show-' + str(identity['source_id']) + ':' + code.lower())
    else:
        registry.require(library is None)
    # Keep only evidence needed for status/reconciliation; no resume/history,
    # arbitrary adapter errors, probe output or unrestricted Kodi metadata.
    evidence = {key: deepcopy(receipt[key]) for key in ('version', 'request_id', 'selection',
        'file', 'completion_evidence_sha256', 'hook_sha256', 'status', 'scan_key',
        'scan_attempts', 'created_at', 'checked_at', 'playback_started')}
    if state == 'imported':
        if kind == 'movie':
            evidence['library'] = {'movieid': library['movieid'], 'file': library['file'],
                                  'tmdb': str(library['uniqueid']['tmdb'])}
        else:
            evidence['library'] = {key: library[key] for key in
                                   ('episodeid', 'tvshowid', 'file', 'season', 'episode')}
            evidence['library'].update(show_tmdb=str(library['show_uniqueid']['tmdb']),
                                       cabinet_id=library['uniqueid']['cabinet'])
    if 'readiness' in receipt:
        readiness = receipt['readiness']
        registry.require(state == 'imported' and type(readiness) is dict
            and set(readiness) == {'file_verified_at', 'library_observed_at',
                                  'validated_sha256', 'video_codec', 'duration'}
            and readiness['validated_sha256'] == item['sha256']
            and type(readiness['video_codec']) is str
            and 0 < len(readiness['video_codec']) <= 40
            and readiness['video_codec'].isascii()
            and readiness['video_codec'].replace('_', '').isalnum()
            and type(readiness['duration']) in (int, float)
            and math.isfinite(readiness['duration'])
            and 0 < readiness['duration'] <= 7 * 86400)
        verified, observed = (datetime.fromisoformat(readiness[key])
                              for key in ('file_verified_at', 'library_observed_at'))
        registry.require(verified.utcoffset() is not None and observed.utcoffset() is not None
            and datetime.fromisoformat(item['acquired_at']) <= verified <= observed <= timezone.now())
        evidence['readiness'] = deepcopy(readiness)
    return evidence, digest(receipt)


def _movie_receipt(receipt, row, entry):
    return _video_receipt(receipt, row, entry, kind='movie')


def _episode_receipt(receipt, row, entry):
    return _video_receipt(receipt, row, entry, kind='episode')


def _normalized(value):
    _text(value, 500)
    return ' '.join(unicodedata.normalize('NFC', value).split()).casefold()


def _positive(value, maximum):
    return type(value) is int and 0 < value <= maximum


def _musicbrainz(item, identity):
    if identity['namespace'] != 'musicbrainz':
        return
    for key, selected in (('musicbrainzalbumid', 'release_id'), ('musicbrainzartistid', 'artist_id')):
        value = item.get(key)
        if value:
            registry.require(value == identity[selected] or value == [identity[selected]])


def _album_artwork(items, complete, tracks, cache):
    common = PurePosixPath(commonpath([str(PurePosixPath(track['relative_path']).parent) for track in tracks]))
    choices = [item for item in complete['files']
               if PurePosixPath(item['relative_path']).parent == common
               and PurePosixPath(item['relative_path']).name.casefold() in
               {'cover.jpg', 'cover.png', 'folder.jpg', 'folder.png'}]
    covers = {str(PurePosixPath(cache, item['relative_path'])) for item in choices}
    embedded = {'music@' + track['local_path'] for track in tracks if track['embedded_artwork']}
    fields = set()
    for item in items:
        art = item.get('art', {})
        registry.require(type(art) is dict and len(art) <= 32)
        for key, value in [('thumbnail', item.get('thumbnail')), *art.items()]:
            _text(key, 80)
            if value:
                _text(value, 4096)
                local = value
                if local.startswith('image://'):
                    local = unquote(local[len('image://'):]).rstrip('/')
                elif local.startswith('file://'):
                    local = unquote(local[len('file://'):])
                registry.require(local in covers or local in embedded)
                fields.add(key)
    registry.require(not (covers or embedded) or bool(fields))
    return fields


def _album_receipt(receipt, row, entry):
    complete = row.completion
    identity = complete['album_identity']
    registry.require(type(receipt) is dict and len(registry.encode_json(receipt)) <= MAX_RECEIPT_BYTES
        and type(receipt.get('version')) is int and receipt['version'] == 1
        and media.kind(complete['source_identity']) == 'album'
        and receipt['request_id'] == entry['request_id']
        and receipt['selection'] == media.selection(complete, entry)
        and receipt['album_identity'] == identity == complete['authorization']['album_identity']
        and receipt['files'] == complete['files']
        and receipt['completion_evidence_sha256'] == row.completion_sha256
        and receipt['playback_started'] is False
        and receipt['scan_key'] == 'album-' + entry['request_id']
        and type(receipt['scan_attempts']) is int and receipt['scan_attempts'] in (0, 1)
        and type(receipt['readback_seen']) is bool)
    hook_files = [dict(item, acquired_at=None, acquired_at_basis='unavailable')
                  for item in complete['files']]
    registry.require(receipt['hook_sha256'] == digest({
        'authorization': complete['authorization'], 'files': hook_files}))
    signature = receipt['signature']
    registry.require(type(signature) is str and len(signature) == 64
        and all(char in '0123456789abcdef' for char in signature))
    state = receipt['status']
    registry.require(state in ('blocked', 'unknown', 'imported'))
    created, checked = (datetime.fromisoformat(receipt[key]) for key in ('created_at', 'checked_at'))
    acquired = datetime.fromisoformat(complete['completion_evidence']['acquired_at'])
    registry.require(created.utcoffset() is not None and checked.utcoffset() is not None
        and acquired <= created <= checked <= timezone.now())
    manifest = {item['relative_path']: item for item in complete['files']
                if PurePosixPath(item['relative_path']).suffix.lower() == '.flac'}
    registry.require(len(manifest) == identity['track_count'])
    tracks = receipt['tracks']
    registry.require(type(tracks) is list and len(tracks) in (0, len(manifest)))
    paths, positions, caches = set(), set(), set()
    for track in tracks:
        registry.require(type(track) is dict and track['relative_path'] in manifest)
        item = manifest[track['relative_path']]
        relative = PurePosixPath(item['relative_path'])
        local = track['local_path']
        path = PurePosixPath(local)
        registry.require(type(local) is str and len(local) <= 4096 and path.is_absolute()
            and str(path) == local and '..' not in path.parts
            and relative.parts[0] == 'CC-' + entry['request_id']
            and path.parts[-len(relative.parts):] == relative.parts
            and track['sha256'] == track['validated_sha256'] == item['sha256']
            and type(track['bytes']) is int and track['bytes'] == item['size']
            and _normalized(track['artist']) == _normalized(identity['artist'])
            and _normalized(track['album']) == _normalized(identity['album'])
            and _positive(track['disc'], 128) and _positive(track['track'], 128)
            and type(track['duration']) in (int, float) and math.isfinite(track['duration'])
            and 0 < track['duration'] <= 86400
            and type(track['embedded_artwork']) is bool)
        _text(track['title'], 500)
        registry.require(track['relative_path'] not in paths
            and (track['disc'], track['track']) not in positions)
        paths.add(track['relative_path'])
        positions.add((track['disc'], track['track']))
        caches.add(str(PurePosixPath(*path.parts[:-len(relative.parts)])))
    registry.require(len(caches) <= 1)
    if caches:
        registry.require(signature == digest([digest([receipt['selection'], receipt['hook_sha256']]), next(iter(caches))]))
    evidence = {key: deepcopy(receipt[key]) for key in (
        'version', 'request_id', 'signature', 'selection', 'album_identity', 'files',
        'hook_sha256', 'completion_evidence_sha256', 'status', 'scan_key',
        'scan_attempts', 'readback_seen', 'created_at', 'checked_at', 'playback_started')}
    evidence['tracks'] = [{key: track[key] for key in (
        'relative_path', 'local_path', 'sha256', 'bytes', 'validated_sha256',
        'title', 'artist', 'album', 'disc', 'track', 'duration', 'embedded_artwork')} for track in tracks]
    library = receipt['library']
    if state == 'imported':
        discs = {disc for disc, _ in positions}
        registry.require(discs == set(range(1, len(discs) + 1)))
        for disc in discs:
            numbers = {number for current, number in positions if current == disc}
            registry.require(numbers == set(range(1, len(numbers) + 1)))
        registry.require(receipt['readback_seen'] is True and paths == set(manifest)
            and type(library) is dict and library['all_tracks_indexed_once'] is True
            and type(library['songs']) is list and len(library['songs']) == len(tracks)
            and type(library['albums']) is list and len(library['albums']) == 1)
        album = library['albums'][0]
        registry.require(type(album) is dict and _positive(album['albumid'], 2**63 - 1)
            and _normalized(album['title']) == _normalized(identity['album'])
            and type(album['artist']) is list and len(album['artist']) == 1
            and _normalized(album['artist'][0]) == _normalized(identity['artist']))
        if album.get('albumartist'):
            registry.require(type(album['albumartist']) is list and len(album['albumartist']) == 1
                and _normalized(album['albumartist'][0]) == _normalized(identity['artist']))
        _musicbrainz(album, identity)
        by_path = {track['local_path']: track for track in tracks}
        song_ids, song_paths = set(), set()
        songs = []
        for song in library['songs']:
            registry.require(type(song) is dict and song['file'] in by_path)
            track = by_path[song['file']]
            registry.require(_positive(song['songid'], 2**63 - 1) and song['songid'] not in song_ids
                and song['file'] not in song_paths and _positive(song['albumid'], 2**63 - 1)
                and song['albumid'] == album['albumid']
                and _normalized(song['title']) == _normalized(track['title'])
                and type(song['artist']) is list and len(song['artist']) == 1
                and _normalized(song['artist'][0]) == _normalized(identity['artist'])
                and _normalized(song['album']) == _normalized(identity['album'])
                and type(song['disc']) is int and song['disc'] == track['disc']
                and type(song['track']) is int and song['track'] == track['track']
                and type(song['duration']) in (int, float) and math.isfinite(song['duration'])
                and 0 < song['duration'] <= 86400
                and abs(song['duration'] - track['duration']) <= 2)
            song_ids.add(song['songid'])
            song_paths.add(song['file'])
            _musicbrainz(song, identity)
            if song.get('albumartist'):
                registry.require(type(song['albumartist']) is list and len(song['albumartist']) == 1
                    and _normalized(song['albumartist'][0]) == _normalized(identity['artist']))
            songs.append({key: deepcopy(song[key]) for key in
                          ('songid', 'albumid', 'file', 'title', 'artist', 'album', 'disc', 'track', 'duration')})
        artwork_fields = _album_artwork([album, *library['songs']], complete, tracks, next(iter(caches)))
        registry.require(type(library['artwork_present']) is bool
            and library['artwork_present'] == bool(artwork_fields))
        evidence['library'] = {
            'songs': songs, 'album': {key: deepcopy(album[key]) for key in ('albumid', 'title', 'artist')},
            'all_tracks_indexed_once': True, 'artwork_present': bool(artwork_fields),
            'artwork_fields': sorted(artwork_fields),
        }
    else:
        registry.require(library is None)
    if 'readiness' in receipt:
        readiness = receipt['readiness']
        registry.require(state == 'imported' and type(readiness) is dict
            and set(readiness) == {'file_verified_at', 'library_observed_at', 'track_count'}
            and type(readiness['track_count']) is int and readiness['track_count'] == len(manifest))
        verified, observed = (datetime.fromisoformat(readiness[key])
                              for key in ('file_verified_at', 'library_observed_at'))
        registry.require(verified.utcoffset() is not None and observed.utcoffset() is not None
            and acquired <= verified <= observed <= timezone.now())
        evidence['readiness'] = deepcopy(readiness)
    return evidence, digest(receipt)


def record_import(*, actor, profile_id, job_id, context, receipt, session_guard=None):
    entry = _authority(actor, profile_id, job_id, context)
    registry.require(context.purpose == 'offline_test' or callable(session_guard))
    if session_guard is not None:
        session_guard()
    with transaction.atomic():
        transfer = _lock(actor, profile_id, job_id, context, entry)
        row = ObtainCompletionStatus.objects.get(transfer=transfer)
        validator = {'movie': _movie_receipt, 'episode': _episode_receipt, 'album': _album_receipt}[
            media.kind(row.completion['source_identity'])]
        evidence, fingerprint = validator(receipt, row, entry)
        if session_guard is not None:
            session_guard()
        registry.require(_authority(actor, profile_id, job_id, context) == entry)
        if row.delivery_sha256 == fingerprint:
            return row
        state = evidence['status']
        # Unknown is only reconciled forward; imported is terminal. A different
        # payload for the same state is a conflict, not an implicit overwrite.
        allowed = {'not_reported': {'blocked', 'unknown', 'imported'},
                   'blocked': {'unknown', 'imported'}, 'unknown': {'imported'}, 'imported': set()}
        registry.require(state in allowed[row.delivery_state])
        if row.delivery_receipt:
            registry.require(evidence['scan_attempts'] >= row.delivery_receipt['scan_attempts'])
            if row.delivery_receipt.get('readback_seen') is True:
                registry.require(evidence.get('readback_seen') is True)
        row.delivery_state, row.delivery_receipt = state, evidence
        row.delivery_sha256, row.delivery_at = fingerprint, timezone.now()
        row.revision += 1
        row.save(update_fields=['delivery_state', 'delivery_receipt', 'delivery_sha256',
                                'delivery_at', 'revision'])
        return row


def record_movie_import(**kwargs):
    """Compatibility entry point: retain the movie-only validation boundary."""
    receipt = kwargs['receipt']
    registry.require(media.kind(receipt['selection']['source_identity']) == 'movie')
    return record_import(**kwargs)


def status_for(transfer):
    row = ObtainCompletionStatus.objects.filter(transfer=transfer).first() if transfer else None
    if row is None:
        return None
    from shelf import apple_tv_completion
    identity = row.completion['source_identity']
    kind = media.SUPPORTED.get(tuple(identity.get(key) for key in ('provider', 'medium', 'scope')))
    return {'apple_tv_status': apple_tv_completion.status_for(transfer), 'kind': kind, 'observed_at': row.observed_at, 'acquired_at': row.completion['completion_evidence']['acquired_at'],
            'delivery_state': row.delivery_state, 'delivery_at': row.delivery_at,
            'file_verified_at': max(item['verified_at'] for item in row.completion['files']),
            'scan_attempts': row.delivery_receipt.get('scan_attempts'),
            'readiness': row.delivery_receipt.get('readiness'),
            'supported_consumer': kind is not None,
            'album_identity': row.completion.get('album_identity'),
            'library_metadata': row.delivery_receipt.get('library'),
            'artwork_present': row.delivery_receipt.get('library', {}).get('artwork_present'),
            'current_availability': 'unknown'}
