"""Native saved-key intents and authenticated hydration; never a secret response."""
from datetime import datetime, timezone
import importlib.util
import hashlib
import json
from pathlib import Path
import uuid

from django.views.decorators.debug import sensitive_variables

from . import grant_authority as ga
from .models import PortableConnection, SavedCredentialBinding
from .portable_authority import AuthorityFault, require_secret_protection

VERSION = 'cabinet-secret-service/v1'
RECORD_SCHEMA = 'cabinet-credential/v1'
DELETE_NO_EFFECT = 'direct-delete-locked/v1'
STATES = frozenset(('AVAILABLE', 'LOCKED', 'MISSING', 'UNAVAILABLE', 'CONFLICT',
                    'CORRUPT', 'UNKNOWN', 'REMOVED'))
_PROVIDERS = {'openai_responses': ('openai', 'inference'),
              'tmdb_movie': ('tmdb', 'catalogue_lookup'),
              'tmdb_tv': ('tmdb', 'catalogue_lookup'),
              'hardcover': ('hardcover', 'catalogue_lookup')}


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def packaged_adapter():
    """Only the reviewed packaged helper is executable; HTTP selects no path."""
    path = Path(__file__).resolve().parents[2] / 'tools' / 'cabinet_secret_service.py'
    spec = importlib.util.spec_from_file_location('cabinet_packaged_secret_service', path)
    if spec is None or spec.loader is None:
        raise AuthorityFault('CREDENTIAL_STORE_UNAVAILABLE', 503)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def packaged_client():
    return packaged_adapter().invoke


@sensitive_variables()
def secret_service_request(request):
    return packaged_client()(request)


class SavedCredentials:
    def __init__(self, authority, vault, *, request, before_save=None, delete_guarantee=None):
        if vault.authority is not authority or not callable(request):
            raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503)
        self.authority, self.vault, self.request = authority, vault, request
        self.before_save = before_save
        self.delete_guarantee = delete_guarantee
        vault._saved_credentials = self

    def _binding(self, session, row):
        provider = _PROVIDERS.get(row.adapter)
        if provider is None:
            raise AuthorityFault('CONTRACT_INVALID', 422)
        return {'installation_id': str(self.authority.boundary.installation_id),
            'owner_id': session.owner_id, 'owner_revision': session.owner_revision,
            'provider': provider[0], 'purpose': provider[1],
            'connection_id_or_null': str(row.pk), 'legacy_account_actor_profile_or_null': None}

    @sensitive_variables()
    def _request_value(self, operation, saved, *, record=None, operation_id=None):
        identifier = str(operation_id or saved.operation_id)
        return {'version': VERSION, 'operation': operation, 'record_kind': 'credential',
            'binding': saved.binding, 'expected_revision': saved.store_revision,
            'operation_id': identifier, 'record_if_put': record}

    @sensitive_variables()
    def _call(self, operation, saved, *, record=None, operation_id=None):
        request = self._request_value(operation, saved, record=record, operation_id=operation_id)
        identifier = request['operation_id']
        try:
            reply = self.request(request)
            if (type(reply) is not dict or set(reply) != {'version', 'operation_id', 'status', 'revision', 'record'}
                    or reply['version'] != VERSION or reply['operation_id'] != identifier
                    or reply['status'] not in STATES
                    or reply['revision'] is not None and (type(reply['revision']) is not str
                        or not 0 < len(reply['revision']) <= 128)
                    or operation != 'read' and reply['record'] is not None
                    or reply['status'] != 'AVAILABLE' and reply['record'] is not None):
                raise ValueError
            if reply['revision'] is not None and str(uuid.UUID(reply['revision'])) != reply['revision']:
                raise ValueError
            return reply
        except Exception:
            # No ambiguous acknowledgement can become a successful write or a
            # fallback to the previous RAM key. Reconciliation uses this UUID.
            return {'version': VERSION, 'operation_id': identifier,
                    'status': 'UNKNOWN', 'revision': None, 'record': None}

    @sensitive_variables()
    def _record(self, saved, secret):
        return {'schema': RECORD_SCHEMA, 'binding': saved.binding,
            'config_revision_or_null': saved.config_revision, 'generation': str(saved.generation),
            'operation_id': str(saved.operation_id), 'entered_at': timestamp(saved.entered_at),
            'updated_at': timestamp(saved.updated_at),
            'expires_at_or_null': timestamp(saved.expires_at) if saved.expires_at else None,
            'scopes': saved.scopes, 'secret': secret.decode('utf-8', 'strict')}

    def _invalidate(self, row):
        row.credential_generation += 1
        fields = ['credential_generation']
        # Source checks have their own typed proof. They never acquire the
        # inference-only validation generation by saving/replacing a bearer.
        if row.adapter == 'openai_responses':
            row.validation_generation += 1
            fields.append('validation_generation')
        row.save(update_fields=fields)
        self.vault._revoke_connection(row.pk)

    def _removal_row(self, connection_id, revision):
        if not isinstance(connection_id, uuid.UUID) or type(revision) is not int or revision < 1:
            raise AuthorityFault('CONTRACT_INVALID', 422)
        row = PortableConnection.objects.filter(pk=connection_id,
            installation_id=self.authority.boundary.installation_id).first()
        saved = SavedCredentialBinding.objects.filter(pk=connection_id).first()
        if (row is None or row.config_revision != revision or row.removed and
                (saved is None or not saved.removed and saved.state != 'PENDING_DELETE')):
            raise AuthorityFault('REVISION_CONFLICT', 409)
        return row, saved

    def supersede_saved(self, row):
        saved = SavedCredentialBinding.objects.filter(pk=row.pk).first()
        if saved and saved.state.startswith('PENDING'):
            raise AuthorityFault('CREDENTIAL_STORE_PENDING', 409)
        if saved and not saved.removed:
            saved.state = 'UNAVAILABLE'
            saved.save(update_fields=['state'])

    def _finalize(self, saved, reply):
        if saved.state == 'PENDING_WRITE' and reply['status'] == 'AVAILABLE' and reply['revision']:
            return SavedCredentialBinding.objects.filter(pk=saved.pk, state='PENDING_WRITE',
                operation_id=saved.operation_id, generation=saved.generation).update(
                    state='ACTIVE', store_revision=reply['revision']) == 1
        if saved.state == 'PENDING_DELETE' and reply['status'] == 'REMOVED' and reply['revision']:
            # Keep the deletion revision: a later explicit enrollment must CAS
            # against its encrypted receipt rather than pretend first creation.
            return SavedCredentialBinding.objects.filter(pk=saved.pk, state='PENDING_DELETE',
                operation_id=saved.operation_id).update(state='UNAVAILABLE', removed=True,
                    remove_requested=False, delete_no_effect_proof=None, store_revision=reply['revision']) == 1
        return False

    def _delete_proof(self, saved):
        request = self._request_value('delete', saved)
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':'),
                                          ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        return {'schema': DELETE_NO_EFFECT, 'operation_id': str(saved.operation_id),
                'delete_request_sha256': digest, 'helper_guarantee': DELETE_NO_EFFECT}

    def _supports_delete_proof(self):
        # This capability is supplied only by the trusted packaged adapter (or
        # an explicit owned test port), never by a request or a status label.
        try:
            if self.delete_guarantee is not None:
                return self.delete_guarantee() == DELETE_NO_EFFECT
            if self.request is secret_service_request:
                module = packaged_adapter()
                return (module.VERSION == VERSION
                        and getattr(module, 'DELETE_NO_EFFECT_GUARANTEE', None) == DELETE_NO_EFFECT)
            return False
        except Exception:
            return False

    @sensitive_variables()
    def save(self, ref, connection_id, revision, secret):
        if type(secret) is not bytes or not 1 <= len(secret) <= 8192:
            raise AuthorityFault('CONTRACT_INVALID', 422)
        try:
            secret.decode('utf-8', 'strict')
        except UnicodeError:
            raise AuthorityFault('CONTRACT_INVALID', 422) from None
        require_secret_protection()
        with ga.recovery_guard(self.authority, ref) as checkpoint, self.authority._lock:
            session = self.authority.authorize(ref, sensitive=True)
            if self.before_save is not None:
                self.before_save()
            row = self.vault._connection(connection_id, revision)
            binding = self._binding(session, row)
            previous = SavedCredentialBinding.objects.filter(pk=row.pk).first()
            if previous and previous.state.startswith('PENDING'):
                self.reconcile(ref, connection_id, revision)
                previous.refresh_from_db()
            if previous and (previous.binding != binding or previous.state.startswith('PENDING')
                             or previous.remove_requested):
                raise AuthorityFault('CREDENTIAL_STORE_PENDING', 409)
            now = self.authority.now()
            with ga._checked_transaction(checkpoint):
                self._invalidate(row)
                saved, _ = SavedCredentialBinding.objects.update_or_create(connection=row,
                    defaults={'binding': binding, 'config_revision': revision, 'state': 'PENDING_WRITE',
                        'generation': uuid.uuid4(), 'operation_id': uuid.uuid4(),
                        'store_revision': previous.store_revision if previous else None,
                        'entered_at': now, 'updated_at': now, 'expires_at': None,
                        'scopes': [binding['purpose']], 'removed': False, 'remove_requested': False,
                        'delete_no_effect_proof': None})
            # SQLite's intent is committed before the external mutation. Keep
            # the recovery/owner barrier but never its SQL transaction over IPC.
            reply = self._call('put', saved, record=self._record(saved, secret))
            with ga._checked_transaction(checkpoint):
                complete = self._finalize(saved, reply)
            if not complete:
                raise AuthorityFault('CREDENTIAL_STORE_' + reply['status'], 503)
            self.vault._install_lease(ref, session, row, secret, saved_generation=str(saved.generation))
            return {'connection_ref': str(row.pk), 'configured': True, 'validation': 'UNPROVEN',
                    'credential_configured': True, 'connection_ready': False,
                    **self.describe(ref, connection_id, revision)}

    def describe(self, ref, connection_id, revision):
        """Metadata-only status: no read, unlock, hydration or activity touch."""
        with self.authority._lock:
            session = self.authority.authorize(ref, touch=False)
            row = self.vault._connection(connection_id, revision)
            saved = SavedCredentialBinding.objects.filter(pk=row.pk).first()
            state, available = 'absent', False
            if saved and not saved.removed:
                state = 'removal_pending' if saved.state == 'PENDING_DELETE' or saved.remove_requested else 'unavailable'
                if (saved.state == 'ACTIVE' and not saved.remove_requested and saved.config_revision == revision
                        and saved.binding == self._binding(session, row)
                        and (saved.expires_at is None or self.authority.now() < saved.expires_at)):
                    result = self._call('availability', saved, operation_id=uuid.uuid4())
                    # Availability is metadata only and deliberately has no
                    # revision. The private read validates the exact revision.
                    available = result['status'] == 'AVAILABLE'
                    state = 'available' if available else 'locked' if result['status'] == 'LOCKED' else 'unavailable'
                lease = self.vault._leases.get(row.pk)
                if not available and lease is not None and lease.saved_generation is not None:
                    self.vault._revoke_connection(row.pk)
            return {'credential_saved': saved is not None and not saved.removed, 'credential_available': available,
                    'credential_store_state': state}

    @sensitive_variables()
    def hydrate(self, ref, connection_id, revision):
        require_secret_protection()
        with ga.recovery_guard(self.authority, ref), self.authority._lock:
            session = self.authority.authorize(ref, touch=False)
            row = self.vault._connection(connection_id, revision)
            saved = SavedCredentialBinding.objects.filter(pk=row.pk, state='ACTIVE').first()
            if (not saved or saved.remove_requested or saved.binding != self._binding(session, row)
                    or saved.config_revision != revision):
                raise AuthorityFault('CREDENTIAL_REQUIRED', 503)
            result = self._call('read', saved, operation_id=uuid.uuid4())
            record = result['record']
            try:
                secret = record['secret'].encode('utf-8', 'strict')
                if (result['status'] != 'AVAILABLE' or result['revision'] != saved.store_revision
                        or self._record(saved, secret) != record or not 1 <= len(secret) <= 8192
                        or saved.expires_at is not None and self.authority.now() >= saved.expires_at):
                    raise ValueError
            except (TypeError, KeyError, ValueError, UnicodeError, AttributeError):
                self.vault._revoke_connection(row.pk)
                raise AuthorityFault('CREDENTIAL_REQUIRED', 503) from None
            self.vault._install_lease(ref, session, row, secret, expires_at=saved.expires_at,
                                      saved_generation=str(saved.generation))

    def remove(self, ref, connection_id, revision, *, disable=False):
        with ga.recovery_guard(self.authority, ref) as checkpoint, self.authority._lock:
            session = self.authority.authorize(ref, sensitive=True)
            row, saved = self._removal_row(connection_id, revision)
            if saved is None:
                return False
            if saved.removed:
                # The durable tombstone describes the encrypted item only.
                # A later explicit RAM-only replacement is still current key
                # material and must be revoked by every native removal command.
                with ga._checked_transaction(checkpoint):
                    self._invalidate(row)
                    if disable and not row.removed:
                        row.removed = True
                        row.config_revision += 1
                        row.save(update_fields=['removed', 'config_revision'])
                return {'removed': True, 'removal_pending': False, 'revision': row.config_revision}
            if saved.binding != self._binding(session, row):
                raise AuthorityFault('CREDENTIAL_STORE_PENDING', 409)
            if saved.state == 'PENDING_WRITE':
                with ga._checked_transaction(checkpoint):
                    saved.remove_requested = True
                    saved.save(update_fields=['remove_requested'])
                    self._invalidate(row)
                # Resolve only the old put first; never lose its operation UUID
                # by replacing it with a delete whose predecessor is unknown.
                result = self._call('reconcile', saved)
                with ga._checked_transaction(checkpoint):
                    complete = self._finalize(saved, result)
                if not complete:
                    return {'removed': False, 'removal_pending': True}
                saved.refresh_from_db()
            with ga._checked_transaction(checkpoint):
                self._invalidate(row)
                if disable and not row.removed:
                    row.removed = True
                    row.config_revision += 1
                    row.save(update_fields=['removed', 'config_revision'])
                if saved.state != 'PENDING_DELETE':
                    saved.state, saved.operation_id = 'PENDING_DELETE', uuid.uuid4()
                    saved.remove_requested = True
                    saved.delete_no_effect_proof = None
                    saved.save(update_fields=['state', 'operation_id', 'remove_requested', 'delete_no_effect_proof'])
                    operation = 'delete'
                elif (self._supports_delete_proof() and saved.delete_no_effect_proof == self._delete_proof(saved)
                        and SavedCredentialBinding.objects.filter(pk=saved.pk, state='PENDING_DELETE',
                            operation_id=saved.operation_id, store_revision=saved.store_revision,
                            delete_no_effect_proof=saved.delete_no_effect_proof).update(delete_no_effect_proof=None) == 1):
                    # A distinct explicit Remove command may continue the SAME
                    # delete only after atomically spending its durable proof.
                    # Death after this commit is uncertainty, never fresh proof.
                    operation = 'delete'
                else:
                    operation = 'reconcile'
            result = self._call(operation, saved)
            with ga._checked_transaction(checkpoint):
                complete = self._finalize(saved, result)
                if (operation == 'delete' and self._supports_delete_proof()
                        and result['status'] == 'LOCKED' and result['revision'] is None and result['record'] is None):
                    # Only a completed direct invocation has this no-effect
                    # guarantee. Reconcile/status LOCKED and UNKNOWN do not.
                    SavedCredentialBinding.objects.filter(pk=saved.pk, state='PENDING_DELETE',
                        operation_id=saved.operation_id, store_revision=saved.store_revision,
                        delete_no_effect_proof__isnull=True).update(delete_no_effect_proof=self._delete_proof(saved))
            return {'removed': complete, 'removal_pending': not complete, 'revision': row.config_revision}

    def reconcile(self, ref, connection_id, revision):
        """Explicit authenticated retry resolves an operation; never resends it."""
        with ga.recovery_guard(self.authority, ref) as checkpoint, self.authority._lock:
            session = self.authority.authorize(ref, sensitive=True)
            row, saved = self._removal_row(connection_id, revision)
            if not saved or saved.binding != self._binding(session, row):
                raise AuthorityFault('CREDENTIAL_REQUIRED', 503)
            if saved.state not in ('PENDING_WRITE', 'PENDING_DELETE'):
                return False
            reply = self._call('reconcile', saved)
            with ga._checked_transaction(checkpoint):
                return self._finalize(saved, reply)

    def hydrate_owner(self, ref):
        self.authority.authorize(ref, sensitive=True, touch=False)
        rows = SavedCredentialBinding.objects.filter(state='ACTIVE',
            connection__installation_id=self.authority.boundary.installation_id,
            connection__removed=False).select_related('connection')
        for saved in rows:
            try:
                self.hydrate(ref, saved.connection_id, saved.connection.config_revision)
            except AuthorityFault:
                # Keyring lock/expiry must not prevent normal app login. Safe
                # status exposes unavailable; no decrypted values reach the UI.
                continue

    def replace_memory(self, ref, connection_id, revision, secret):
        with ga.recovery_guard(self.authority, ref) as checkpoint:
            try:
                with ga._checked_transaction(checkpoint):
                    return self.vault._replace_memory(ref, connection_id, revision, secret)
            except BaseException:
                self.vault._revoke_connection(connection_id)
                raise
