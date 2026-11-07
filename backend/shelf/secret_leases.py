"""Memory-only connection credentials. No transport or persistent secret mode.

Private handles and credential bytes never enter browser/session state. Python
cannot promise byte erasure and this boundary cannot protect against OS admins.
Configuration is unproven metadata; it supplies no validation/dispatch authority.
"""
from dataclasses import dataclass, field
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
    state: str = 'live'
    schema: str = 'SecretLease/v1'


class MemoryCredentials:
    def __init__(self, authority):
        self.authority = authority
        self._leases = {}
        # Shared lock establishes one order for session callbacks and vault access.
        self._lock = authority._lock
        authority._revokers.append(self._revoke_session)

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
            if (type(config) is not dict or set(config) != {'adapter', 'model', 'reasoning'}
                    or config != {'adapter': 'openai_responses', 'model': 'gpt-6-astra', 'reasoning': 'high'}):
                raise AuthorityFault('CONTRACT_INVALID', 422)
            with transaction.atomic():
                if connection_id is None:
                    row = PortableConnection.objects.create(id=uuid.uuid4(),
                        installation_id=self.authority.boundary.installation_id, **config)
                else:
                    row = self._connection(connection_id, revision)
                    row.adapter, row.model, row.reasoning = config['adapter'], config['model'], config['reasoning']
                    row.config_revision += 1
                    row.credential_generation += 1
                    row.save(update_fields=['adapter', 'model', 'reasoning', 'config_revision', 'credential_generation'])
                    self._revoke_connection(row.pk)
            return row

    @sensitive_variables()
    def replace(self, ref, connection_id, revision, secret):
        with self._lock:
            session = self.authority.authorize(ref, sensitive=True)
            if type(secret) is not bytes or not 1 <= len(secret) <= 8192:
                raise AuthorityFault('CONTRACT_INVALID', 422)
            require_secret_protection()
            with transaction.atomic():
                row = self._connection(connection_id, revision)
                row.credential_generation += 1
                row.save(update_fields=['credential_generation'])
            self._revoke_connection(row.pk)
            now = self.authority.now()
            expires = session.created + 28800
            self._leases[row.pk] = _Lease(secrets.token_urlsafe(32),
                self.authority.boundary.installation_id, row.pk, ref, session.owner_id,
                session.owner_revision, self.authority.boundary.process_generation,
                row.config_revision, row.credential_generation, expires, now,
                now + timedelta(seconds=expires - self.authority.monotonic()), secret)
            return {'connection_ref': str(row.pk), 'configured': True, 'validation': 'UNPROVEN'}

    def remove(self, ref, connection_id, revision, *, disable=False):
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
            try:
                self.resolve(ref, connection_id, revision)
            except AuthorityFault as fault:
                if fault.code != 'CREDENTIAL_REQUIRED':
                    raise
                return {'configured': False, 'credential_generation': row.credential_generation, 'usable_until': None}
            lease = self._leases[row.pk]
            remaining = min(lease.expires, session.created + 28800, session.last_seen + 1800) - self.authority.monotonic()
            return {'configured': True, 'credential_generation': row.credential_generation,
                    'usable_until': self.authority.now() + timedelta(seconds=max(0, remaining))}
