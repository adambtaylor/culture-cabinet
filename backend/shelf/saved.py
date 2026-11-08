"""Durable human intent and an entirely local, unconfigured request boundary."""
import hashlib
import re
import uuid

from django import forms
from django.core import signing
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.http import Http404
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from shelf import intake
from shelf.episode_scope import episode_code
from shelf.models import AcquisitionJob, SavedItem

LISTS = (('watch', 'To watch'), ('read', 'To read'), ('favorites', 'Favourites'))
SCOPES = {'movie': 'film', 'tv': 'series_s01e01', 'book': 'audiobook'}
PROVIDERS = {'tmdb': 'TMDB', 'hardcover': 'Hardcover'}
MEDIA = {'movie': 'Film', 'tv': 'Series', 'book': 'Book'}
SALT = 'saved-obtain-v1'
UNCONFIGURED = ('Culture Cabinet acquisition is unconfigured; no indexer connection or permissions '
                'are confirmed for this app. Availability, size and storage capacity are unknown. '
                'No download has been queued or started.')


class SavedError(Exception):
    def __init__(self, message='Invalid saved-item request.', status=400):
        self.message, self.status = message, status
        super().__init__(message)


def available():
    # Do not touch absent feature tables on the original 0001 runtime.
    tables = set(connection.introspection.table_names())
    return {SavedItem._meta.db_table, AcquisitionJob._meta.db_table} <= tables


def require_available():
    if not available():
        raise SavedError('Saved storage requires explicit operator preparation.', 503)


def authority(actor, profile_id, action='select'):
    from shelf.portable_saved_authority import active_portable_access
    access = active_portable_access()
    if access is not None:
        # An active refused/closed portable scope never falls back to provider
        # authority. Legacy callers without a scope retain their original path.
        return access.require(actor_id=actor.pk, profile_id=profile_id, action=action)
    from shelf.provider_views import _profiles
    if not _profiles(actor, action).filter(pk=profile_id).exists():
        raise Http404


def canonical_url(provider, medium, source_id, url):
    """Rebuild only fixed source URLs; no query, fragment, credentials or redirect."""
    if type(source_id) is not int or not 0 < source_id <= 2147483647:
        return ''
    if provider == 'tmdb' and medium in ('movie', 'tv'):
        # The numeric identity determines the source page, even if a stored URL changed.
        return f'https://www.themoviedb.org/{medium}/{source_id}'
    if provider == 'hardcover' and medium == 'book' and isinstance(url, str):
        match = re.fullmatch(r'https://hardcover\.app/books/([a-z0-9]+(?:-[a-z0-9]+)*)', url, re.ASCII)
        if match and len(match[1]) <= 200:
            return 'https://hardcover.app/books/' + match[1]
    return ''


def catalogue_context(profile_id):
    enabled = available()
    return {'saved_available': enabled,
            'saved_url': f'/profiles/{profile_id}/saved/' if enabled else '',
            'save_url': f'/profiles/{profile_id}/saved/save/' if enabled else ''}


def save_candidate(*, actor, profile_id, session, item_id, list_kind):
    authority(actor, profile_id)
    require_available()
    state = intake.load(session, actor.pk, profile_id)
    candidate = state and state['candidates'].get(item_id)
    if not candidate:
        raise SavedError('Candidate is unavailable or intake has expired.', 409)
    provider, medium, source_id = (candidate.get(k) for k in ('provider', 'medium', 'source_id'))
    url = canonical_url(provider, medium, source_id, candidate.get('url'))
    fetched = parse_datetime(candidate.get('fetched_at', ''))
    title = candidate.get('title')
    if (not url or candidate.get('url') != url
            or item_id != f'{provider}:{medium}:{source_id}'
            or not isinstance(title, str) or not title.strip() or len(title) > 300
            or fetched is None or timezone.is_naive(fetched)):
        raise SavedError('Source identity or provenance is invalid.')
    if not (list_kind == 'favorites' or list_kind == ('read' if medium == 'book' else 'watch')):
        raise SavedError('List does not support this medium.')
    from shelf.grant_authority import writer_window
    with transaction.atomic(), writer_window(actor, profile_id):
        item, created = SavedItem.objects.get_or_create(
            actor=actor, profile_id=profile_id, provider=provider, medium=medium,
            source_id=source_id, list_kind=list_kind,
            defaults={'title': title, 'url': url, 'fetched_at': fetched})
    return item, created



def save_publication_candidate(*, access, service, expected_publication_revision, candidate_key, list_kind):
    """PS1 owns current/source-rights evidence; PA1 owns live human authority."""
    from shelf.portable_saved_authority import active_portable_access
    from shelf.portable_enhance_service import EnhanceService
    from shelf.models import ProfileCabinetState, validate_publication_lineage
    from shelf import grant_authority as ga
    ga._require(active_portable_access() is access and type(service) is EnhanceService
                and service.authority is access.authority)
    identity = access.require(actor_id=access.identity.actor_id, profile_id=access.identity.profile_id, action='mutate')
    evidence = service.resolve_saved_candidate(access.session_ref, profile_ref=identity.profile_ref,
        expected_publication_revision=expected_publication_revision, candidate_key=candidate_key)
    # read_current may commit privacy redaction. PS1 performs it BEFORE this
    # transaction; its window retains the owner/recovery lock across this write.
    with service.saved_candidate_window(access.session_ref, evidence=evidence) as current:
        ga._require(current.schema == 'portable-saved-candidate/v1'
            and (str(current.installation_ref), current.owner_revision, str(current.profile_ref), current.actor_id, current.profile_id) ==
                (identity.installation_ref, identity.owner_revision, identity.profile_ref, identity.actor_id, identity.profile_id))
        provider, medium, source_id = current.provider, current.medium, current.source_id
        title, url = current.title, canonical_url(provider, medium, source_id, current.url)
        fetched = parse_datetime(current.fetched_at) if type(current.fetched_at) is str else current.fetched_at
        until = parse_datetime(current.source_valid_until) if type(current.source_valid_until) is str else current.source_valid_until
        if (not url or url != current.url or type(title) is not str or not title.strip() or len(title) > 300
                or fetched is None or timezone.is_naive(fetched) or until is None or timezone.is_naive(until)
                or until <= access.authority.now()):
            raise SavedError('Current source attribution is unavailable.', 409)
        if not (list_kind == 'favorites' or list_kind == ('read' if medium == 'book' else 'watch')):
            raise SavedError('List does not support this medium.')
        lineage = {'schema': 'portable-saved-lineage/v1'}
        for key in ('installation_ref', 'owner_revision', 'profile_ref', 'publication_ref',
                    'publication_revision', 'run_ref', 'candidate_key', 'candidate_digest', 'source_evidence_digest'):
            value = getattr(current, key)
            lineage[key] = str(value) if key.endswith('_ref') else value
        try:
            validate_publication_lineage(lineage)
        except (ValueError, TypeError, AttributeError):
            raise SavedError('Current publication lineage is unavailable.', 409) from None
        with ga.current_writer(access.authority, access.session_ref, identity.profile_ref), transaction.atomic(), ga.writer_window(access.actor, identity.profile_id):
            before = ProfileCabinetState.objects.get(profile_id=identity.profile_id)
            item, created = SavedItem.objects.get_or_create(actor_id=identity.actor_id, profile_id=identity.profile_id,
                provider=provider, medium=medium, source_id=source_id, list_kind=list_kind,
                defaults={'title': title, 'url': url, 'fetched_at': fetched, 'publication_lineage': lineage})
            after = ProfileCabinetState.objects.get(profile_id=identity.profile_id)
            # Save intentionally invalidates the old publication once via its
            # existing floor triggers. Do not re-read that old binding after our
            # own write; allow only this exact advancement, and no duplicate write.
            ga._require((after.policy_epoch, after.restriction_watermark) ==
                (before.policy_epoch + int(created), before.restriction_watermark + int(created)),
                'REVISION_CONFLICT', 409)
            access.require(actor_id=identity.actor_id, profile_id=identity.profile_id, action='mutate')
        return item, created

def owned_item(actor, profile_id, item_id, action='select'):
    authority(actor, profile_id, action)
    require_available()
    try:
        return SavedItem.objects.get(pk=item_id, actor=actor, profile_id=profile_id)
    except SavedItem.DoesNotExist:
        raise Http404 from None


def remove(*, actor, profile_id, item_id):
    # Jobs retain their immutable snapshot when the heart is removed.
    from shelf.grant_authority import writer_window
    with transaction.atomic(), writer_window(actor, profile_id):
        authority(actor, profile_id)
        require_available()
        SavedItem.objects.filter(pk=item_id, actor=actor, profile_id=profile_id).delete()


def active_key(item, scope=None):
    return active_identity_key(item.actor_id, item.profile_id, item.provider, item.medium, item.source_id, scope)


def active_identity_key(actor_id, profile_id, provider, medium, source_id, scope=None):
    value = f'{actor_id}:{profile_id}:{provider}:{medium}:{source_id}'
    # Explicit pilot choices share the original key, preserving old active jobs.
    if scope is not None and scope.startswith('episode_') and episode_code(scope) != 'S01E01':
        value += ':' + scope
    return hashlib.sha256(value.encode('ascii')).hexdigest()


class UnconfiguredAcquisition:
    """Provider-neutral adapter: no transport, search, credentials or filesystem I/O."""
    @staticmethod
    def configuration():
        return {'status': 'blocked_configuration', 'reason': UNCONFIGURED,
                'availability': 'unknown', 'size': 'unknown', 'storage_capacity': 'unknown'}


class ConfirmationForm(forms.Form):
    review_token = forms.CharField(max_length=2000, widget=forms.HiddenInput)
    scope = forms.ChoiceField(choices=[(s, s) for s in SCOPES.values()], widget=forms.HiddenInput)
    edition = forms.CharField(max_length=200, strip=True)
    authorized_content = forms.BooleanField(
        label='I have the legal right to obtain this public-domain or licensed selection.')
    storage = forms.ChoiceField(choices=[('local_library', 'Local library (destination unconfigured)')])
    storage_check = forms.BooleanField(
        label='I acknowledge that destination and capacity are unknown; no transfer can start.')

    def clean_edition(self):
        edition = self.cleaned_data['edition']
        if any(ord(c) < 32 for c in edition):
            raise forms.ValidationError('Enter an explicit edition label.')
        return edition


def review_token(item):
    previous = AcquisitionJob.objects.filter(actor_id=item.actor_id, profile_id=item.profile_id,
        provider=item.provider, medium=item.medium, source_id=item.source_id,
        scope__in=(('series_s01e01', 'episode_s01e01') if item.medium == 'tv'
                   else (SCOPES[item.medium],))).order_by('-id').first()
    # All reviews of one source share a receipt until explicit cancellation and
    # a fresh review. Even a losing double click cannot replay into a new job.
    request_id = (previous.request_id if previous and previous.status != 'cancelled' else
                  uuid.uuid5(uuid.NAMESPACE_URL, active_key(item) + ':' + str(previous.pk if previous else 0)))
    return signing.dumps({'actor': item.actor_id, 'profile': item.profile_id,
                          'item': item.pk, 'scope': SCOPES[item.medium],
                          'request_id': str(request_id)}, salt=SALT)


def confirm(*, actor, profile_id, item_id, data):
    from .portable_saved_authority import write_for
    item = owned_item(actor, profile_id, item_id)
    try:
        token = signing.loads(data['review_token'], salt=SALT, max_age=3600)
        request_id = uuid.UUID(token['request_id'])
    except (signing.BadSignature, KeyError, ValueError, TypeError):
        raise SavedError('Review expired or invalid. Review the saved item again.', 409) from None
    if (token.get('actor'), token.get('profile'), token.get('item'), token.get('scope')) != (
            actor.pk, profile_id, item.pk, SCOPES[item.medium]):
        raise SavedError('Review does not belong to this selection.', 409)
    form = ConfirmationForm(data)
    if not form.is_valid() or form.cleaned_data['scope'] != SCOPES[item.medium]:
        raise SavedError('Confirm the exact scope, edition, legal right and storage check.')
    clean = form.cleaned_data
    values = {k: clean[k] for k in ('scope', 'edition', 'storage', 'authorized_content', 'storage_check')}
    values.update(provider=item.provider, medium=item.medium, source_id=item.source_id, title=item.title)
    configuration = UnconfiguredAcquisition.configuration()
    key = active_key(item)
    try:
        with write_for(actor, profile_id):
            job, created = AcquisitionJob.objects.get_or_create(
                actor=actor, profile_id=profile_id, request_id=request_id,
                defaults={**values, 'saved_item': item, 'active_key': key,
                          'status': configuration['status'], 'reason': configuration['reason']})
    except IntegrityError:
        # Another review/tab won the active identity constraint. Never enqueue twice.
        job = AcquisitionJob.objects.filter(active_key=key, actor=actor, profile_id=profile_id).first()
        if job is None:
            raise
        created = False
    if any(getattr(job, k) != value for k, value in values.items()):
        raise SavedError('An existing request has a different edition or selection. Cancel it before a new review.', 409)
    return job, created


def cancel(*, actor, profile_id, job_id):
    from .portable_saved_authority import write_for
    authority(actor, profile_id)
    require_available()
    with write_for(actor, profile_id):
        jobs = AcquisitionJob.objects.filter(pk=job_id, actor=actor, profile_id=profile_id)
        if not jobs.update(reason=F('reason')):
            raise Http404
        from shelf.obtain_acquisition import cancel_local, transfer_for
        job = jobs.get()
        if transfer_for(job, reconcile=False) is not None and cancel_local(job):
            return jobs.get()
        jobs.filter(status='blocked_configuration').update(
            status='cancelled', active_key=None, cancelled_at=timezone.now())
        return jobs.get()


def job_context(job):
    from shelf.obtain_acquisition import transfer_for
    from shelf.completion_consumer import available
    from shelf.models import PostprocessReceipt
    from shelf.completion_status import status_for
    transfer = transfer_for(job)
    completion = (PostprocessReceipt.objects.filter(transfer=transfer).first()
                  if transfer and available() else None)
    return {'id': job.pk, 'title': job.title, 'edition': job.edition, 'scope': job.scope,
            'episode_code': episode_code(job.scope),
            'status': transfer.state if transfer else job.status,
            'reason': transfer.reason if transfer else job.reason, 'storage': job.storage,
            'submission_enabled': bool(transfer),
            'has_transfer': bool(transfer),
            'postprocess_state': completion.state if completion else None,
            'completion_status': status_for(transfer),
            'library_url': f'/profiles/{job.profile_id}/acquired-files/',
            'submission_review_url': (f'/profiles/{job.profile_id}/saved/jobs/{job.pk}/submit/review/'
                                      if not transfer and job.status == 'blocked_configuration' else None),
            'local_handoff_url': (f'/profiles/{job.profile_id}/saved/jobs/{job.pk}/local-handoff/'
                                  if transfer and transfer.state == 'local_verified' else None),
            'authorized_content': job.authorized_content, 'storage_check': job.storage_check,
            'availability': 'local bytes verified; indexing and playback separate' if transfer and transfer.state == 'local_verified' else 'unknown',
            'size': transfer.binding['exact']['size'] if transfer else 'unknown',
            'storage_capacity': 'unknown',
            'cancel_url': f'/profiles/{job.profile_id}/saved/jobs/{job.pk}/cancel/',
            'status_url': f'/profiles/{job.profile_id}/saved/jobs/{job.pk}/'}


def lists_context(actor, profile_id):
    authority(actor, profile_id, 'view')
    context = catalogue_context(profile_id)
    context.update(obtain_available=context['saved_available'], saved_lists=[], acquisition_jobs=[])
    if not context['saved_available']:
        return context
    items = SavedItem.objects.filter(actor=actor, profile_id=profile_id)
    for kind, label in LISTS:
        rows = []
        for item in items:
            if item.list_kind == kind:
                base = f'/profiles/{profile_id}/saved/{item.pk}/'
                rows.append({'id': item.pk, 'title': item.title, 'medium_label': MEDIA.get(item.medium, ''),
                             'provider_label': PROVIDERS.get(item.provider, ''),
                             'fetched_at': item.fetched_at.isoformat(),
                             'url': canonical_url(item.provider, item.medium, item.source_id, item.url),
                             'obtain_preview_url': base + 'preview/', 'remove_url': base + 'remove/', 'obtain_review_url': base + 'obtain/'})
        context['saved_lists'].append({'label': label, 'items': rows})
    context['acquisition_jobs'] = [job_context(j) for j in AcquisitionJob.objects.filter(actor=actor, profile_id=profile_id)]
    return context
