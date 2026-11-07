"""Independent Apple TV episode discovery and private exact-bound observations.

Only the bounded SSH operator writes this store. Kodi delivery is deliberately
irrelevant; no browser assertion, manual import or filename creates authority.
"""
from contextlib import contextmanager
from copy import deepcopy
from shelf.episode_scope import parse_scope, episode_code, is_episode_scope
from datetime import datetime
import fcntl
import math
import os
from pathlib import Path
import time
from uuid import uuid4

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from shelf import completion_dispatch as dispatch, completion_media as media
from shelf import completion_registry as registry, completion_status as completion
from shelf import obtain_acquisition as acquire
from shelf.models import ObtainCompletionStatus, ObtainTransfer

ROOT = Path('/home/tang/Projects/culture-cabinet/.acquisition-control/apple-tv-receipts')
WATCHFOLDER = '/Users/adamtaylor/Movies/TV/Media.localized/Automatically Add to TV.localized'
LIBRARY_ROOT = '/Users/adamtaylor/Movies/TV/Media.localized/TV Shows'
STATES = ('blocked', 'remux_ready', 'handoff_pending', 'unknown', 'library_import_observed')


def contract(handoff, entry, job):
    normalized = completion._completion(handoff, entry, job)
    identity = media.source_identity(job)
    registry.require(media.kind(identity) == 'episode')
    season, episode = parse_scope(job.scope)
    registry.require(len(normalized['files']) == 1)
    item = normalized['files'][0]
    registry.require(item['observed_media'] == 'video' and item['selected_scope'] == job.scope
        and item['playback_started'] is False and item['content_identity'] == 'unverified')
    result = {'version': 1, 'destination': 'apple_tv', 'completion': deepcopy(handoff),
        'selection': media.selection(normalized, entry),
        'episode': {'show': identity['title'], 'season': season, 'episode': episode, 'title': episode_code(job.scope),
                    'source_id': identity['source_id'], 'basis': 'selected_job_scope'},
        'source': {'relative_path': item['relative_path'], 'sha256': item['sha256'], 'bytes': item['size']}}
    result['contract_sha256'] = completion.digest(result)
    return result


def _current(actor, profile_id, job_id, context, session_guard):
    dispatch._require(actor, profile_id, context, session_guard)
    return _bound_completion(actor, profile_id, job_id, context)


def _bound_completion(actor, profile_id, job_id, context):
    entry = completion._authority(actor, profile_id, job_id, context)
    job = acquire.owned_job(actor, profile_id, job_id)
    transfer = ObtainTransfer.objects.get(job_id=job_id)
    registry.require(not ObtainTransfer.objects.filter(provider_job=entry['nzo_id']).exclude(pk=transfer.pk).exists())
    row = ObtainCompletionStatus.objects.get(transfer=transfer)
    # Bind the original recorded check, after fresh export independently verifies
    # the identical immutable completion. Refreshing a timestamp cannot rekey it.
    handoff = {**row.completion, 'checked_at': row.observed_at.isoformat()}
    value = contract(handoff, entry, job)
    registry.require(row.completion_sha256 == value['selection']['completion_evidence_sha256'])
    return entry, row, value


@contextmanager
def _store(*, create=False):
    # The provisioned control parent must already be private. Open every parent
    # without following links; mkdir is relative to its pinned descriptor.
    with registry.directory(str(ROOT.parent), trusted=True) as parent:
        registry.private(os.fstat(parent), 0o700)
        if create:
            try:
                os.mkdir(ROOT.name, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
        fd = os.open(ROOT.name, registry.DIR_FLAGS, dir_fd=parent)
        try:
            registry.private(os.fstat(fd), 0o700)
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.01)
            yield fd
        finally:
            os.close(fd)


def _read(fd, request):
    try:
        return registry.read_json(fd, request + '.json', single_link=True)
    except FileNotFoundError:
        return None


def validate_receipt(receipt, value):
    registry.require(type(receipt) is dict and len(registry.encode_json(receipt)) <= registry.MAX_JSON)
    registry.require(type(receipt.get('version')) is int and receipt['version'] == 1
        and receipt['destination'] == 'apple_tv' and receipt['contract_sha256'] == value['contract_sha256']
        and receipt['request_id'] == value['selection']['provenance']['request_id']
        and receipt['source_sha256'] == value['source']['sha256']
        and type(receipt['source_bytes']) is int and receipt['source_bytes'] == value['source']['bytes']
        and receipt['episode'] == value['episode'] and receipt['playback_started'] is False
        and type(receipt['import_attempts']) is int and receipt['import_attempts'] in (0, 1)
        and receipt['status'] in STATES)
    created, checked = (datetime.fromisoformat(receipt[key]) for key in ('created_at', 'checked_at'))
    acquired = datetime.fromisoformat(value['completion']['completion_evidence']['acquired_at'])
    registry.require(created.utcoffset() is not None and checked.utcoffset() is not None
        and acquired <= created <= checked <= timezone.now())
    reason = receipt['reason']
    registry.require(reason is None or (type(reason) is str and 0 < len(reason) <= 160
        and all(ord(char) >= 32 for char in reason)))
    derivative, native, handoff, library = (receipt[key] for key in ('derivative', 'native', 'handoff', 'library'))
    episode = value['episode']
    code = 'S%02dE%02d' % (episode['season'], episode['episode'])
    expected_title = episode['show'] + ' - ' + code
    if episode['title'] != code:
        expected_title += ' - ' + episode['title']
    if derivative is not None:
        registry.require(type(derivative) is dict and type(derivative.get('version')) is int
            and derivative['version'] == 1 and dispatch._hash(derivative['sha256'])
            and type(derivative['bytes']) is int and 0 < derivative['bytes'] <= 1024**4
            and derivative['contract_sha256'] == value['contract_sha256']
            and derivative['source_sha256'] == value['source']['sha256']
            and derivative['remote_path'] == '/home/tang/Projects/culture-cabinet/.apple-tv-work/'
                + value['contract_sha256'] + '/episode.mp4')
        proof = derivative['validation']
        registry.require(type(proof) is dict and proof['valid'] is True
            and dispatch._hash(proof['plan_sha256'])
            and proof['metadata'] == {'title': expected_title, 'show': episode['show'],
                'season_number': str(episode['season']), 'episode_sort': str(episode['episode']),
                'episode_id': code, 'media_type': '10'}
            and type(proof['duration_seconds']) in (int, float)
            and math.isfinite(proof['duration_seconds']) and 0 < proof['duration_seconds'] <= 7*86400
            and proof['hdr_display_verified'] is False)
        for label in ('video', 'audio'):
            packets = proof['packet_integrity'][label]
            registry.require(type(packets['source_packets']) is int and packets['source_packets'] > 0
                and type(packets['derivative_packets']) is int
                and packets['derivative_packets'] == packets['source_packets']
                and packets['identical_packet_payload_hashes'] is True)
    if native is not None:
        registry.require(derivative is not None and type(native) is dict and native['is_playable'] is True
            and native['playback_started'] is False and type(native['duration_seconds']) in (int, float)
            and math.isfinite(native['duration_seconds']) and 0 < native['duration_seconds'] <= 7*86400
            and type(derivative['validation']['duration_seconds']) in (int, float)
            and math.isfinite(derivative['validation']['duration_seconds'])
            and abs(native['duration_seconds'] - derivative['validation']['duration_seconds']) <= 1)
        tracks = native['tracks']
        registry.require(type(tracks) is list and 2 <= len(tracks) <= 32)
        codecs = {}
        for track in tracks:
            registry.require(type(track) is dict and type(track['formats']) is list and bool(track['formats']))
            registry.require(all(type(item) is dict and type(item['codec']) is str for item in track['formats']))
            codecs.setdefault(track['type'], set()).update(item['codec'] for item in track['formats'])
        entry = derivative['validation']['atoms']['video_sample_entry']
        registry.require(entry in ('hvc1', 'avc1') and codecs.get('vide') == {entry}
            and bool(codecs.get('soun')) and codecs['soun'] <= {'ec-3', 'ac-3', 'mp4a'})
        audio = [track for track in tracks if track['type'] == 'soun']
        audio_entry = derivative['validation']['atoms']['audio_sample_entry']
        registry.require(len(audio) == 1 and audio[0]['language'] in ('eng', 'en')
            and audio_entry in ('ec-3', 'ac-3', 'mp4a') and codecs['soun'] == {audio_entry})
        if derivative['validation'].get('atmos') is True:
            registry.require(codecs['soun'] == {'ec-3'})
    if receipt['status'] in ('handoff_pending', 'unknown'):
        registry.require(receipt['import_attempts'] == 1 and type(handoff) is dict
            and derivative is not None and native is not None)
    if handoff is not None:
        registry.require(type(handoff) is dict and derivative is not None and native is not None
            and receipt['import_attempts'] == 1)
        expected = WATCHFOLDER + '/CC-' + receipt['request_id']
        registry.require(handoff['path'] == expected + '-' + code + '-' + derivative['sha256'] + '.mp4')
        reserved = datetime.fromisoformat(handoff['reserved_at'])
        registry.require(reserved.utcoffset() is not None and created <= reserved <= checked)
        if handoff['delivered_at'] is not None:
            delivered = datetime.fromisoformat(handoff['delivered_at'])
            registry.require(delivered.utcoffset() is not None and reserved <= delivered <= checked)
    else:
        registry.require(receipt['import_attempts'] == 0)
    if receipt['status'] == 'library_import_observed':
        registry.require(derivative is not None and native is not None and type(library) is dict
            and library['sha256'] == derivative['sha256']
            and type(library['bytes']) is int and library['bytes'] == derivative['bytes']
            and library['metadata'] == {**{key: episode[key] for key in ('show', 'season', 'episode')},
                                       'title': expected_title} and library['native'] == native)
        registry.path_parts(library['path'], absolute=True)
        registry.require(library['path'].startswith(LIBRARY_ROOT + '/'))
    else:
        registry.require(library is None)
    if receipt['status'] == 'remux_ready':
        registry.require(derivative is not None)
    return deepcopy(receipt)


def record_receipt(*, actor, profile_id, job_id, context, receipt, session_guard=None):
    entry, _, value = _current(actor, profile_id, job_id, context, session_guard)
    evidence = validate_receipt(receipt, value)
    with transaction.atomic():
        completion._lock(actor, profile_id, job_id, context, entry)
        with _store(create=True) as fd:
            previous = _read(fd, entry['request_id'])
            if previous is not None:
                validate_receipt(previous, value)
                if previous == evidence:
                    registry.require(_current(actor, profile_id, job_id, context, session_guard)[2] == value)
                    return evidence
                allowed = {'blocked': {'blocked', 'remux_ready', 'handoff_pending', 'unknown', 'library_import_observed'},
                    'remux_ready': {'blocked', 'handoff_pending', 'unknown', 'library_import_observed'},
                    'handoff_pending': {'unknown', 'library_import_observed'},
                    'unknown': {'library_import_observed'}, 'library_import_observed': set()}
                registry.require(evidence['status'] in allowed[previous['status']]
                    and evidence['import_attempts'] >= previous['import_attempts']
                    and evidence['created_at'] == previous['created_at']
                    and datetime.fromisoformat(evidence['checked_at']) >= datetime.fromisoformat(previous['checked_at']))
                if previous['derivative'] is not None:
                    registry.require(evidence['derivative'] == previous['derivative'])
                if previous['handoff'] is not None:
                    registry.require(evidence['handoff'] is not None
                        and all(evidence['handoff'][key] == previous['handoff'][key] for key in ('path', 'reserved_at'))
                        and (previous['handoff']['delivered_at'] is None
                            or evidence['handoff']['delivered_at'] == previous['handoff']['delivered_at']))
            registry.require(_current(actor, profile_id, job_id, context, session_guard)[2] == value)
            temporary = '.receipt-' + uuid4().hex
            target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(target, 'wb') as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(registry.encode_json(evidence))
                    output.flush()
                    os.fsync(output.fileno())
                registry.require(_current(actor, profile_id, job_id, context, session_guard)[2] == value)
                os.replace(temporary, entry['request_id'] + '.json', src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
    return evidence


def export_job(*, actor, profile_id, job_id, context, session_guard=None):
    from shelf import completion_observer
    dispatch._require(actor, profile_id, context, session_guard)
    # Only already-recorded verified episode jobs enter fresh source verification.
    entry, _, _ = _current(actor, profile_id, job_id, context, session_guard)
    observed = completion_observer.observe(actor=actor, profile_id=profile_id, job_id=job_id, context=context)
    registry.require(observed['state'] == 'completed')
    current_entry, row, value = _current(actor, profile_id, job_id, context, session_guard)
    registry.require(current_entry == entry and completion._completion(observed['handoff'], entry,
        acquire.owned_job(actor, profile_id, job_id)) == row.completion)
    return value


def status_for(transfer):
    """Fail closed on missing, malformed or stale private receipts; never infer import."""
    if transfer is None or (transfer.job.provider, transfer.job.medium) != ('tmdb', 'tv'):
        return None
    try:
        registry.require(is_episode_scope(transfer.job.scope))
        registry.require(transfer.state == 'queued' and transfer.local_cancelled_at is None
            and transfer.job.status == 'blocked_configuration'
            and transfer.job.actor_id == 1 and transfer.job.profile_id == 1)
        row = ObtainCompletionStatus.objects.get(transfer=transfer)
        # Rebuild the current selected-result binding and owner grant on reads
        # too. This context authorizes no write, session or receipt publication.
        context = acquire.OperatorContext(1, 1, 'candidate_operator', True)
        entry = acquire.completion_registration(actor=transfer.job.actor, profile_id=1,
            job_id=transfer.job_id, context=context)
        value = contract({**row.completion, 'checked_at': row.observed_at.isoformat()}, entry, transfer.job)
        with _store() as fd:
            receipt = _read(fd, str(transfer.job.request_id))
        if receipt is None:
            return {'state': 'not_reported', 'status': 'not_reported', 'reason': None, 'hdr_display': 'unverified'}
        validate_receipt(receipt, value)
        return {'state': receipt['status'], 'status': receipt['status'], 'reason': receipt['reason'],
                'checked_at': receipt['checked_at'],
                'import_attempts': receipt['import_attempts'], 'hdr_display': 'unverified'}
    except FileNotFoundError:
        return {'state': 'not_reported', 'status': 'not_reported', 'reason': None, 'hdr_display': 'unverified'}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ObjectDoesNotExist, Http404):
        return {'state': 'unavailable', 'status': 'unavailable', 'reason': 'current_binding_unavailable', 'hdr_display': 'unverified'}


def list_jobs(*, actor, profile_id, context, after=0, limit=16, session_guard=None):
    registry.require(type(after) is int and 0 <= after < 2**63
        and type(limit) is int and 1 <= limit <= dispatch.MAX_LIMIT)
    policy = dispatch._require(actor, profile_id, context, session_guard)
    # Query the episode ledger directly: Kodi delivery JSON/state is no input to
    # Apple TV admission, including a prior successful Kodi library import.
    transfers = list(ObtainTransfer.objects.filter(job__actor=actor, job__profile_id=profile_id,
        job__provider='tmdb', job__medium='tv',
        job__status='blocked_configuration', local_cancelled_at__isnull=True,
        job_id__gt=after, state='queued').exclude(provider_job='').order_by('job_id')[:limit+1])
    jobs = []
    for transfer in transfers[:limit]:
        registry.require(dispatch._require(actor, profile_id, context, session_guard) == policy)
        try:
            # Session/policy checks stay outside this row-local error boundary.
            entry, row, value = _bound_completion(actor, profile_id, transfer.pk, context)
        except ObtainCompletionStatus.DoesNotExist:
            continue
        except (ValueError, TypeError, KeyError, AttributeError, OSError,
                ObjectDoesNotExist, Http404, RecursionError):
            jobs.append({'job_id': transfer.pk, 'state': 'unknown',
                         'reason': 'binding_unavailable', 'dispatchable': False})
            continue
        state = status_for(row.transfer)['state']
        jobs.append({'job_id': transfer.pk, **{key: entry[key] for key in
            ('request_id', 'nzo_id', 'result_id', 'result_digest', 'nzb_sha256')},
            'source_identity': value['selection']['source_identity'], 'state': 'completed',
            'completion_evidence_sha256': row.completion_sha256,
            'test_only': entry['test_only'], 'dispatchable': state not in ('unavailable', 'library_import_observed'),
            'apple_tv_state': state, 'contract_sha256': value['contract_sha256']})
    registry.require(dispatch._require(actor, profile_id, context, session_guard) == policy)
    page = {'version': 2, 'scope': 'apple_tv_episodes', 'state': 'listed', 'actor_id': 1, 'profile_id': 1,
        'after': after, 'limit': limit, 'has_more': len(transfers) > limit,
        'next_cursor': transfers[min(limit, len(transfers))-1].pk if transfers else after, 'jobs': jobs}
    registry.require(len(registry.encode_json(page)) <= dispatch.MAX_PAGE_BYTES)
    return page
