"""Explicit search and durable local selection. No retrieval or submission port."""
from datetime import timedelta
import uuid

from django import forms
from django.core import signing
from django.db import IntegrityError, connection, transaction
from django.http import Http404
from django.utils import timezone

from shelf import credential_store as store, saved
from shelf.portable_saved_authority import effect_for
from shelf.acquisition_backend import Backend, PrivateSearch
from shelf.acquisition_contract import Blocked, ContractError
from shelf.models import AcquisitionJob, ObtainPreview, ObtainResult, ObtainSelection
from shelf.obtain_parser import digest, listing_reason, parse, plain
from shelf.episode_scope import ScopeError, episode_code, make_scope, parse_scope

SALT = 'obtain-preview-v1'
DISABLED = 'Submission disabled. Local ownership only; no download permission or content verification.'


class SearchUnavailable(Exception):
    def __init__(self, state):
        self.state = state


def available():
    tables = set(connection.introspection.table_names())
    return {ObtainPreview._meta.db_table, ObtainResult._meta.db_table,
            ObtainSelection._meta.db_table, AcquisitionJob._meta.db_table} <= tables


def require_available():
    if not available():
        raise saved.SavedError('Obtain preview storage unavailable; explicit operator preparation required.', 503)


def search_backend(*, actor, profile_id, _open_store=None):
    """Read existing trusted metadata only after current application authority.

    No grant is fabricated or extended. Backend independently rereads the record
    and validates its binding/expiry before each caps/search operation.
    """
    saved.authority(actor, profile_id)
    opener = store.open_store if _open_store is None else _open_store
    handle = record = None
    try:
        handle = opener(provider='hydra')
        if handle.context != store._provider_context('hydra'):
            raise store.Refused()
        store.lock_store(handle)
        record = store.read_record(handle)
        metadata = record.metadata
        store.validate_metadata(metadata, now=store.utc_now(), provider='hydra')
        if (metadata.binding.actor_id, metadata.binding.profile_id) != (str(actor.pk), str(profile_id)):
            raise store.Refused()
    except (store.Refused, OSError) as error:
        raise SearchUnavailable('expired' if isinstance(error, store.Refused)
                                and error.reason == 'expired' else 'unconfigured') from None
    finally:
        if record is not None:
            record.close()
        if handle is not None:
            handle.close()
    return Backend(hydra_grant=metadata, _open_store=opener)


class SearchForm(forms.Form):
    season = forms.IntegerField(required=False, min_value=0, max_value=999)
    episode = forms.IntegerField(required=False, min_value=1, max_value=999)

    def clean(self):
        clean = super().clean()
        if (clean.get('season') is None) != (clean.get('episode') is None):
            raise forms.ValidationError('Choose both season and episode, or leave both blank for S01E01.')
        return clean


def selected_scope(item, season=None, episode=None):
    if season is None and episode is None:
        return saved.SCOPES[item.medium]
    if item.medium != 'tv':
        raise saved.SavedError('Episode selection requires a saved TV series.')
    try:
        return make_scope(season, episode)
    except ScopeError:
        raise saved.SavedError('Choose a season from 0–999 and an episode from 1–999.') from None


def identity(item, scope=None):
    scope = saved.SCOPES[item.medium] if scope is None else scope
    try:
        valid = parse_scope(scope) is not None if item.medium == 'tv' else scope == saved.SCOPES[item.medium]
    except ScopeError:
        valid = False
    if not valid:
        raise saved.SavedError('Saved scope changed. Start a fresh preview.', 409)
    return {k: getattr(item, k) for k in ('provider', 'medium', 'source_id', 'title')} | {
        'saved_identity': item.pk, 'scope': scope}


def current_item(actor, profile_id, preview):
    if preview.provider == 'album_request':
        from shelf.album_acquisition import current_request
        return current_request(actor, profile_id, preview)
    item = saved.owned_item(actor, profile_id, preview.saved_identity)
    expected = identity(item, preview.scope)
    if expected != {k: getattr(preview, k) for k in expected}:
        raise saved.SavedError('Saved identity changed. Start a fresh preview.', 409)
    return item


def search(*, actor, profile_id, item_id, factory=None, season=None, episode=None):
    item = saved.owned_item(actor, profile_id, item_id)
    scope = selected_scope(item, season, episode)
    require_available()
    try:
        plain(item.title)
    except ContractError:
        raise saved.SavedError('Saved title cannot be searched safely.', 409) from None
    now = timezone.now()
    with effect_for(actor, profile_id):
        preview = ObtainPreview.objects.create(actor=actor, profile_id=profile_id,
            **identity(item, scope), created_at=now, expires_at=now + timedelta(minutes=15))
    state, rows = 'failed', []
    private = None
    try:
        backend = (factory or search_backend)(actor=actor, profile_id=profile_id)
        caps = backend.caps(actor_id=actor.pk, profile_id=profile_id)
        # Caps is provider work too: do not start search after authority was revoked.
        current_item(actor, profile_id, preview)
        query = item.title
        if preview.scope.startswith('episode_'):
            query += ' ' + episode_code(preview.scope)
        with effect_for(actor, profile_id):
            private = backend.search(caps, query,
                {'movie': 'film', 'tv': 'episode', 'book': 'audiobook'}[item.medium],
                actor_id=actor.pk, profile_id=profile_id)
        if type(private) is not PrivateSearch:
            raise ContractError('Invalid private search')
        rows, omitted_rows = parse(private._payload, title=item.title, scope=preview.scope)
        state = 'partial' if omitted_rows else ('results' if rows else 'empty')
    except SearchUnavailable as error:
        state = error.state
    except (Blocked, ContractError):
        state = 'failed'
    finally:
        if type(private) is PrivateSearch:
            private.close()
    # No private payload or upstream exception is persisted, even on failure.
    with effect_for(actor, profile_id), transaction.atomic():
        current_item(actor, profile_id, preview)
        if preview.expires_at <= timezone.now():
            rows, state = [], 'expired'
        ObtainResult.objects.bulk_create([ObtainResult(preview=preview, guid=row['guid'],
            metadata=row, digest=digest(row)) for row in rows])
        preview.state = state
        preview.save(update_fields=['state'])
    return preview


def owned_preview(actor, profile_id, preview_id):
    saved.authority(actor, profile_id)
    require_available()
    try:
        return ObtainPreview.objects.get(pk=preview_id, actor=actor, profile_id=profile_id)
    except ObtainPreview.DoesNotExist:
        raise Http404 from None


def owned_result(actor, profile_id, preview_id, result_id):
    preview = owned_preview(actor, profile_id, preview_id)
    try:
        return preview, preview.results.get(pk=result_id)
    except ObtainResult.DoesNotExist:
        raise Http404 from None


def review_token(preview, result):
    return signing.dumps({'preview': str(preview.pk), 'result': str(result.pk),
        'actor': preview.actor_id, 'profile': preview.profile_id,
        'nonce': str(preview.nonce), 'version': preview.version, 'digest': result.digest,
        'scope': preview.scope, 'edition': result.metadata['title']}, salt=SALT)


class SelectionForm(saved.ConfirmationForm):
    """Edition must equal the selected listing title; it is still unverified."""
    edition = forms.CharField(max_length=200, strip=False)
    scope = forms.CharField(max_length=16, strip=False, widget=forms.HiddenInput)

    def clean_scope(self):
        scope = self.cleaned_data['scope']
        try:
            if scope not in saved.SCOPES.values() and parse_scope(scope) is None:
                raise ScopeError('Invalid scope')
        except ScopeError:
            raise forms.ValidationError('Confirm the exact selected scope.') from None
        return scope


def review(*, actor, profile_id, preview_id, result_id):
    preview, result = owned_result(actor, profile_id, preview_id, result_id)
    current_item(actor, profile_id, preview)
    if preview.expires_at <= timezone.now() or preview.state not in ('results', 'partial'):
        raise saved.SavedError('Preview expired or unavailable. Search again.', 409)
    if (not result.metadata['selectable'] or digest(result.metadata) != result.digest
            or listing_reason(result.metadata, preview.title, preview.scope)):
        raise saved.SavedError('Listing cannot be selected.', 409)
    return preview, result


def confirm(*, actor, profile_id, preview_id, result_id, data):
    with effect_for(actor, profile_id), transaction.atomic():
        preview, result = review(actor=actor, profile_id=profile_id,
                                 preview_id=preview_id, result_id=result_id)
        item = current_item(actor, profile_id, preview)
        form = SelectionForm(data)
        try:
            token = signing.loads(data.get('review_token', ''), salt=SALT, max_age=900)
            expected = signing.loads(review_token(preview, result), salt=SALT)
        except signing.BadSignature:
            raise saved.SavedError('Review expired or invalid.', 409) from None
        if token != expected or not form.is_valid():
            raise saved.SavedError('Review conflict or invalid confirmation.', 409)
        clean = form.cleaned_data
        if clean['scope'] != preview.scope or clean['edition'] != result.metadata['title']:
            raise saved.SavedError('Exact result, scope and listing edition required.', 409)
        values = {k: getattr(preview, k) for k in ('provider', 'medium', 'source_id', 'title', 'scope')}
        values.update({k: clean[k] for k in ('edition', 'storage', 'authorized_content', 'storage_check')})
        try:
            with transaction.atomic():
                job, created = AcquisitionJob.objects.get_or_create(actor=actor, profile_id=profile_id,
                    request_id=preview.pk, defaults={**values, 'saved_item': item,
                        'active_key': saved.active_key(item, preview.scope),
                        'status': 'blocked_configuration', 'reason': DISABLED})
                if created:
                    ObtainSelection.objects.create(job=job, result=result)
        except IntegrityError:
            job = AcquisitionJob.objects.filter(actor=actor, profile_id=profile_id,
                active_key=saved.active_key(item, preview.scope)).first()
            created = False
        if (job is None or job.request_id != preview.pk
                or not ObtainSelection.objects.filter(job=job, result=result).exists()
                or job.status != 'blocked_configuration'
                or any(getattr(job, k) != v for k, v in values.items())):
            raise saved.SavedError('An existing local choice conflicts. Start a fresh review after cancellation.', 409)
        return job, created


def context(preview):
    base = f'/profiles/{preview.profile_id}/saved/previews/{preview.pk}/'
    return {'id': str(preview.pk), 'title': preview.title,
        'state': 'expired' if preview.expires_at <= timezone.now() else preview.state,
        'scope': preview.scope, 'episode_code': episode_code(preview.scope), 'expires_at': preview.expires_at.isoformat(),
        'submission_enabled': False, 'message': DISABLED,
        'results': [{**r.metadata, 'id': str(r.pk), 'digest': r.digest,
                     'review_url': base + f'results/{r.pk}/'} for r in preview.results.all()]}
