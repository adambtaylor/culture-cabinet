"""Current, owner-only D3 authority for a fresh portable profile.

No HTTP registration, secret exposure, source lookup or inference is added. Existing
legacy stores need their later D5 adapter. The existing live recovery barrier
protects this fresh-store slice without removing the migrated singleton. Trusted
Python is the security boundary: SQLite write windows protect ordinary raw/bulk callers, not arbitrary code execution.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import UUID

from django.db import connection, transaction
from django.views.decorators.debug import sensitive_variables

from .connection_validation import ConnectionValidation
from .disclosure import CLASSES, DisclosureError, _time, canonical_bytes, read_json
from .inference_adapters import BUILD
from .inference_transport import ORIGIN
from .models import (AuthoredDiscoveryState, DiscoveryState, FeedbackEvent,
    FeedbackProjection, FeedbackSubject, FeedbackTarget, Grant, IntakeRun, PortableConnection, PortableDisclosureGrant,
    PortableInstallation, Profile, ProfileCabinetState,
    RecoveryRestriction, SavedItem, SocialDiscoveryState, SynthesisRun)
from .portable_authority import AuthorityFault, PortableAuthority
from . import recovery

_PURPOSES = {'inference', 'catalogue_lookup'}
_VIEW_TOKEN = object()
_FIXTURE_TOKEN = object()
_CURRENT_WRITER = ContextVar("portable_current_writer", default=None)


def _require(ok, code='AUTHORITY_DENIED', status=403):
    if not ok:
        raise AuthorityFault(code, status)


def _uuid(value):
    try:
        result = UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise AuthorityFault('CONTRACT_INVALID', 422) from None
    _require(isinstance(value, UUID) or type(value) is str and str(result) == value,
             'CONTRACT_INVALID', 422)
    return result


def _revision(value):
    _require(type(value) is int and 0 <= value <= 2**53 - 1, 'CONTRACT_INVALID', 422)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _fixture_database():
    # Explicit fixture construction is unavailable to the normal portable root.
    # No request or environment flag can turn an actual on-disk store into this.
    return str(connection.settings_dict['NAME']).startswith('file:memorydb_')


@contextmanager
def _write_window():
    """Authorize only this connection and transaction, closing even on rollback."""
    connection.ensure_connection()
    _require(connection.in_atomic_block and not getattr(connection, '_disclosure_write_open', False))
    raw = connection.connection
    connection._disclosure_write_open = True
    raw.create_function('cabinet_disclosure_write', 0, lambda: 1)
    current = _CURRENT_WRITER.get()
    # Obtain's new rows bind the actual writer profile as well as the existing
    # open SQL window. Another owned profile cannot borrow this window merely
    # because both rows live on the same SQLite connection.
    raw.create_function('cabinet_obtain_write', 2, lambda actor, profile: int(
        current is not None and current[3:5] == (actor, profile) and current[5].raw is raw))
    try:
        yield
    finally:
        raw.create_function('cabinet_disclosure_write', 0, lambda: 0)
        raw.create_function('cabinet_obtain_write', 2, lambda actor, profile: 0)
        connection._disclosure_write_open = False


class _RecoveryCheckpoint:
    def __init__(self, authority, session_ref, witness, raw):
        self.authority, self.session_ref = authority, session_ref
        self.witness, self.raw = witness, raw

    def check(self):
        """Recheck policy under the existing enclosing coordinator mutex."""
        try:
            self._check()
        except recovery.RecoveryBlocked:
            raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503) from None

    def _check(self):
        current = recovery.installed_coordinator()
        if current is not self.witness or connection.connection is not self.raw:
            raise recovery.RecoveryBlocked()
        if current is not None:
            # Pending can precede its SQL mirror. Checking the mirror first would
            # accept an empty snapshot while an external restriction is pending.
            if current.closed or current.blocked or current.pending is not None:
                raise recovery.RecoveryBlocked()
            if not current.matches_connection(self.raw) or recovery.raw_store(self.raw) != current.store_id:
                raise recovery.RecoveryBlocked()
            current.verify_mirror(self.raw)
            if current.committed_restrictions() or RecoveryRestriction.objects.exists():
                raise recovery.RecoveryBlocked()
        else:
            # Reuse the original empty-verification rules; never turn a failed
            # installed witness into this narrow, explicitly isolated fallback.
            with recovery.access_barrier():
                pass
        self.authority.authorize(self.session_ref, touch=False)
        _require(self.authority.boundary.active
            and str(connection.settings_dict['NAME']) == self.authority.boundary.database_name)


@contextmanager
def recovery_guard(authority, session_ref, *, profile_ref=None):
    """Enclose fresh authority/capture in the EXISTING recovery access barrier.

    Lock order is coordinator -> authority -> SQL. Nested checks reuse the live
    verified checkpoint; the original outer barrier verifies journal continuity
    again on exit, so an exit failure cannot hand a snapshot or bytes to a caller.
    profile_ref is the shared capture seam; this conservative policy denies every
    pending/committed restriction rather than narrowing away another subject.
    """
    _require(type(authority) is PortableAuthority)
    try:
        with recovery.access_barrier():
            witness = recovery.installed_coordinator()
            # Disk use needs a constructor-issued witness for this exact root.
            # The isolated memory fallback and explicit synthetic restart remain
            # separate; neither becomes authority for an arbitrary disk store.
            trusted = (witness is not None and getattr(witness, '_trusted_restart_seal', None)
                is recovery._TRUSTED_RESTART_SEAL and witness.trusted_boundary is authority.boundary)
            native = recovery.native_first_start_matches(witness, authority.boundary)
            clean = recovery.native_clean_start_matches(witness, authority.boundary)
            _require(_fixture_database() or trusted or native or clean, 'CAPABILITY_UNAVAILABLE', 503)
            checkpoint = _RecoveryCheckpoint(authority, session_ref, witness, connection.connection)
            checkpoint.check()
            yield checkpoint
            checkpoint.check()
    except recovery.RecoveryBlocked:
        raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503) from None


@contextmanager
def _checked_transaction(checkpoint):
    with transaction.atomic():
        yield
        # Keep the policy check before commit. The original enclosing barrier
        # retains its nested-check behavior; also verify its actual witness here
        # so physical journal loss cannot commit a newly minted preview row.
        checkpoint.check()
        if checkpoint.witness is not None:
            checkpoint.witness.verify_current()


def _owner(authority, session_ref, *, sensitive=False):
    _require(type(authority) is PortableAuthority)
    session = authority.authorize(session_ref, sensitive=sensitive, touch=False)
    _require(authority.boundary.active
        and str(connection.settings_dict['NAME']) == authority.boundary.database_name)
    installation = PortableInstallation.objects.get(pk=authority.boundary.installation_id)
    _require(installation.owner_id == session.owner_id
        and installation.owner_revision == session.owner_revision
        and installation.process_generation == authority.boundary.process_generation)
    # Recovery is enclosed by the public entry point, before this authority lock.
    # The migration-owned singleton is identity, not evidence of a restriction.
    return installation, session


def _bound(authority, session_ref, profile_ref, *, sensitive=False):
    installation, session = _owner(authority, session_ref, sensitive=sensitive)
    state = ProfileCabinetState.objects.select_related('profile').filter(
        profile_ref=_uuid(profile_ref), installation=installation, actor_id=session.owner_id).first()
    _require(state is not None and state.owner_revision == session.owner_revision
        and not state.blocked and state.data_schema_version in (1, 2)
        and state.profile.kind == 'owner' and state.profile.active)
    _require(Grant.objects.filter(user_id=session.owner_id, profile=state.profile,
                                 can_view=True, can_select=True, can_mutate=True).exists())
    return state, session


def create_owner_profile(authority, session_ref, *, expected_owner_revision, label='Your cabinet'):
    """Explicit, repeatable fresh-owner binding; existing profiles are never adopted."""
    _revision(expected_owner_revision)
    _require(type(label) is str and 1 <= len(label) <= 80 and bool(label.strip())
        and not any(ord(c) < 32 or ord(c) == 127 for c in label), 'CONTRACT_INVALID', 422)
    _require(type(authority) is PortableAuthority)
    with recovery_guard(authority, session_ref) as checkpoint, authority._lock, _checked_transaction(checkpoint):
        installation, session = _owner(authority, session_ref, sensitive=True)
        _require(session.owner_revision == expected_owner_revision, 'REVISION_CONFLICT', 409)
        existing = ProfileCabinetState.objects.filter(installation=installation, actor_id=session.owner_id).first()
        if existing is not None:
            _bound(authority, session_ref, existing.profile_ref, sensitive=True)
            return str(existing.profile_ref)
        _require(not Profile.objects.exists() and not ProfileCabinetState.objects.exists())
        checkpoint.check()
        with _write_window():
            profile = Profile.objects.create(kind='owner', label=label, active=True)
            Grant.objects.create(user_id=session.owner_id, profile=profile,
                                 can_view=True, can_select=True, can_mutate=True, can_export=True)
            # Only genuinely new cabinets start in the native feedback schema;
            # returning an existing profile above never upgrades its history.
            state = ProfileCabinetState.objects.create(profile=profile, installation=installation,
                actor_id=session.owner_id, owner_revision=session.owner_revision, data_schema_version=2)
        return str(state.profile_ref)


def bind_existing_profile(authority, session_ref, *, profile_id, confirmed):
    """Bind the selected profile in this identified store, never copy/adopt a DB."""
    _require(confirmed is True and type(profile_id) is int and profile_id > 0,
             'CONTRACT_INVALID', 422)
    with recovery_guard(authority, session_ref) as checkpoint, authority._lock, _checked_transaction(checkpoint):
        installation, session = _owner(authority, session_ref, sensitive=True)
        _require(installation.root_identity == authority.boundary.root_identity
            and PortableInstallation.objects.count() == 1)
        profile = Profile.objects.filter(pk=profile_id, kind='owner', active=True).first()
        _require(profile is not None and Grant.objects.filter(profile=profile, user_id=session.owner_id,
                    can_view=True, can_select=True, can_mutate=True).exists())
        prior = ProfileCabinetState.objects.filter(profile=profile).first()
        if prior is not None:
            _require(prior.installation_id == installation.pk and prior.actor_id == session.owner_id
                     and prior.owner_revision == session.owner_revision and not prior.blocked)
            if prior.data_schema_version == 1:
                checkpoint.check()
                with _write_window():
                    ProfileCabinetState.objects.filter(pk=prior.pk).update(data_schema_version=2,
                        policy_epoch=prior.policy_epoch + 1, restriction_watermark=prior.restriction_watermark + 1)
            return str(prior.profile_ref)
        from .models import FeedbackSubject
        subject = FeedbackSubject.objects.filter(profile=profile).first()
        arguments = {'subject_ref': subject.subject_id} if subject else {}
        checkpoint.check()
        with _write_window():
            state = ProfileCabinetState.objects.create(profile=profile, installation=installation,
                actor_id=session.owner_id, owner_revision=session.owner_revision,
                data_schema_version=2, **arguments)
        return str(state.profile_ref)


@contextmanager
def current_writer(authority, session_ref, profile_ref):
    """Recovery -> owner lock, without stealing feedback.immediate's transaction."""
    with recovery_guard(authority, session_ref, profile_ref=profile_ref) as checkpoint, authority._lock:
        state, _ = _bound(authority, session_ref, profile_ref, sensitive=True)
        _require(state.data_schema_version == 2, 'CAPABILITY_UNAVAILABLE', 503)
        token = _CURRENT_WRITER.set((authority, session_ref, str(profile_ref), state.actor_id, state.profile_id, checkpoint))
        try:
            yield state
            checkpoint.check()
        finally:
            _CURRENT_WRITER.reset(token)


@contextmanager
def writer_window(actor, profile_id):
    """Join the real writer transaction; raw/bulk callers cannot mint a window."""
    state = ProfileCabinetState.objects.filter(profile_id=profile_id, data_schema_version=2).first()
    if state is None:
        yield
        return
    context = _CURRENT_WRITER.get()
    _require(context is not None and (actor.pk, profile_id) == context[3:5], 'AUTHORITY_DENIED', 403)
    _require(connection.in_atomic_block)
    authority, session, profile, _, _, checkpoint = context
    _bound(authority, session, profile, sensitive=True)
    if getattr(connection, '_disclosure_write_open', False):
        yield  # Nested favourite/discovery writes remain in the owning window.
        checkpoint.check()
    else:
        with _write_window():
            yield
            checkpoint.check()
            _bound(authority, session, profile)


def _connection(authority, session_ref, connection_ref, *, purpose=None):
    candidate = PortableConnection.objects.filter(pk=_uuid(connection_ref),
        installation_id=authority.boundary.installation_id, removed=False).first()
    if candidate is not None and candidate.adapter in ('moshtix_public', 'tmdb_movie', 'tmdb_tv', 'hardcover',
                                                      'musicbrainz_recording', 'musicbrainz_artist', 'apple_podcast',
                                                      'apw','gertrude'):
        # Public source consent never borrows a processor key or its validation.
        # The source issuer separately checks current rights and exact operation.
        _require(purpose == 'catalogue_lookup')
        from .source_authority import _connection as source_connection
        return None, source_connection(authority, session_ref, connection_ref)
    service = getattr(authority, '_disclosure_validation', None)
    _require(type(service) is ConnectionValidation and service.authority is authority
        and service.vault.authority is authority, 'CAPABILITY_UNAVAILABLE', 503)
    row = PortableConnection.objects.filter(pk=_uuid(connection_ref),
        installation_id=authority.boundary.installation_id, removed=False).first()
    _require(row is not None)
    return service, service._current(session_ref, row.pk, row.config_revision)


def _end(authority, session):
    remaining = min(session.created + 28800, session.last_seen + 1800) - authority.monotonic()
    _require(remaining > 0, 'UNAUTHENTICATED', 401)
    deadline = min(session.created + 28800, session.last_seen + 1800)
    cache = getattr(authority, '_disclosure_session_ends', {})
    key = (session.ref, deadline)
    if key not in cache:
        cache = {pair: end for pair, end in cache.items() if pair[0] in authority._sessions}
        cache[key] = authority.now() + timedelta(seconds=remaining)
        authority._disclosure_session_ends = cache
    return cache[key]


def change_grant(authority, session_ref, *, profile_ref, expected_revision, purpose,
                 connection_ref, allowed_data_classes, expires_at, action):
    """Consent changes serialize with their monotone DB-triggered profile floors."""
    _revision(expected_revision)
    _require(type(authority) is PortableAuthority)
    _require(type(purpose) is str and type(action) is str and purpose in _PURPOSES and action in ('grant', 'revoke', 'regrant'), 'CONTRACT_INVALID', 422)
    _require(type(allowed_data_classes) in (list, tuple)
        and all(type(value) is str and value in CLASSES for value in allowed_data_classes)
        and len(set(allowed_data_classes)) == len(allowed_data_classes), 'CONTRACT_INVALID', 422)
    classes = sorted(allowed_data_classes)
    _require(type(expires_at) is datetime and expires_at.utcoffset() == timedelta(0), 'CONTRACT_INVALID', 422)
    with recovery_guard(authority, session_ref) as checkpoint, authority._lock, _checked_transaction(checkpoint):
        state, session = _bound(authority, session_ref, profile_ref, sensitive=True)
        candidate = None
        if action == 'revoke' and purpose == 'catalogue_lookup':
            candidate = PortableConnection.objects.filter(pk=_uuid(connection_ref),
                installation=state.installation, removed=False,
                adapter__in=('tmdb_movie', 'tmdb_tv', 'hardcover')).first()
        if candidate is not None:
            # Revocation removes an existing grant; it needs owned source
            # metadata, not a live token or a fabricated current config binding.
            # Grant/regrant and every effect still use the strict resolver below.
            from .source_authority import source_identity
            source_identity(candidate.adapter)
            _require(candidate.model == candidate.reasoning == ''
                and candidate.credential_generation >= 0 and candidate.validation_generation == 0,
                'CAPABILITY_UNAVAILABLE', 503)
            row = candidate
        else:
            service, (row, binding, description) = _connection(authority, session_ref, connection_ref, purpose=purpose)
        now = authority.now()
        prior = PortableDisclosureGrant.objects.filter(cabinet=state, connection=row, purpose=purpose).first()
        _require((prior.revision if prior else 0) == expected_revision, 'REVISION_CONFLICT', 409)
        if action == 'revoke':
            _require(prior is not None, 'REVISION_CONFLICT', 409)
            if prior.state == 'revoked':
                return str(prior.pk), prior.revision
            checkpoint.check()
            with _write_window():
                changed = PortableDisclosureGrant.objects.filter(pk=prior.pk, revision=expected_revision).update(
                    state='revoked', revision=expected_revision + 1)
            _require(changed == 1, 'REVISION_CONFLICT', 409)
            return str(prior.pk), expected_revision + 1
        _require(bool(classes) and now < expires_at <= _end(authority, session), 'CONTRACT_INVALID', 422)
        if prior is not None and action == 'grant':
            _require(prior.state == 'active' and prior.allowed_data_classes == classes
                and prior.expires_at == expires_at and prior.config_digest == binding['config_digest'],
                'REVISION_CONFLICT', 409)
            return str(prior.pk), prior.revision
        _require((prior is None and action == 'grant') or
                 (prior is not None and prior.state == 'revoked' and action == 'regrant'), 'REVISION_CONFLICT', 409)
        checkpoint.check()
        with _write_window():
            if prior is None:
                grant = PortableDisclosureGrant.objects.create(cabinet=state, connection=row, purpose=purpose,
                    config_revision=row.config_revision, config_digest=binding['config_digest'],
                    allowed_data_classes=classes, issued_at=now, expires_at=expires_at)
            else:
                changed = PortableDisclosureGrant.objects.filter(pk=prior.pk, revision=expected_revision,
                    state='revoked').update(revision=expected_revision + 1, state='active', issued_at=now,
                    expires_at=expires_at, config_revision=row.config_revision,
                    config_digest=binding['config_digest'], allowed_data_classes=classes)
                _require(changed == 1, 'REVISION_CONFLICT', 409)
                grant = PortableDisclosureGrant.objects.get(pk=prior.pk)
        return str(grant.pk), grant.revision


def _local_default(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _stamp(value)
    raise TypeError('Unsupported local revision value')


def _synthesis_guards():
    """Require the named trusted migrations, not a stored claim of provenance."""
    from importlib import import_module
    import re

    _require(connection.vendor == 'sqlite', 'CAPABILITY_UNAVAILABLE', 503)
    prior = import_module('shelf.migrations.0021_portable_disclosure_authority').TRIGGERS
    broker = import_module('shelf.migrations.0022_portable_enhance_state').TRIGGERS
    normalize = lambda sql: sql.strip().removesuffix(';').rstrip()
    current = import_module('shelf.migrations.0030_run_selection_retention')
    try:
        definitions = current.effective_inventory(connection.connection)
    except RuntimeError:
        _require(False, 'CAPABILITY_UNAVAILABLE', 503)
    expected = set()
    for sql in definitions:
        match = re.match(r'CREATE TRIGGER (\w+)\s+(?:BEFORE|AFTER) (?:INSERT|UPDATE|DELETE) ON (\w+)\b', sql)
        _require(match is not None, 'CAPABILITY_UNAVAILABLE', 503)
        expected.add((match[1], match[2], normalize(sql)))
    _require(len(prior) == 17 and len(broker) == 7 and len(expected) == len(definitions)
             and len({row[0] for row in expected}) == len(definitions),
             'CAPABILITY_UNAVAILABLE', 503)
    with connection.cursor() as cursor:
        actual = {(name, table, normalize(sql)) for name, table, sql in cursor.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'").fetchall()}
    _require(actual == expected, 'CAPABILITY_UNAVAILABLE', 503)


@sensitive_variables()
def _broker_history_row(row, state):
    """Recognize immutable broker intent; never derive a preference from output."""
    from .enhance_contract import (VERSION, STAGES, TERMINALS, SAFE_CODES,
                                   CategoryOutcome, EnhanceBindings, RegistryView, digest)
    from .inference_contract import _submitted, validate_result

    def require(ok):
        _require(ok, 'CAPABILITY_UNAVAILABLE', 503)

    def sha(value):
        return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)

    require(row['contract_version'] == VERSION and row['mode'] == 'portable')
    for name in ('id', 'request_identity', 'client_key', 'refresh_nonce', 'installation_id',
                 'portable_connection_id'):
        require(isinstance(row[name], UUID))
    require((row['installation_id'], row['actor_id'], row['profile_id']) ==
            (state.installation_id, state.actor_id, state.profile_id))
    require(type(row['input_snapshot']) is dict and row['input_snapshot'] == {})
    # Removal/configuration/grant expiry must not poison later taste snapshots.
    # This checks historical ownership and immutable shape, not present readiness;
    # current_view/assert_current still enforce today's session, grants and floors.
    require(PortableConnection.objects.filter(pk=row['portable_connection_id'],
                                             installation_id=state.installation_id).exists())
    record = row['portable_bindings']
    require(type(record) is dict and set(record) in (
        {'bindings','categories','source_revision'},
        {'bindings','categories','source_revision','local_selection_digest'}))
    if 'local_selection_digest' in record:require(sha(record['local_selection_digest']))
    binding = record['bindings']
    require(type(binding) is dict and set(binding) == set(EnhanceBindings.__dataclass_fields__))
    bound = EnhanceBindings(**binding)
    require((bound.installation_id, bound.actor_ref, bound.profile_ref) ==
            (str(state.installation_id), str(state.actor_ref), str(state.profile_ref)))
    require(all(sha(binding[name]) for name in ('snapshot_revision', 'grants_digest', 'connection_digest')))
    require(bound.restriction_watermark.isascii() and bound.restriction_watermark.isdecimal()
            and str(int(bound.restriction_watermark)) == bound.restriction_watermark)
    require(bound.policy_epoch <= state.policy_epoch
            and int(bound.restriction_watermark) <= state.restriction_watermark)
    require(type(record['categories']) is list)
    registry = RegistryView(bound.registry_revision, tuple(record['categories']))
    require(type(record['source_revision']) is str and 0 < len(record['source_revision']) <= 300)
    # Reuse the broker's existing intent definition and canonical codec. Mutable
    # stage/attempt/receipt fields are deliberately absent from this fingerprint.
    from .enhance_selection import validate_selection_history
    evidence=row['evidence']
    try:
        projected=validate_selection_history(record,evidence,input_digest=row['input_digest'],
            refresh_nonce=row['refresh_nonce'],run_ref=row['id'],collection_digest=row['collection_digest'],
            redacted=row['content_redacted_at'] is not None)
    except (ValueError,TypeError,KeyError):
        require(False)
    require(type(row['revision']) is int and row['revision'] >= 0
            and type(row['attempts']) is int and row['attempts'] in (0, 1)
            and type(row['cancel_requested']) is bool)
    require(row['portable_stage'] in STAGES + TERMINALS)
    for name in ('created_at', 'updated_at', 'portable_expires_at'):
        require(type(row[name]) is datetime and row[name].utcoffset() == timedelta(0))
    if row['content_redacted_at'] is not None:
        # Only the effective one-way guards can create this grammar. Deleted
        # receipt content is no longer required, and can never become evidence.
        require(type(row['content_redacted_at']) is datetime and sha(row['content_digest'])
                and row['output']=={} and row['evidence']==projected)
        require((row['attempts'] == 0 and row['transport_request_id'] is None and row['state'] in ('queued', 'failed'))
                or (row['attempts'] == 1 and isinstance(row['transport_request_id'], UUID)
                    and row['state'] in ('running', 'unknown', 'completed', 'failed')))
        return
    require(row['content_digest'] == '')
    evidence = row['evidence']
    require(type(evidence) is dict and {'code', 'outcomes', 'phase', 'expected_publication_revision'} <= set(evidence))
    require(set(evidence) <= {'code', 'outcomes', 'phase', 'expected_publication_revision',
                            'preview_ref', 'preview_digest', 'session_digest', 'submitted_refs',
                            'publication_revision', 'publication_ref', 'source_use',
                            'selection_policy','selection_policy_digest','selection_seed','selection_report'})
    require(evidence['code'] in SAFE_CODES and evidence['phase'] in
            ('catalogue', 'inference', 'source_model_context_unavailable'))
    if 'source_use' in evidence:
        summary = evidence['source_use']
        require(type(summary) is list and len(summary) == len(registry.categories))
        collected = eligible = 0
        for category, item in zip(registry.categories, summary):
            require(type(item) is dict and set(item) ==
                    {'category', 'collected_count', 'model_eligible_count'}
                    and item['category'] == category)
            count, usable = item['collected_count'], item['model_eligible_count']
            require(type(count) is int and type(usable) is int and 0 <= usable <= count <= 100)
            collected += count
            eligible += usable
        require(collected <= 100 and sha(row['collection_digest']))
        # These are historical collection counts, not today's permissions or
        # the smaller queues later published after model/personal filtering.
    if evidence['phase'] == 'source_model_context_unavailable':
        require('source_use' in evidence and collected > 0 and eligible == 0
                and row['attempts'] == 0 and row['transport_request_id'] is None
                and row['output'] == {} and row['body_digest'] == row['sealed_run_digest'] == ''
                and not {'submitted_refs', 'publication_revision', 'publication_ref'} & set(evidence))
        require((row['state'] == 'queued' and row['portable_stage'] == 'sealed'
                 and evidence['code'] == 'AUTHORITY_DENIED' and not row['cancel_requested'])
                or (row['state'] == 'failed' and row['portable_stage'] in
                    ('blocked', 'failed', 'cancelled', 'stale')))
    require(type(evidence['expected_publication_revision']) is int
            and 0 <= evidence['expected_publication_revision'] <= state.publication_revision)
    require(type(evidence['outcomes']) is list)
    if evidence['outcomes']:
        require(len(evidence['outcomes']) == len(registry.categories))
        for category, value in zip(registry.categories, evidence['outcomes']):
            require(type(value) is dict and set(value) == {'category', 'state', 'candidate_count', 'code'})
            require(CategoryOutcome(**value).category == category)
    for name in ('preview_ref', 'publication_ref'):
        if name in evidence:
            require(type(evidence[name]) is str and str(UUID(evidence[name])) == evidence[name])
    for name in ('preview_digest', 'session_digest'):
        if name in evidence:
            require(sha(evidence[name]))
    for name in ('collection_digest', 'body_digest', 'sealed_run_digest'):
        require(row[name] == '' or sha(row[name]))
    if 'submitted_refs' in evidence:
        refs = evidence['submitted_refs']
        require(type(refs) is dict and set(refs) == {'signals', 'candidates'})
        _submitted(**refs)
    require(type(row['output']) is dict)
    if row['attempts'] == 0:
        require(row['state'] in ('queued', 'failed') and row['transport_request_id'] is None
                and row['output'] == {})
        require(row['portable_stage'] not in ('reserved', 'in_flight', 'validated', 'staged', 'published', 'unknown'))
        require(not row['cancel_requested'] or row['state'] == 'failed')
        if row['state'] == 'queued':
            require(row['portable_stage'] in ('authorized', 'collecting', 'sealed', 'awaiting_preview', 'ready'))
    else:
        require(isinstance(row['transport_request_id'], UUID)
                and row['state'] in ('running', 'unknown', 'completed', 'failed'))
        require(all(sha(row[name]) for name in ('collection_digest', 'body_digest', 'sealed_run_digest')))
        require([value.get('category') for value in evidence['outcomes'] if type(value) is dict]
                == list(registry.categories) and len(evidence['outcomes']) == len(registry.categories))
        refs = evidence.get('submitted_refs')
        require(type(refs) is dict and set(refs) == {'signals', 'candidates'})
        if row['state'] == 'completed':
            require(not row['cancel_requested'] and row['output'] != {})
            # Shape/citation validation is receipt coherence, never human lineage
            # or evidence that a current disclosure/send is authorized.
            require(validate_result(canonical_bytes(row['output']), **refs) == row['output'])
        else:
            require(row['output'] == {})
        if row['state'] == 'running':
            require(row['portable_stage'] in ('reserved', 'in_flight') and not row['cancel_requested'])
        elif row['state'] == 'unknown':
            require(row['portable_stage'] == 'unknown')
    if row['state'] == 'failed':
        require(row['portable_stage'] in TERMINALS)
    elif row['state'] == 'completed':
        require(row['portable_stage'] in ('validated', 'staged', 'published') + TERMINALS)
    if row['portable_stage'] == 'published':
        require(row['state'] == 'completed' and type(evidence.get('publication_revision')) is int
                and evidence['publication_revision'] == evidence['expected_publication_revision'] + 1
                and 'publication_ref' in evidence)
        from .models import PortableCabinetManifest
        publication = PortableCabinetManifest.objects.filter(pk=UUID(evidence['publication_ref']),
            run_id=row['id'], cabinet_id=state.profile_id,
            publication_revision=evidence['publication_revision']).first()
        require(publication is not None and publication.bindings == record)
    else:
        require('publication_revision' not in evidence and 'publication_ref' not in evidence)


@sensitive_variables()
def synthesis_history(state):
    """Return all unsupported history, excluding only verified guarded bookkeeping.

    A portable run records work performed ON a snapshot, not a preference added
    TO it. Hashing its progress makes that same operation invalidate itself.
    Legacy/unknown rows keep their complete local hash and capture denial. A
    malformed purported portable row is an error, never an invisible exclusion.
    Callers hold the existing owner/recovery/SQL boundary throughout these reads.
    """
    from .disclosure import DisclosureError
    from .enhance_contract import EnhanceFault
    from .inference_contract import ContractError

    rows = list(SynthesisRun.objects.filter(profile_id=state.profile_id).order_by('pk').values())
    legacy, guards_checked = [], False
    for row in rows:
        if row['contract_version'] != 'enhance/v1' and row['mode'] != 'portable':
            legacy.append(row)
            continue
        if not guards_checked:
            _synthesis_guards()
            guards_checked = True
        try:
            _broker_history_row(row, state)
        except (ValueError, TypeError, KeyError, AttributeError, DisclosureError, EnhanceFault, ContractError):
            raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503) from None
    return legacy


def _history_revisions(state, *, event_ids=None, feedback_subject_present=None):
    # This is a LOCAL complete revision hash, not another outbound serializer.
    # No notes or this hash are sent. Include all committed row fields so denied
    # note edits, retractions, archives and source-only changes stale a preview.
    queries = {
        'profile_binding': Profile.objects.filter(pk=state.profile_id),
        'local_permissions': Grant.objects.filter(profile_id=state.profile_id),
        'derived_history': SynthesisRun.objects.filter(profile_id=state.profile_id),
        'feedback_events': FeedbackEvent.objects.filter(profile_id=state.profile_id),
        'feedback_subjects': FeedbackSubject.objects.filter(profile_id=state.profile_id),
        'feedback_targets': FeedbackTarget.objects.filter(pk__in=FeedbackEvent.objects.filter(
            profile_id=state.profile_id).values('target_id')),
        'feedback_projections': FeedbackProjection.objects.filter(profile_id=state.profile_id),
        'discovery': DiscoveryState.objects.filter(profile_id=state.profile_id),
        'intake': IntakeRun.objects.filter(profile_id=state.profile_id),
        'authored': AuthoredDiscoveryState.objects.filter(profile_id=state.profile_id),
        'social': SocialDiscoveryState.objects.filter(profile_id=state.profile_id),
        'favourites': SavedItem.objects.filter(profile_id=state.profile_id, list_kind='favorites'),
    }
    if event_ids is not None:
        # Reconstruct a committed card-action boundary from actual immutable
        # feedback history, rather than trusting a receipt's claimed digest.
        # All other writers remain in the revision and therefore break a chain.
        from .feedback import reduce_events
        events = list(FeedbackEvent.objects.filter(profile_id=state.profile_id, pk__in=event_ids)
                      .order_by('revision', 'id'))
        groups = {}
        for event in events:
            groups.setdefault((event.target_id, event.signal, event.scope), []).append(event)
        reconstructed = []
        for key, history in groups.items():
            row = FeedbackProjection.objects.filter(profile_id=state.profile_id,
                target_id=key[0], signal=key[1], scope=key[2]).values().first()
            _require(row is not None, 'PREVIEW_STALE', 409)
            effective, value, erased = reduce_events(history)
            reconstructed.append({**row, 'revision': history[-1].revision,
                                  'effective_event_id': effective, 'value': value, 'erased': erased})
        queries['feedback_events'] = FeedbackEvent.objects.filter(profile_id=state.profile_id, pk__in=event_ids)
        queries['feedback_targets'] = FeedbackTarget.objects.filter(pk__in={e.target_id for e in events})
        queries['feedback_projections'] = sorted(reconstructed, key=lambda row: row['id'])
        if feedback_subject_present is False:
            queries['feedback_subjects'] = []
    versions = {}
    for name, queryset in queries.items():
        rows = (synthesis_history(state) if name == 'derived_history' else
                queryset if type(queryset) is list else list(queryset.order_by('pk').values()))
        try:
            raw = json.dumps(rows, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                             allow_nan=False, default=_local_default).encode()
        except (ValueError, TypeError, UnicodeError):
            raise AuthorityFault('CONTRACT_INVALID', 422) from None
        versions[name] = hashlib.sha256(raw).hexdigest()
    return versions


@dataclass(frozen=True, repr=False)
class AuthorityView:
    """Backend-only capture input. Properties return detached current D3 views."""
    installation_id: str
    actor_ref: str
    profile_ref: str
    subject_ref: str
    actor_id: int
    profile_id: int
    profile_kind: str
    policy_epoch: int
    restriction_watermark: str
    profile_revision_digest: str
    connection_ref: str
    expires_at: datetime
    _context: bytes = field(repr=False)
    _grants: bytes = field(repr=False)
    _revisions: bytes = field(repr=False)
    _connection_binding: bytes = field(repr=False)
    _authority: object = field(repr=False)
    _session: str = field(repr=False)
    _token: object = field(repr=False)

    @property
    def context(self):
        return read_json(self._context)

    @property
    def grants(self):
        return read_json(self._grants)

    @property
    def source_revisions(self):
        return read_json(self._revisions)

    @property
    def connection_binding(self):
        return read_json(self._connection_binding)


def current_view(authority, session_ref, *, profile_ref, connection_ref, purpose):
    _require(type(authority) is PortableAuthority)
    _require(type(purpose) is str and purpose in _PURPOSES, 'CONTRACT_INVALID', 422)
    with recovery_guard(authority, session_ref) as checkpoint, authority._lock, _checked_transaction(checkpoint):
        state, session = _bound(authority, session_ref, profile_ref)
        service, (row, binding, description) = _connection(authority, session_ref, connection_ref, purpose=purpose)
        source = service is None
        fixture = _fixture_database() if source else getattr(authority, '_disclosure_fixture_token', None) is _FIXTURE_TOKEN
        status = None if source else service.status(session_ref, row.pk, row.config_revision)
        if source:
            adapter_build, destination = binding['build'], binding['origin']
        else:
            adapter_build, destination = BUILD, ORIGIN
        if fixture and not source:
            _require(_fixture_database(), 'CAPABILITY_UNAVAILABLE', 503)
            from .models import PortableProbeAttempt, PortableValidation
            attempt = PortableProbeAttempt.objects.filter(connection=row,
                bindings__validation_generation=row.validation_generation, state='completed').first()
            proof = PortableValidation.objects.filter(attempt=attempt).first() if attempt else None
            _require(proof is not None and proof.proof_kind == 'isolated_test_fake'
                and proof.bindings == binding and authority.now() < proof.expires_at
                and authority.monotonic() < service._deadlines.get(proof.pk, 0)
                and description['configured'] and not service._blocked(), 'VALIDATION_STALE', 409)
        elif not source:
            _require(status['connection_ready'] and status['proof_kind'] == 'real_transport_contract',
                     'VALIDATION_STALE', 409)
        end = min(_end(authority, session), description['usable_until'])
        if not source:
            end = min(end, datetime.fromisoformat(status['expires_at']))
        now = authority.now()
        grants = list(PortableDisclosureGrant.objects.filter(cabinet=state, connection=row,
            purpose=purpose, state='active', config_revision=row.config_revision,
            config_digest=binding['config_digest'], issued_at__lte=now, expires_at__gt=now).order_by('id'))
        _require(bool(grants))
        end = min(end, *(grant.expires_at for grant in grants))
        context = {'schema': 'disclosure-context/v1', 'installation_id': str(state.installation_id),
            'actor_ref': str(state.actor_ref), 'profile_ref': str(state.profile_ref),
            'applicable_subjects': [str(state.subject_ref)], 'purpose': purpose,
            'adapter_id': row.adapter, 'adapter_version': adapter_build, 'destination_origin': destination,
            'config_revision': row.config_revision, 'policy_epoch': state.policy_epoch,
            'grant_revisions': {str(grant.pk): grant.revision for grant in grants},
            'session_expires_at': _stamp(_end(authority, session)), 'isolated_fixture': fixture}
        records = []
        for grant in grants:
            _require(type(grant.allowed_data_classes) is list and bool(grant.allowed_data_classes)
                and all(type(value) is str and value in CLASSES for value in grant.allowed_data_classes)
                and len(set(grant.allowed_data_classes)) == len(grant.allowed_data_classes))
            record = {name: context[name] for name in ('installation_id', 'actor_ref', 'profile_ref',
                'purpose', 'adapter_id', 'adapter_version', 'destination_origin', 'config_revision', 'policy_epoch')}
            record.update(schema='grant-authority/v1', grant_id=str(grant.pk), granting_subject=str(state.subject_ref),
                applicable_subjects=[str(state.subject_ref)], allowed_data_classes=grant.allowed_data_classes,
                grant_revision=grant.revision, issued_at=_stamp(grant.issued_at),
                expires_at=_stamp(min(grant.expires_at, _end(authority, session))), state=grant.state)
            records.append(record)
        revisions = _history_revisions(state)
        full = hashlib.sha256(canonical_bytes(revisions)).hexdigest()
        return AuthorityView(str(state.installation_id), str(state.actor_ref), str(state.profile_ref),
            str(state.subject_ref), session.owner_id, state.profile_id, 'owner', state.policy_epoch,
            str(state.restriction_watermark), full, str(row.pk), end, canonical_bytes(context),
            canonical_bytes(records), canonical_bytes(revisions), canonical_bytes(binding),
            authority, session_ref, _VIEW_TOKEN)


def assert_current(view):
    """Capture callers cannot substitute a JSON assertion for current authority."""
    _require(type(view) is AuthorityView and view._token is _VIEW_TOKEN)
    with recovery_guard(view._authority, view._session, profile_ref=view.profile_ref) as checkpoint:
        fresh = current_view(view._authority, view._session, profile_ref=view.profile_ref,
                             connection_ref=view.connection_ref, purpose=view.context['purpose'])
        captured, current = view.context, fresh.context
        try:
            original_end = _time(captured['session_expires_at'])
            fresh_end = _time(current['session_expires_at'])
        except (DisclosureError, KeyError):
            raise AuthorityFault('PREVIEW_STALE', 409) from None
        now = view._authority.now()
        _require(now < view.expires_at and now < original_end and fresh_end >= original_end,
                 'PREVIEW_STALE', 409)
        # Ordinary activity may move the live idle cap forward. Compare against
        # the still-valid original cap so approved bytes never renew authority;
        # every other freshly reconstructed field must remain exactly equal.
        comparison = replace(fresh, _context=canonical_bytes({**current,
            'session_expires_at': captured['session_expires_at']}))
        _require(comparison == view, 'PREVIEW_STALE', 409)
        return comparison
