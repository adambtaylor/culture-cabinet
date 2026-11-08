"""Candidate submission and local handoff with separately trusted operator evidence."""
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import re
from pathlib import Path
from uuid import UUID

from django.core import signing
from django.db import connection, transaction
from django.db.models import F
from django.http import Http404
from django.utils import timezone

from shelf import obtain_preview as preview, saved
from shelf import completion_registry as registry
from shelf.portable_saved_authority import effect_for, write_for, active_portable_access
from shelf.acquisition_contract import Blocked, ContractError, MAX_NZB_BYTES
from shelf.acquisition_pipeline import (Checkpoint, Completion, Evidence, Pipeline,
                                        ProviderJob, Snapshot, StagedFile, verify_staging)
from shelf.models import AcquisitionJob, ObtainSelection, ObtainTransfer, ObtainLocalFile
from shelf.obtain_parser import digest, listing_reason

SALT = 'obtain-submit-v1'
MAX_LOCAL_BYTES = 128 * 1024**3
ORIGIN_SUBMISSION_TAGS = {
    'obtain-publication-origin/v1': 'obtain-publication-submission/v1',
    'obtain-owner-selection-origin/v1': 'obtain-owner-selection-submission/v1',
}


@dataclass(frozen=True)
class OperatorContext:
    """Injected by trusted candidate operator/test code, never request fields."""
    actor_id: int
    profile_id: int
    purpose: str
    enabled: bool = False

    def require(self, actor, profile_id):
        if (self.enabled is not True or self.purpose not in {'candidate_operator', 'offline_test'}
                or (self.actor_id, self.profile_id) != (actor.pk, profile_id)):
            raise Blocked('Explicit candidate operator context required')
        saved.authority(actor, profile_id)


@dataclass(frozen=True)
class ExactResult:
    snapshot: Snapshot
    result_id: str
    guid: str
    digest: str
    size: int

    def __post_init__(self):
        if (type(self.snapshot) is not Snapshot
                or type(self.guid) is not str
                or not re.fullmatch(r'-?(?:0|[1-9][0-9]{0,18})', self.guid)
                or not -(2**63) <= int(self.guid) < 2**63 or str(int(self.guid)) != self.guid
                or type(self.digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', self.digest)
                or type(self.size) is not int or not 0 < self.size < 2**63):
            raise ContractError('Invalid exact Hydra result identity')
        try:
            valid_id = str(UUID(self.result_id)) == self.result_id
        except (TypeError, ValueError, AttributeError):
            valid_id = False
        if not valid_id:
            raise ContractError('Invalid local result identity')

    def material(self):
        return asdict(self)


@dataclass(frozen=True, repr=False)
class OriginExactResult(ExactResult):
    publication_origin: dict

    def __post_init__(self):
        super().__post_init__()
        from .portable_obtain_origin import validate_obtain_origin
        validate_obtain_origin(self.publication_origin)
        if (self.snapshot.provider, self.snapshot.medium) != ('tmdb', 'tv'):
            raise ContractError('Exact source TV identity required')

    @property
    def snapshot_digest(self):
        from .disclosure import canonical_bytes
        return hashlib.sha256(canonical_bytes(asdict(self.snapshot))).hexdigest()

    @property
    def content_identity_digest(self):
        from .obtain_tv_flow import tv_content_identity_digest
        from .episode_scope import parse_scope
        season, episode = parse_scope(self.snapshot.scope)
        return tv_content_identity_digest(tmdb_show_id=self.snapshot.source_id, season=season, episode=episode)

    def material(self):
        # Descriptor identity contains only opaque provenance/digests and the
        # independent Hydra fields. Dataclass recursion would retain source IDs.
        return {'result_id': self.result_id, 'guid': self.guid, 'digest': self.digest, 'size': self.size,
            'publication_origin': self.publication_origin, 'snapshot_digest': self.snapshot_digest,
            'content_identity_digest': self.content_identity_digest}


@dataclass(frozen=True, repr=False)
class RetrievedNZB:
    result: ExactResult
    payload: bytes

    def sha256(self, expected):
        if (self.result != expected or type(self.payload) is not bytes
                or not 0 < len(self.payload) <= MAX_NZB_BYTES):
            raise ContractError('Exact result NZB binding required')
        return hashlib.sha256(self.payload).hexdigest()


class Retriever:
    def __init__(self, backend):
        self.backend = backend

    def retrieve(self, result: ExactResult) -> RetrievedNZB:
        """Fixed Hydra GET through the credential and bounded transport boundaries."""
        if type(result) not in (ExactResult, OriginExactResult):
            raise Blocked('Exact selected result required')
        payload = self.backend.retrieve(result.guid, actor_id=result.snapshot.actor_id,
                                        profile_id=result.snapshot.profile_id)
        if result.snapshot.scope == 'album_flac':
            from shelf.nzb_parser import validate_nzb
            validate_nzb(payload, album=True)
        return RetrievedNZB(result, payload)


class UnavailableRetriever:
    def retrieve(self, result):
        raise Blocked('Hydra exact-result retrieval is not configured')


def owned_job(actor, profile_id, job_id):
    saved.authority(actor, profile_id)
    try:
        return AcquisitionJob.objects.get(pk=job_id, actor=actor, profile_id=profile_id)
    except AcquisitionJob.DoesNotExist:
        raise Http404 from None


def exact_result(job, *, fresh):
    try:
        selection = ObtainSelection.objects.select_related('result__preview').get(job=job)
    except ObtainSelection.DoesNotExist:
        raise Blocked('A saved item alone is not an exact result selection') from None
    result, receipt = selection.result, selection.result.preview
    if job.publication_origin is not None:
        return _origin_exact_result(job, receipt, result, fresh=fresh)
    snapshot = Snapshot.from_saved_job(job)
    if job.provider == 'album_request':
        from shelf.album_acquisition import current_request
        album = current_request(job.actor, job.profile_id, receipt)
        if result.metadata.get('album_identity') != album.identity:
            raise ContractError('Selected album identity changed')
    if ((receipt.actor_id, receipt.profile_id, str(receipt.pk)) !=
            (job.actor_id, job.profile_id, snapshot.request_id)
            or any(getattr(receipt, key) != getattr(job, key)
                   for key in ('provider', 'medium', 'source_id', 'scope', 'title'))
            or result.guid != result.metadata['guid'] or digest(result.metadata) != result.digest
            or result.metadata['selectable'] is not True
            or listing_reason(result.metadata, receipt.title, receipt.scope)
            or job.edition != result.metadata['title'] or result.metadata['size'] <= 0):
        raise ContractError('Exact selection identity conflict')
    if fresh:
        preview.current_item(job.actor, job.profile_id, receipt)
        if receipt.state not in ('results', 'partial') or receipt.expires_at <= timezone.now():
            raise Blocked('Fresh exact result review required')
    return ExactResult(snapshot, str(result.pk), result.guid, result.digest, result.metadata['size']), receipt


def _origin_exact_result(job, receipt, result, *, fresh):
    from .portable_obtain_origin import resolve_obtain_origin_again
    access = active_portable_access()
    if access is None:
        raise Blocked('Current native source selection required')
    access.require(actor_id=job.actor_id, profile_id=job.profile_id, action='export')
    target = resolve_obtain_origin_again(access, job.publication_origin, job.scope, access.authority.now())
    target.require_title_export()
    if ((receipt.actor_id, receipt.profile_id, str(receipt.pk), receipt.scope, receipt.publication_origin) !=
            (job.actor_id, job.profile_id, str(job.request_id), job.scope, job.publication_origin)
            or any(getattr(row, key) is not None for row in (job, receipt)
                   for key in ('provider', 'medium', 'source_id', 'title'))
            or job.saved_item_id is not None or receipt.saved_identity is not None
            or result.guid != result.metadata['guid'] or digest(result.metadata) != result.digest
            or result.metadata['selectable'] is not True
            or listing_reason(result.metadata, preview.release_title(target), job.scope)
            or job.edition != result.metadata['title']):
        raise ContractError('Exact source selection identity conflict')
    if fresh and (receipt.state not in ('results', 'partial') or receipt.expires_at <= access.authority.now()):
        raise Blocked('Fresh exact source result required')
    snapshot = Snapshot(job.actor_id, job.profile_id, str(job.request_id), target.provider, target.medium,
        target.source_id, job.edition, job.scope, job.storage, job.authorized_content, job.storage_check)
    return OriginExactResult(snapshot, str(result.pk), result.guid, result.digest, result.metadata['size'],
                             job.publication_origin), receipt


def review_submission(*, actor, profile_id, job_id, context, evidence, max_bytes,
                      retriever=None, complete_root=None, test_only=True, completion_intent=None, delivery_binding=None):
    job = owned_job(actor, profile_id, job_id)
    if job.publication_origin is not None:
        _require_source_context(context, actor, profile_id, job, delivery_binding=delivery_binding)
    else:
        context.require(actor, profile_id)
    if job.status != 'blocked_configuration' or ObtainTransfer.objects.filter(job=job).exists():
        raise Blocked('Submission already recorded or cancelled')
    exact, receipt = exact_result(job, fresh=True)
    if type(evidence) is not Evidence or evidence.snapshot != exact.snapshot:
        raise ContractError('Independent matching verification required')
    exact.snapshot.check_selection(evidence.selection)
    if type(max_bytes) is not int or not exact.size <= max_bytes <= MAX_LOCAL_BYTES:
        raise ContractError('Explicit bounded size approval required')
    source = retriever or UnavailableRetriever()
    # Runtime stores the exact reviewed bytes; injected legacy/test retrievers
    # retain their explicit retrieval contract. Confirmation never normalizes NZBs.
    # Review can persist/prune descriptors: check live PA1 under its owner fence
    # before the descriptor/transport effect, with no enclosing SQL transaction.
    with effect_for(actor, profile_id):
        if job.publication_origin is not None:
            _require_source_context(context, actor, profile_id, job, delivery_binding=delivery_binding)
        nzb = getattr(source, 'review', source.retrieve)(exact)
    if type(nzb) is not RetrievedNZB:
        raise ContractError('Exact result NZB binding required')
    if job.publication_origin is not None:
        _require_source_context(context, actor, profile_id, job, delivery_binding=delivery_binding)
    else:
        context.require(actor, profile_id)
    job = owned_job(actor, profile_id, job_id)
    current, current_receipt = exact_result(job, fresh=True)
    if (current != exact or job.status != 'blocked_configuration'
            or current_receipt.nonce != receipt.nonce or current_receipt.version != receipt.version
            or ObtainTransfer.objects.filter(job=job).exists()):
        raise Blocked('Selection changed during retrieval')
    binding = {'exact': exact.material(), 'selection': asdict(evidence.selection),
               'nzb_sha256': nzb.sha256(exact), 'max_bytes': max_bytes,
               'nonce': str(receipt.nonce), 'version': receipt.version,
               'complete_root': complete_root, 'test_only': test_only}
    if type(exact) is OriginExactResult:
        binding = _origin_submission_binding(binding, exact, evidence, delivery_binding)
    if job.provider == 'album_request':
        from shelf.album_acquisition import authorization_binding
        binding['album_authorization'] = authorization_binding(job)
    if completion_intent is not None:
        binding['completion_intent'] = completion_intent
        check_completion_intent(binding, before_send=True)
    return {'token': signing.dumps(binding, salt=SALT), 'binding': binding}


def _origin_submission_binding(binding, exact, evidence, delivery_binding):
    from .disclosure import canonical_bytes
    from .obtain_tv_flow import delivery_binding_material
    result = {**binding, 'schema': ORIGIN_SUBMISSION_TAGS[exact.publication_origin['schema']],
        'publication_origin': exact.publication_origin, 'request_id': exact.snapshot.request_id,
        'snapshot_digest': exact.snapshot_digest, 'content_identity_digest': exact.content_identity_digest,
        'selection_digest': hashlib.sha256(canonical_bytes(asdict(evidence.selection))).hexdigest(),
        'delivery_binding': delivery_binding_material(delivery_binding),
        'exact': {key: getattr(exact, key) for key in ('result_id', 'guid', 'digest', 'size')},
        'selection': {'edition': evidence.selection.edition, 'storage': evidence.selection.storage}}
    validate_origin_submission(result)
    return result


def validate_origin_submission(binding):
    """Closed structural receipt, never authority to reconstruct a source target."""
    from .portable_obtain_origin import validate_obtain_origin
    from .disclosure import canonical_bytes
    fields = {'schema', 'publication_origin', 'request_id', 'snapshot_digest', 'selection_digest',
        'content_identity_digest', 'exact', 'selection', 'nzb_sha256', 'max_bytes', 'nonce', 'version',
        'complete_root', 'test_only'}
    allowed = tuple(fields | extra for extra in (set(), {'completion_intent'}, {'delivery_binding'},
                                                 {'completion_intent', 'delivery_binding'}))
    if type(binding) is not dict or set(binding) not in allowed:
        raise ContractError('Closed source submission required')
    try:
        origin = validate_obtain_origin(binding['publication_origin'])
        valid = binding['schema'] == ORIGIN_SUBMISSION_TAGS[origin['schema']]
        for name in ('request_id', 'nonce'):
            valid = valid and type(binding[name]) is str and str(UUID(binding[name])) == binding[name] and UUID(binding[name]).int != 0
        for name in ('snapshot_digest', 'selection_digest', 'content_identity_digest', 'nzb_sha256'):
            valid = valid and type(binding[name]) is str and re.fullmatch('[0-9a-f]{64}', binding[name]) is not None
        exact = binding['exact']; choice = binding['selection']
        valid = valid and type(exact) is dict and set(exact) == {'result_id', 'guid', 'digest', 'size'}
        valid = valid and str(UUID(exact['result_id'])) == exact['result_id'] and UUID(exact['result_id']).int != 0
        valid = valid and type(exact['guid']) is str and re.fullmatch(r'-?(?:0|[1-9][0-9]{0,18})', exact['guid']) is not None
        valid = valid and -(2**63) <= int(exact['guid']) < 2**63 and str(int(exact['guid'])) == exact['guid']
        valid = valid and type(exact['digest']) is str and re.fullmatch('[0-9a-f]{64}', exact['digest']) is not None
        valid = valid and type(exact['size']) is int and 0 < exact['size'] <= binding['max_bytes'] <= MAX_LOCAL_BYTES
        valid = valid and type(binding['max_bytes']) is int and type(binding['version']) is int and 1 <= binding['version'] < 2**53
        valid = valid and type(binding['test_only']) is bool
        valid = valid and (binding['complete_root'] is None or type(binding['complete_root']) is str
                          and 0 < len(binding['complete_root']) <= 4096)
        valid = valid and type(choice) is dict and set(choice) == {'edition', 'storage'}
        valid = valid and preview.plain(choice['edition']) == choice['edition'] and choice['storage'] == 'local_library'
        if origin['schema'] == 'obtain-owner-selection-origin/v1':
            valid = valid and origin['target_digest'] == binding['content_identity_digest']
        if 'delivery_binding' in binding:
            from .obtain_tv_flow import read_delivery_binding
            read_delivery_binding(binding['delivery_binding'])
        valid = valid and len(canonical_bytes(binding)) <= 6144
    except (ValueError, KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ContractError('Source submission identity conflict')
    return binding


def retained_origin_submission(*, actor, profile_id, job_id, token):
    job = owned_job(actor, profile_id, job_id)
    if job.publication_origin is None:
        return None
    access = active_portable_access()
    if access is None:
        raise Blocked('Current native job access required')
    access.require(actor_id=job.actor_id, profile_id=job.profile_id, action='view')
    transfer = ObtainTransfer.objects.filter(job=job).first()
    if transfer is None:
        return None
    # MAC authenticity, not token freshness, is enough to identify an existing
    # read-only receipt. This path cannot acquire services, repair or resend.
    try:
        if type(token) is not str or not 0 < len(token) <= 8192:
            raise signing.BadSignature()
        binding = validate_origin_submission(signing.loads(token, salt=SALT))
    except signing.BadSignature:
        raise Blocked('Invalid retained submission review') from None
    if (binding != transfer.binding or binding['publication_origin'] != job.publication_origin
            or binding['request_id'] != str(job.request_id)):
        raise ContractError('Retained source submission conflict')
    return transfer


class DjangoCheckpoints:
    """CAS owns each dispatch transition; stale owners fail before sending.

    A committed outcome_unknown precedes addfile. There is deliberately no lease
    expiry or automatic takeover/retry of an uncertain send. Only dispatch uses
    Pipeline here; file handoff is a separate local receipt, never Emby available.
    """
    def __init__(self, transfer, *, trusted_snapshot=None):
        self.transfer = transfer
        if transfer.job.publication_origin is not None:
            validate_origin_submission(transfer.binding)
            from .disclosure import canonical_bytes
            if (type(trusted_snapshot) is not Snapshot or hashlib.sha256(canonical_bytes(asdict(trusted_snapshot))).hexdigest()
                    != transfer.binding['snapshot_digest']
                    or (trusted_snapshot.actor_id, trusted_snapshot.profile_id, trusted_snapshot.request_id) !=
                       (transfer.job.actor_id, transfer.job.profile_id, str(transfer.job.request_id))):
                raise Blocked('Fresh trusted source snapshot required for dispatch')
            self.snapshot = trusted_snapshot
        else:
            if trusted_snapshot is not None:
                raise ContractError('Legacy checkpoint cannot borrow source identity')
            self.snapshot = Snapshot(**transfer.binding['exact']['snapshot'])
        self.revision = transfer.revision

    def load(self, request_id):
        self.transfer.refresh_from_db()
        if request_id != self.snapshot.request_id:
            raise ContractError('Checkpoint request mismatch')
        self.revision = self.transfer.revision
        from shelf.acquisition_contract import ConfirmedSelection
        if self.transfer.job.publication_origin is not None:
            from .disclosure import canonical_bytes
            selection = _source_selection(self.snapshot)
            if hashlib.sha256(canonical_bytes(asdict(selection))).hexdigest() != self.transfer.binding['selection_digest']:
                raise ContractError('Source checkpoint selection conflict')
        else:
            selection = ConfirmedSelection(**self.transfer.binding['selection'])
        evidence = Evidence(self.snapshot, selection)
        job = ProviderJob(self.snapshot, self.transfer.provider_job) if self.transfer.provider_job else None
        return Checkpoint(self.snapshot, self.transfer.state, evidence=evidence,
                          job=job, reason=self.transfer.reason)

    def save(self, checkpoint):
        if checkpoint.snapshot != self.snapshot:
            raise ContractError('Checkpoint identity mismatch')
        with write_for(self.transfer.job.actor, self.transfer.job.profile_id, durable=True):
            updated = ObtainTransfer.objects.filter(pk=self.transfer.pk, revision=self.revision).update(
                state=checkpoint.state, reason=checkpoint.reason,
                provider_job=checkpoint.job.job_id if checkpoint.job else '', revision=F('revision') + 1)
            if updated != 1:
                raise ContractError('Concurrent checkpoint changed; reload status')
        self.revision += 1


def submit(*, actor, profile_id, job_id, context, token, confirmed, retriever=None, backend,
           max_bytes=None, complete_root=None, test_only=True, completion_intent=None, delivery_binding=None):
    job = owned_job(actor, profile_id, job_id)
    if job.publication_origin is not None:
        if confirmed is not True:
            raise Blocked('Explicit source submission confirmation required')
        previous = retained_origin_submission(actor=actor, profile_id=profile_id, job_id=job_id, token=token)
        if previous is not None:
            return previous
        return _submit_origin(actor=actor, profile_id=profile_id, job=job, context=context, token=token,
            retriever=retriever, backend=backend, max_bytes=max_bytes, complete_root=complete_root,
            test_only=test_only, completion_intent=completion_intent, delivery_binding=delivery_binding)
    context.require(actor, profile_id)
    if type(token) is not str or not 0 < len(token) <= 8192:
        raise Blocked('Invalid submission review')
    if confirmed is not True:
        raise Blocked('Fresh explicit rights, exact selection, scope and size confirmation required')
    try:
        binding = signing.loads(token, salt=SALT, max_age=300)
    except signing.BadSignature:
        raise Blocked('Submission review expired or invalid') from None
    if max_bytes is not None and (binding.get('max_bytes') != max_bytes
            or binding.get('complete_root') != complete_root or binding.get('test_only') != test_only):
        raise Blocked('Storage policy changed; review again')
    if binding.get('completion_intent') != completion_intent:
        raise Blocked('Completion policy changed; review again')
    job = owned_job(actor, profile_id, job_id)
    exact, receipt = exact_result(job, fresh=False)
    check_album_authorization(job, binding)
    if (binding.get('exact') != asdict(exact) or binding.get('nonce') != str(receipt.nonce)
            or binding.get('version') != receipt.version):
        raise ContractError('Submission review identity conflict')
    previous = ObtainTransfer.objects.filter(job=job).first()
    if previous:
        if previous.binding != binding:
            raise ContractError('Submission binding conflict')
        with effect_for(actor, profile_id):
            repair_registration(previous)
        return previous  # includes unknown, failed and cancelled; never a retry
    if job.status != 'blocked_configuration':
        raise Blocked('Selection cancelled')
    if completion_intent is not None:
        check_completion_intent(binding, before_send=True)
    exact_result(job, fresh=True)
    with effect_for(actor, profile_id):
        nzb = (retriever or UnavailableRetriever()).retrieve(exact)
    if type(nzb) is not RetrievedNZB or nzb.sha256(exact) != binding['nzb_sha256']:
        raise ContractError('Reviewed NZB changed')
    # Serialize with saved.cancel and competing submitters using the existing job.
    # No network operation occurs inside this durable transaction.
    with write_for(actor, profile_id, durable=True):
        AcquisitionJob.objects.filter(pk=job.pk).update(reason=F('reason'))
        job = owned_job(actor, profile_id, job_id)
        current, current_receipt = exact_result(job, fresh=True)
        check_album_authorization(job, binding)
        if (job.status != 'blocked_configuration' or current != exact
                or str(current_receipt.nonce) != binding['nonce']
                or current_receipt.version != binding['version']):
            raise Blocked('Selection changed or cancelled')
        transfer, created = ObtainTransfer.objects.get_or_create(job=job, defaults={
            'binding': binding, 'result_guid': exact.guid, 'nzb_sha256': binding['nzb_sha256']})
        if not created:
            if transfer.binding != binding:
                raise ContractError('Submission binding conflict')
            return transfer
    # Hold the existing recovery/owner fence through provider construction, send
    # and durable outcome. No SQL transaction encloses the external operation.
    with effect_for(actor, profile_id):
        checkpoints = DjangoCheckpoints(transfer)
        evidence = checkpoints.load(exact.snapshot.request_id).evidence
        provider = backend.provider(actor_id=actor.pk, profile_id=profile_id,
            snapshot=exact.snapshot, selection=evidence.selection, nzb=nzb.payload)
        pipeline = Pipeline(job, checkpoints, provider, None, None)
        context.require(actor, profile_id)
        current_job = owned_job(actor, profile_id, job_id)
        current, current_receipt = exact_result(current_job, fresh=True)
        check_album_authorization(current_job, binding)
        if (current != exact or str(current_receipt.nonce) != binding['nonce']
                or current_receipt.version != binding['version']):
            raise Blocked('Selection changed before dispatch')
        if completion_intent is not None:
            check_completion_intent(binding, before_send=True)
        pipeline.dispatch(evidence)
        transfer.refresh_from_db()
        repair_registration(transfer)
    return transfer


def check_completion_intent(binding, *, before_send=False):
    """Only the current approved fixed layout can realize this persisted intent."""
    from shelf.obtain_runtime import operator_policy
    policy = operator_policy()
    if 'schema' in binding:
        validate_origin_submission(binding)
        request_id = binding['request_id']
    else:
        request_id = binding['exact']['snapshot']['request_id']
    intent = policy.intent(request_id)
    if (binding.get('completion_intent') != intent
            or binding.get('complete_root') != policy.complete_root
            or binding.get('max_bytes') != policy.max_bytes
            or binding.get('test_only') != policy.test_only):
        raise Blocked('Completion policy or binding changed')
    try:
        registry.check_destination(policy.registry_path, policy.complete_root,
                                   intent['nzbname'], before_send=before_send)
    except (OSError, ValueError):
        raise Blocked('Completion destination unavailable') from None
    return intent


def _source_selection(snapshot):
    from .acquisition_contract import ConfirmedSelection
    from .acquisition_pipeline import scope_family
    media, episode = scope_family(snapshot.provider, snapshot.medium, snapshot.scope)
    return ConfirmedSelection(item_id=f'{snapshot.provider}:{snapshot.medium}:{snapshot.source_id}',
        media=media, edition=snapshot.edition, storage=snapshot.storage,
        authorized_content=snapshot.authorized_content, user_confirmed=True,
        storage_checked=snapshot.storage_check, duplicate_checked=True, episode=episode)


def _require_source_context(context, actor, profile_id, job, *, delivery_binding):
    from .obtain_runtime import SubmissionContext
    from .obtain_tv_flow import delivery_binding_material
    if type(context) is not SubmissionContext or context.job_id != job.pk:
        raise Blocked('Current native submission context required')
    context.require(actor, profile_id)
    reviewed = delivery_binding_material(delivery_binding)
    if delivery_binding_material(context.current_delivery_binding(actor, profile_id)) != reviewed:
        raise Blocked('Confirmed TV delivery mapping changed; review again')


def _submit_origin(*, actor, profile_id, job, context, token, retriever, backend, max_bytes,
                   complete_root, test_only, completion_intent, delivery_binding):
    access = active_portable_access()
    if access is None:
        raise Blocked('Current native source access required')
    if type(token) is not str or not 0 < len(token) <= 8192:
        raise Blocked('Invalid source submission review')
    try:
        binding = validate_origin_submission(signing.loads(token, salt=SALT, max_age=300))
    except signing.BadSignature:
        raise Blocked('Source submission review expired or invalid') from None
    with access.effect_window(action='mutate'):
        _require_source_context(context, actor, profile_id, job, delivery_binding=delivery_binding)
        exact, receipt = exact_result(job, fresh=True)
        evidence = Evidence(exact.snapshot, _source_selection(exact.snapshot))
        expected = _origin_submission_binding({key: binding[key] for key in
            ('nzb_sha256', 'max_bytes', 'nonce', 'version', 'complete_root', 'test_only')}, exact, evidence, delivery_binding)
        if completion_intent is not None:
            expected['completion_intent'] = completion_intent
        if (binding != expected or binding['nonce'] != str(receipt.nonce) or binding['version'] != receipt.version
                or binding.get('completion_intent') != completion_intent
                or max_bytes is not None and (max_bytes, complete_root, test_only) !=
                    (binding['max_bytes'], binding['complete_root'], binding['test_only'])
                or job.status != 'blocked_configuration'):
            raise ContractError('Current source submission review conflict')
        if completion_intent is not None:
            check_completion_intent(binding, before_send=True)
        nzb = (retriever or UnavailableRetriever()).retrieve(exact)
        if type(nzb) is not RetrievedNZB or nzb.sha256(exact) != binding['nzb_sha256']:
            raise ContractError('Reviewed source NZB changed')
        _require_source_context(context, actor, profile_id, job, delivery_binding=delivery_binding)
        current, current_receipt = exact_result(owned_job(actor, profile_id, job.pk), fresh=True)
        if current != exact or (current_receipt.nonce, current_receipt.version) != (receipt.nonce, receipt.version):
            raise Blocked('Source selection changed during retrieval')
        with write_for(actor, profile_id, durable=True):
            AcquisitionJob.objects.filter(pk=job.pk).update(reason=F('reason'))
            fresh_job = owned_job(actor, profile_id, job.pk)
            if fresh_job.status != 'blocked_configuration' or fresh_job.publication_origin != exact.publication_origin:
                raise Blocked('Source choice cancelled or changed')
            transfer, created = ObtainTransfer.objects.get_or_create(job=fresh_job, defaults={
                'binding': binding, 'result_guid': exact.guid, 'nzb_sha256': binding['nzb_sha256']})
            if not created:
                if transfer.binding != binding:
                    raise ContractError('Concurrent source submission conflict')
                return transfer
        checkpoints = DjangoCheckpoints(transfer, trusted_snapshot=exact.snapshot)
        provider = backend.provider(actor_id=actor.pk, profile_id=profile_id, snapshot=exact.snapshot,
                                    selection=evidence.selection, nzb=nzb.payload)
        raw_provider = provider
        class OriginDispatchProvider:
            def enqueue(self, snapshot, request):
                # Pipeline commits its no-resend barrier before invoking us.
                # Recheck at this final edge too; expiry never rolls that
                # durable barrier back into an implicitly retryable request.
                _require_source_context(context, actor, profile_id, fresh_job, delivery_binding=delivery_binding)
                latest, _ = exact_result(owned_job(actor, profile_id, job.pk), fresh=True)
                if latest != exact or snapshot != exact.snapshot:
                    raise Blocked('Source authority changed at dispatch')
                return raw_provider.enqueue(snapshot, request)
        provider = OriginDispatchProvider()
        pipeline = Pipeline(fresh_job, checkpoints, provider, None, None, trusted_snapshot=exact.snapshot)
        _require_source_context(context, actor, profile_id, job, delivery_binding=delivery_binding)
        before, _ = exact_result(owned_job(actor, profile_id, job.pk), fresh=True)
        if before != exact:
            raise Blocked('Source selection changed before dispatch')
        if completion_intent is not None:
            check_completion_intent(binding, before_send=True)
        pipeline.dispatch(evidence)
        transfer.refresh_from_db()
        repair_registration(transfer)
        return transfer


def source_receipt(transfer):
    """Read independent retained release/command evidence after source expiry."""
    binding = validate_origin_submission(transfer.binding)
    job = transfer.job
    try:
        choice = ObtainSelection.objects.select_related('result__preview').get(job=job)
    except ObtainSelection.DoesNotExist:
        raise ContractError('Retained selected release missing') from None
    result, receipt = choice.result, choice.result.preview
    if (job.publication_origin != binding['publication_origin'] or str(job.request_id) != binding['request_id']
            or (receipt.actor_id, receipt.profile_id, str(receipt.pk), receipt.scope, receipt.publication_origin) !=
               (job.actor_id, job.profile_id, str(job.request_id), job.scope, job.publication_origin)
            or result.metadata['selectable'] is not True or digest(result.metadata) != result.digest
            or binding['exact'] != {'result_id': str(result.pk), 'guid': result.guid,
                                   'digest': result.digest, 'size': result.metadata['size']}
            or binding['selection'] != {'edition': job.edition, 'storage': job.storage}
            or job.edition != result.metadata['title'] or transfer.result_guid != result.guid
            or binding['nzb_sha256'] != transfer.nzb_sha256 or binding['nonce'] != str(receipt.nonce)
            or binding['version'] != receipt.version):
        raise ContractError('Retained source command identity conflict')
    return binding


def completion_registration(*, actor, profile_id, job_id, context, include_failed=False):
    """Build the hook binding only from a durable acknowledged exact intent."""
    job = owned_job(actor, profile_id, job_id)
    if job.publication_origin is not None:
        from .obtain_native_context import NativeJobContext
        if type(context) is not NativeJobContext or context.operation != 'observe' or context.job_id != job.pk:
            raise Blocked('Current native observation context required')
        context.require(actor, profile_id)
        transfer = ObtainTransfer.objects.get(job=job)
        return _source_completion_registration(job, transfer, include_failed=include_failed)
    context.require(actor, profile_id)
    transfer = ObtainTransfer.objects.get(job=job)
    exact, _ = exact_result(job, fresh=False)
    check_album_authorization(job, transfer.binding)
    from shelf.acquisition_contract import ConfirmedSelection
    exact.snapshot.check_selection(ConfirmedSelection(**transfer.binding['selection']))
    intent = check_completion_intent(transfer.binding)
    root = transfer.binding.get('complete_root')
    state_allowed = transfer.state == 'queued' or (include_failed is True
        and transfer.state == 'failed' and transfer.reason == 'provider_failed')
    if (job.status != 'blocked_configuration' or not state_allowed or not transfer.provider_job
            or transfer.local_cancelled_at is not None
            or transfer.binding['exact'] != asdict(exact) or not root
            or transfer.result_guid != exact.guid
            or transfer.nzb_sha256 != transfer.binding['nzb_sha256']):
        raise Blocked('Known job directory under approved Complete required')
    return {'nzo_id': transfer.provider_job, 'request_id': str(job.request_id),
        'result_id': exact.result_id, 'result_digest': exact.digest,
        'nzb_sha256': transfer.nzb_sha256, 'scope': job.scope,
        'complete_root': root, 'root': intent['root'], 'files': None,
        'max_bytes': transfer.binding['max_bytes'], 'test_only': transfer.binding['test_only']}


def _source_completion_registration(job, transfer, *, include_failed):
    binding = source_receipt(transfer)
    intent = check_completion_intent(binding)
    allowed = transfer.state == 'queued' or include_failed is True and transfer.state == 'failed' and transfer.reason == 'provider_failed'
    if (job.status != 'blocked_configuration' or not allowed or not transfer.provider_job
            or transfer.local_cancelled_at is not None or not binding['complete_root']):
        raise Blocked('Known selected source job required')
    return {'nzo_id': transfer.provider_job, 'request_id': str(job.request_id),
        'result_id': binding['exact']['result_id'], 'result_digest': binding['exact']['digest'],
        'nzb_sha256': transfer.nzb_sha256, 'scope': job.scope, 'complete_root': binding['complete_root'],
        'root': intent['root'], 'files': None, 'max_bytes': binding['max_bytes'], 'test_only': binding['test_only']}


def check_album_authorization(job, binding):
    if job.provider == 'album_request':
        from shelf.album_acquisition import authorization_binding
        if binding.get('album_authorization') != authorization_binding(job):
            raise ContractError('Album authorization changed')


def repair_registration(transfer):
    """Retry only local publication, including after restart/status; never addfile.

    The accepted provider ID and intent were committed first. Failure leaves that
    ownership intact; unknown acknowledgements have no registration to publish.
    """
    if (transfer.state != 'queued' or not transfer.provider_job
            or transfer.local_cancelled_at is not None
            or 'completion_intent' not in transfer.binding):
        return
    job = transfer.job
    try:
        access = active_portable_access()
        if access is None:
            if job.publication_origin is not None:
                return
            context = OperatorContext(job.actor_id, job.profile_id, 'candidate_operator', True)
        else:
            from .obtain_native_context import native_job_context
            context = native_job_context(access, job_id=job.pk, operation='observe')
        if access is not None:
            with context.effect_window():
                entry = completion_registration(actor=job.actor, profile_id=job.profile_id,
                                                job_id=job.pk, context=context)
                registry.publish_registration(transfer.binding['completion_intent']['registry_path'], entry)
        else:
            entry = completion_registration(actor=job.actor, profile_id=job.profile_id,
                                            job_id=job.pk, context=context)
            registry.publish_registration(transfer.binding['completion_intent']['registry_path'], entry)
    except (ContractError, OSError, ValueError, KeyError, Http404):
        reason = 'registration_pending'
    else:
        reason = ''
    if transfer.reason != reason:
        with write_for(job.actor, job.profile_id):
            ObtainTransfer.objects.filter(pk=transfer.pk, state='queued',
                revision=transfer.revision, local_cancelled_at__isnull=True).update(
                    reason=reason, revision=F('revision') + 1)
        transfer.refresh_from_db()


def cancel_local(job):
    """Called under saved.cancel's job write lock. Never invokes SAB cancellation."""
    transfer = ObtainTransfer.objects.filter(job=job).first()
    if transfer is None:
        return False
    if transfer.local_cancelled_at is not None:
        return True
    local_only = transfer.state == 'blocked' or (
        transfer.state == 'failed' and transfer.reason == 'queue_rejected')
    state = 'cancelled' if local_only else 'cancel_pending'
    if ObtainLocalFile.objects.filter(transfer=transfer).exists():
        state = transfer.state
    ObtainTransfer.objects.filter(pk=transfer.pk).update(state=state,
        local_cancelled_at=timezone.now(), reason=(
            'cancelled_before_dispatch' if local_only else 'local_cancelled_remote_unconfirmed'),
        revision=F('revision') + 1)
    # Retain active identity after any possible send, even a lost acknowledgement.
    if state == 'cancelled':
        AcquisitionJob.objects.filter(pk=job.pk).update(
            status='cancelled', active_key=None, cancelled_at=timezone.now())
    return True


@dataclass(frozen=True)
class LocalCompletion:
    completion: Completion
    result_digest: str
    nzb_sha256: str
    expected_path: str
    acquired_at: datetime | None = None
    playback_verified: bool = False
    metadata: dict | None = None


def _local_metadata(evidence):
    from shelf.acquisition_contract import _text
    metadata = {} if evidence.metadata is None else evidence.metadata
    allowed = {'source', 'artist', 'artist_id', 'track_title', 'track_id', 'album',
               'acquired_at_basis'}
    if (type(evidence.playback_verified) is not bool or type(metadata) is not dict
            or set(metadata) - allowed or (metadata and not metadata.get('source'))):
        raise ContractError('Trusted bounded media metadata required')
    for value in metadata.values():
        if _text(value) != value:
            raise ContractError('Trusted bounded media metadata required')
    acquired_at = evidence.acquired_at
    if acquired_at is not None and (type(acquired_at) is not datetime
            or timezone.is_naive(acquired_at) or acquired_at > timezone.now()):
        raise ContractError('Invalid acquisition time')
    if acquired_at is not None and (not metadata.get('source')
            or metadata.get('acquired_at_basis') not in
            {'sab_history_completed_at', 'authoritative_acquisition_record'}):
        raise ContractError('Authoritative acquisition timestamp source and basis required')
    return dict(metadata)


def handoff(*, actor, profile_id, job_id, context, evidence):
    context.require(actor, profile_id)
    job = owned_job(actor, profile_id, job_id)
    transfer = ObtainTransfer.objects.get(job=job)
    exact, _ = exact_result(job, fresh=False)
    if (transfer.binding['exact'] != asdict(exact) or transfer.result_guid != exact.guid
            or transfer.nzb_sha256 != transfer.binding['nzb_sha256']):
        raise ContractError('Stored submission identity conflict')
    if (type(evidence) is not LocalCompletion or not transfer.provider_job
            or transfer.state not in {'queued', 'local_verified'}
            or transfer.local_cancelled_at is not None):
        raise Blocked('Known active provider job and trusted local completion required')
    metadata = _local_metadata(evidence)
    completion = evidence.completion
    if type(completion) is not Completion:
        raise ContractError('Trusted completion evidence required')
    expected_job = ProviderJob(exact.snapshot, transfer.provider_job)
    staged = completion.staged
    if (completion.job != expected_job or type(staged) is not StagedFile or staged.job != expected_job
            or evidence.result_digest != exact.digest
            or evidence.nzb_sha256 != transfer.binding['nzb_sha256']
            or staged.path != evidence.expected_path):
        raise ContractError('Completion provenance mismatch')
    if (completion.final_status != 'completed' or completion.repair_ok is not True
            or completion.unpack_ok is not True or type(completion.postprocess_status) is not int
            or completion.postprocess_status != 0):
        ObtainTransfer.objects.filter(pk=transfer.pk, revision=transfer.revision).update(
            state='failed', reason='provider_failed', revision=F('revision') + 1)
        raise ContractError('Provider completion failed')
    root = transfer.binding.get('complete_root')
    if root and not staged.path.startswith(root + '/'):
        raise ContractError('Local file is outside approved Complete root')
    verify_staging(staged, max_bytes=transfer.binding['max_bytes'])
    context.require(actor, profile_id)
    values = {'path': staged.path, 'sha256': staged.sha256, 'size': staged.size,
              'metadata': metadata, 'playback_verified': evidence.playback_verified}
    if evidence.acquired_at is not None:
        values['acquired_at'] = evidence.acquired_at
    with transaction.atomic(durable=True):
        changed = ObtainTransfer.objects.filter(pk=transfer.pk, revision=transfer.revision,
            local_cancelled_at__isnull=True).update(state='local_verified', reason='awaiting_library_index',
                                                  revision=F('revision') + 1)
        if changed != 1:
            raise ContractError('Concurrent handoff changed; reload status')
        local, created = ObtainLocalFile.objects.get_or_create(transfer=transfer, defaults=values)
        if any(getattr(local, key) != value for key, value in values.items()):
            raise ContractError('Local handoff identity conflict')
        if not created:
            local.verified_at = timezone.now()
            local.save(update_fields=['verified_at'])
    return local


def export_local(*, actor, profile_id, job_id):
    """Point-in-time receipt for Kodi; no file access, player action or history."""
    job = owned_job(actor, profile_id, job_id)
    transfer = transfer_for(job)
    if transfer is None or transfer.state != 'local_verified':
        raise Blocked('Verified local receipt required')
    exact, _ = exact_result(job, fresh=False)
    if transfer.binding['exact'] != asdict(exact):
        raise ContractError('Local receipt identity conflict')
    try:
        local = ObtainLocalFile.objects.get(transfer=transfer)
    except ObtainLocalFile.DoesNotExist:
        raise Blocked('Verified local receipt required') from None
    selection = transfer.binding['selection']
    time_basis = local.metadata.get('acquired_at_basis')
    acquired_at = local.acquired_at if local.metadata.get('source') and time_basis in {
        'sab_history_completed_at', 'authoritative_acquisition_record'} else None
    root = transfer.binding.get('complete_root')
    relative = None
    if root:
        try:
            relative = str(Path(local.path).relative_to(root))
        except ValueError:
            raise ContractError('Local receipt is outside approved Complete root') from None
    return {'version': 2, 'state': 'local_verified',
        'media_identity': f'sha256:{local.sha256}',
        'catalogue_identity': f'{job.provider}:{job.medium}:{job.source_id}:{job.scope}',
        'media_kind': selection['media'], 'media_kind_basis': 'selected_scope',
        'observed_media': 'unknown', 'title': job.title, 'edition': job.edition,
        'scope': job.scope, 'path': local.path, 'sha256': local.sha256, 'size': local.size,
        'relative_path': relative, 'nzo_id': transfer.provider_job or None,
        'provenance': {'request_id': str(job.request_id), 'result_id': exact.result_id,
                       'result_digest': exact.digest, 'nzb_sha256': transfer.nzb_sha256},
        'acquired_at': acquired_at.isoformat() if acquired_at else None,
        'acquired_at_basis': time_basis if acquired_at else 'unavailable',
        'test_only': transfer.binding.get('test_only', True),
        'kodi_music_catalog_eligible': False,
        'verified_at': local.verified_at.isoformat(), 'metadata': local.metadata,
        'playback_verification': 'verified_playable' if local.playback_verified else 'unverified',
        'indexed': False, 'playback_started': False, 'reverify_before_playback': True}


def transfer_for(job, *, reconcile=True):
    # Existing 0003/0005 stores remain usable without activating or migrating them.
    if ObtainTransfer._meta.db_table not in connection.introspection.table_names():
        return None
    transfer = ObtainTransfer.objects.filter(job=job).first()
    if job.publication_origin is not None:
        return transfer  # Native continuation is an explicit selected command.
    if transfer is not None:
        repair_registration(transfer)
        if reconcile:
            from shelf.completion_consumer import consume
            try:
                consume(actor=job.actor, profile_id=job.profile_id, job_id=job.pk)
            except Http404:
                pass  # Viewing an old submission does not grant import authority.
            transfer.refresh_from_db()
    return transfer
