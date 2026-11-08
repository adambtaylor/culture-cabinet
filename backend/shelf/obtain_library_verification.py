"""Pure TV arrival comparison behind one current native/service fence.

Metadata cannot supply a destination hash. Only Taste persists the resulting
verification value under its CAS; constructing any receipt here grants nothing.
"""
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
import re

from shelf import completion_registry as registry
from shelf.jellyfin_native import (
    ObservedMediaSource, ResolvedEpisodeBinding, SelectedLibraryObservation, ServiceBinding,
    _digest as library_binding_digest,
)
from shelf.jellyfin_tv_import import DestinationFileReceipt
from shelf.obtain_tv_flow import LibraryVerification, SelectedCompletion, ServiceGeneration
from shelf.sab_selected import canonical_digest, require_native


@dataclass(frozen=True)
class VerificationOutcome:
    status: str
    reason: str
    verification: LibraryVerification | None = None


class _Mismatch(ValueError):
    pass


def _match(condition):
    if not condition:
        raise _Mismatch()


def _aware(value):
    _match(type(value) is datetime and value.utcoffset() is not None)
    return value


def _material(value):
    if is_dataclass(value):
        return _material(asdict(value))
    if type(value) is datetime:
        return _aware(value).isoformat()
    if type(value) is dict:
        return {key: _material(item) for key, item in value.items()}
    if type(value) in (tuple, list):
        return [_material(item) for item in value]
    return value


def _proof_digest(value, domain):
    return canonical_digest(_material(value), domain=domain)


def check_library_evidence(*, completion, destination, resolution, observation,
                           observed_at, authority_deadline):
    """Deterministic comparison only; callable values are not live authority."""
    try:
        now, auth_until = _aware(observed_at), _aware(authority_deadline)
        _match(type(completion) is SelectedCompletion
               and type(destination) is DestinationFileReceipt
               and type(resolution) is ResolvedEpisodeBinding
               and type(observation) is SelectedLibraryObservation)
        job = completion.job
        if resolution.status != 'resolved' or observation.status != 'matched':
            return VerificationOutcome('unavailable', 'awaiting_library_index_or_readback')
        _match(completion.state == 'completed' and completion.reason == ''
               and len(completion.files) == 1 and completion.history_digest is not None
               and completion.hook_digest is not None
               and completion.repair in ('performed_successfully', 'not_needed', 'unobserved')
               and completion.unpack in ('performed_successfully', 'not_needed', 'unobserved'))
        source = completion.files[0]
        manifest = canonical_digest({'request_id': job.request_id, 'nzo_id': job.nzo_id,
            'binding_digest': job.binding_digest, 'files': [asdict(source)]},
            domain='obtain-tv-source-manifest/v1')
        _match(manifest == completion.source_manifest_digest == destination.source_manifest_digest)
        _match((destination.request_id, destination.job_id, destination.nzo_id,
                destination.binding_digest, destination.content_identity_digest, destination.test_only) ==
               (job.request_id, job.job_id, job.nzo_id, job.binding_digest,
                job.content_identity_digest, job.test_only))
        _match(type(destination.test_only) is bool and type(job.test_only) is bool
               and type(destination.size) is int and destination.size == source.size
               and destination.sha256 == source.sha256
               and re.fullmatch('[0-9a-f]{64}', destination.sha256) is not None)
        registry.path_parts(destination.root, absolute=True)
        registry.path_parts(destination.relative_path)
        destination_path = str(PurePosixPath(destination.root, destination.relative_path))
        _match((resolution.request_id, resolution.job_binding_digest, resolution.content_identity_digest,
                resolution.destination_binding_digest, resolution.root) ==
               (job.request_id, job.binding_digest, job.content_identity_digest,
                destination.destination_binding_digest, destination.root))
        _match(resolution.selection is not None and resolution.selection.scope == job.episode_scope
               and observation.scope == job.episode_scope and observation == resolution.observation
               and (observation.server_id, observation.user_id, observation.library_id,
                    observation.item_id, observation.series_id, observation.binding_digest) ==
               (resolution.server_id, resolution.user_id, resolution.library_id,
                resolution.selection.item_id, resolution.series_id, resolution.service_binding_digest))
        _match(len(resolution.operation_digests) == len(resolution.response_digests) == 3
               and resolution.operation_digests[-1] == observation.operation_digest
               and resolution.response_digests[-1] == observation.response_digest)
        # Actual configured library generation must be the one observed. Initial
        # search/add-only grants do not become requirements or read capabilities.
        library = [item for item in job.service_generations if item.service == 'jellyfin']
        _match(len(library) == 1 and type(library[0]) is ServiceGeneration
               and type(resolution.binding) is ServiceBinding
               and library_binding_digest(resolution.binding.material()) == resolution.service_binding_digest
               and (resolution.binding.server_id, resolution.binding.user_id, resolution.binding.library_id,
                    resolution.binding.tv_root, resolution.binding.actor_id, resolution.binding.profile_id) ==
               (resolution.server_id, resolution.user_id, resolution.library_id,
                resolution.root, job.actor_id, job.profile_id)
               and library[0].binding_digest == resolution.service_binding_digest
               and library[0].configuration_generation == resolution.configuration_generation
               == observation.configuration_generation
               and library[0].credential_generation == resolution.credential_generation
               == observation.credential_generation)
        _match(type(observation.media_sources) is tuple and 0 < len(observation.media_sources) <= 8
               and all(type(item) is ObservedMediaSource for item in observation.media_sources)
               and len({item.source_id for item in observation.media_sources}) == len(observation.media_sources))
        sources = [item for item in observation.media_sources if item.path == destination_path]
        _match(len(sources) == 1 and type(sources[0].size) is int
               and sources[0].size == destination.size and bool(sources[0].source_id))
        copied_at = _aware(datetime.fromisoformat(destination.observed_at))
        _match(_aware(completion.observed_at) <= copied_at <= _aware(observation.observed_at)
               <= _aware(resolution.observed_at) <= now)
        valid_until = min(observation.observed_at + timedelta(seconds=300),
                          _aware(resolution.valid_until), resolution.binding.expires_at, auth_until)
        if not now < valid_until:
            return VerificationOutcome('unavailable', 'current_verification_required')
        return VerificationOutcome('verified', '', LibraryVerification(
            job.request_id, job.binding_digest, job.content_identity_digest,
            _proof_digest(completion, 'obtain-tv-completion/v1'),
            _proof_digest(destination, 'obtain-tv-destination-receipt/v1'),
            _proof_digest(resolution, 'obtain-tv-resolution/v1'),
            _proof_digest(observation, 'obtain-tv-observation/v1'),
            resolution.server_id, resolution.user_id, resolution.library_id,
            resolution.selection.item_id, observation.observed_at, valid_until))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return VerificationOutcome('mismatch', 'library_evidence_mismatch')


def verify_library_match(context, *, completion, destination, resolution, observation,
                         revalidate_services=None, clock=None):
    """Revalidate actual service generations and actual native authority expiry.

    The trusted backend port returns (current library/destination generations,
    actual native/session deadline). A guessed datetime or Boolean from a browser
    is never accepted as this port. Missing deadline/currentness means no green.
    """
    registry.require(type(completion) is SelectedCompletion)
    job = completion.job
    require_native(context, job, 'verify_library')
    clock = clock or (lambda: datetime.now(timezone.utc))
    if revalidate_services is None:
        return VerificationOutcome('unavailable', 'current_service_binding_required')
    try:
        with context.effect_window():
            def current():
                require_native(context, job, 'verify_library')
                actual = revalidate_services()
                registry.require(type(actual) is tuple and len(actual) == 2
                                 and type(actual[0]) is tuple and len(actual[0]) == 2
                                 and {item.service for item in actual[0]} == {'jellyfin', 'destination_file'}
                                 and all(type(item) is ServiceGeneration
                                         and item in job.service_generations for item in actual[0])
                                 and type(actual[1]) is datetime and actual[1].utcoffset() is not None
                                 and clock() < actual[1])
                return actual
            authority = current()
            result = check_library_evidence(completion=completion, destination=destination,
                resolution=resolution, observation=observation, observed_at=clock(),
                authority_deadline=authority[1])
            registry.require(current() == authority)
            return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return VerificationOutcome('unavailable', 'current_library_authority_unavailable')
