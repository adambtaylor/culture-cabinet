"""CSRF-protected saved intent actions; GET renders only local state."""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.db import OperationalError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from shelf import saved
from shelf.models import AcquisitionJob
from shelf.provider_views import _profile


def guard(view):
    @wraps(view)
    def call(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except saved.SavedError as error:
            return HttpResponse(error.message + '\n', status=error.status, content_type='text/plain')
        except OperationalError:
            # A contending SQLite writer may require retry; never claim success.
            return HttpResponse('Local saved storage unavailable. Retry this request.\n',
                                status=503, content_type='text/plain')
    return call


def fields(request, allowed):
    if (int(request.META.get('CONTENT_LENGTH') or 0) > 8192
            or any(k not in allowed | {'csrfmiddlewaretoken'} or len(request.POST.getlist(k)) != 1
                   for k in request.POST)):
        raise saved.SavedError()
    return request.POST


@login_required
@require_GET
@guard
def index(request, profile_id):
    profile = _profile(request, profile_id)
    return render(request, 'cabinet/saved_items.html',
                  {'selected_profile': profile, **saved.lists_context(request.user, profile_id)})


@login_required
@require_POST
@guard
def save(request, profile_id):
    _profile(request, profile_id, 'select')
    data = fields(request, {'item_id', 'list_kind'})
    saved.save_candidate(actor=request.user, profile_id=profile_id, session=request.session,
                         item_id=data.get('item_id'), list_kind=data.get('list_kind'))
    return redirect('saved_items', profile_id=profile_id)


@login_required
@require_POST
@guard
def remove(request, profile_id, item_id):
    _profile(request, profile_id, 'select')
    fields(request, set())
    saved.remove(actor=request.user, profile_id=profile_id, item_id=item_id)
    return redirect('saved_items', profile_id=profile_id)


@login_required
@require_GET
@guard
def review(request, profile_id, item_id):
    profile = _profile(request, profile_id, 'select')
    item = saved.owned_item(request.user, profile_id, item_id)
    active = AcquisitionJob.objects.filter(active_key=saved.active_key(item)).first()
    context = saved.lists_context(request.user, profile_id)
    context.update(selected_profile=profile,
        obtain_confirmation={'title': item.title, 'edition': 'Unconfirmed — enter the exact edition label',
            'scope': saved.SCOPES[item.medium],
            'authorized_content': 'Required: your legal right to this public-domain or licensed content; no DRM or access bypass.',
            'storage': 'Local library: destination unconfigured; size and capacity unknown.',
            'duplicate_status': 'Existing active local request' if active else 'No active local request',
            'cancel_status': 'Local blocked request can be cancelled; no transfer has started.'},
        obtain_form=saved.ConfirmationForm(initial={'scope': saved.SCOPES[item.medium],
                                                  'review_token': saved.review_token(item)}),
        obtain_confirm_url=f'/profiles/{profile_id}/saved/{item.pk}/obtain/confirm/',
        acquisition_configuration=saved.UnconfiguredAcquisition.configuration())
    return render(request, 'cabinet/saved_items.html', context)


@login_required
@require_POST
@guard
def confirm(request, profile_id, item_id):
    _profile(request, profile_id, 'select')
    data = fields(request, set(saved.ConfirmationForm.base_fields))
    job, _ = saved.confirm(actor=request.user, profile_id=profile_id, item_id=item_id, data=data)
    return redirect('acquisition_status', profile_id=profile_id, job_id=job.pk)


@login_required
@require_GET
@guard
def status(request, profile_id, job_id):
    profile = _profile(request, profile_id)
    saved.require_available()
    try:
        job = AcquisitionJob.objects.get(pk=job_id, actor=request.user, profile_id=profile_id)
    except AcquisitionJob.DoesNotExist:
        raise Http404 from None
    context = saved.lists_context(request.user, profile_id)
    context.update(selected_profile=profile, acquisition_job=saved.job_context(job),
                   obtain_confirmation={'title': job.title, 'edition': job.edition, 'scope': job.scope,
                       'authorized_content': 'Legal right confirmed by you; availability remains unknown.',
                       'storage': 'Local library: unconfigured; size and capacity unknown.',
                       'duplicate_status': job.reason,
                       'cancel_status': job.status})
    return render(request, 'cabinet/saved_items.html', context)


@login_required
@require_POST
@guard
def cancel(request, profile_id, job_id):
    _profile(request, profile_id, 'select')
    fields(request, set())
    saved.cancel(actor=request.user, profile_id=profile_id, job_id=job_id)
    return redirect('acquisition_status', profile_id=profile_id, job_id=job_id)
