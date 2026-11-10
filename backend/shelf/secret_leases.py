"""Current-session credential leases, optionally hydrated by the saved-key port.

Private handles and credential bytes never enter browser/session state. Python
cannot promise byte erasure and this boundary cannot protect against OS admins.
Configuration is unproven metadata; it supplies no validation/dispatch authority.
"""
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timedelta
import secrets
import uuid

from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from .models import PortableConnection
from .portable_authority import AuthorityFault, require_secret_protection


@dataclass(repr=False)
class _Lease:
    random_handle: str
    installation_id: uuid.UUID
    connection_id: uuid.UUID
    session_ref: str
    owner_id: int
    owner_revision: int
    process_generation: uuid.UUID
    config_revision: int
    credential_generation: int
    expires: float
    created_at: datetime
    absolute_expires_at: datetime
    secret: bytes = field(repr=False)
    saved_generation: str | None = None
    state: str = 'live'
    schema: str = 'SecretLease/v1'


class MemoryCredentials:
    def __init__(self, authority):
        self.authority = authority
        self._leases = {}
        self._saved_credentials = None
        # Shared lock establishes one order for session callbacks and vault access.
        self._lock = authority._lock
        authority._revokers.append(self._revoke_session)
        authority._source_vault = self  # Explicit host-owned vault, never a store lookup.

    def _revoke_session(self, ref):
        for key, lease in list(self._leases.items()):
            if lease.session_ref == ref:
                lease.state = 'revoked'
                del self._leases[key]

    def _revoke_connection(self, key):
        lease = self._leases.pop(key, None)
        if lease is not None:
            lease.state = 'revoked'

    def _connection(self, connection_id, revision):
        if not isinstance(connection_id, uuid.UUID) or type(revision) is not int or revision < 1:
            raise AuthorityFault('CONTRACT_INVALID', 422)
        row = PortableConnection.objects.filter(pk=connection_id,
            installation_id=self.authority.boundary.installation_id).first()
        if row is None:
            raise AuthorityFault('AUTHORITY_DENIED')
        if row.config_revision != revision or row.removed:
            raise AuthorityFault('REVISION_CONFLICT', 409)
        return row

    def configure(self, ref, config, *, connection_id=None, revision=None):
        with self._lock:
            self.authority.authorize(ref, sensitive=True)
            inference = {'adapter': 'openai_responses', 'model': 'gpt-6-astra', 'reasoning': 'high'}
            media = (type(config) is dict and set(config) == {'adapter', 'model', 'reasoning'}
                and config['adapter'] in ('tmdb_movie', 'tmdb_tv', 'hardcover')
                and config['model'] == config['reasoning'] == '')
            if type(config) is not dict or config != inference and not media:
                raise AuthorityFault('CONTRACT_INVALID', 422)
            with transaction.atomic():
                if connection_id is None:
                    row = PortableConnection.objects.create(id=uuid.uuid4(),
                        installation_id=self.authority.boundary.installation_id, **config)
                else:
                    row = self._connection(connection_id, revision)
                    if row.adapter != config['adapter']:
                        raise AuthorityFault('CONTRACT_INVALID', 422)
                    row.adapter, row.model, row.reasoning = config['adapter'], config['model'], config['reasoning']
                    row.config_revision += 1
                    row.credential_generation += 1
                    row.save(update_fields=['adapter', 'model', 'reasoning', 'config_revision', 'credential_generation'])
                    self._revoke_connection(row.pk)
            return row

    @sensitive_variables()
    def replace(self, ref, connection_id, revision, secret, *, storage=None):
        if storage is not None:
            if storage != 'persistent':
                raise AuthorityFault('CONTRACT_INVALID', 422)
            if self._saved_credentials is None:
                raise AuthorityFault('CREDENTIAL_STORE_UNAVAILABLE', 503)
            return self._saved_credentials.save(ref, connection_id, revision, secret)
        if self._saved_credentials is not None:
            return self._saved_credentials.replace_memory(ref, connection_id, revision, secret)
        return self._replace_memory(ref, connection_id, revision, secret)

    @sensitive_variables()
    def _replace_memory(self, ref, connection_id, revision, secret):
        with self._lock:
            session = self.authority.authorize(ref, sensitive=True)
            if type(secret) is not bytes or not 1 <= len(secret) <= 8192:
                raise AuthorityFault('CONTRACT_INVALID', 422)
            require_secret_protection()
            with transaction.atomic():
                row = self._connection(connection_id, revision)
                if self._saved_credentials is not None:
                    # Explicit RAM replacement must never later resurrect the
                    # previous saved bytes after this session logs out.
                    self._saved_credentials.supersede_saved(row)
                row.credential_generation += 1
                row.save(update_fields=['credential_generation'])
            self._revoke_connection(row.pk)
            self._install_lease(ref, session, row, secret)
            return {'connection_ref': str(row.pk), 'configured': True, 'validation': 'UNPROVEN'}

    @sensitive_variables()
    def _install_lease(self, ref, session, row, secret, *, expires_at=None, saved_generation=None):
        """Hydration creates current authority, never a longer saved-key lifetime."""
        now = self.authority.now()
        expires = session.created + 28800
        if expires_at is not None:
            expires = min(expires, self.authority.monotonic() + (expires_at-now).total_seconds())
        if expires <= self.authority.monotonic():
            raise AuthorityFault('CREDENTIAL_REQUIRED', 503)
        self._leases[row.pk] = _Lease(secrets.token_urlsafe(32),
            self.authority.boundary.installation_id, row.pk, ref, session.owner_id,
            session.owner_revision, self.authority.boundary.process_generation,
            row.config_revision, row.credential_generation, expires, now,
            now + timedelta(seconds=expires-self.authority.monotonic()), secret, saved_generation)

    def remove(self, ref, connection_id, revision, *, disable=False):
        if self._saved_credentials is not None:
            result = self._saved_credentials.remove(ref, connection_id, revision, disable=disable)
            if result is not False:
                return result
        with self._lock:
            self.authority.authorize(ref, sensitive=True)
            with transaction.atomic():
                row = self._connection(connection_id, revision)
                row.credential_generation += 1
                if disable:
                    row.removed = True
                    row.config_revision += 1
                row.save(update_fields=['credential_generation', 'removed', 'config_revision'])
            self._revoke_connection(row.pk)

    @sensitive_variables()
    def resolve(self, ref, connection_id, revision):
        """Internal dispatch port, never an HTTP response or validation receipt."""
        with self._lock:
            lease = self._leases.get(connection_id)
            if lease is not None and lease.saved_generation is None:
                return self._resolve_memory(ref, connection_id, revision)
            if self._saved_credentials is not None:
                status = self._saved_credentials.describe(ref, connection_id, revision)
                if status['credential_saved']:
                    if not status['credential_available']:
                        raise AuthorityFault('CREDENTIAL_REQUIRED', 503)
                    self._saved_credentials.hydrate(ref, connection_id, revision)
            return self._resolve_memory(ref, connection_id, revision)

    @sensitive_variables()
    def _resolve_memory(self, ref, connection_id, revision):
        with self._lock:
            session = self.authority.authorize(ref, touch=False)
            row = self._connection(connection_id, revision)
            lease = self._leases.get(row.pk)
            if (lease is None or lease.state != 'live' or lease.session_ref != ref or lease.owner_id != session.owner_id
                    or lease.owner_revision != session.owner_revision
                    or lease.process_generation != self.authority.boundary.process_generation
                    or lease.config_revision != row.config_revision
                    or lease.credential_generation != row.credential_generation
                    or self.authority.monotonic() >= lease.expires):
                raise AuthorityFault('CREDENTIAL_REQUIRED', 503)
            return lease.secret


    @sensitive_variables()
    def describe(self, ref, connection_id, revision):
        """Current nonsecret status; inspection never renews owner activity."""
        with self._lock:
            session = self.authority.authorize(ref, touch=False)
            row = self._connection(connection_id, revision)
            saved_status = {}
            try:
                # Passive status never hydrates/decrypts a saved key. It may
                # observe a lock and revoke bytes already held in this process.
                if self._saved_credentials is not None:
                    saved_status = self._saved_credentials.describe(ref, connection_id, revision)
                self._resolve_memory(ref, connection_id, revision)
            except AuthorityFault as fault:
                if fault.code != 'CREDENTIAL_REQUIRED':
                    raise
                return {'configured': False, 'credential_generation': row.credential_generation,
                        'usable_until': None, **saved_status}
            lease = self._leases[row.pk]
            remaining = min(lease.expires, session.created + 28800, session.last_seen + 1800) - self.authority.monotonic()
            return {'configured': True, 'credential_generation': row.credential_generation,
                    'usable_until': self.authority.now() + timedelta(seconds=max(0, remaining)), **saved_status}

    @contextmanager
    @sensitive_variables()
    def ephemeral(self, ref, connection_id, revision, *, credential_generation):
        """Resolve only at send under the same lock as rotation and revocation."""
        with self._lock:
            row = self._connection(connection_id, revision)
            if type(credential_generation) is not int or row.credential_generation != credential_generation:
                raise AuthorityFault('PREVIEW_STALE', 409)
            value = bytearray(self.resolve(ref, connection_id, revision))
            try:
                yield value
            finally:
                for index in range(len(value)):
                    value[index] = 0
