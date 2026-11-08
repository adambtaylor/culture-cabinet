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


def selected_job(context, *, services):
    """One native acknowledged receipt, never legacy discovery or actor1."""
    from shelf.obtain_native_context import NativeJobContext, operational_digest
    from shelf.obtain_tv_flow import (SelectedTVServices, SelectedJobBinding, ServiceGeneration,
                                     read_delivery_binding, delivery_binding_material)
    from shelf.disclosure import canonical_bytes
    import hashlib
    if type(context) is not NativeJobContext or context.operation not in ('observe', 'import', 'verify_library'):
        raise Blocked('Current selected native effect context required')
    context.revalidate()
    access = context._access
    job = acquire.owned_job(access.actor, context.profile_id, context.job_id)
    transfer = ObtainTransfer.objects.get(job=job)
    if (job.publication_origin is None or transfer.state != 'queued' or not transfer.provider_job
            or transfer.local_cancelled_at is not None or job.status != 'blocked_configuration'):
        raise Blocked('Acknowledged current source TV request required')
    binding = acquire.source_receipt(transfer)
    if 'delivery_binding' not in binding or not binding.get('complete_root') or 'completion_intent' not in binding:
        raise Blocked('Historical request has no confirmed delivery mapping')
    policy = operator_policy()
    if (binding['completion_intent'] != policy.intent(str(job.request_id))
            or binding['complete_root'] != policy.complete_root or binding['test_only'] != policy.test_only):
        raise Blocked('Confirmed completion policy changed')
    delivery = read_delivery_binding(binding['delivery_binding'])
    progress = transfer.native_progress
    from shelf.obtain_tv_flow import validate_native_progress
    validate_native_progress(progress, binding=binding, job_id=job.pk,
                             verification=transfer.library_verification, provider_job=transfer.provider_job, scope=job.scope)
    if (type(progress) is not dict or progress.get('schema') != 'obtain-tv-progress/v1'
            or type(progress.get('service_generations')) is not list):
        raise Blocked('Historical request has no frozen service binding')
    try:
        generations = tuple(ServiceGeneration(**value) for value in progress['service_generations'])
    except (TypeError, ValueError):
        raise Blocked('Retained service binding malformed') from None
    if (type(services) is not SelectedTVServices or services.generations(context) != generations
            or delivery_binding_material(services.delivery) != binding['delivery_binding']):
        raise Blocked('Confirmed destination or service generation changed')
    digest = lambda value: hashlib.sha256(canonical_bytes(value)).hexdigest()
    result = SelectedJobBinding(str(job.request_id), job.pk, job.actor_id, job.profile_id,
        digest(job.publication_origin), operational_digest(binding), binding['selection_digest'],
        digest(binding), job.scope, binding['exact']['result_id'], binding['exact']['guid'],
        binding['exact']['digest'], transfer.nzb_sha256, transfer.provider_job,
        digest(binding.get('completion_intent')), binding['complete_root'], delivery.destination.ref,
        binding['test_only'], binding['content_identity_digest'], generations)
    context.revalidate()
    return result


def _native_cas(context, progress, *, verification=None, failed=False, series_evidence=None):
    from django.db.models import F
    from shelf.portable_saved_authority import write_for
    from shelf.obtain_native_context import native_job_context
    from shelf.disclosure import canonical_bytes
    context.revalidate()
    from shelf.obtain_tv_flow import validate_native_progress
    transfer = ObtainTransfer.objects.select_related('job').get(pk=context.job_id)
    original = transfer.native_progress.get('series_evidence')
    introduced = progress.get('series_evidence')
    if original is not None:
        if introduced != original:
            raise Blocked('The first validated Series receipt is immutable')
    elif introduced is not None:
        from shelf.jellyfin_native import ValidatedSeriesBinding, series_evidence_material
        if type(series_evidence) is not ValidatedSeriesBinding or series_evidence_material(series_evidence) != introduced:
            raise Blocked('Only a typed resolver result may introduce Series evidence')
    validate_native_progress(progress, binding=transfer.binding, job_id=context.job_id,
                             verification=verification, provider_job=transfer.provider_job, scope=transfer.job.scope)
    if len(canonical_bytes(progress)) > 65536:
        raise Blocked('Bounded selected progress required')
    access = context._access
    with write_for(access.actor, context.profile_id, durable=True):
        values = {'native_progress': progress, 'library_verification': verification, 'revision': F('revision') + 1}
        if failed:
            values.update(state='failed', reason='provider_failed')
        count = ObtainTransfer.objects.filter(pk=context.job_id, revision=context.transfer_revision,
            state='queued', local_cancelled_at__isnull=True).update(**values)
        if count != 1:
            raise Blocked('Selected progress changed; reload the current receipt')
    return native_job_context(access, job_id=context.job_id, operation='observe')


def _completion_material(completion):
    from dataclasses import asdict
    return {'files': [asdict(value) for value in completion.files],
        'source_manifest_digest': completion.source_manifest_digest, 'history_digest': completion.history_digest,
        'hook_digest': completion.hook_digest, 'repair': completion.repair, 'unpack': completion.unpack,
        'observed_at': completion.observed_at.isoformat()}


def _original_completion(progress, fresh):
    """Freshly verify the bytes, preserving the original copy chronology.

    Re-observation cannot pretend the original destination copy happened later.
    Only unchanged independently verified source files may reuse that receipt.
    """
    from datetime import datetime
    from shelf.obtain_tv_flow import SelectedCompletion, VerifiedSourceFile
    old = progress.get('completion')
    if old is None:
        return fresh
    try:
        original = SelectedCompletion(fresh.job, 'completed', '', old['source_manifest_digest'],
            tuple(VerifiedSourceFile(**value) for value in old['files']), old['repair'], old['unpack'],
            old['history_digest'], old['hook_digest'], datetime.fromisoformat(old['observed_at']))
        if (original.files != fresh.files or original.source_manifest_digest != fresh.source_manifest_digest
                or original.history_digest != fresh.history_digest or original.hook_digest != fresh.hook_digest
                or (original.repair, original.unpack) != (fresh.repair, fresh.unpack)
                or original.observed_at.utcoffset() is None or original.observed_at > fresh.observed_at):
            raise ValueError
        return original
    except (KeyError, TypeError, ValueError):
        raise Blocked('Verified source manifest changed; retained copy must be reconciled') from None


def _copy_plan(context, delivery, completion, intent, edition, ports):
    from pathlib import PurePosixPath
    from shelf.jellyfin_tv_import import CopyPlan, destination_binding_digest
    from shelf.obtain_tv_flow import require_confirmed_delivery_plan
    from shelf.obtain_parser import plain
    if completion.state != 'completed' or len(completion.files) != 1:
        raise Blocked('One verified selected video required')
    source = completion.files[0]; suffix = PurePosixPath(source.relative_path).suffix.lower()
    if suffix not in ('.mkv', '.mp4', '.webm') or '/' in edition or '\\' in edition:
        raise Blocked('Independent selected filename unavailable')
    name = plain(edition) + suffix
    relative = (delivery.relative_parent + '/' if delivery.relative_parent else '') + name
    end = min(context.authority_deadline(), ports.authority_deadline())
    job = completion.job; destination = delivery.destination
    plan = CopyPlan(job.request_id, job.job_id, job.nzo_id, job.binding_digest, job.content_identity_digest,
        completion.source_manifest_digest, destination_binding_digest(destination, relative, delivery.create_parent),
        intent, destination.host, destination.root, relative, source.size, source.sha256,
        job.episode_scope, end.isoformat(), job.test_only, delivery.create_parent)
    require_confirmed_delivery_plan(delivery, plan)
    plan.validate((destination.root,))
    return plan


def _services_again(context, expected_job, delivery):
    from django.conf import settings
    from shelf.obtain_tv_flow import TVServiceProvider, SelectedTVServices, delivery_binding_material
    provider = getattr(settings, 'PORTABLE_OBTAIN_TV_SERVICES', None)
    if not isinstance(provider, TVServiceProvider):
        raise Blocked('Current native TV factory unavailable')
    current = provider.current(context)
    if (type(current) is not SelectedTVServices or current.generations(context) != expected_job.service_generations
            or delivery_binding_material(current.delivery) != delivery_binding_material(delivery)):
        raise Blocked('Confirmed selected service binding changed')
    worker = getattr(context._access.authority, '_obtain_workers', {}).get(context.job_id)
    if worker is not None and not worker.done.is_set() and worker.session_ref == context._access.session_ref:
        worker.stop_children = current.stop_owned_children
    return current


def advance_selected(context, *, expected_revision, services):
    """Claim each exact effect before calling O1, then publish with fresh CAS."""
    from dataclasses import asdict, replace
    from uuid import uuid4
    from shelf.obtain_native_context import NativeJobContext, native_job_context
    from shelf.obtain_tv_flow import SelectedTVServices, delivery_binding_material
    from shelf import sab_selected, jellyfin_tv_import, jellyfin_native, obtain_library_verification
    if (type(context) is not NativeJobContext or context.operation != 'observe'
            or type(expected_revision) is not int or expected_revision != context.transfer_revision
            or type(services) is not SelectedTVServices):
        raise Blocked('Exact current native continuation revision required')
    context.revalidate(); access = context._access
    transfer = ObtainTransfer.objects.get(pk=context.job_id)
    job = selected_job(context, services=services)
    # This is an explicit continuation, never a GET/replay side effect. Repair
    # only its already acknowledged private hook registration; no enqueue.
    acquire.repair_registration(transfer)
    context = native_job_context(access, job_id=job.job_id, operation='observe')
    progress = dict(transfer.native_progress)
    progress.update(stage='observing', reason='', intent={
        'id': str(uuid4()), 'stage': 'observe', 'revision': context.transfer_revision + 1})
    context = _native_cas(context, progress)
    services = _services_again(context, job, services.delivery)
    completion = sab_selected.observe_selected(context, job, ports=services.observer)
    if completion.state != 'completed':
        progress.update(stage='download_failed' if completion.state == 'failed' else 'awaiting_completion',
                        reason=completion.reason, observed_at=completion.observed_at.isoformat())
        return _native_cas(context, progress, failed=completion.state == 'failed').transfer_revision
    completion = _original_completion(progress, completion)
    progress.update(stage='copying', reason='', completion=_completion_material(completion),
        copy_intent=progress.get('copy_intent') or str(uuid4()), intent={
            'id': str(uuid4()), 'stage': 'import', 'revision': context.transfer_revision + 1})
    _native_cas(context, progress)
    context = native_job_context(access, job_id=job.job_id, operation='import')
    services = _services_again(context, job, services.delivery)
    raw_ports = services.destination; delivery = services.delivery
    def deadline():
        return min(context.authority_deadline(), raw_ports.authority_deadline())
    def plan_for(current_completion, current_destination, intent):
        if current_destination != delivery.destination:
            raise Blocked('Copy destination differs from confirmed mapping')
        current = acquire.owned_job(access.actor, context.profile_id, context.job_id)
        return _copy_plan(context, delivery, current_completion, intent, current.edition, raw_ports)
    ports = replace(raw_ports, plan_for=plan_for, authority_deadline=deadline)
    outcome = jellyfin_tv_import.import_selected(context, completion, delivery.destination,
        intent=progress['copy_intent'], ports=ports)
    if outcome.status != 'verified':
        progress.update(stage='copy_' + outcome.status, reason=outcome.reason)
        return _native_cas(context, progress).transfer_revision
    destination = outcome.receipt
    progress.update(stage='verifying_library', reason='', destination=asdict(destination), intent={
        'id': str(uuid4()), 'stage': 'verify_library', 'revision': context.transfer_revision + 1})
    _native_cas(context, progress)
    context = native_job_context(access, job_id=job.job_id, operation='verify_library')
    services = _services_again(context, job, services.delivery)
    # Decoded history selects one exact Series for a fresh authenticated read;
    # it cannot supply identity authority or renew its original observation.
    evidence = (jellyfin_native.read_series_evidence(progress['series_evidence'])
                if 'series_evidence' in progress else None)
    if evidence is not None and evidence.binding != services.library:
        raise Blocked('Validated Series belongs to a different library binding')
    with context.effect_window():
        target = services.target_for(context, job, destination, evidence)
        if (type(target) is not jellyfin_native.EpisodeResolutionTarget
                or (target.request_id, target.binding_digest, target.content_identity_digest,
                    target.destination_binding_digest, target.scope) !=
                   (job.request_id, job.binding_digest, job.content_identity_digest,
                    destination.destination_binding_digest, job.episode_scope)):
            raise Blocked('Current permitted library target required')
        if ((evidence is not None and (target.show_id, target.show_name, target.series_id) !=
                (None, None, evidence.series_id)) or (evidence is None and target.series_id is not None)):
            raise Blocked('Series continuation requires exact typed evidence, without a search fallback')
        target = replace(target, facts_valid_until=min(target.facts_valid_until, context.authority_deadline()))
        def current(binding, selected_target):
            if (services.generations(context) != job.service_generations or binding != services.library
                    or selected_target != target):
                raise Blocked('Current library/native binding changed')
            renewed = services.target_for(context, job, destination, evidence)
            renewed = replace(renewed, facts_valid_until=min(renewed.facts_valid_until, context.authority_deadline()))
            if renewed != target:
                raise Blocked('Permitted library identity changed')
            return binding, selected_target
        resolution = jellyfin_native.resolve_episode(services.library, target,
            credential_scope=services.library_credential_scope, revalidate=current, clock=services.library_clock)
    observed_series = resolution.series_evidence
    if observed_series is not None:
        if (type(observed_series) is not jellyfin_native.ValidatedSeriesBinding
                or (observed_series.request_id, observed_series.job_binding_digest,
                    observed_series.content_identity_digest, observed_series.destination_binding_digest,
                    observed_series.scope, observed_series.binding, observed_series.target_digest) !=
                   (job.request_id, job.binding_digest, job.content_identity_digest,
                    destination.destination_binding_digest, job.episode_scope, services.library,
                    jellyfin_native._digest(target.material()))
                or resolution.status not in ('resolved', 'not_indexed')
                or (evidence is not None and observed_series.series_id != evidence.series_id)):
            raise Blocked('Validated Series result changed its selected binding')
        # Later reads prove currentness for this request. Keep the bootstrap
        # receipt byte-for-byte so recovery never restamps expired source facts.
        if evidence is None:
            progress['series_evidence'] = jellyfin_native.series_evidence_material(observed_series)
    if resolution.status != 'resolved':
        progress.update(stage='awaiting_library_index', reason=resolution.reason,
                        series_ref=resolution.series_id or progress.get('series_ref'))
        return _native_cas(context, progress, series_evidence=observed_series).transfer_revision
    def revalidate_services():
        actual = services.generations(context)
        if actual != job.service_generations:
            raise Blocked('Current verification services changed')
        return tuple(value for value in actual if value.service in ('jellyfin', 'destination_file')), context.authority_deadline()
    verified = obtain_library_verification.verify_library_match(context, completion=completion,
        destination=destination, resolution=resolution, observation=resolution.observation,
        revalidate_services=revalidate_services, clock=services.library_clock.now)
    if verified.status != 'verified':
        progress.update(stage='verification_' + verified.status, reason=verified.reason,
                        series_ref=resolution.series_id)
        return _native_cas(context, progress, series_evidence=observed_series).transfer_revision
    proof = verified.verification
    wire = {**asdict(proof), 'observed_at': proof.observed_at.isoformat(), 'valid_until': proof.valid_until.isoformat()}
    progress.update(stage='verified', reason='', series_ref=resolution.series_id, observed_at=proof.observed_at.isoformat())
    fresh = _native_cas(context, progress, verification=wire, series_evidence=observed_series)
    _remember_verified(fresh, proof, wire)
    return fresh.transfer_revision


def _remember_verified(context, proof, wire):
    from shelf.obtain_tv_flow import LibraryVerification
    from shelf.obtain_native_context import operational_digest
    if type(proof) is not LibraryVerification:
        raise Blocked('Trusted typed verification result required')
    context.revalidate(); authority = context._access.authority
    if not hasattr(authority, '_obtain_verified'):
        authority._obtain_verified = {}
        authority.boundary._closing.append(lambda: authority._obtain_verified.clear())
        def revoke(ref):
            for key, value in list(authority._obtain_verified.items()):
                if value[0] == ref:
                    authority._obtain_verified.pop(key, None)
        authority._revokers.append(revoke)
    authority._obtain_verified = {key: value for key, value in authority._obtain_verified.items()
                                 if authority.now() < value[-1].valid_until}
    # JSON is history, not a live verification issuer. After process loss or a
    # selection/revision change, only a fresh O1 verification can restore green.
    authority._obtain_verified[context.job_id] = (context._access.session_ref,
        operational_digest(context._access._signed), context.transfer_revision,
        context.binding_digest, operational_digest(wire), proof)


def current_verification(access, transfer):
    from shelf.obtain_native_context import operational_digest
    value = getattr(access.authority, '_obtain_verified', {}).get(transfer.pk)
    if value is None:
        return None
    session_ref, selection, revision, binding, wire_digest, proof = value
    if (session_ref != access.session_ref or selection != operational_digest(access._signed)
            or revision != transfer.revision or binding != operational_digest(transfer.binding)
            or wire_digest != operational_digest(transfer.library_verification)
            or access.authority.now() >= proof.valid_until):
        access.authority._obtain_verified.pop(transfer.pk, None)
        return None
    return proof


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
