"""Versioned completion export from separately authorized SAB history evidence.

This module does not fetch history or broaden the existing add-only grant. The
trusted caller supplies an exact-job response from an independently authorized
history read. A hook receipt alone never supplies an acquisition timestamp.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib

from shelf import completion_registry as registry
from shelf.acquisition_contract import ContractError


def completed_export(handoff, history, *, root, source_identity=None):
    """Bind a final SAB observation to a freshly reverified per-job handoff.

    SAB 5.1 history.completed is integer Unix seconds, recorded after the hook.
    Compare the hook clock at the same precision. Completed is SAB's reported
    state, not independent evidence of repair, full album identity or playback.
    """
    try:
        registry.require(type(history) is dict and type(history['history']) is dict)
        slots = history['history']['slots']
        registry.require(type(slots) is list and len(slots) == 1)
        slot = slots[0]
        registry.require(type(slot) is dict)
        files = handoff['files']
        registry.require(handoff['version'] == 1
                         and handoff['state'] == 'POSTPROCESS_FILE_VERIFIED'
                         and type(files) is list and bool(files))
        nzo = files[0]['nzo_id']
        binding = files[0]['provenance']
        registry.require(all(item['nzo_id'] == nzo and item['provenance'] == binding
                             for item in files))
        registry.require(slot['nzo_id'] == nzo and slot['storage'] == root
                         and slot['status'] == 'Completed' and slot['fail_message'] == ''
                         and slot['script'] == 'culture-cabinet-completion')
        seconds = slot['completed']
        registry.require(type(seconds) is int and seconds > 0)
        acquired = datetime.fromtimestamp(seconds, timezone.utc)
        checked = datetime.fromisoformat(handoff['checked_at'])
        registry.require(checked.utcoffset() is not None and acquired <= checked)
        for item in files:
            verified = datetime.fromisoformat(item['verified_at'])
            registry.require(verified.utcoffset() is not None
                             and int(verified.timestamp()) <= seconds)
        stamp = acquired.isoformat()
        evidence = {'source': 'sab_history', 'nzo_id': nzo,
                    'acquired_at': stamp, 'acquired_at_basis': 'sab_history_completed_at',
                    'provenance': binding,
                    'files': [{key: item[key] for key in ('relative_path', 'sha256', 'size')}
                              for item in files]}
        album_identity = None
        if source_identity is not None:
            evidence['source_identity'] = source_identity
            if (source_identity['provider'], source_identity['medium'], source_identity['scope']) == (
                    'album_request', 'music', 'album_flac'):
                from shelf.album_acquisition import validate_identity
                album_identity = deepcopy(handoff['authorization']['album_identity'])
                validate_identity(album_identity)
                evidence['album_identity'] = album_identity
        revision = hashlib.sha256(registry.encode_json(evidence)).hexdigest()
        return {**handoff, 'version': 2, 'state': 'COMPLETED_FILES_VERIFIED',
                'sab_history_completed': True, 'completion_evidence': evidence,
                **({'source_identity': source_identity} if source_identity is not None else {}),
                **({'album_identity': album_identity} if album_identity is not None else {}),
                'completion_evidence_sha256': revision,
                'files': [{**item, 'acquired_at': stamp,
                           'acquired_at_basis': 'sab_history_completed_at'} for item in files]}
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        raise ContractError('Bound final SAB history evidence required') from None


def export_completed(*, actor, profile_id, job_id, context, history):
    """Operator-only v2 export; the ordinary HTTP/Mac v1 export is unchanged.

    No observation is persisted here. Every call requires the separately
    obtained history response and repeats current ownership/file validation.
    Do not pass HTTP-supplied history to this trusted interface.
    """
    from shelf import completion_consumer, obtain_acquisition
    from shelf.models import PostprocessReceipt
    if type(context) is not obtain_acquisition.OperatorContext:
        raise ContractError('Trusted completion operator context required')
    context.require(actor, profile_id)
    handoff = completion_consumer.export(actor=actor, profile_id=profile_id, job_id=job_id)
    local = PostprocessReceipt.objects.get(transfer__job_id=job_id)
    job = obtain_acquisition.owned_job(actor, profile_id, job_id)
    source_identity = {key: getattr(job, key) for key in
                       ('provider', 'medium', 'source_id', 'title', 'edition', 'scope')}
    source_identity['basis'] = 'selected_catalogue_identity_not_content_verification'
    result = completed_export(handoff, history, root=local.receipt['root'],
                              source_identity=source_identity)
    context.require(actor, profile_id)
    return result
