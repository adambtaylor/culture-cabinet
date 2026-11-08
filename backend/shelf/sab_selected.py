"""Exact acknowledged SAB job evidence, with no queue API or add-key escalation."""
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
from pathlib import PurePosixPath
import re

from shelf import completion_files, completion_registry as registry
from shelf.acquisition_contract import sab_name
from shelf.episode_scope import episode_code
from shelf.obtain_tv_flow import (
    SelectedCompletion, SelectedJobBinding, ServiceGeneration, VerifiedSourceFile,
)


@dataclass(frozen=True)
class SelectedSabPorts:
    generation: ServiceGeneration
    revalidate: object
    read_history: object
    read_registration: object
    read_hook: object
    clock: object


def canonical_digest(value, *, domain):
    return hashlib.sha256(domain.encode('ascii') + b'\0' + registry.encode_json(value)).hexdigest()


def require_native(context, job, operation):
    # Import the one Taste-owned issuer only at the effect boundary. Pure
    # evidence checks remain usable without importing the application/ORM.
    from shelf.obtain_native_context import NativeJobContext
    registry.require(type(context) is NativeJobContext and type(job) is SelectedJobBinding
                     and context.operation == operation
                     and all(type(value) is int and value > 0 for value in
                             (job.job_id, job.actor_id, job.profile_id))
                     and type(job.test_only) is bool
                     and type(context.transfer_revision) is int and context.transfer_revision >= 0
                     and type(job.binding_digest) is str
                     and re.fullmatch('[0-9a-f]{64}', job.binding_digest) is not None
                     and (context.job_id, context.actor_id, context.profile_id,
                          context.request_id, context.binding_digest) ==
                     (job.job_id, job.actor_id, job.profile_id, job.request_id, job.binding_digest))
    context.revalidate()
    registry.nzo_id(job.nzo_id)


def _outcome(job, state, reason, now, *, files=(), manifest=None, history=None, hook=None):
    # A successful SAB job/hook reports post-processing success. Neither fact
    # says a PAR2 repair or an unpack actually ran; preserve that distinction.
    return SelectedCompletion(job, state, reason, manifest, files,
                              'unobserved', 'unobserved', history, hook, now)


def check_selected_evidence(job, entry, hook, history, *, observed_at):
    """Pure binding checks followed by fresh reads of only registered files.

    This function grants no native/service authority. Only observe_selected's
    current bound ports may supply actual registrations, hook and history data.
    """
    registry.require(type(job) is SelectedJobBinding and type(observed_at) is datetime
                     and observed_at.utcoffset() is not None and episode_code(job.episode_scope))
    try:
        entry = registry.registry_entry({'version': 2, 'requests': [entry]}, job.nzo_id)
        for key, wanted in (
                ('request_id', job.request_id), ('result_id', job.result_id),
                ('result_digest', job.result_digest), ('nzb_sha256', job.nzb_sha256),
                ('scope', job.episode_scope), ('complete_root', job.complete_root),
                ('test_only', job.test_only)):
            registry.require(entry[key] == wanted)
        expected_name = sab_name(job.request_id)
        registry.require(entry['root'] == job.complete_root + '/' + expected_name)
        if history is None:
            return _outcome(job, 'pending', 'final_history_pending', observed_at)
        registry.require(type(history) is dict and history['nzo_id'] == job.nzo_id
                         and history['name'] == expected_name)
        history_digest = canonical_digest(history, domain='obtain-tv-sab-history/v1')
        if history['status'] == 'Failed':
            return _outcome(job, 'failed', 'sab_reported_failure', observed_at, history=history_digest)
        if history['status'] != 'Completed':
            return _outcome(job, 'pending', 'sab_postprocessing', observed_at, history=history_digest)
        registry.require(history['storage'] == entry['root'] and history['fail_message'] == ''
                         and history['script'] == 'culture-cabinet-completion'
                         and type(history['completed']) is int and history['completed'] > 0
                         and history['completed'] <= observed_at.timestamp())
        if hook is None:
            return _outcome(job, 'pending', 'verified_hook_pending', observed_at, history=history_digest)
        registry.require(type(hook) is dict and hook['version'] == 2
                         and hook['state'] == 'POSTPROCESS_FILE_VERIFIED')
        for key in ('nzo_id', 'request_id', 'result_id', 'result_digest', 'nzb_sha256',
                    'scope', 'root', 'complete_root', 'max_bytes', 'test_only'):
            registry.require(hook[key] == entry[key])
        registry.require(hook['expected_manifest'] == entry['files']
                         and hook['sab_status'] == 'Running'
                         and type(hook['sab_pp_status']) is int)
        hook_digest = canonical_digest(hook, domain='obtain-tv-sab-hook/v1')
        if hook['sab_pp_status'] != 0:
            return _outcome(job, 'failed', 'sab_postprocessing_failed', observed_at,
                            history=history_digest, hook=hook_digest)
        registry.require(hook['postprocess_evidence'] == 'sab_job_result_zero'
                         and hook['repair_verified'] is None
                         and hook['crc_verified'] is None
                         and type(hook['files']) is list and 0 < len(hook['files']) <= registry.MAX_FILES)
        registry.require(len(registry.encode_json(hook)) <= registry.MAX_JSON
                         and all(type(item) is dict and set(item) == {'path', 'bytes', 'sha256'}
                                 for item in hook['files'])
                         and len({item['path'] for item in hook['files']}) == len(hook['files']))
        stamp = datetime.fromisoformat(hook['verified_at'])
        registry.require(stamp.utcoffset() is not None
                         and int(stamp.timestamp()) <= history['completed'])
        registry.require(hook['sab_files'] == sorted(item['path'] for item in hook['files']))
        checked = completion_files.verify_media({**entry, 'files': hook['files']})
        registry.require(checked == hook['files'] and hook['total_bytes'] == sum(f['bytes'] for f in checked))
        videos = [item for item in checked if PurePosixPath(item['path']).suffix.lower()
                  in ('.mkv', '.mp4', '.webm', '.avi')]
        registry.require(len(videos) == 1 and PurePosixPath(videos[0]['path']).suffix.lower()
                         in ('.mkv', '.mp4', '.webm'))
        file = videos[0]
        selected = (VerifiedSourceFile(file['path'], file['bytes'], file['sha256']),)
        manifest = canonical_digest({'request_id': job.request_id, 'nzo_id': job.nzo_id,
            'binding_digest': job.binding_digest, 'files': [asdict(item) for item in selected]},
            domain='obtain-tv-source-manifest/v1')
        return _outcome(job, 'completed', '', observed_at, files=selected, manifest=manifest,
                        history=history_digest, hook=hook_digest)
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return _outcome(job, 'unavailable', 'selected_completion_unverified', observed_at)


def observe_selected(context, job_binding, *, ports=None):
    """Current native fence plus separately legitimate exact-history capability."""
    require_native(context, job_binding, 'observe')
    try:
        registry.require(type(ports) is SelectedSabPorts
                         and type(ports.generation) is ServiceGeneration
                         and ports.generation.service == 'sab_history'
                         and ports.generation in job_binding.service_generations)
        with context.effect_window():
            # A local exact-job reader is not implied by the add-only SAB grant.
            # Its custodian revalidates the actual independent observer binding.
            def current():
                require_native(context, job_binding, 'observe')
                registry.require(ports.revalidate() == ports.generation)
            current()
            entry = ports.read_registration(job_binding.nzo_id)
            current()
            history = ports.read_history(job_binding.nzo_id)
            current()
            hook = ports.read_hook(job_binding.request_id)
            current()
            result = check_selected_evidence(job_binding, entry, hook, history,
                                             observed_at=ports.clock())
            current()
            return result
    except Exception:
        context.revalidate()  # Revoked/stale native scope still aborts the caller.
        from datetime import timezone
        return _outcome(job_binding, 'unavailable', 'selected_observer_unavailable',
                        datetime.now(timezone.utc))
