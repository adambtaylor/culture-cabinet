"""Dormant D3 preview/reconstruction; no route, transport or Run capability.

Taste's fixed integration contract is capture(AuthorityView) -> profile-snapshot/v1
and serialize(snapshot, grants, context, CollectionView, *, now) -> frozen object
with body: bytes, serializer_version: str, omitted_groups: tuple[str, ...], and
expires_at: aware datetime. Only Taste's codec builds provider bytes. This service
reconstructs from server-held references, not browser JSON or a second body builder.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
from uuid import uuid4

from django.db import transaction

from . import grant_authority as grants
from .connection_validation import ConnectionValidation
from .disclosure import DisclosureError, canonical_bytes, read_json
from .inference_transport import ORIGIN, PATH
from .models import PortableDisclosurePreview
from .portable_authority import AuthorityFault


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, repr=False)
class CollectionView:
    """Immutable backend fixture seal; a production collector is not implemented."""
    ref: str
    collection_digest: str
    expires_at: datetime
    _material: bytes = field(repr=False)

    @property
    def records(self):
        return read_json(self._material)['records']

    @property
    def observations(self):
        return read_json(self._material)['observations']

    @property
    def policies(self):
        return read_json(self._material)['policies']


class DisclosureServices:
    """Explicit backend composition, never created from a request or settings flag."""
    def __init__(self, authority, validation):
        grants._require(type(validation) is ConnectionValidation and validation.authority is authority
            and validation.vault.authority is authority, 'CAPABILITY_UNAVAILABLE', 503)
        grants._require(not hasattr(authority, '_disclosure_services'), 'REVISION_CONFLICT', 409)
        self.authority, self.validation = authority, validation
        self._collections, self._live = {}, {}
        authority._disclosure_services = self
        authority._disclosure_validation = validation
        authority.boundary._closing.append(self.close)

    @classmethod
    def isolated_test(cls, authority, validation):
        """A test DB is necessary; production HTTP has no fixture selector."""
        grants._require(grants._fixture_database(), 'CAPABILITY_UNAVAILABLE', 503)
        instance = cls(authority, validation)
        authority._disclosure_fixture_token = grants._FIXTURE_TOKEN
        return instance

    def close(self):
        self._collections.clear()
        self._live.clear()

    def fixture_collection(self, records, observations, policies, *, expires_at):
        grants._require(grants._fixture_database()
            and getattr(self.authority, '_disclosure_fixture_token', None) is grants._FIXTURE_TOKEN,
            'CAPABILITY_UNAVAILABLE', 503)
        grants._require(type(records) is list and len(records) <= 100
            and type(expires_at) is datetime and expires_at.utcoffset() == timedelta(0)
            and self.authority.now() < expires_at, 'CONTRACT_INVALID', 422)
        material = canonical_bytes({'records': records, 'observations': observations, 'policies': policies})
        # The fixture seal vouches only for its supplied test material. Rights and
        # transitive personal ancestry are still checked by the ONE Taste codec.
        self._collections = {key: value for key, value in self._collections.items()
                             if self.authority.now() < value.expires_at}
        grants._require(len(self._collections) < 32, 'LIMIT_EXCEEDED', 422)
        value = CollectionView(str(uuid4()), _digest(material), expires_at, material)
        self._collections[value.ref] = value
        return value

    def collection(self, ref):
        value = self._collections.get(str(grants._uuid(ref)))
        # No production source producer is admitted. Absence is capability failure,
        # never a fabricated empty-success pool or a caller-supplied replacement.
        grants._require(value is not None, 'CAPABILITY_UNAVAILABLE', 503)
        grants._require(self.authority.now() < value.expires_at, 'PREVIEW_STALE', 409)
        return value


def _services(authority):
    value = getattr(authority, '_disclosure_services', None)
    grants._require(type(value) is DisclosureServices and value.authority is authority,
                    'CAPABILITY_UNAVAILABLE', 503)
    return value


def _compose(view, collection, now):
    """Invoke only the committed Taste capture/codec seams; no fallback serializer."""
    try:
        from .profile_snapshot import capture
        from .inference_body import serialize
    except ImportError:
        raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503) from None
    snapshot = capture(view)
    grants._require(type(snapshot) is dict
        and snapshot.get('profile_revision_digest') == view.profile_revision_digest
        and snapshot.get('installation_id') == view.installation_id
        and snapshot.get('profile_ref') == view.profile_ref, 'PREVIEW_STALE', 409)
    return serialize(snapshot, view.grants, view.context, collection, now=now)


def _encode(view, collection, now):
    grants._require(bool(collection.records), 'NO_GROUNDED_CANDIDATES', 422)
    try:
        result = _compose(view, collection, now)
    except DisclosureError as fault:
        raise AuthorityFault(fault.code, 422) from None
    grants._require(all(hasattr(result, name) for name in
        ('body', 'serializer_version', 'omitted_groups', 'expires_at')), 'CONTRACT_INVALID', 422)
    grants._require(type(result.body) is bytes and 0 < len(result.body) <= 262144,
                    'LIMIT_EXCEEDED', 422)
    grants._require(type(result.serializer_version) is str and 0 < len(result.serializer_version) <= 40
        and type(result.omitted_groups) is tuple and len(result.omitted_groups) <= len(grants.CLASSES)
        and all(type(value) is str and value in grants.CLASSES for value in result.omitted_groups)
        and len(set(result.omitted_groups)) == len(result.omitted_groups)
        and type(result.expires_at) is datetime and result.expires_at.utcoffset() == timedelta(0)
        and now < result.expires_at, 'CONTRACT_INVALID', 422)
    return result


def _bindings(view, collection, session_ref):
    return {'installation_id': view.installation_id, 'actor_ref': view.actor_ref,
        'profile_ref': view.profile_ref, 'subject_ref': view.subject_ref,
        'session_digest': _digest(session_ref.encode()),
        'connection_digest': _digest(view._connection_binding),
        'context_digest': _digest(view._context), 'grants_digest': _digest(view._grants),
        'policy_epoch': view.policy_epoch, 'restriction_watermark': view.restriction_watermark,
        'profile_revision_digest': view.profile_revision_digest,
        'collection_digest': collection.collection_digest}


def preview(authority, session_ref, *, profile_ref, connection_ref, collection_ref):
    grants._require(type(authority) is grants.PortableAuthority)
    with grants.recovery_guard(authority, session_ref, profile_ref=profile_ref) as checkpoint, authority._lock, grants._checked_transaction(checkpoint):
        services = _services(authority)
        view = grants.current_view(authority, session_ref, profile_ref=profile_ref,
                                   connection_ref=connection_ref, purpose='inference')
        collection = services.collection(collection_ref)
        now = authority.now()
        started = authority.monotonic()
        encoded = _encode(view, collection, now)
        grants.assert_current(view)
        expires = min(now + timedelta(minutes=5), view.expires_at, collection.expires_at, encoded.expires_at)
        deadline = started + (expires - now).total_seconds()
        grants._require(authority.now() < expires and authority.monotonic() < deadline, 'PREVIEW_STALE', 409)
        refs = {'profile_ref': view.profile_ref, 'connection_ref': view.connection_ref,
                'collection_ref': collection.ref}
        bindings = _bindings(view, collection, session_ref)
        services._live = {key: value for key, value in services._live.items()
                          if value > authority.monotonic()}
        grants._require(len(services._live) < 32, 'LIMIT_EXCEEDED', 422)
        checkpoint.check()
        with grants._write_window():
            row = PortableDisclosurePreview.objects.create(cabinet_id=view.profile_id,
                connection_id=grants._uuid(view.connection_ref), input_refs=refs, bindings=bindings,
                serializer_version=encoded.serializer_version, body_digest=_digest(encoded.body),
                omitted_groups=list(encoded.omitted_groups), created_at=now, expires_at=expires)
        services._live[row.pk] = deadline
        return {'preview_ref': str(row.pk), 'body': encoded.body, 'body_digest': row.body_digest,
                'serializer_version': row.serializer_version, 'omitted_groups': tuple(row.omitted_groups),
                'expires_at': expires, 'destination': ORIGIN + PATH, 'run_ready': False}


def revalidate(preview_ref, authority, session_ref):
    """Reauthorize and reproduce bytes; lost process state never revives a preview."""
    grants._require(type(authority) is grants.PortableAuthority)
    with grants.recovery_guard(authority, session_ref) as checkpoint, authority._lock:
        services = _services(authority)
        installation, session = grants._owner(authority, session_ref)
        row = PortableDisclosurePreview.objects.filter(pk=grants._uuid(preview_ref),
            cabinet__installation=installation, cabinet__actor_id=session.owner_id).first()
        grants._require(row is not None)
        grants._require(row.bindings.get('session_digest') == _digest(session_ref.encode()))
        try:
            with grants._checked_transaction(checkpoint):
                now = authority.now()
                grants._require(row.state == 'preview' and now < row.expires_at
                    and authority.monotonic() < services._live.get(row.pk, 0), 'PREVIEW_STALE', 409)
                refs = row.input_refs
                grants._require(set(refs) == {'profile_ref', 'connection_ref', 'collection_ref'}, 'PREVIEW_STALE', 409)
                view = grants.current_view(authority, session_ref, profile_ref=refs['profile_ref'],
                    connection_ref=refs['connection_ref'], purpose='inference')
                collection = services.collection(refs['collection_ref'])
                grants._require(_bindings(view, collection, session_ref) == row.bindings, 'PREVIEW_STALE', 409)
                encoded = _encode(view, collection, now)
                grants.assert_current(view)
                grants._require(_digest(encoded.body) == row.body_digest
                    and encoded.serializer_version == row.serializer_version
                    and list(encoded.omitted_groups) == row.omitted_groups
                    and row.expires_at <= encoded.expires_at, 'PREVIEW_STALE', 409)
                return encoded.body
        except AuthorityFault as fault:
            if fault.code == 'CAPABILITY_UNAVAILABLE':
                raise
            checkpoint.check()
            # Persist invalidation outside the failed reconstruction transaction.
            # No body is retained and no exception can authorize capture or send.
            with transaction.atomic(), grants._write_window():
                PortableDisclosurePreview.objects.filter(pk=row.pk, state='preview').update(state='stale')
            raise


def isolated_capture(preview_ref, authority, session_ref):
    """Test-only byte capture; no callback, transport selection or external effect."""
    with grants.recovery_guard(authority, session_ref) as checkpoint:
        grants._require(grants._fixture_database()
            and getattr(authority, '_disclosure_fixture_token', None) is grants._FIXTURE_TOKEN,
            'CAPABILITY_UNAVAILABLE', 503)
        return revalidate(preview_ref, authority, session_ref)
