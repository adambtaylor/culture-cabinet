"""Owner-scoped authority for the fixed anonymous Moshtix operation.

Source consent is independent of inference consent/validation. The trusted host
supplies current rights evidence; no source response, browser flag or OpenAI
credential can create those rights. Normal composition has no default provider.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
import uuid

from django.views.decorators.debug import sensitive_variables

from . import grant_authority as ga
from .disclosure import canonical_bytes
from .enhance_contract import CataloguePlan, EnhanceBindings, digest, require, utc
from .models import PortableConnection
from .source_adapters import SourceAuthorityIssuer, SourcePermit, SourceRequest
from .source_evidence import FieldPolicy, validate_moshtix_policy
from .discovery_transport import MoshtixOperation

ADAPTER = 'moshtix_public'
BUILD = 'moshtix-fixed-v1'
ORIGIN = 'https://api.moshtix.com'
PATH = '/v1/graphql'


@dataclass(frozen=True)
class SourceRights:
    """Backend evidence bindings, never an HTTP consent/permission envelope."""
    policy: FieldPolicy
    revision: int
    terms_ref: str
    privacy_ref: str
    locale_ref: str
    application_identity: str
    processor_ref: str | None = None

    def validate(self, now):
        require(type(self.policy) is FieldPolicy and self.policy.current(now), 'AUTHORITY_DENIED')
        require(type(self.revision) is int and 0 <= self.revision < 2**53)
        for value in (self.terms_ref, self.privacy_ref, self.locale_ref):
            require(type(value) is str and 0 < len(value) <= 300, 'AUTHORITY_DENIED')
        require(self.processor_ref is None or type(self.processor_ref) is str
                and 0 < len(self.processor_ref) <= 300, 'AUTHORITY_DENIED')
        MoshtixOperation(utc(now).replace(microsecond=0), 9, self.application_identity)
        return self


class SourcePolicyProvider(ABC):
    """Host-owned evidence producer; absent/unresolved evidence denies activation."""
    @abstractmethod
    def current(self, authority, session_ref, *, profile_ref, connection_ref, now): ...


def configure_source(authority, session_ref, *, confirmed, connection_ref=None,
                     expected_revision=0, locale='VIC'):
    ga._require(confirmed is True and locale == 'VIC', 'CONTRACT_INVALID', 422)
    ga._revision(expected_revision)
    with ga.recovery_guard(authority, session_ref) as checkpoint, authority._lock, ga._checked_transaction(checkpoint):
        installation, _ = ga._owner(authority, session_ref, sensitive=True)
        rows = PortableConnection.objects.filter(installation=installation, adapter=ADAPTER, removed=False)
        if connection_ref is None:
            ga._require(rows.count() <= 1, 'REVISION_CONFLICT', 409)
            row = rows.first()
            if row is not None:
                ga._require(expected_revision in (0, row.config_revision), 'REVISION_CONFLICT', 409)
                _connection(authority, session_ref, row.pk)
                return str(row.pk)
            ga._require(expected_revision == 0, 'REVISION_CONFLICT', 409)
            checkpoint.check()
            row = PortableConnection.objects.create(id=uuid.uuid4(), installation=installation,
                adapter=ADAPTER, model='', reasoning='', credential_generation=0)
            return str(row.pk)
        row, _, _ = _connection(authority, session_ref, connection_ref)
        ga._require(row.config_revision == expected_revision, 'REVISION_CONFLICT', 409)
        return str(row.pk)


def _connection(authority, session_ref, connection_ref):
    installation, session = ga._owner(authority, session_ref)
    row = PortableConnection.objects.filter(pk=ga._uuid(connection_ref), installation=installation,
                                           removed=False, adapter=ADAPTER).first()
    ga._require(row is not None, 'AUTHORITY_DENIED', 403)
    ga._require(row.model == row.reasoning == '' and row.credential_generation >= 0
                and row.validation_generation == 0, 'CAPABILITY_UNAVAILABLE', 503)
    config = {'schema': 'source-configuration/v1', 'adapter': ADAPTER, 'build': BUILD,
        'origin': ORIGIN, 'path': PATH, 'locale': 'VIC', 'credential_mode': 'none',
        'config_revision': row.config_revision, 'credential_generation': row.credential_generation}
    binding = {**config, 'installation_id': str(installation.pk), 'owner_id': session.owner_id,
        'owner_revision': session.owner_revision, 'process_generation': str(authority.boundary.process_generation),
        'session_id': session_ref, 'connection_id': str(row.pk), 'config_digest': digest(canonical_bytes(config))}
    return row, binding, {'usable_until': ga._end(authority, session), 'configured': True}


def current_binding(authority, session_ref, *, profile_ref, connection_ref):
    return ga.current_view(authority, session_ref, profile_ref=profile_ref,
                           connection_ref=connection_ref, purpose='catalogue_lookup')


@dataclass(frozen=True, repr=False)
class _Approval:
    plan: CataloguePlan
    request: SourceRequest
    operation: MoshtixOperation
    view: ga.AuthorityView
    rights: SourceRights
    expires_at: datetime


class SourceAuthority(SourceAuthorityIssuer):
    def __init__(self, authority, session_ref, *, profile_ref, connection_ref, policy_provider=None):
        ga._require(type(authority) is ga.PortableAuthority)
        ga._require(policy_provider is None or isinstance(policy_provider, SourcePolicyProvider))
        self.authority, self.session_ref = authority, session_ref
        self.profile_ref, self.connection_ref = str(ga._uuid(profile_ref)), str(ga._uuid(connection_ref))
        self.provider = policy_provider
        self._previews, self._approved, self._permits = {}, {}, {}
        authority.boundary._closing.append(self.close)

    def close(self):
        self._previews.clear(); self._approved.clear(); self._permits.clear()

    def _rights(self, now):
        require(isinstance(self.provider, SourcePolicyProvider), 'CAPABILITY_UNAVAILABLE')
        result = self.provider.current(self.authority, self.session_ref,
            profile_ref=self.profile_ref, connection_ref=self.connection_ref, now=now)
        require(type(result) is SourceRights, 'AUTHORITY_DENIED')
        return result.validate(now)

    def _view(self):
        return current_binding(self.authority, self.session_ref, profile_ref=self.profile_ref,
                               connection_ref=self.connection_ref)

    def _match(self, plan, view):
        require(type(plan) is CataloguePlan and type(view) is ga.AuthorityView)
        binding = plan.bindings
        require((binding.installation_id, binding.actor_ref, binding.profile_ref,
                 binding.snapshot_revision, binding.policy_epoch, binding.restriction_watermark) ==
                (view.installation_id, view.actor_ref, view.profile_ref, view.profile_revision_digest,
                 view.policy_epoch, view.restriction_watermark), 'PREVIEW_STALE')
        require(binding.config_revision == view.context['config_revision']
                and binding.grants_digest == digest(canonical_bytes(view.grants))
                and binding.connection_digest == digest(canonical_bytes(view.connection_binding)), 'PREVIEW_STALE')

    @sensitive_variables()
    def preview(self, plan):
        with ga.recovery_guard(self.authority, self.session_ref, profile_ref=self.profile_ref), self.authority._lock:
            ga._bound(self.authority, self.session_ref, self.profile_ref, sensitive=True)
            now = utc(self.authority.now()); view = self._view(); self._match(plan, view)
            rights = self._rights(now)
            operation = MoshtixOperation(now.replace(microsecond=0), 9, rights.application_identity)
            request = SourceRequest('moshtix', 'event', 'events')
            expires = min(plan.expires_at, view.expires_at, rights.policy.expires_at, now + timedelta(minutes=5))
            require(now < expires, 'PREVIEW_STALE')
            self._previews = {k: v for k, v in self._previews.items() if now < v.expires_at}
            self._approved = {k: v for k, v in self._approved.items() if now < v.expires_at}
            self._permits = {k: v for k, v in self._permits.items() if now < v[1].expires_at}
            require(len(self._previews) < 32, 'CAPABILITY_UNAVAILABLE')
            ref = str(uuid.uuid4())
            self._previews[ref] = _Approval(plan, request, operation, view, rights, expires)
            return {'preview_ref': ref, 'plan_ref': plan.plan_ref, 'plan_digest': plan.body_digest,
                'operation_digest': operation.operation_digest, **operation.material,
                'body': operation.body.decode('utf-8'), 'expires_at': ga._stamp(expires),
                'kind': 'catalogue', 'destination': ORIGIN, 'locale': 'VIC'}

    def confirm(self, preview_ref, *, plan_digest, operation_digest, confirmed):
        require(confirmed is True)
        with ga.recovery_guard(self.authority, self.session_ref, profile_ref=self.profile_ref), self.authority._lock:
            ga._bound(self.authority, self.session_ref, self.profile_ref, sensitive=True)
            saved = self._previews.get(str(ga._uuid(preview_ref)))
            require(saved is not None, 'PREVIEW_STALE')
            require(saved.plan.body_digest == plan_digest and saved.operation.operation_digest == operation_digest,
                    'PREVIEW_STALE')
            self._check(saved, utc(self.authority.now()))
            self._approved[saved.plan.plan_ref] = saved
            return saved.plan.plan_ref

    def _check(self, saved, now):
        current = utc(self.authority.now())
        require(utc(now) < saved.expires_at and current < saved.expires_at, 'PREVIEW_STALE')
        ga.assert_current(saved.view)
        self._match(saved.plan, saved.view)
        require(self._rights(current) == saved.rights, 'PREVIEW_STALE')
        return True

    def operation_for(self, request, plan, *, now):
        with ga.recovery_guard(self.authority, self.session_ref, profile_ref=self.profile_ref), self.authority._lock:
            saved = self._approved.get(plan.plan_ref)
            require(saved is not None and saved.plan == plan and saved.request == request, 'AUTHORITY_DENIED')
            self._check(saved, utc(now))
            return saved.operation

    def authorize(self, request, plan, operation, *, now):
        with ga.recovery_guard(self.authority, self.session_ref, profile_ref=self.profile_ref), self.authority._lock:
            require(self.operation_for(request, plan, now=now) is operation, 'AUTHORITY_DENIED')
            require(len(self._permits) < 128 and not any(saved.plan.plan_ref == plan.plan_ref
                and saved.request == request for _, saved in self._permits.values()), 'AUTHORITY_DENIED')
            saved = self._approved[plan.plan_ref]
            rights = saved.rights
            permit = SourcePermit('moshtix', ORIGIN, 'catalogue_lookup', plan.plan_ref, plan.bindings,
                request.request_digest, saved.view.context['config_revision'], rights.revision,
                rights.policy.ref, rights.policy.revision, saved.expires_at)
            validate_moshtix_policy(rights.policy, permit=permit, now=now,
                                    model_context_current=rights.processor_ref is not None)
            self._permits[id(permit)] = (permit, saved)
            return permit

    def revalidate(self, permit, request, plan, operation, *, now):
        saved = self._permits.get(id(permit))
        require(saved is not None and saved[0] is permit and saved[1].plan == plan
                and saved[1].request == request and saved[1].operation is operation, 'AUTHORITY_DENIED')
        with ga.recovery_guard(self.authority, self.session_ref, profile_ref=self.profile_ref), self.authority._lock:
            self._check(saved[1], utc(now)); permit.verify(request, plan, now)
            return True

    def policy(self, permit, operation, *, now):
        saved = self._permits.get(id(permit))
        require(saved is not None and saved[0] is permit, 'AUTHORITY_DENIED')
        self.revalidate(permit, saved[1].request, saved[1].plan, operation, now=now)
        return saved[1].rights.policy

    def model_context_current(self, permit, operation, *, now):
        self.policy(permit, operation, now=now)
        return self._permits[id(permit)][1].rights.processor_ref is not None


def authorize_request(issuer, request, plan, operation, *, now):
    require(type(issuer) is SourceAuthority, 'AUTHORITY_DENIED')
    return issuer.authorize(request, plan, operation, now=now)
