"""Immutable selected TV receipts shared by native authority and effect ports.

These values describe one command. Construction never grants permission, marks
an import verified, or supplies a missing remote destination hash.
"""
from dataclasses import asdict, dataclass
from abc import ABC, abstractmethod
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
class DeliveryBinding:
    """Confirmed mapping only; a DTO never grants directory/channel authority."""
    destination: DestinationBinding
    relative_parent: str
    create_parent: bool

    def __post_init__(self):
        from . import completion_registry as registry
        from .jellyfin_native import TV_ROOTS
        import re
        destination = self.destination
        if type(destination) is not DestinationBinding or type(destination.service_generation) is not ServiceGeneration:
            raise ContractError('Current typed delivery mapping required')
        generation = destination.service_generation
        for value in (destination.ref, destination.host, generation.configuration_generation, generation.credential_generation):
            if type(value) is not str or not 0 < len(value) <= 200 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ContractError('Canonical delivery binding required')
        if (generation.service != 'destination_file' or type(generation.binding_digest) is not str
                or re.fullmatch('[0-9a-f]{64}', generation.binding_digest) is None
                or destination.root not in TV_ROOTS or type(self.relative_parent) is not str
                or type(self.create_parent) is not bool):
            raise ContractError('Approved delivery binding required')
        try:
            registry.path_parts(destination.root, absolute=True)
            parts = registry.path_parts(self.relative_parent) if self.relative_parent else []
        except ValueError:
            raise ContractError('Canonical confirmed delivery folder required') from None
        if self.create_parent and len(parts) != 1:
            raise ContractError('One authorized new parent required')


class TVServiceProvider(ABC):
    """Trusted runtime factories supply current scoped ports, never HTTP data."""
    @abstractmethod
    def delivery_binding(self, access, job):
        """Current authorized mapping metadata, without a file/provider effect."""

    @abstractmethod
    def current(self, context):
        """Current selected service ports for this one native job."""


def delivery_binding_material(binding):
    """The one pre-submit wire/digest producer; O1 keeps its full-path digest."""
    if type(binding) is not DeliveryBinding:
        raise ContractError('Typed delivery binding required')
    binding.__post_init__()
    material = {'schema': 'obtain-tv-delivery-binding/v1', 'destination': asdict(binding.destination),
                'relative_parent': binding.relative_parent, 'create_parent': binding.create_parent}
    return {**material, 'digest': hashlib.sha256(b'obtain-tv-delivery-binding/v1\x00' +
                                               canonical_bytes(material)).hexdigest()}


def read_delivery_binding(value):
    """Decode immutable material, without reconstructing current permission."""
    try:
        if (type(value) is not dict or set(value) != {'schema', 'destination', 'relative_parent', 'create_parent', 'digest'}
                or value['schema'] != 'obtain-tv-delivery-binding/v1'):
            raise ValueError
        destination = value['destination']
        if type(destination) is not dict or set(destination) != {'ref', 'host', 'root', 'service_generation'}:
            raise ValueError
        generation = destination['service_generation']
        if type(generation) is not dict or set(generation) != {'service', 'binding_digest', 'configuration_generation', 'credential_generation'}:
            raise ValueError
        result = DeliveryBinding(DestinationBinding(destination['ref'], destination['host'], destination['root'],
            ServiceGeneration(**generation)), value['relative_parent'], value['create_parent'])
        if delivery_binding_material(result) != value:
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError):
        raise ContractError('Closed confirmed delivery binding required') from None


def require_confirmed_delivery_plan(delivery, plan):
    """Check the confirmed directory before O1 may inspect or receive bytes."""
    from .jellyfin_tv_import import CopyPlan
    from . import completion_registry as registry
    if type(delivery) is not DeliveryBinding or type(plan) is not CopyPlan:
        raise ContractError('Typed confirmed copy plan required')
    delivery.__post_init__()
    try:
        parts = registry.path_parts(plan.relative_path)
    except ValueError:
        raise ContractError('Canonical delivery path required') from None
    if ((plan.host_id, plan.root) != (delivery.destination.host, delivery.destination.root)
            or '/'.join(parts[:-1]) != delivery.relative_parent or plan.create_parent is not delivery.create_parent):
        raise ContractError('Copy plan changed the confirmed destination folder')
    return plan


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
