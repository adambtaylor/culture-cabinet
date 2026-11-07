"""Explicit host-approved transfer from the existing store to a native session.

This imports a credential, never legacy profile data, source rights or consent.
The host supplies the exact reuse intent; no HTTP field can construct it.
"""
from dataclasses import dataclass
from datetime import timedelta
import uuid

from django.views.decorators.debug import sensitive_variables

from . import credential_store as store, grant_authority as ga
from .portable_authority import AuthorityFault, require_secret_protection
from .secret_leases import MemoryCredentials


@dataclass(frozen=True, repr=False)
class SourceCredentialReuse:
    """Trusted host intent, binding one unchanged legacy record to one target."""
    authority: object
    session_ref: str
    installation_id: uuid.UUID
    process_generation: uuid.UUID
    owner_id: int
    owner_revision: int
    profile_ref: str
    connection_ref: str
    config_revision: int
    legacy_binding: store.Binding


@sensitive_variables()
def import_existing_source_credential(authority, session_ref, *, profile_ref,
                                      connection_ref, expected_revision, approved_reuse):
    record = handle = transferred = vault = row = None
    complete = False
    try:
        ga._require(type(approved_reuse) is SourceCredentialReuse
                    and approved_reuse.authority is authority)
        profile_ref, connection_ref = str(ga._uuid(profile_ref)), str(ga._uuid(connection_ref))
        with ga.recovery_guard(authority, session_ref, profile_ref=profile_ref) as checkpoint, authority._lock:
            _, session = ga._bound(authority, session_ref, profile_ref, sensitive=True)
            intent = approved_reuse
            ga._require((intent.session_ref, intent.installation_id, intent.process_generation,
                         intent.owner_id, intent.owner_revision, intent.profile_ref,
                         intent.connection_ref, intent.config_revision) ==
                        (session_ref, authority.boundary.installation_id,
                         authority.boundary.process_generation, session.owner_id,
                         session.owner_revision, profile_ref, connection_ref, expected_revision))
            vault = getattr(authority, '_source_vault', None)
            ga._require(type(vault) is MemoryCredentials and vault.authority is authority,
                        'CAPABILITY_UNAVAILABLE', 503)
            row = vault._connection(ga._uuid(connection_ref), expected_revision)
            provider = {'tmdb_movie': 'tmdb', 'tmdb_tv': 'tmdb', 'hardcover': 'hardcover'}.get(row.adapter)
            ga._require(provider is not None and row.model == row.reasoning == '',
                        'CAPABILITY_UNAVAILABLE', 503)
            ga._require(type(intent.legacy_binding) is store.Binding)
            store.validate_binding(intent.legacy_binding, provider=provider)
            require_secret_protection()
            handle = store.open_store(provider=provider)
            ga._require(handle.context == store._provider_context(provider)
                        and (handle.uid, handle.gid) == (store.OWNER_UID, store.OWNER_GID))
            store.lock_store(handle)
            record = store.read_record(handle)
            store.validate_metadata(record.metadata, now=authority.now(), provider=provider)
            ga._require(record.metadata.binding == intent.legacy_binding)
            store._secret_text(record.secret, provider=provider)
            # The unchanged vault gives a lease until the absolute session end.
            # Refuse a shorter imported lifetime rather than silently extending it.
            end = authority.now() + timedelta(seconds=session.created + 28800 - authority.monotonic())
            ga._require(record.metadata.expires_at >= end, 'CREDENTIAL_LIFETIME_REQUIRED', 503)
            handle.close()
            handle = None
            with ga._checked_transaction(checkpoint):
                checkpoint.check()
                status = vault.replace(session_ref, row.pk, expected_revision, bytes(record.secret))
                transferred = vault._leases.get(row.pk)
                ga._bound(authority, session_ref, profile_ref, sensitive=True)
        complete = True
        return status
    except (store.Refused, OSError, ValueError):
        raise AuthorityFault('CREDENTIAL_REQUIRED', 503) from None
    finally:
        # A failed outer recovery recheck must not leave an imported RAM lease.
        if not complete and transferred is not None and vault._leases.get(row.pk) is transferred:
            vault._revoke_connection(row.pk)
        if record is not None:
            record.close()
        if handle is not None:
            try:
                handle.close()
            except OSError:
                raise AuthorityFault('CREDENTIAL_REQUIRED', 503) from None
