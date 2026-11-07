"""Explicit operator album selection, reusing the existing submission machinery.

No save/like handler calls this entrypoint. Canonical release metadata and rights
are supplied separately; neither is inferred from a listing or downloaded bytes.
"""
from datetime import timedelta
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone

from shelf import obtain_preview, saved
from shelf.acquisition_backend import PrivateSearch
from shelf.acquisition_contract import Blocked, ContractError
from shelf.models import AlbumRequest, AcquisitionJob, ObtainPreview, ObtainResult, ObtainSelection
from shelf.obtain_parser import digest, parse, plain


def validate_identity(identity):
    if type(identity) is not dict or set(identity) != {
            'namespace', 'artist_id', 'release_id', 'artist', 'album', 'track_count', 'source'}:
        raise ContractError('Explicit album identity required')
    if identity['namespace'] not in {'musicbrainz', 'licensed_catalogue'}:
        raise ContractError('Unsupported album identity namespace')
    for key in ('artist_id', 'release_id', 'artist', 'album', 'source'):
        if plain(identity[key]) != identity[key]:
            raise ContractError('Canonical album identity required')
    if identity['namespace'] == 'musicbrainz':
        for key in ('artist_id', 'release_id'):
            try:
                valid = str(UUID(identity[key])) == identity[key]
            except ValueError:
                valid = False
            if not valid:
                raise ContractError('Canonical supplied MusicBrainz IDs required')
    if type(identity['track_count']) is not int or not 1 <= identity['track_count'] <= 128:
        raise ContractError('One bounded album track count required')
    plain(album_title(identity))


def album_title(identity):
    return identity['artist'] + ' - ' + identity['album']


def current_request(actor, profile_id, preview):
    saved.authority(actor, profile_id)
    try:
        request = AlbumRequest.objects.get(pk=preview.source_id, actor=actor,
            profile_id=profile_id, request_id=preview.pk)
    except AlbumRequest.DoesNotExist:
        raise Blocked('Album request unavailable') from None
    validate_identity(request.identity)
    validate_authorization(request.authorization)
    if request.release_key != release_key(request.identity):
        raise ContractError('Album release mapping changed')
    if (preview.provider, preview.medium, preview.scope, preview.saved_identity, preview.title) != (
            'album_request', 'music', 'album_flac', request.pk, album_title(request.identity)):
        raise ContractError('Album identity mapping changed')
    return request


def release_key(identity):
    return identity['namespace'] + ':' + identity['release_id']


def validate_authorization(authorization):
    if (type(authorization) is not dict or set(authorization) != {
            'rights_basis', 'one_album', 'storage_checked', 'duplicates_checked'}
            or any(authorization[k] is not True for k in
                   ('one_album', 'storage_checked', 'duplicates_checked'))):
        raise Blocked('Explicit rights and one-album authorization required')
    plain(authorization['rights_basis'])


def authorization_binding(job):
    request = AlbumRequest.objects.get(pk=job.source_id, request_id=job.request_id,
                                      actor_id=job.actor_id, profile_id=job.profile_id)
    validate_authorization(request.authorization)
    return {'authorization': request.authorization, 'authorized_at': request.authorized_at.isoformat()}


def prepare_album(*, actor, profile_id, identity, authorization, selected_guid,
                  selected_title, backend=None):
    """Search actual caps and bind one explicitly selected listing; never enqueue.

    The caller reviews the exact listing GUID/title and supplies an independent
    release identity. Ordinary submission review/confirmation is still required.
    """
    saved.authority(actor, profile_id)
    validate_identity(identity)
    validate_authorization(authorization)
    plain(selected_title)
    backend = backend or obtain_preview.search_backend(actor=actor, profile_id=profile_id)
    caps = backend.caps(actor_id=actor.pk, profile_id=profile_id)
    saved.authority(actor, profile_id)
    private = backend.search(caps, album_title(identity), 'music',
                             actor_id=actor.pk, profile_id=profile_id)
    if type(private) is not PrivateSearch:
        raise ContractError('Invalid private album search')
    try:
        rows, omitted_rows = parse(private._payload, title=album_title(identity), scope='album_flac')
    finally:
        private.close()
    selected = [row for row in rows if row['guid'] == selected_guid
                and row['title'] == selected_title and row['selectable'] and row['size'] > 0]
    if len(selected) != 1:
        raise Blocked('Exact lossless album release selection required')
    with transaction.atomic():
        saved.authority(actor, profile_id)
        # Retain previously submitted identities, including uncertain sends.
        if AlbumRequest.objects.filter(actor=actor, profile_id=profile_id,
                                       release_key=release_key(identity)).exists():
            raise Blocked('Existing album request requires review')
        request = AlbumRequest.objects.create(actor=actor, profile_id=profile_id,
            request_id=uuid4(), release_key=release_key(identity),
            identity=identity, authorization=authorization)
        if request.pk > 2147483647:
            raise Blocked('Album mapping capacity exhausted')
        now = timezone.now()
        preview = ObtainPreview.objects.create(id=request.request_id, actor=actor,
            profile_id=profile_id, saved_identity=request.pk, source_id=request.pk,
            provider='album_request', medium='music', scope='album_flac',
            title=album_title(identity), state='partial' if omitted_rows else 'results',
            expires_at=now + timedelta(minutes=15))
        metadata = selected[0] | {'album_identity': identity}
        result = ObtainResult.objects.create(preview=preview, guid=selected_guid,
                                            metadata=metadata, digest=digest(metadata))
        job = AcquisitionJob.objects.create(actor=actor, profile_id=profile_id,
            request_id=request.request_id, active_key='album:' + str(request.request_id),
            provider=preview.provider, medium=preview.medium, source_id=request.pk,
            title=preview.title, scope=preview.scope, edition=selected_title,
            storage='local_library', authorized_content=True, storage_check=True,
            reason='Explicit album choice; submission review required')
        ObtainSelection.objects.create(job=job, result=result)
    return job


def check_album_files(job, files):
    """Validate the exact track count while retaining bounded artwork/CUE evidence."""
    request = AlbumRequest.objects.get(pk=job.source_id, request_id=job.request_id,
                                      actor_id=job.actor_id, profile_id=job.profile_id)
    from shelf.album_files import validate_manifest
    validate_identity(request.identity)
    try:
        return validate_manifest(files, request.identity['track_count'])
    except (ValueError, TypeError, KeyError):
        raise ContractError('Declared complete album track set unavailable') from None

