"""Bounded, read-only discovery for the existing owned media consumers.

Pages advance by scanned job ID, including invalid rows. Restart at after=0 on
the next bounded cycle when has_more is false. A recorded completed state is
historical: callers must use export_completed_job.py for fresh final SAB/file
verification, then the existing consumer and record_kodi_completion.py. No
observer, registry publication, provider call or import occurs during listing.
"""
import re
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.http import Http404

from shelf import completion_registry as registry, completion_status, obtain_acquisition as acquire
from shelf.acquisition_contract import Blocked
from shelf import completion_media as media
from shelf.models import ObtainCompletionStatus, ObtainTransfer
from shelf.obtain_runtime import operator_policy

MAX_LIMIT = 32
MAX_PAGE_BYTES = 512 * 1024


def _hash(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _require(actor, profile_id, context, session_guard):
    if type(context) is not acquire.OperatorContext or actor.pk != 1 or type(profile_id) is not int or profile_id != 1:
        raise Blocked('Existing owner completion operator required')
    context.require(actor, profile_id)
    if context.purpose != 'offline_test' and not callable(session_guard):
        raise Blocked('Current bounded session required')
    if session_guard is not None:
        session_guard()
    policy = operator_policy()
    if context.purpose != 'offline_test' and policy.test_only is not False:
        raise Blocked('Synthetic completion discovery is not live')
    return policy


def _recorded(transfer, entry, job):
    row = ObtainCompletionStatus.objects.filter(transfer=transfer).first()
    if row is None:
        return None, 'not_reported', None
    # Reuse the producer's binding/hash grammar. observed_at is the recorded
    # observation time, not an invented current file verification timestamp.
    normalized = completion_status._completion(
        {**row.completion, 'checked_at': row.observed_at.isoformat()}, entry, job)
    registry.require(normalized == row.completion and _hash(row.completion_sha256)
        and row.completion_sha256 == normalized['completion_evidence_sha256'])
    state, receipt = row.delivery_state, row.delivery_receipt
    registry.require(state in ('not_reported', 'blocked', 'unknown', 'imported'))
    if state == 'not_reported':
        registry.require(row.delivery_sha256 == '' and receipt == {})
        return row.completion_sha256, state, None
    registry.require(_hash(row.delivery_sha256) and type(receipt) is dict
        and receipt['status'] == state and receipt['request_id'] == entry['request_id']
        and receipt['selection'] == media.selection(row.completion, entry)
        and receipt['completion_evidence_sha256'] == row.completion_sha256
        and receipt['playback_started'] is False)
    return row.completion_sha256, state, row.delivery_sha256


def _job(actor, profile_id, transfer, context):
    entry = acquire.completion_registration(actor=actor, profile_id=profile_id,
        job_id=transfer.pk, context=context, include_failed=True)
    registry.nzo_id(entry['nzo_id'])
    for key in ('request_id', 'result_id'):
        registry.require(type(entry[key]) is str and str(UUID(entry[key])) == entry[key])
    registry.require(all(_hash(entry[key]) for key in ('result_digest', 'nzb_sha256')))
    # A provider job claimed by two requests is ambiguous even across pages or
    # profiles. Return no foreign identifiers, and never dispatch either claim.
    registry.require(not ObtainTransfer.objects.filter(provider_job=entry['nzo_id']).exclude(pk=transfer.pk).exists())
    job = acquire.owned_job(actor, profile_id, transfer.pk)
    identity = media.source_identity(job)
    album = media.album_identity(job) if media.kind(identity) == 'album' else None
    completion, delivery, delivery_hash = _recorded(transfer, entry, job)
    current = ObtainTransfer.objects.get(pk=transfer.pk)
    registry.require(acquire.completion_registration(actor=actor, profile_id=profile_id,
        job_id=transfer.pk, context=context, include_failed=True) == entry)
    failed = current.state == 'failed'
    state = 'failed' if failed else ('completed' if completion else 'pending')
    return {'job_id': job.pk,
        **{key: entry[key] for key in ('request_id', 'nzo_id', 'result_id', 'result_digest', 'nzb_sha256')},
        'source_identity': identity, **({'album_identity': album} if album is not None else {}),
        'state': state,
        'reason': 'provider_failed' if failed else ('recorded_final_history' if completion else 'final_history_not_observed'),
        'dispatchable': not failed and delivery != 'imported',
        'completion_evidence_sha256': completion, 'delivery_state': delivery,
        'delivery_sha256': delivery_hash, 'test_only': entry['test_only']}


def list_jobs(*, actor, profile_id, context, after=0, limit=16, session_guard=None):
    """List actual acknowledged supported media jobs; this function performs no writes.

Unknown binding rows have only job_id/state/reason/dispatchable. Delivery
blocked/unknown remain dispatchable for the existing consumer's durable
reconciliation; a caller must not manufacture or clear its private scan receipt.
"""
    if type(after) is not int or not 0 <= after < 2**63 or type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise Blocked('Bounded integer page required')
    policy = _require(actor, profile_id, context, session_guard)
    query = ObtainTransfer.objects.filter(job__actor=actor, job__profile_id=profile_id,
        job__status='blocked_configuration', local_cancelled_at__isnull=True, job_id__gt=after
    ).filter(
        Q(job__provider='tmdb', job__medium='movie', job__scope='film')
        | Q(job__provider='tmdb', job__medium='tv')
        | Q(job__provider='album_request', job__medium='music', job__scope='album_flac')
    ).exclude(provider_job='').filter(Q(state='queued') | Q(state='failed', reason='provider_failed')).order_by('job_id')
    transfers = list(query[:limit + 1])
    jobs = []
    for transfer in transfers[:limit]:
        registry.require(_require(actor, profile_id, context, session_guard) == policy)
        try:
            item = _job(actor, profile_id, transfer, context)
        except (ValueError, TypeError, KeyError, AttributeError, OSError, ObjectDoesNotExist, Http404, RecursionError):
            item = {'job_id': transfer.pk, 'state': 'unknown', 'reason': 'binding_unavailable', 'dispatchable': False}
        jobs.append(item)
    # Revoke the entire response on a changed session, policy or owner grant;
    # never leak an earlier partial page after authorization disappears.
    registry.require(_require(actor, profile_id, context, session_guard) == policy)
    page = {'version': 2, 'state': 'listed', 'actor_id': actor.pk, 'profile_id': profile_id,
        'scope': 'supported_media', 'after': after, 'limit': limit,
        'next_cursor': transfers[min(limit, len(transfers)) - 1].pk if transfers else after,
        'has_more': len(transfers) > limit, 'jobs': jobs}
    registry.require(len(registry.encode_json(page)) <= MAX_PAGE_BYTES)
    return page
