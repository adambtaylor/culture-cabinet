"""Native portable Save/Obtain, reusing services without forging a Django user."""
from functools import wraps
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from urllib.parse import urlencode

from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.conf import settings
from django.urls import reverse, NoReverseMatch
from django.template import TemplateDoesNotExist
from django.db import IntegrityError, OperationalError
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from . import grant_authority as ga, saved, obtain_preview as preview, obtain_acquisition as acquire, obtain_runtime
from .models import AcquisitionJob, ProfileCabinetState, SavedItem, ObtainTransfer, ObtainLocalFile
from .portable_authority import AuthorityFault
from .portable_saved_authority import (portable_saved_scope, select_portable_profile,
                                       selected_context)

BASE = '/cabinet/saved/'
STATUS_SCRIPT = Path(__file__).resolve().parents[2] / 'static' / 'obtain.js'


def status_script(request):
    """Serve one immutable packaged asset, with no owner/session access."""
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
               'Cross-Origin-Resource-Policy': 'same-origin'}
    if request.method not in {'GET', 'HEAD'}:
        return HttpResponse('Method not allowed.\n', status=405,
                            headers={**headers, 'Allow': 'GET, HEAD'})
    if request.META.get('QUERY_STRING'):
        return HttpResponse('Unexpected asset parameters.\n', status=400, headers=headers)
    descriptor = None
    try:
        descriptor = os.open(STATUS_SCRIPT, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 65536:
            raise ValueError
        body = os.read(descriptor, 65537)
        after = os.fstat(descriptor)
        if (len(body) != before.st_size or len(body) > 65536
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                   (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError
        body.decode('utf-8')
    except (OSError, ValueError, UnicodeError):
        return HttpResponse('Packaged status script unavailable.\n', status=503, headers=headers)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    response = HttpResponse(body if request.method == 'GET' else b'', headers=headers,
                            content_type='application/javascript; charset=utf-8')
    response['Content-Length'] = str(len(body))
    return response


def _guard(function):
    @wraps(function)
    def view(request, *args, **kwargs):
        try:
            return function(request, *args, **kwargs)
        except (AuthorityFault, saved.SavedError) as fault:
            code = getattr(fault, 'code', 'SAVED_REQUEST_UNAVAILABLE')
            status = getattr(fault, 'status', 409)
            from . import portable_enhance_service
            if type(fault) is getattr(portable_enhance_service, 'SavedCandidateUnavailable', None):
                return HttpResponse(fault.message + '\n', status=status, content_type='text/plain')
            return HttpResponse(code + '. Reload and explicitly select your profile; do not resend an uncertain enqueue.\n', status=status, content_type='text/plain')
        except acquire.ContractError:
            return HttpResponse('Obtain unavailable or exact review changed. Reload the persisted receipt; do not retry an uncertain enqueue.\n', status=409, content_type='text/plain')
        except (IntegrityError, OperationalError):
            return HttpResponse('Saved/Obtain storage conflict. Read the retained request; do not resend an uncertain enqueue.\n', status=503, content_type='text/plain')
    return view


def _fields(request, fields):
    try:
        size = int(request.META.get('CONTENT_LENGTH') or 0)
    except ValueError:
        raise AuthorityFault('CONTRACT_INVALID', 422) from None
    # CSRF middleware may already have consumed a multipart stream. Its parsed
    # fields remain authoritative; do not try to reread request.body afterward.
    ga._require(request.content_type in ('application/x-www-form-urlencoded', 'multipart/form-data')
        and 0 <= size <= 16384 and not request.FILES, 'CONTRACT_INVALID', 422)
    keys = set(request.POST) - {'csrfmiddlewaretoken'}
    ga._require(keys <= fields and all(len(request.POST.getlist(key)) == 1 for key in request.POST), 'CONTRACT_INVALID', 422)
    return {key: request.POST[key] for key in keys}


def _scope(request, data=None, *, mutate=False):
    selection = request.session.get('portable_saved_selection')
    authority = request.portable_authority
    session = request.session.get('portable_session')
    if data is None:
        token = selected_context(authority, session, selection)
        profile_ref = selection['profile_ref']
    else:
        profile_ref, token = data.get('profile_ref'), data.get('selection_token')
    return portable_saved_scope(authority, session, profile_ref=profile_ref, selection_token=token, mutate=mutate)


def cabinet_saved_context(request, *, profile_ref):
    """N3's GET seam never initializes selection, grants or acquisition state."""
    selection = request.session.get('portable_saved_selection')
    if type(selection) is not dict or selection.get('profile_ref') != str(profile_ref):
        return {}
    try:
        token = selected_context(request.portable_authority, request.session.get('portable_session'), selection)
    except AuthorityFault:
        return {}
    return {'selection_token': token}


def _job(access, job):
    # Deliberately do not call transfer_for/job_context/completion_status: even
    # reconcile=False repairs registration. GET is a local projection, not work.
    access.require(actor_id=job.actor_id, profile_id=job.profile_id, action='view')
    transfer = ObtainTransfer.objects.filter(job=job).first()
    local = ObtainLocalFile.objects.filter(transfer=transfer).first() if transfer else None
    root = BASE + f'jobs/{job.pk}/'
    return {'id': job.pk, 'title': job.title, 'edition': job.edition, 'scope': job.scope,
        'storage': job.storage, 'status': transfer.state if transfer else job.status,
        'reason': transfer.reason if transfer else job.reason, 'has_transfer': transfer is not None,
        'provider_job': transfer.provider_job if transfer else '',
        'submission_enabled': transfer is not None, 'submission_review_url': root + 'submit/review/' if transfer is None and job.status == 'blocked_configuration' else None,
        'cancel_url': root + 'cancel/', 'status_url': root,
        'size': transfer.binding['exact']['size'] if transfer else 'unknown',
        'availability': 'local bytes verified; indexing and playback separate' if local else 'unknown',
        'storage_capacity': 'unknown', 'test_only': transfer.binding.get('test_only', True) if transfer else True,
        'indexed': False, 'playback_started': False, 'local_handoff_url': None}


def _context(access):
    identity = access.require(actor_id=access.identity.actor_id, profile_id=access.identity.profile_id, action='view')
    lists = []
    for key, label in saved.LISTS:
        rows = []
        for item in SavedItem.objects.filter(actor_id=identity.actor_id, profile_id=identity.profile_id, list_kind=key):
            rows.append({'id': item.pk, 'title': item.title, 'provider_label': saved.PROVIDERS.get(item.provider, ''),
                'medium_label': saved.MEDIA.get(item.medium, ''), 'url': saved.canonical_url(item.provider, item.medium, item.source_id, item.url),
                'fetched_at': item.fetched_at.isoformat(), 'scope': saved.SCOPES[item.medium], 'is_tv': item.medium == 'tv',
                'remove_url': BASE + f'{item.pk}/remove/', 'obtain_preview_url': BASE + f'{item.pk}/search/',
                'search_url': BASE + f'{item.pk}/search/'})
        lists.append({'label': label, 'items': rows})
    jobs = [_job(access, job) for job in AcquisitionJob.objects.filter(actor_id=identity.actor_id, profile_id=identity.profile_id)]
    return {'profile_ref': identity.profile_ref, 'selection_token': access.selection_token,
        'profile_label': ProfileCabinetState.objects.select_related('profile').get(profile_id=identity.profile_id).profile.label,
        'cabinet_url': '/setup/cabinet?profile_ref=' + identity.profile_ref,
        'saved_available': saved.available(), 'obtain_available': preview.available(), 'saved_url': BASE,
        'saved_lists': lists, 'acquisition_jobs': jobs, 'save_url': BASE + 'save/'}


def _render(request, access, template, **extra):
    from .enhancement_views import render_portable_saved
    context = {**_context(access), **extra}
    access.require(actor_id=access.identity.actor_id, profile_id=access.identity.profile_id, action='view')
    return render_portable_saved(request, template=template, context=context)


@require_POST
@csrf_protect
@_guard
def select(request):
    data = _fields(request, {'profile_ref'})
    ga._require(set(data) == {'profile_ref'}, 'CONTRACT_INVALID', 422)
    selection = select_portable_profile(request.portable_authority, request.session.get('portable_session'), profile_ref=data['profile_ref'])
    request.session['portable_saved_selection'] = selection
    return HttpResponseRedirect(BASE)


@require_GET
@_guard
def index(request):
    with _scope(request) as access:
        return _render(request, access, 'cabinet/portable_saved_items.html')


@require_POST
@csrf_protect
@_guard
def save(request):
    data = _fields(request, {'profile_ref', 'selection_token', 'publication_revision', 'candidate_key', 'list_kind'})
    ga._require(set(data) == {'profile_ref', 'selection_token', 'publication_revision', 'candidate_key', 'list_kind'}, 'CONTRACT_INVALID', 422)
    value = data['publication_revision']
    ga._require(len(value) <= 16 and value.isascii() and value.isdecimal() and str(int(value)) == value and 0 < int(value) <= 2**53 - 1, 'CONTRACT_INVALID', 422)
    with _scope(request, data, mutate=True) as access:
        from .portable_enhance_service import get_service
        saved.save_publication_candidate(access=access, service=get_service(access.authority),
            expected_publication_revision=int(value), candidate_key=data['candidate_key'], list_kind=data['list_kind'])
    return HttpResponseRedirect(BASE)


@require_POST
@csrf_protect
@_guard
def remove(request, item_id):
    data = _fields(request, {'profile_ref', 'selection_token'})
    with _scope(request, data, mutate=True) as access:
        with ga.current_writer(access.authority, access.session_ref, access.profile_ref):
            saved.remove(actor=access.actor, profile_id=access.identity.profile_id, item_id=item_id)
    return HttpResponseRedirect(BASE)


@require_POST
@csrf_protect
@_guard
def search(request, item_id):
    data = _fields(request, {'profile_ref', 'selection_token', 'season', 'episode'})
    form = preview.SearchForm({k: v for k, v in data.items() if k in ('season', 'episode')})
    ga._require(form.is_valid(), 'CONTRACT_INVALID', 422)
    with _scope(request, data, mutate=True) as access:
        row = preview.search(actor=access.actor, profile_id=access.identity.profile_id, item_id=item_id, **form.cleaned_data)
        access.require(actor_id=access.identity.actor_id, profile_id=access.identity.profile_id, action='select')
        target = BASE + f'previews/{row.pk}/'
    return HttpResponseRedirect(target)


def _preview_context(receipt):
    result = preview.context(receipt)
    for row in result['results']:
        row['review_url'] = BASE + f"previews/{receipt.pk}/{row['id']}/review/"
    return result


@require_GET
@_guard
def results(request, preview_ref):
    with _scope(request) as access:
        receipt = preview.owned_preview(access.actor, access.identity.profile_id, preview_ref)
        return _render(request, access, 'cabinet/portable_obtain_preview.html', obtain_preview=_preview_context(receipt))


@require_GET
@_guard
def review(request, preview_ref, result_ref):
    with _scope(request) as access:
        receipt, result = preview.review(actor=access.actor, profile_id=access.identity.profile_id, preview_id=preview_ref, result_id=result_ref)
        form = preview.SelectionForm(initial={'review_token': preview.review_token(receipt, result), 'scope': receipt.scope,
            'edition': result.metadata['title'], 'storage': 'local_library'})
        return _render(request, access, 'cabinet/portable_obtain_preview.html', obtain_preview=_preview_context(receipt),
            obtain_selected_result={**result.metadata, 'id': str(result.pk)}, obtain_result_form=form,
            obtain_result_confirm_url=BASE + f'previews/{receipt.pk}/{result.pk}/confirm/')


@require_POST
@csrf_protect
@_guard
def confirm(request, preview_ref, result_ref):
    data = _fields(request, {'profile_ref', 'selection_token'} | set(preview.SelectionForm.base_fields))
    with _scope(request, data, mutate=True) as access:
        job, _ = preview.confirm(actor=access.actor, profile_id=access.identity.profile_id,
            preview_id=preview_ref, result_id=result_ref, data={k: v for k, v in data.items() if k in preview.SelectionForm.base_fields})
        target = BASE + f'jobs/{job.pk}/'
    return HttpResponseRedirect(target)


@require_POST
@csrf_protect
@_guard
def submission_review(request, job_id):
    from .obtain_views import SubmissionForm
    data = _fields(request, {'profile_ref', 'selection_token'})
    with _scope(request, data, mutate=True) as access:
        job = acquire.owned_job(access.actor, access.identity.profile_id, job_id)
        try:
            with access.effect_window(action='select'):
                services = obtain_runtime.submission_factory(actor=access.actor, profile_id=access.identity.profile_id, job=job)
            result = acquire.review_submission(actor=access.actor, profile_id=access.identity.profile_id, job_id=job_id,
                context=services.context, evidence=services.evidence, max_bytes=services.max_bytes,
                retriever=services.retriever, complete_root=services.complete_root, test_only=services.test_only,
                completion_intent=services.completion_intent)
        except acquire.ContractError:
            # Render before the live scope closes. Both projections recheck PA1;
            # authority loss must still deny, without exposing retained job data.
            response = _render(request, access, 'cabinet/portable_saved_items.html',
                acquisition_job=_job(access, job), error_message=(
                    'Submission review is unavailable or has changed. '
                    'Read this request’s recorded status before taking further action.'))
            response.status_code = 409
            return response
        return _render(request, access, 'cabinet/portable_obtain_submission.html', acquisition_job=_job(access, job),
            obtain_submission=result['binding'], obtain_submit_form=SubmissionForm(initial={'token': result['token']}),
            obtain_submit_url=BASE + f'jobs/{job_id}/submit/')


@require_POST
@csrf_protect
@_guard
def submit(request, job_id):
    from .obtain_views import SubmissionForm
    data = _fields(request, {'profile_ref', 'selection_token'} | set(SubmissionForm.base_fields))
    form = SubmissionForm({k: v for k, v in data.items() if k in SubmissionForm.base_fields})
    ga._require(form.is_valid(), 'CONTRACT_INVALID', 422)
    with _scope(request, data, mutate=True) as access:
        job = acquire.owned_job(access.actor, access.identity.profile_id, job_id)
        with access.effect_window(action='select'):
            services = obtain_runtime.submission_factory(actor=access.actor, profile_id=access.identity.profile_id, job=job)
        acquire.submit(actor=access.actor, profile_id=access.identity.profile_id, job_id=job_id, context=services.context,
            token=form.cleaned_data['token'], confirmed=True, retriever=services.retriever, backend=services.backend,
            max_bytes=services.max_bytes, complete_root=services.complete_root, test_only=services.test_only,
            completion_intent=services.completion_intent)
    return HttpResponseRedirect(BASE + f'jobs/{job_id}/')


@require_GET
@_guard
def status(request, job_id):
    with _scope(request) as access:
        job = acquire.owned_job(access.actor, access.identity.profile_id, job_id)
        return _render(request, access, 'cabinet/portable_saved_items.html', acquisition_job=_job(access, job))


@require_POST
@csrf_protect
@_guard
def cancel(request, job_id):
    data = _fields(request, {'profile_ref', 'selection_token'})
    with _scope(request, data, mutate=True) as access:
        with access.effect_window(action='select'):
            saved.cancel(actor=access.actor, profile_id=access.identity.profile_id, job_id=job_id)
    return HttpResponseRedirect(BASE + f'jobs/{job_id}/')


def _optional_route(name, **kwargs):
    try:
        return reverse(name, kwargs=kwargs)
    except NoReverseMatch:
        return None


def _status_job(access, job_id):
    from .episode_scope import parse_scope, ScopeError
    from .obtain_native_context import native_job_context
    ga._require(type(job_id) is int and 0 < job_id < 2**53, 'CONTRACT_INVALID', 422)
    context = native_job_context(access, job_id=job_id, operation='view')
    job = acquire.owned_job(access.actor, access.identity.profile_id, job_id)
    try:
        episode = parse_scope(job.scope)
    except ScopeError:
        episode = None
    publication = getattr(job, 'publication_origin', None)
    ga._require(episode is not None and (publication is not None
        or (job.provider, job.medium) == ('tmdb', 'tv')), 'CAPABILITY_UNAVAILABLE', 503)
    context.revalidate()
    return job, context


def _flat_status(access, job, context):
    """Read local checkpoints only; download/file receipts cannot produce green."""
    transfer = ObtainTransfer.objects.filter(job=job).first()
    revision = transfer.revision if transfer else 0
    ga._require(type(revision) is int and 0 <= revision < 2**53, 'CAPABILITY_UNAVAILABLE', 503)
    advance = _optional_route('portable_obtain_advance', job_id=job.pk)
    stage, reason, state, label, next_action = (
        'awaiting_submission', 'confirmation_required', 'working', 'Review exact submission', 'confirm')
    if job.status == 'cancelled':
        stage, reason, state, label, next_action = 'cancelled', 'local_cancelled', 'idle', 'Request cancelled', 'none'
    elif transfer is not None:
        states = {
            'blocked': ('awaiting_submission', 'confirmation_required', 'working', 'Submission not sent', 'confirm'),
            'queued': ('queued', 'completion_unobserved', 'working', 'Accepted by downloader; completion unverified', 'check'),
            'outcome_unknown': ('outcome_unknown', 'submission_unconfirmed', 'error', 'Submission outcome unconfirmed', 'reconcile'),
            'failed': ('failed', 'request_failed', 'error', 'Request needs attention', 'none'),
            'cancel_pending': ('cancel_pending', 'remote_cancellation_unconfirmed', 'working', 'Remote cancellation unconfirmed', 'reconcile'),
            'cancelled': ('cancelled', 'local_cancelled', 'idle', 'Request cancelled', 'none'),
            'local_verified': ('verification_needed', 'library_verification_required', 'working', 'Library verification needed', 'check'),
        }
        ga._require(transfer.state in states, 'CAPABILITY_UNAVAILABLE', 503)
        stage, reason, state, label, next_action = states[transfer.state]
        if next_action in {'check', 'reconcile'} and advance is None:
            next_action = 'none'
    # The current integration supplies no accepted library-verification writer.
    # Keep local_verified historical; T1/O1's typed proof/CAS producer must be
    # composed before this projection can expose any verified availability.
    context.revalidate()
    return {'id': job.pk, 'state': state, 'label': label,
        'detail_href': reverse('portable_obtain_job', kwargs={'job_id': job.pk}),
        'verification_until': None, 'stage': stage, 'reason': reason, 'revision': revision,
        'next_action': next_action, 'observed_at': transfer.created_at if transfer else job.created_at}


def _read_query(request, access):
    ga._require(set(request.GET) <= {'profile_ref'}
        and all(len(request.GET.getlist(key)) == 1 for key in request.GET), 'CONTRACT_INVALID', 422)
    if 'profile_ref' in request.GET:
        ga._require(request.GET['profile_ref'] == access.profile_ref, 'AUTHORITY_DENIED', 403)


def _poll_until(access):
    # An assigned finite runtime supplies its original bound once. Missing means
    # manual reads, never a fabricated duration or a polling session keepalive.
    bound = getattr(settings, 'PORTABLE_OBTAIN_POLL_UNTIL', None)
    if type(bound) is not datetime or bound.utcoffset() is None:
        return ''
    _, session = ga._bound(access.authority, access.session_ref, access.profile_ref)
    end = min(bound, ga._end(access.authority, session))
    return end if access.authority.now() < end else ''


def cabinet_card_obtain_context(request, *, profile_ref, publication_revision, card):
    """Attach one owned current job; source identity alone never selects a job."""
    from .obtain_tv_flow import card_projection
    choose = _optional_route('portable_obtain_choose')
    options = _optional_route('portable_obtain_options')
    if (card.get('medium') != 'tv' or choose is None or options is None
            or type(publication_revision) is not int or publication_revision <= 0):
        return {}
    href = options + '?' + urlencode({'profile_ref': profile_ref,
        'publication_revision': publication_revision, 'candidate_key': card['candidate_key']})
    value = {'obtain_action': card_projection(state='idle', label='Choose an episode', detail_href=href),
             'obtain_choose_url': choose}
    selection = request.session.get('portable_saved_selection')
    if (type(selection) is not dict or selection.get('profile_ref') != str(profile_ref)
            or not hasattr(AcquisitionJob, 'publication_origin')):
        return value
    from .portable_obtain_origin import candidate_key_digest
    with _scope(request) as access:
        jobs = list(AcquisitionJob.objects.filter(actor_id=access.identity.actor_id,
            profile_id=access.identity.profile_id,
            publication_origin__profile_ref=access.profile_ref,
            publication_origin__publication_revision=publication_revision,
            publication_origin__candidate_key_digest=candidate_key_digest(card['candidate_key']))[:2])
        if len(jobs) != 1:
            return value
        job, context = _status_job(access, jobs[0].pk)
        projection = _flat_status(access, job, context)
        value['obtain_action'] = card_projection(**{key: projection[key]
            for key in ('state', 'label', 'detail_href', 'verification_until')})
        value['obtain_status_context'] = {'job_id': job.pk, 'profile_ref': access.profile_ref,
            'status_url': reverse('portable_obtain_job_status', kwargs={'job_id': job.pk}),
            'poll_until': _poll_until(access)}
        return value


@require_GET
@_guard
def job_status(request, job_id):
    with _scope(request) as access:
        _read_query(request, access)
        job, context = _status_job(access, job_id)
        projection = _flat_status(access, job, context)
        return JsonResponse(projection, headers={'Cache-Control': 'no-store',
                                                 'X-Content-Type-Options': 'nosniff'})


@require_GET
@_guard
def job_page(request, job_id):
    from .enhancement_views import render_portable_saved
    with _scope(request) as access:
        _read_query(request, access)
        job, context = _status_job(access, job_id)
        state = ProfileCabinetState.objects.select_related('profile').get(profile_id=access.identity.profile_id)
        values = {'profile_ref': access.profile_ref, 'profile_label': state.profile.label,
            'selection_token': access.selection_token,
            'cabinet_url': '/cabinet?' + urlencode({'profile_ref': access.profile_ref}),
            'obtain_job': _flat_status(access, job, context), 'obtain_request_label': job.edition,
            'obtain_status_url': reverse('portable_obtain_job_status', kwargs={'job_id': job.pk}),
            'obtain_advance_url': _optional_route('portable_obtain_advance', job_id=job.pk),
            'obtain_poll_until': _poll_until(access)}
        context.revalidate()
        try:
            return render_portable_saved(request, template='cabinet/portable_obtain_job.html', context=values)
        except TemplateDoesNotExist:
            return HttpResponse('Packaged recorded-job page unavailable.\n', status=503)
