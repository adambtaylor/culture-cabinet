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
from .disclosure import canonical_bytes, read_json
from .enhance_contract import CataloguePlan, EnhanceBindings, digest, require, utc
from .models import PortableConnection
from .source_adapters import SourceAuthorityIssuer, SourcePermit, SourceRequest
from .source_evidence import FieldPolicy, validate_moshtix_policy
from .discovery_transport import MoshtixOperation
from .source_adapters import registered_source
from .secret_leases import MemoryCredentials

ADAPTER = 'moshtix_public'
BUILD = 'moshtix-fixed-v1'
ORIGIN = 'https://api.moshtix.com'
PATH = '/v1/graphql'
MEDIA = {'tmdb_movie': ('tmdb-movie-fixed-v1', 'https://api.themoviedb.org', '/3/search/movie'),
         'tmdb_tv': ('tmdb-tv-fixed-v1', 'https://api.themoviedb.org', '/3/search/tv'),
         'hardcover': ('hardcover-fixed-v1', 'https://api.hardcover.app', '/v1/graphql')}


def source_identity(adapter):
    require(adapter == ADAPTER or adapter in MEDIA, 'CAPABILITY_UNAVAILABLE')
    return (BUILD, ORIGIN, PATH) if adapter == ADAPTER else MEDIA[adapter]


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
    source: str = 'moshtix'

    def validate(self, now):
        require(type(self.policy) is FieldPolicy and self.policy.current(now), 'AUTHORITY_DENIED')
        require(self.source == 'moshtix' or self.source in MEDIA, 'AUTHORITY_DENIED')
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
                     expected_revision=0, locale='VIC', adapter=ADAPTER):
    source_identity(adapter)
    ga._require(confirmed is True and (adapter != ADAPTER or locale == 'VIC'), 'CONTRACT_INVALID', 422)
    ga._revision(expected_revision)
    with ga.recovery_guard(authority, session_ref) as checkpoint, authority._lock, ga._checked_transaction(checkpoint):
        installation, _ = ga._owner(authority, session_ref, sensitive=True)
        rows = PortableConnection.objects.filter(installation=installation, adapter=adapter, removed=False)
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
                adapter=adapter, model='', reasoning='', credential_generation=0)
            return str(row.pk)
        row, _, _ = _connection(authority, session_ref, connection_ref)
        ga._require(row.adapter == adapter, 'AUTHORITY_DENIED')
        ga._require(row.config_revision == expected_revision, 'REVISION_CONFLICT', 409)
        return str(row.pk)


def _connection(authority, session_ref, connection_ref):
    installation, session = ga._owner(authority, session_ref)
    row = PortableConnection.objects.filter(pk=ga._uuid(connection_ref), installation=installation,
                                           removed=False).first()
    ga._require(row is not None, 'AUTHORITY_DENIED', 403)
    ga._require(row.model == row.reasoning == '' and row.credential_generation >= 0
                and row.validation_generation == 0, 'CAPABILITY_UNAVAILABLE', 503)
    build, origin, path = source_identity(row.adapter)
    description = {'usable_until': ga._end(authority, session), 'configured': True}
    if row.adapter in MEDIA:
        vault = getattr(authority, '_source_vault', None)
        ga._require(type(vault) is MemoryCredentials and vault.authority is authority,
                    'CREDENTIAL_REQUIRED', 503)
        description = vault.describe(session_ref, row.pk, row.config_revision)
        ga._require(description['configured'] and description['credential_generation'] == row.credential_generation,
                    'CREDENTIAL_REQUIRED', 503)
    config = {'schema': 'source-configuration/v1', 'adapter': row.adapter, 'build': build,
        'origin': origin, 'path': path, 'locale': 'VIC' if row.adapter == ADAPTER else '',
        'credential_mode': 'none' if row.adapter == ADAPTER else 'session-memory-bearer',
        'config_revision': row.config_revision, 'credential_generation': row.credential_generation}
    binding = {**config, 'installation_id': str(installation.pk), 'owner_id': session.owner_id,
        'owner_revision': session.owner_revision, 'process_generation': str(authority.boundary.process_generation),
        'session_id': session_ref, 'connection_id': str(row.pk), 'config_digest': digest(canonical_bytes(config))}
    return row, binding, description


def current_binding(authority, session_ref, *, profile_ref, connection_ref):
    return ga.current_view(authority, session_ref, profile_ref=profile_ref,
                           connection_ref=connection_ref, purpose='catalogue_lookup')


@dataclass(frozen=True, repr=False)
class _Approval:
    plan: CataloguePlan
    request: SourceRequest
    operation: object
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
        self._derived = {}
        authority.boundary._closing.append(self.close)
        authority._revokers.append(lambda ref: self.close() if ref == self.session_ref else None)

    def close(self):
        self._previews.clear(); self._approved.clear(); self._permits.clear()
        self._derived.clear()

    def _purge_derived(self, now):
        """Expired/invalidated proposals release their IDs and approval handles.

        Cleanup is fail-closed: an unavailable issuer or revoked grant cannot
        leave an old proposal alive merely because its send would be refused.
        """
        for key, value in list(self._derived.items()):
            try:
                require(now < value[4], 'PREVIEW_STALE')
                self._check(value[1], now)
            except Exception:
                self._derived.pop(key, None)

    def source_key(self):
        row, _, _ = _connection(self.authority, self.session_ref, self.connection_ref)
        return 'moshtix' if row.adapter == ADAPTER else row.adapter

    def operation_template(self):
        source = self.source_key()
        if source == 'moshtix':
            from .discovery_transport import MOSHTIX_QUERY
            return {'source': source, 'query': MOSHTIX_QUERY, 'page_size': 9, 'window_weeks': 8}
        from .provider_catalogue import BOOK_SEARCH, BOOK_DETAILS
        return {'source': source, 'identity': source_identity(source),
                'documents': [BOOK_SEARCH, BOOK_DETAILS] if source == 'hardcover' else [],
                'operations': list(registered_source(source).operations)}

    def _rights(self, now):
        require(isinstance(self.provider, SourcePolicyProvider), 'CAPABILITY_UNAVAILABLE')
        result = self.provider.current(self.authority, self.session_ref,
            profile_ref=self.profile_ref, connection_ref=self.connection_ref, now=now)
        require(type(result) is SourceRights, 'AUTHORITY_DENIED')
        require(result.source == self.source_key(), 'AUTHORITY_DENIED')
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
        # A dataclass/digest is not authority. Rebuild with the existing sole
        # catalogue serializer over this exact current purpose projection.
        from .profile_snapshot import capture
        from .inference_body import serialize_catalogue
        from .category_registry import registry_view
        encoded = serialize_catalogue(capture(view), view.grants, view.context,
            registry_view(plan.categories), now=utc(self.authority.now()),
            plan_ref=plan.plan_ref, bindings=plan.bindings)
        require(encoded.body == plan.body and encoded.body_digest == plan.body_digest, 'PREVIEW_STALE')

    def _parents(self, view):
        from .profile_snapshot import capture
        from . import disclosure
        snapshot = capture(view)
        nodes = disclosure._nodes(snapshot, view.context)
        allowed, _ = disclosure._lineage(nodes,
            disclosure._grant_permissions(view.grants, view.context, utc(self.authority.now())), 'catalogue_lookup')
        result = tuple(sorted(key for key, node in nodes.items()
            if node.get('basis') and node['basis'] != 'feedback_note' and node.get('title') and allowed(key)))
        require(len(result) <= 500, 'LIMIT_EXCEEDED')
        return result

    @sensitive_variables()
    def preview(self, plan, *, request=None):
        with ga.recovery_guard(self.authority, self.session_ref, profile_ref=self.profile_ref), self.authority._lock:
            ga._bound(self.authority, self.session_ref, self.profile_ref, sensitive=True)
            now = utc(self.authority.now()); view = self._view(); self._match(plan, view)
            self._purge_derived(now)
            rights = self._rights(now)
            source = self.source_key()
            if source == 'moshtix':
                require(request is None or request == SourceRequest('moshtix', 'event', 'events'))
                operation = MoshtixOperation(now.replace(microsecond=0), 9, rights.application_identity)
                request = SourceRequest('moshtix', 'event', 'events')
            else:
                from .provider_catalogue import media_operation
                spec = registered_source(source)
                if request is None:
                    terms = next(q['terms'] for q in read_json(plan.body)['queries'] if q['category'] == spec.categories[0])
                    require(bool(terms), 'NO_GROUNDED_CANDIDATES')
                    request = SourceRequest(source, spec.categories[0], spec.operations[0], tuple(terms[:1]))
                require(type(request) is SourceRequest and request.source == source, 'AUTHORITY_DENIED')
                if request.operation == 'book_details':
                    derived = self._derived.get((plan.plan_ref, request.request_digest))
                    require(derived is not None and derived[0] == request, 'AUTHORITY_DENIED')
                    self._check(derived[1], now)
                # Detail IDs require their OWN exact disclosure/confirmation.
                # There is no wildcard permission inherited from book search.
                operation = media_operation(request)
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
                'kind': 'catalogue', 'destination': operation.material['origin'],
                'locale': 'VIC' if source == 'moshtix' else ''}

    def confirm(self, preview_ref, *, plan_digest, operation_digest, confirmed):
        require(confirmed is True)
        with ga.recovery_guard(self.authority, self.session_ref, profile_ref=self.profile_ref), self.authority._lock:
            ga._bound(self.authority, self.session_ref, self.profile_ref, sensitive=True)
            saved = self._previews.get(str(ga._uuid(preview_ref)))
            require(saved is not None, 'PREVIEW_STALE')
            require(saved.plan.body_digest == plan_digest and saved.operation.operation_digest == operation_digest,
                    'PREVIEW_STALE')
            self._check(saved, utc(self.authority.now()))
            self._approved[(saved.plan.plan_ref, saved.request.request_digest)] = saved
            return saved.plan.plan_ref

    def _check(self, saved, now):
        try:
            current = utc(self.authority.now())
            require(utc(now) < saved.expires_at and current < saved.expires_at, 'PREVIEW_STALE')
            ga.assert_current(saved.view)
            self._match(saved.plan, saved.view)
            require(self._rights(current) == saved.rights, 'PREVIEW_STALE')
            return True
        except Exception:
            for key, value in list(self._derived.items()):
                if value[1] is saved:
                    self._derived.pop(key, None)
            raise

    def operation_for(self, request, plan, *, now):
        with ga.recovery_guard(self.authority, self.session_ref, profile_ref=self.profile_ref), self.authority._lock:
            saved = self._approved.get((plan.plan_ref, request.request_digest))
            require(saved is not None and saved.plan == plan and saved.request == request, 'AUTHORITY_DENIED')
            self._check(saved, utc(now))
            return saved.operation

    def authorize(self, request, plan, operation, *, now):
        with ga.recovery_guard(self.authority, self.session_ref, profile_ref=self.profile_ref), self.authority._lock:
            require(self.operation_for(request, plan, now=now) is operation, 'AUTHORITY_DENIED')
            require(len(self._permits) < 128 and not any(saved.plan.plan_ref == plan.plan_ref
                and saved.request == request for _, saved in self._permits.values()), 'AUTHORITY_DENIED')
            saved = self._approved[(plan.plan_ref, request.request_digest)]
            rights = saved.rights
            generation = saved.view.connection_binding['credential_generation'] if request.source != 'moshtix' else None
            parents = self._parents(saved.view) if request.terms else ()
            permit = SourcePermit(request.source, operation.material['origin'], 'catalogue_lookup', plan.plan_ref, plan.bindings,
                request.request_digest, saved.view.context['config_revision'], rights.revision,
                rights.policy.ref, rights.policy.revision, saved.expires_at, parents, generation)
            self._validate_policy(rights, permit, now)
            self._permits[id(permit)] = (permit, saved)
            return permit

    def _validate_policy(self, rights, permit, now):
        if permit.source == 'moshtix':
            validate_moshtix_policy(rights.policy, permit=permit, now=now,
                model_context_current=rights.processor_ref is not None)
        else:
            require(rights.policy.current(now) and rights.policy.allows('*', 'fetch'), 'AUTHORITY_DENIED')
            require(rights.policy.ref == permit.policy_ref and rights.policy.revision == permit.policy_revision,
                    'PREVIEW_STALE')
            require(not any('model_context' in uses for _, uses in rights.policy.permissions)
                    or rights.processor_ref is not None, 'AUTHORITY_DENIED')

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
        self._validate_policy(saved[1].rights, permit, now)
        return saved[1].rights.policy

    def model_context_current(self, permit, operation, *, now):
        self.policy(permit, operation, now=now)
        return self._permits[id(permit)][1].rights.processor_ref is not None

    def remember_details(self, permit, reply):
        """Only an authenticated search observation can propose detail IDs.

        This proposal is not approval: preview and confirm must still bind the
        exact derived operation. No client-provided ID list inherits a search.
        """
        from .source_adapters import SourceReply, normalize
        saved = self._permits.get(id(permit))
        require(saved is not None and saved[0] is permit, 'AUTHORITY_DENIED')
        approval = saved[1]
        with ga.recovery_guard(self.authority, self.session_ref, profile_ref=self.profile_ref), self.authority._lock:
            now = utc(self.authority.now())
            self._purge_derived(now)
            self.revalidate(permit, approval.request, approval.plan, approval.operation, now=now)
            require(approval.request.operation == 'book_search' and type(reply) is SourceReply
                    and reply.request_digest == approval.request.request_digest
                    and reply.observed_at <= now < permit.expires_at, 'AUTHORITY_DENIED')
            from .disclosure import SCALARS
            from .source_collection import checkpoint_end
            policy = approval.rights.policy
            for key, value in list(self._derived.items()):
                if value[1] is approval:
                    self._derived.pop(key, None)
            cache_control = dict(reply.headers).get('cache-control', '').lower()
            if (not all(policy.allows(target, use) for target in SCALARS for use in ('cache', 'retain'))
                    or any(flag in cache_control for flag in ('no-store', 'no-cache'))):
                return None  # No hidden retained copy when continuation is denied.
            end = checkpoint_end(reply, expires_at=min(approval.expires_at, policy.expires_at))
            if now >= end:
                return None
            parsed = normalize(registered_source('hardcover'), approval.request, reply, now=now)
            if not parsed.enrichment_ids:
                return None
            request = SourceRequest('hardcover', 'book', 'book_details',
                approval.request.terms, parsed.enrichment_ids)
            key = (approval.plan.plan_ref, request.request_digest)
            require(key in self._derived or len(self._derived) < 128, 'CAPABILITY_UNAVAILABLE')
            self._derived[key] = (request, approval, digest(reply.body), reply.observed_at, end)
            return request

    def pending_details(self, plan_ref):
        now = utc(self.authority.now())
        self._purge_derived(now)
        return tuple(value[0] for key, value in self._derived.items()
                     if key[0] == plan_ref)


def authorize_request(issuer, request, plan, operation, *, now):
    require(type(issuer) is SourceAuthority, 'AUTHORITY_DENIED')
    return issuer.authorize(request, plan, operation, now=now)
