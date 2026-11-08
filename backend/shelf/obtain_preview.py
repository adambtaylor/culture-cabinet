"""Explicit search and durable local selection. No retrieval or submission port."""
from datetime import timedelta
from dataclasses import dataclass
import hashlib
import uuid

from django import forms
from django.core import signing
from django.db import IntegrityError, connection, transaction
from django.http import Http404
from django.utils import timezone

from shelf import credential_store as store, saved
from shelf.portable_saved_authority import effect_for, write_for, active_portable_access
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


@dataclass(frozen=True, repr=False)
class SearchRecipient:
    metadata: store.EntryMetadata
    credential_generation: str


def _record_recipient(record, actor, profile_id):
    metadata = record.metadata
    store.validate_metadata(metadata, now=store.utc_now(), provider='hydra')
    if (metadata.binding.actor_id, metadata.binding.profile_id) != (str(actor.pk), str(profile_id)):
        raise store.Refused()
    return SearchRecipient(metadata, record.generation)


def read_search_recipient(*, actor, profile_id):
    """Read the real credential-record generation without exporting its secret."""
    saved.authority(actor, profile_id)
    handle = record = None
    try:
        handle = store.open_store(provider='hydra')
        if handle.context != store._provider_context('hydra'):
            raise store.Refused()
        store.lock_store(handle); record = store.read_record(handle)
        return _record_recipient(record, actor, profile_id)
    except (store.Refused, OSError):
        raise SearchUnavailable('unconfigured') from None
    finally:
        if record is not None:
            record.close()
        if handle is not None:
            handle.close()


def _reviewed_backend(*, actor, profile_id, recipient, revalidate):
    def opener(*, provider):
        if provider != 'hydra':
            raise store.Refused()
        revalidate()
        handle = record = None
        try:
            handle = store.open_store(provider=provider)
            if handle.context != store._provider_context(provider):
                raise store.Refused()
            store.lock_store(handle); record = store.read_record(handle)
            if _record_recipient(record, actor, profile_id) != recipient:
                raise store.Refused()
            # Keep this SAME store lock until Backend closes the handle. A key
            # replacement cannot slip between our generation check and its
            # credential read; Backend retains its normal validation and I/O.
            record.close(); record = None
            result, handle = handle, None
            return result
        finally:
            if record is not None:
                record.close()
            if handle is not None:
                handle.close()
    return Backend(hydra_grant=recipient.metadata, _open_store=opener)


def origin_search_request(target):
    from .acquisition_contract import PublicRequest
    return PublicRequest('search', (('q', release_title(target) + ' ' + episode_code(target.scope)), ('cat', '5000')))


def _search_reviews(authority):
    if not hasattr(authority, '_obtain_search_reviews'):
        authority._obtain_search_reviews = {}
        authority.boundary._closing.append(lambda: authority._obtain_search_reviews.clear())
        def revoke(ref):
            authority._obtain_search_reviews = {key: value for key, value in authority._obtain_search_reviews.items()
                                               if value['session_ref'] != ref}
        authority._revokers.append(revoke)
    now = authority.now()
    authority._obtain_search_reviews = {key: value for key, value in authority._obtain_search_reviews.items()
                                       if now < value['expires_at']}
    return authority._obtain_search_reviews


def issue_search_review(*, access, origin, scope):
    from .portable_obtain_origin import resolve_obtain_origin_again
    from .disclosure import canonical_bytes
    access.require(actor_id=access.identity.actor_id, profile_id=access.identity.profile_id, action='export')
    with access.effect_window(action='export'):
        target = resolve_obtain_origin_again(access, origin, scope, access.authority.now())
        target.require_title_export()
        operation = origin_search_request(target)
        recipient = read_search_recipient(actor=access.actor, profile_id=access.identity.profile_id)
        reviews = _search_reviews(access.authority)
        if len(reviews) >= 64:
            raise saved.SavedError('Too many pending search reviews.', 409)
        end = min(target.valid_until, recipient.metadata.expires_at, recipient.metadata.declared_provider_expires_at,
                  access.authority.now() + timedelta(minutes=5))
        if not access.authority.now() < end:
            raise saved.SavedError('Search review expired.', 409)
        ref = str(uuid.uuid4())
        reviews[ref] = {'session_ref': access.session_ref, 'selection': dict(access._signed), 'origin': dict(origin),
            'scope': scope, 'operation_digest': hashlib.sha256(canonical_bytes(operation.public_dict())).hexdigest(),
            'recipient': recipient, 'expires_at': end, 'attempted': False}
        return {'search_review_ref': ref, 'query': dict(operation.params)['q'],
                'destination': operation.endpoint, 'expires_at': end.isoformat()}


def _checked_search_review(access, origin, scope, review_ref):
    from .portable_obtain_origin import resolve_obtain_origin_again
    from .disclosure import canonical_bytes
    reviews = _search_reviews(access.authority)
    value = reviews.get(str(review_ref))
    if (value is None or value['session_ref'] != access.session_ref or value['selection'] != access._signed
            or value['origin'] != origin or value['scope'] != scope):
        if value is not None and value['session_ref'] == access.session_ref:
            reviews.pop(str(review_ref), None)
        raise saved.SavedError('The reviewed search changed. Open fresh options.', 409)
    try:
        access.require(actor_id=access.identity.actor_id, profile_id=access.identity.profile_id, action='export')
        target = resolve_obtain_origin_again(access, origin, scope, access.authority.now())
        target.require_title_export()
        operation = origin_search_request(target)
        if (hashlib.sha256(canonical_bytes(operation.public_dict())).hexdigest() != value['operation_digest']
                or read_search_recipient(actor=access.actor, profile_id=access.identity.profile_id) != value['recipient']):
            raise saved.SavedError('The reviewed query or indexer connection changed. Review again.', 409)
        return value, target, operation
    except Exception:
        reviews.pop(str(review_ref), None)
        raise


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
    if preview.publication_origin is not None:
        from .portable_saved_authority import active_portable_access
        from .portable_obtain_origin import resolve_obtain_origin_again
        access = active_portable_access()
        if access is None or (preview.actor_id, preview.profile_id) != (actor.pk, profile_id):
            raise saved.SavedError('Current native source selection required.', 409)
        access.require(actor_id=actor.pk, profile_id=profile_id, action='view')
        return resolve_obtain_origin_again(access, preview.publication_origin, preview.scope, access.authority.now())
    if preview.provider == 'album_request':
        from shelf.album_acquisition import current_request
        return current_request(actor, profile_id, preview)
    item = saved.owned_item(actor, profile_id, preview.saved_identity)
    expected = identity(item, preview.scope)
    if expected != {k: getattr(preview, k) for k in expected}:
        raise saved.SavedError('Saved identity changed. Start a fresh preview.', 409)
    return item


def release_title(target):
    """One title/year value drives query construction and every release check."""
    year = getattr(target, 'series_year', None)
    return plain(target.title + (' ' + str(year) if year is not None else ''))


def receipt_now(receipt):
    if receipt.publication_origin is not None:
        from .portable_saved_authority import active_portable_access
        access = active_portable_access()
        if access is None:
            raise saved.SavedError('Current native source selection required.', 409)
        return access.authority.now()
    return timezone.now()


def search_origin(*, access, origin, scope, search_review_ref, factory=None):
    from .portable_obtain_origin import resolve_obtain_origin_again
    actor, profile_id = access.actor, access.identity.profile_id
    require_available()
    def current():
        return _checked_search_review(access, origin, scope, search_review_ref)
    with access.effect_window(action='export'):
        review, target, operation = current()
        if review['attempted']:
            raise saved.SavedError('This reviewed search has already been attempted. Open a fresh review.', 409)
        title = release_title(target)
        now = access.authority.now()
        with write_for(actor, profile_id):
            receipt = ObtainPreview.objects.create(actor=actor, profile_id=profile_id,
                provider=None, medium=None, source_id=None, title=None, saved_identity=None,
                scope=scope, publication_origin=origin, created_at=now,
                expires_at=min(now + timedelta(minutes=15), target.valid_until, review['expires_at']))
        private = None; rows = []; state = 'failed'
        try:
            review['attempted'] = True
            def fence():
                _, fresh, current_operation = current()
                if release_title(fresh) != title or current_operation != operation:
                    raise Blocked('Current source or indexer destination changed')
            fence()
            backend = (factory(actor=actor, profile_id=profile_id) if factory is not None else
                _reviewed_backend(actor=actor, profile_id=profile_id, recipient=review['recipient'], revalidate=fence))
            if backend._grants['hydra'] != review['recipient'].metadata:
                raise Blocked('Reviewed indexer destination changed')
            caps = backend.caps(actor_id=actor.pk, profile_id=profile_id)
            fence()
            from .acquisition_contract import search_request
            if search_request(caps, dict(operation.params)['q'], 'episode') != operation:
                raise Blocked('Reviewed search is not advertised')
            private = backend.search(caps, dict(operation.params)['q'], 'episode',
                                     actor_id=actor.pk, profile_id=profile_id)
            fence()
            if type(private) is not PrivateSearch:
                raise ContractError('Invalid private search')
            rows, omitted = parse(private._payload, title=title, scope=scope)
            state = 'partial' if omitted else ('results' if rows else 'empty')
        except SearchUnavailable as fault:
            state = fault.state
        except (Blocked, ContractError):
            state = 'failed'
        finally:
            if type(private) is PrivateSearch:
                private.close()
        # The source target is still live here; only independently obtained
        # Hydra release metadata enters SQL. Catalogue facts remain NULL.
        current()
        with write_for(actor, profile_id):
            ObtainResult.objects.bulk_create([ObtainResult(preview=receipt, guid=row['guid'],
                metadata=row, digest=digest(row)) for row in rows])
            receipt.state = state
            receipt.save(update_fields=['state'])
        return receipt


def search(*, actor, profile_id, item_id, factory=None, season=None, episode=None):
    item = saved.owned_item(actor, profile_id, item_id)
    scope = selected_scope(item, season, episode)
    require_available()
    try:
        plain(item.title)
    except ContractError:
        raise saved.SavedError('Saved title cannot be searched safely.', 409) from None
    now = timezone.now()
    with write_for(actor, profile_id):
        preview = ObtainPreview.objects.create(actor=actor, profile_id=profile_id,
            **identity(item, scope), created_at=now, expires_at=now + timedelta(minutes=15))
    state, rows = 'failed', []
    private = None
    try:
        # Provider metadata cannot validate PA1's live generation. Fence backend
        # acquisition and caps themselves, not only their later result consumer.
        with effect_for(actor, profile_id):
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
    with write_for(actor, profile_id):
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
    target = current_item(actor, profile_id, preview)
    if preview.expires_at <= receipt_now(preview) or preview.state not in ('results', 'partial'):
        raise saved.SavedError('Preview expired or unavailable. Search again.', 409)
    if (not result.metadata['selectable'] or digest(result.metadata) != result.digest
            or listing_reason(result.metadata, release_title(target), preview.scope)):
        raise saved.SavedError('Listing cannot be selected.', 409)
    return preview, result


def confirm(*, actor, profile_id, preview_id, result_id, data):
    receipt = owned_preview(actor, profile_id, preview_id)
    if receipt.publication_origin is not None:
        return _confirm_origin(actor=actor, profile_id=profile_id, preview_id=preview_id,
                               result_id=result_id, data=data)
    with write_for(actor, profile_id):
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


def _confirm_origin(*, actor, profile_id, preview_id, result_id, data):
    from .portable_saved_authority import active_portable_access
    access = active_portable_access()
    if access is None:
        raise saved.SavedError('Current native selection required.', 409)
    with access.effect_window(action='mutate'):
        receipt, result = review(actor=actor, profile_id=profile_id, preview_id=preview_id, result_id=result_id)
        target = current_item(actor, profile_id, receipt)
        form = SelectionForm(data)
        try:
            token = signing.loads(data.get('review_token', ''), salt=SALT, max_age=900)
            expected = signing.loads(review_token(receipt, result), salt=SALT)
        except signing.BadSignature:
            raise saved.SavedError('Review expired or invalid.', 409) from None
        if token != expected or not form.is_valid():
            raise saved.SavedError('Review conflict or invalid confirmation.', 409)
        clean = form.cleaned_data
        if clean['scope'] != receipt.scope or clean['edition'] != result.metadata['title']:
            raise saved.SavedError('Exact release and episode required.', 409)
        active = saved.active_identity_key(actor.pk, profile_id, target.provider, target.medium,
                                            str(target.source_id), receipt.scope)
        values = {key: None for key in ('provider', 'medium', 'source_id', 'title')}
        values.update(publication_origin=receipt.publication_origin, scope=receipt.scope)
        values.update({key: clean[key] for key in ('edition', 'storage', 'authorized_content', 'storage_check')})
        with write_for(actor, profile_id):
            try:
                with transaction.atomic():
                    job, created = AcquisitionJob.objects.get_or_create(actor=actor, profile_id=profile_id,
                        request_id=receipt.pk, defaults={**values, 'saved_item': None, 'active_key': active,
                            'status': 'blocked_configuration', 'reason': DISABLED})
                    if created:
                        ObtainSelection.objects.create(job=job, result=result)
            except IntegrityError:
                job = AcquisitionJob.objects.filter(actor=actor, profile_id=profile_id, active_key=active).first()
                created = False
            if (job is None or job.request_id != receipt.pk or job.status != 'blocked_configuration'
                    or not ObtainSelection.objects.filter(job=job, result=result).exists()
                    or any(getattr(job, key) != value for key, value in values.items())):
                raise saved.SavedError('An existing choice conflicts; read its retained status.', 409)
        current_item(actor, profile_id, receipt)
        return job, created


def context(preview):
    base = f'/profiles/{preview.profile_id}/saved/previews/{preview.pk}/'
    return {'id': str(preview.pk), 'title': preview.title or 'Selected TV episode',
        'state': 'expired' if preview.expires_at <= receipt_now(preview) else preview.state,
        'scope': preview.scope, 'episode_code': episode_code(preview.scope), 'expires_at': preview.expires_at.isoformat(),
        'submission_enabled': False, 'message': DISABLED,
        'results': [{**r.metadata, 'id': str(r.pk), 'digest': r.digest,
                     'review_url': base + f'results/{r.pk}/'} for r in preview.results.all()]}
