"""Immutable selected TV receipts shared by native authority and effect ports.

These values describe one command. Construction never grants permission, marks
an import verified, or supplies a missing remote destination hash.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib

from .acquisition_contract import ContractError
from .disclosure import canonical_bytes


TV_CONTENT_IDENTITY_DOMAIN = b'obtain-tv-content-identity/v1\x00'


def tv_content_identity_digest(*, tmdb_show_id, season, episode):
    """Bind one canonical episode without retaining or recovering its source ID."""
    values = {'tmdb_show_id': tmdb_show_id, 'season': season, 'episode': episode}
    if (any(type(value) is not int for value in values.values())
            or not 1 <= tmdb_show_id <= 2147483647
            or not 0 <= season <= 999 or not 1 <= episode <= 999):
        raise ContractError('Canonical selected TV episode required')
    # One shared byte contract lets a fresh library identity match the original
    # confirmed command. Equality never supplies expired catalogue facts or
    # reauthorizes the command; each producer must validate its own evidence.
    return hashlib.sha256(TV_CONTENT_IDENTITY_DOMAIN + canonical_bytes(values)).hexdigest()


@dataclass(frozen=True)
class ServiceGeneration:
    service: str
    binding_digest: str
    configuration_generation: str
    credential_generation: str


@dataclass(frozen=True)
class SelectedJobBinding:
    request_id: str
    job_id: int
    actor_id: int
    profile_id: int
    publication_origin_digest: str | None
    binding_digest: str
    selection_digest: str
    review_digest: str
    episode_scope: str
    result_id: str
    result_guid: str
    result_digest: str
    nzb_sha256: str
    nzo_id: str
    completion_policy_digest: str
    complete_root: str
    destination_ref: str
    test_only: bool
    content_identity_digest: str
    service_generations: tuple[ServiceGeneration, ...]


@dataclass(frozen=True)
class VerifiedSourceFile:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SelectedCompletion:
    job: SelectedJobBinding
    state: str
    reason: str
    source_manifest_digest: str | None
    files: tuple[VerifiedSourceFile, ...]
    repair: str
    unpack: str
    history_digest: str | None
    hook_digest: str | None
    observed_at: datetime


@dataclass(frozen=True)
class DestinationBinding:
    ref: str
    host: str
    root: str
    service_generation: ServiceGeneration


@dataclass(frozen=True)
class LibraryVerification:
    request_id: str
    binding_digest: str
    content_identity_digest: str
    completion_digest: str
    destination_digest: str
    resolution_digest: str
    observation_digest: str
    server_ref: str
    user_ref: str
    library_ref: str
    item_ref: str
    observed_at: datetime
    valid_until: datetime


def card_projection(*, state, label, detail_href, verification_until=None):
    if state not in {'idle', 'working', 'error', 'done'}:
        raise ValueError('Closed native card state required')
    return {'state': state, 'label': label, 'detail_href': detail_href,
            'verification_until': verification_until}
