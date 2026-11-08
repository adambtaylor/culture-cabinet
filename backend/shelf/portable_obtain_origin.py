"""Current publication attribution for one request, never a durable title copy."""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import re
from uuid import UUID

from .acquisition_contract import Blocked
from .disclosure import SCALARS, canonical_bytes
from .episode_scope import parse_scope

ORIGIN_FIELDS = frozenset({'schema', 'installation_ref', 'profile_ref', 'run_ref',
    'source_connection_ref', 'publication_revision', 'candidate_key_digest',
    'source_binding_digest', 'valid_until'})
OWNER_ORIGIN_FIELDS = frozenset({'schema', 'installation_ref', 'profile_ref', 'selection_ref',
    'source_connection_ref', 'owner_intent_digest', 'target_digest', 'source_binding_digest', 'valid_until'})


def validate_obtain_origin(value):
    if type(value) is not dict:
        raise ValueError('Known Obtain origin required')
    if value.get('schema') == 'obtain-publication-origin/v1':
        return validate_publication_origin(value)
    if value.get('schema') != 'obtain-owner-selection-origin/v1' or set(value) != OWNER_ORIGIN_FIELDS:
        raise ValueError('Known Obtain origin required')
    return _validate_origin_values(value,
        refs=('installation_ref', 'profile_ref', 'selection_ref', 'source_connection_ref'),
        digests=('owner_intent_digest', 'target_digest', 'source_binding_digest'))


def validate_publication_origin(value):
    if (type(value) is not dict or set(value) != ORIGIN_FIELDS
            or value['schema'] != 'obtain-publication-origin/v1'):
        raise ValueError('Bounded publication origin required')
    revision = value['publication_revision']
    if type(revision) is not int or not 1 <= revision < 2**53:
        raise ValueError('Positive publication revision required')
    return _validate_origin_values(value,
        refs=('installation_ref', 'profile_ref', 'run_ref', 'source_connection_ref'),
        digests=('candidate_key_digest', 'source_binding_digest'))


def validate_obtain_history(raw):
    """Validate restored attribution without reviving its expired authority.

    Operational receipts may outlive their source selection. Restoration checks
    the closed tagged shape and ownership, never reconstructs source facts.
    """
    from .disclosure import read_json
    from .episode_scope import parse_scope
    columns = {row[1] for row in raw.execute('PRAGMA table_info(shelf_acquisitionjob)')}
    if 'publication_origin' not in columns:
        return
    for table in ('acquisitionjob', 'obtainpreview'):
        owner = 'saved_item_id' if table == 'acquisitionjob' else 'saved_identity'
        query = f'''SELECT j.publication_origin,j.scope,j.provider,j.medium,j.source_id,j.title,j.{owner},
            s.profile_ref,s.installation_id,s.actor_id,j.actor_id,s.data_schema_version FROM shelf_{table} j
            LEFT JOIN shelf_profilecabinetstate s ON s.profile_id=j.profile_id
            WHERE j.publication_origin IS NOT NULL'''
        for row in raw.execute(query):
            origin = validate_obtain_origin(read_json(row[0].encode('utf-8')))
            if (parse_scope(row[1]) is None or any(value is not None for value in row[2:7])
                    or row[7] != origin['profile_ref'].replace('-', '')
                    or row[8] != origin['installation_ref'].replace('-', '') or row[9] != row[10]
                    or row[11] != 2):
                raise ValueError('Restored Obtain origin ownership mismatch')
    query = '''SELECT j.publication_origin,p.publication_origin,j.scope,p.scope,j.actor_id,p.actor_id,
        j.profile_id,p.profile_id,j.request_id,p.id FROM shelf_obtainselection s
        JOIN shelf_acquisitionjob j ON j.id=s.job_id JOIN shelf_obtainresult r ON r.id=s.result_id
        JOIN shelf_obtainpreview p ON p.id=r.preview_id
        WHERE j.publication_origin IS NOT NULL OR p.publication_origin IS NOT NULL'''
    for row in raw.execute(query):
        if row[0] is None or row[1] is None or read_json(row[0].encode()) != read_json(row[1].encode()) \
                or any(row[index] != row[index + 1] for index in (2, 4, 6, 8)):
            raise ValueError('Restored Obtain selection mismatch')
    from .obtain_acquisition import validate_origin_submission
    query = '''SELECT t.binding,j.publication_origin,j.request_id,t.result_guid,t.nzb_sha256
        FROM shelf_obtaintransfer t JOIN shelf_acquisitionjob j ON j.id=t.job_id
        WHERE j.publication_origin IS NOT NULL'''
    for raw_binding, raw_origin, request_id, guid, nzb_sha in raw.execute(query):
        binding = validate_origin_submission(read_json(raw_binding.encode()))
        if (binding['publication_origin'] != read_json(raw_origin.encode())
                or binding['request_id'].replace('-', '') != request_id
                or binding['exact']['guid'] != guid or binding['nzb_sha256'] != nzb_sha):
            raise ValueError('Restored source command receipt mismatch')


def _validate_origin_values(value, *, refs, digests):
    for key in refs:
        item = value[key]
        if type(item) is not str or str(UUID(item)) != item or UUID(item).int == 0:
            raise ValueError('Canonical origin reference required')
    for key in digests:
        if type(value[key]) is not str or re.fullmatch('[0-9a-f]{64}', value[key]) is None:
            raise ValueError('Canonical origin digest required')
    raw = value['valid_until']
    if type(raw) is not str:
        raise ValueError('Canonical UTC deadline required')
    end = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if end.utcoffset() is None or end.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') != raw:
        raise ValueError('Canonical UTC deadline required')
    if len(canonical_bytes(value)) > 1536:
        raise ValueError('Bounded publication origin required')
    return value


@dataclass(frozen=True, repr=False)
class CurrentObtainTarget:
    provider: str
    medium: str
    source_id: int
    title: str
    scope: str
    valid_until: datetime
    title_export_allowed: bool
    series_year: int | None = None
    year_export_allowed: bool = False

    def require_title_export(self):
        if not self.title_export_allowed:
            raise Blocked('Current source permission does not allow exporting this title to the indexer')
        if self.series_year is not None and not self.year_export_allowed:
            raise Blocked('Current source permission does not allow the selected year in the indexer query')


def candidate_key_digest(candidate_key):
    return hashlib.sha256(canonical_bytes(candidate_key)).hexdigest()


def _resolve(access, *, publication_revision, candidate_digest, scope, now):
    from django.db import connection
    from . import grant_authority as ga
    from .models import PortableCabinetManifest, PortableConnection
    from .portable_saved_authority import PortableSavedAccess, active_portable_access
    from .portable_enhance_service import EnhanceService, get_service
    from .source_authority import SourcePolicyProvider, SourceRights, current_binding
    from .source_evidence import FieldPolicy

    ga._require(type(access) is PortableSavedAccess and active_portable_access() is access)
    ga._require(type(publication_revision) is int and 1 <= publication_revision < 2**53)
    ga._require(type(candidate_digest) is str and re.fullmatch('[0-9a-f]{64}', candidate_digest) is not None)
    ga._require(type(now) is datetime and now.utcoffset() is not None)
    ga._require(parse_scope(scope) is not None, 'CONTRACT_INVALID', 422)
    # The normal publication reader may commit privacy redaction. Never put it
    # in a caller's rollback-capable transaction or use an old manifest on error.
    ga._require(not connection.in_atomic_block, 'CAPABILITY_UNAVAILABLE', 503)
    with ga.recovery_guard(access.authority, access.session_ref), access.authority._lock:
        identity = access.require(actor_id=access.identity.actor_id,
                                  profile_id=access.identity.profile_id, action='view')
        state, session = ga._bound(access.authority, access.session_ref, access.profile_ref)
        service = get_service(access.authority)
        ga._require(type(service) is EnhanceService and service.authority is access.authority)
        current = service.current(access.session_ref, profile_ref=access.profile_ref)
        ga._require(current['published'] and current['publication_revision'] == publication_revision,
                    'PREVIEW_STALE', 409)
        cards = [card for group in current['manifest']['categories'] for card in group['cards']
                 if candidate_key_digest(card['key']) == candidate_digest]
        ga._require(len(cards) == 1, 'PREVIEW_STALE', 409)
        card = cards[0]
        publication = PortableCabinetManifest.objects.filter(pk=state.active_manifest_ref,
            cabinet=state, publication_revision=publication_revision,
            content_redacted_at__isnull=True).first()
        actual_now = access.authority.now()
        ga._require(publication is not None and actual_now < publication.expires_at, 'PREVIEW_STALE', 409)
        # Reuse attribution validation, not the Save-only durable-retention gate.
        # Only this detached target contains source facts, for this live request.
        evidence = service._saved_evidence(state, session, publication, card)
        ga._require((evidence.provider, evidence.medium) == ('tmdb', 'tv'), 'CAPABILITY_UNAVAILABLE', 503)
        rows = list(PortableConnection.objects.filter(installation_id=state.installation_id,
            adapter='tmdb_tv', removed=False)[:2])
        ga._require(len(rows) == 1, 'CAPABILITY_UNAVAILABLE', 503)
        source = rows[0]
        view = current_binding(access.authority, access.session_ref,
            profile_ref=access.profile_ref, connection_ref=str(source.pk))
        provider = service._policy_provider
        ga._require(isinstance(provider, SourcePolicyProvider), 'CAPABILITY_UNAVAILABLE', 503)
        rights = provider.current(access.authority, access.session_ref, profile_ref=access.profile_ref,
                                  connection_ref=str(source.pk), now=actual_now)
        ga._require(type(rights) is SourceRights and type(rights.policy) is FieldPolicy)
        rights.validate(actual_now)
        ga._require(rights.source == 'tmdb_tv')
        ga._require(all(rights.policy.allows(field, 'display') for field in SCALARS))
        end = min(publication.expires_at, evidence.source_valid_until, rights.policy.expires_at, view.expires_at)
        ga._require(actual_now < end, 'PREVIEW_STALE', 409)
        source_binding = {'attribution': {key: card[key] for key in SCALARS},
            'source_roots': card['source_roots'], 'rights_record_ref': card['rights_ref'],
            'connection': view.connection_binding,
            # Session freshness remains an independent minimum in valid_until;
            # ordinary activity cannot renew this provenance by changing a
            # projected session-clipped grant deadline.
            'grants': [{key: grant[key] for key in ('grant_id', 'grant_revision',
                'allowed_data_classes', 'state', 'issued_at')} for grant in view.grants],
            'rights': {'revision': rights.revision, 'terms_ref': rights.terms_ref,
                'privacy_ref': rights.privacy_ref, 'locale_ref': rights.locale_ref,
                'processor_ref': rights.processor_ref},
            'policy': {'ref': rights.policy.ref, 'revision': rights.policy.revision,
                'checked_at': ga._stamp(rights.policy.checked_at),
                'expires_at': ga._stamp(rights.policy.expires_at),
                'permissions': [[key, list(uses)] for key, uses in rights.policy.permissions]}}
        origin = {'schema': 'obtain-publication-origin/v1',
            'installation_ref': identity.installation_ref, 'profile_ref': identity.profile_ref,
            'run_ref': str(publication.run_id), 'source_connection_ref': str(source.pk),
            'publication_revision': publication_revision, 'candidate_key_digest': candidate_digest,
            'source_binding_digest': hashlib.sha256(canonical_bytes(source_binding)).hexdigest(),
            'valid_until': ga._stamp(end)}
        validate_publication_origin(origin)
        access.require(actor_id=identity.actor_id, profile_id=identity.profile_id, action='view')
        ga.assert_current(view)
        target = CurrentObtainTarget(evidence.provider, evidence.medium, evidence.source_id,
            evidence.title, scope, end, rights.policy.allows('title', 'export'))
        return origin, target


def resolve_current_origin(access, *, publication_revision, candidate_key, scope, now):
    if type(candidate_key) is not str or not 0 < len(candidate_key) <= 300:
        raise Blocked('Current candidate key required')
    return _resolve(access, publication_revision=publication_revision,
                    candidate_digest=candidate_key_digest(candidate_key), scope=scope, now=now)


def resolve_origin_again(access, *, origin, scope, now):
    validate_publication_origin(origin)
    if now >= datetime.fromisoformat(origin['valid_until'].replace('Z', '+00:00')):
        raise Blocked('Current publication origin expired')
    fresh, target = _resolve(access, publication_revision=origin['publication_revision'],
        candidate_digest=origin['candidate_key_digest'], scope=scope, now=now)
    original_end = datetime.fromisoformat(origin['valid_until'].replace('Z', '+00:00'))
    if target.valid_until >= original_end:
        fresh = {**fresh, 'valid_until': origin['valid_until']}
        target = replace(target, valid_until=original_end)
    if fresh != origin:
        raise Blocked('Current publication origin changed')
    return target


def resolve_owner_selection(access, selection_ref, now):
    from .portable_owner_tv import get_owner_tv
    return get_owner_tv(access).resolve(selection_ref, now=now)[1]


def resolve_owner_selection_origin(access, selection_ref, now):
    from .portable_owner_tv import get_owner_tv
    return get_owner_tv(access).resolve(selection_ref, now=now)


def resolve_obtain_origin_again(access, origin, scope, now):
    validate_obtain_origin(origin)
    if origin['schema'] == 'obtain-publication-origin/v1':
        return resolve_origin_again(access, origin=origin, scope=scope, now=now)
    fresh, target = resolve_owner_selection_origin(access, origin['selection_ref'], now)
    if fresh != origin or target.scope != scope:
        raise Blocked('Current owner source selection changed')
    return target
