"""Import only exact owned receipts from approved private control paths.

Status/library reads retry this local work. No credential, provider API, discovery
scan or per-file operator attestation is involved. Evidence remains point-in-time.
"""
from datetime import datetime
import fcntl
import os

from django.conf import settings
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from shelf import completion_registry as registry, obtain_acquisition as acquire
from shelf.acquisition_contract import Blocked, ContractError
from shelf.completion_files import completion_receipt, postprocess_evidence, verify_media
from shelf.models import PostprocessReceipt, PostprocessFile, ObtainTransfer


def receipt_path_policy(transfer):
    """Only server configuration can select control paths; HTTP supplies no path."""
    path = getattr(settings, 'OBTAIN_COMPLETION_RECEIPTS', None)
    registry.path_parts(path, absolute=True)
    intent = acquire.check_completion_intent(transfer.binding)
    root = transfer.binding['complete_root']
    registry.require(path != root and not path.startswith(root + '/'))
    return intent['registry_path'], path


def read_verified_receipt(registry_path, receipts_path, entry):
    parts = registry.path_parts(registry_path, absolute=True)
    with registry.directory('/' + '/'.join(parts[:-1]), trusted=True) as parent:
        registry.private(os.fstat(parent), 0o700)
        fcntl.flock(parent, fcntl.LOCK_SH | fcntl.LOCK_NB)
        registered = registry.registry_entry(
            registry.read_json(parent, parts[-1], single_link=True), entry['nzo_id'])
        registry.require(registered == entry)
        with registry.directory(receipts_path, trusted=True) as output:
            registry.private(os.fstat(output), 0o700)
            receipt = registry.read_json(output, entry['request_id'] + '.json')
        registry.require(type(receipt) is dict)
        files = receipt['files']
        # Reuse the strict registry manifest grammar, then require observed fields.
        registry.registry_entry({'version': 2, 'requests': [{**entry, 'files': files}]},
                                entry['nzo_id'])
        registry.require(all(set(item) == {'path', 'bytes', 'sha256'} for item in files))
        sab_bytes = receipt['sab_bytes']
        if sab_bytes is not None:
            registry.positive(sab_bytes, entry['max_bytes'])
        stamp = receipt['completion_observed_at']
        registry.require(type(stamp) is str and stamp.endswith('Z'))
        observed_at = datetime.fromisoformat(stamp)
        registry.require(observed_at.utcoffset().total_seconds() == 0
                         and observed_at <= timezone.now())
        expected = completion_receipt(postprocess_evidence(entry, files, sab_bytes), stamp)
        registry.require(registry.encode_json(receipt) == registry.encode_json(expected))
        registry.require(verify_media({**entry, 'files': files}) == files)
    return receipt


def available():
    tables = connection.introspection.table_names()
    return all(model._meta.db_table in tables for model in (PostprocessReceipt, PostprocessFile))


def consume(*, actor, profile_id, job_id):
    job = acquire.owned_job(actor, profile_id, job_id)
    if not available():
        return None
    transfer = ObtainTransfer.objects.filter(job=job).first()
    if transfer is None or transfer.state == 'local_verified':
        return None  # The stronger legacy path retains its own semantics.
    try:
        registry_path, receipts_path = receipt_path_policy(transfer)
        context = acquire.OperatorContext(actor.pk, profile_id, 'candidate_operator', True)
        entry = acquire.completion_registration(actor=actor, profile_id=profile_id,
                                               job_id=job_id, context=context)
        receipt = read_verified_receipt(registry_path, receipts_path, entry)
        registry.require(datetime.fromisoformat(receipt['verified_at']) >= transfer.created_at)
        if job.scope == 'album_flac':
            from shelf.album_acquisition import check_album_files
            check_album_files(job, receipt['files'])
        with transaction.atomic():
            # Take the transfer write lock. Identical concurrent verifiers may
            # converge; cancellation and changed bindings must still refuse.
            changed = ObtainTransfer.objects.filter(pk=transfer.pk, binding=transfer.binding,
                provider_job=transfer.provider_job,
                state='queued', local_cancelled_at__isnull=True).update(revision=F('revision') + 1)
            if changed != 1:
                raise Blocked('Completion changed during verification')
            context.require(actor, profile_id)
            current = acquire.completion_registration(actor=actor, profile_id=profile_id,
                                                     job_id=job_id, context=context)
            registry.require(current == entry)
            local, created = PostprocessReceipt.objects.get_or_create(transfer=transfer,
                                                                      defaults={'receipt': receipt})
            registry.require(local.receipt == receipt)
            if created:
                PostprocessFile.objects.bulk_create([PostprocessFile(receipt=local,
                    relative_path=item['relative_path'], sha256=item['sha256'], size=item['size'],
                    verified_at=datetime.fromisoformat(item['verified_at'])) for item in receipt['handoff']])
            else:
                actual = list(local.files.values_list('relative_path', 'sha256', 'size', 'verified_at'))
                expected = [(item['relative_path'], item['sha256'], item['size'],
                             datetime.fromisoformat(item['verified_at'])) for item in receipt['handoff']]
                registry.require(actual == expected)
            local.state, local.checked_at = 'postprocess_verified', timezone.now()
            local.save(update_fields=['state', 'checked_at'])
            return local
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        # Keep historical evidence/files, but never present them as current readiness.
        PostprocessReceipt.objects.filter(transfer=transfer).update(
            state='unavailable', checked_at=timezone.now())
        return None


def export(*, actor, profile_id, job_id):
    local = consume(actor=actor, profile_id=profile_id, job_id=job_id)
    if local is None:
        raise Blocked('Current postprocessing evidence unavailable')
    job = local.transfer.job
    authorization = {'scope': job.scope, 'edition': job.edition,
                     'authorized_content': job.authorized_content}
    if job.provider == 'album_request':
        from shelf.models import AlbumRequest
        album = AlbumRequest.objects.get(pk=job.source_id, request_id=job.request_id)
        authorization.update(album_identity=album.identity, user_authorization=album.authorization,
                             authorized_at=album.authorized_at.isoformat())
    return {'version': 1, 'state': 'POSTPROCESS_FILE_VERIFIED',
        'checked_at': local.checked_at.isoformat(), 'authorization': authorization,
        'sab_history_completed': False, 'progress': None, 'files': local.receipt['handoff']}
