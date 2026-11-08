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


@dataclass(frozen=True)
class SelectedTVServices:
    """Current custodian-owned ports; no credential or authority is serialized."""
    delivery: DeliveryBinding
    observer: object
    destination: object
    library: object
    library_current: object
    library_credential_scope: object
    library_clock: object
    target_for: object
    stop_owned_children: object

    def generations(self, context):
        from uuid import UUID
        from .obtain_native_context import NativeJobContext
        from .sab_selected import SelectedSabPorts
        from .jellyfin_tv_import import DestinationImportPorts
        from .jellyfin_native import ServiceBinding, _digest
        if (type(context) is not NativeJobContext or type(self.delivery) is not DeliveryBinding
                or type(self.observer) is not SelectedSabPorts or type(self.destination) is not DestinationImportPorts
                or type(self.observer.generation) is not ServiceGeneration
                or type(self.destination.generation) is not ServiceGeneration
                or type(self.library) is not ServiceBinding
                or not all(callable(value) for value in (self.library_current, self.library_credential_scope,
                    self.target_for, self.stop_owned_children, self.destination.authority_deadline,
                    getattr(self.library_clock, 'now', None), getattr(self.library_clock, 'monotonic', None)))):
            raise ContractError('Complete current selected TV ports required')
        context.revalidate(); self.library.validate()
        if ((self.library.actor_id, self.library.profile_id, self.library.installation_id) !=
                (context.actor_id, context.profile_id, UUID(context._access.identity.installation_ref).hex)
                or self.library.tv_root != self.delivery.destination.root
                or self.library_current() != self.library
                or context._access.authority.now() >= self.library.expires_at
                or self.observer.generation.service != 'sab_history'
                or self.destination.generation != self.delivery.destination.service_generation
                or self.observer.revalidate() != self.observer.generation
                or self.destination.revalidate() != self.destination.generation):
            raise ContractError('Selected TV service binding changed')
        library = ServiceGeneration('jellyfin', _digest(self.library.material()),
            self.library.configuration_generation, self.library.credential_generation)
        return tuple(sorted((self.observer.generation, self.destination.generation, library), key=lambda item: item.service))


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


def validate_native_progress(value, *, binding, job_id, verification=None, provider_job=None):
    try:
        return _validate_native_progress(value, binding=binding, job_id=job_id,
                                         verification=verification, provider_job=provider_job)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ContractError('Closed native operational receipt required') from None


def _validate_native_progress(value, *, binding, job_id, verification=None, provider_job=None):
    """Closed operational history; decoding never creates a live green proof."""
    from uuid import UUID
    import re
    from . import completion_registry as registry
    from .obtain_native_context import operational_digest
    from .jellyfin_tv_import import DestinationFileReceipt, destination_binding_digest
    def require(condition):
        if not condition:
            raise ContractError('Closed native operational receipt required')
    def sha(raw):
        require(type(raw) is str and re.fullmatch('[0-9a-f]{64}', raw) is not None)
    def stamp(raw):
        require(type(raw) is str)
        moment = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        require(moment.utcoffset() is not None)
        return moment
    def ref(raw):
        require(type(raw) is str and str(UUID(raw)) == raw and UUID(raw).int != 0)
    if value == {}:
        # Historical receipts remain readable without adopting today's mapping.
        require(verification is None)
        return
    allowed = {'schema', 'stage', 'reason', 'service_generations', 'intent', 'completion',
               'copy_intent', 'destination', 'series_ref', 'observed_at'}
    stages = {'submitted', 'observing', 'awaiting_completion', 'download_failed', 'copying', 'copy_pending',
        'copy_unknown', 'copy_conflict', 'verifying_library', 'awaiting_library_index',
        'verification_unavailable', 'verification_mismatch', 'verified'}
    require(type(value) is dict and set(value) <= allowed and value.get('schema') == 'obtain-tv-progress/v1'
        and value.get('stage') in stages and type(value.get('reason')) is str
        and re.fullmatch('[a-z0-9_]{0,64}', value['reason']) is not None
        and len(canonical_bytes(value)) <= 65536)
    generations = value.get('service_generations')
    require(type(generations) is list and len(generations) == 3)
    names = []
    for generation in generations:
        require(type(generation) is dict and set(generation) == set(ServiceGeneration.__dataclass_fields__))
        names.append(generation['service']); sha(generation['binding_digest'])
        for key in ('configuration_generation', 'credential_generation'):
            require(type(generation[key]) is str and 0 < len(generation[key]) <= 200
                    and all(ord(c) >= 32 and ord(c) != 127 for c in generation[key]))
    require(names == ['destination_file', 'jellyfin', 'sab_history'])
    if 'intent' in value:
        intent = value['intent']
        require(type(intent) is dict and set(intent) == {'id', 'stage', 'revision'})
        ref(intent['id'])
        require(intent['stage'] in ('observe', 'import', 'verify_library')
                and type(intent['revision']) is int and 0 < intent['revision'] < 2**53)
    if 'copy_intent' in value:
        ref(value['copy_intent'])
    if value.get('series_ref') is not None:
        require(type(value['series_ref']) is str and re.fullmatch('[0-9a-f]{32}', value['series_ref']) is not None
                and value['series_ref'] != '0' * 32)
    if 'observed_at' in value:
        stamp(value['observed_at'])
    if 'completion' in value:
        completed = value['completion']
        require(type(completed) is dict and set(completed) == {'files', 'source_manifest_digest', 'history_digest',
            'hook_digest', 'repair', 'unpack', 'observed_at'} and type(completed['files']) is list
            and len(completed['files']) == 1)
        for key in ('source_manifest_digest', 'history_digest', 'hook_digest'):
            sha(completed[key])
        require(completed['repair'] in ('performed_successfully', 'not_needed', 'unobserved')
                and completed['unpack'] in ('performed_successfully', 'not_needed', 'unobserved'))
        stamp(completed['observed_at'])
        file = completed['files'][0]
        require(type(file) is dict and set(file) == set(VerifiedSourceFile.__dataclass_fields__))
        registry.path_parts(file['relative_path']); sha(file['sha256'])
        require(type(file['size']) is int and 0 < file['size'] <= binding['max_bytes'])
    if 'destination' in value:
        destination = value['destination']
        require(type(destination) is dict and set(destination) == set(DestinationFileReceipt.__dataclass_fields__)
                and 'completion' in value and 'copy_intent' in value and 'delivery_binding' in binding)
        delivery = read_delivery_binding(binding['delivery_binding'])
        parts = registry.path_parts(destination['relative_path'])
        require(destination['request_id'] == binding['request_id'] and destination['job_id'] == job_id
                and type(destination['job_id']) is int and type(destination['size']) is int
                and destination['nzo_id'] == provider_job
                and destination['binding_digest'] == operational_digest(binding)
                and destination['content_identity_digest'] == binding['content_identity_digest']
                and destination['source_manifest_digest'] == value['completion']['source_manifest_digest']
                and destination['intent'] == value['copy_intent']
                and (destination['host_id'], destination['root']) == (delivery.destination.host, delivery.destination.root)
                and '/'.join(parts[:-1]) == delivery.relative_parent
                and type(destination['test_only']) is bool and destination['test_only'] == binding['test_only']
                and destination['size'] == value['completion']['files'][0]['size']
                and destination['sha256'] == value['completion']['files'][0]['sha256']
                and destination['destination_binding_digest'] == destination_binding_digest(
                    delivery.destination, destination['relative_path'], delivery.create_parent)
                and stamp(value['completion']['observed_at']) <= stamp(destination['observed_at']))
        registry.nzo_id(destination['nzo_id'])
    if verification is not None:
        require(type(verification) is dict and set(verification) == set(LibraryVerification.__dataclass_fields__)
                and 'destination' in value and value['stage'] == 'verified'
                and verification['request_id'] == binding['request_id']
                and verification['binding_digest'] == operational_digest(binding)
                and verification['content_identity_digest'] == binding['content_identity_digest'])
        for key in ('completion_digest', 'destination_digest', 'resolution_digest', 'observation_digest'):
            sha(verification[key])
        for key in ('server_ref', 'user_ref', 'library_ref', 'item_ref'):
            require(type(verification[key]) is str and re.fullmatch('[0-9a-f]{32}', verification[key]) is not None
                    and verification[key] != '0' * 32)
        require(stamp(value['destination']['observed_at']) <= stamp(verification['observed_at']) < stamp(verification['valid_until']))


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
