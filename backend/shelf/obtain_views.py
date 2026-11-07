"""Provider work is reachable only through an authenticated CSRF-protected POST."""
from django import forms
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from shelf import obtain_preview as preview, obtain_acquisition as acquire, obtain_runtime, saved
from shelf.acquisition_contract import ContractError
from shelf.provider_views import _profile
from shelf.saved_views import fields, guard


def page(request, profile, **extra):
    return render(request, 'cabinet/saved_items.html', {
        **saved.lists_context(request.user, profile.pk), 'selected_profile': profile,
        'obtain_submission_enabled': False, 'obtain_preview_message': preview.DISABLED, **extra})


@login_required
@require_GET
@guard
def start(request, profile_id, item_id):
    profile = _profile(request, profile_id, 'select')
    item = saved.owned_item(request.user, profile_id, item_id)
    return page(request, profile, obtain_preview_available=preview.available(),
        obtain_search={'title': item.title, 'scope': saved.SCOPES[item.medium], 'is_tv': item.medium == 'tv',
                       'url': f'/profiles/{profile_id}/saved/{item_id}/preview/search/'})


@login_required
@require_POST
@guard
def search(request, profile_id, item_id):
    _profile(request, profile_id, 'select')
    data = fields(request, set(preview.SearchForm.base_fields))
    form = preview.SearchForm(data)
    if not form.is_valid():
        raise saved.SavedError('Choose both season (0–999) and episode (1–999), or leave both blank for S01E01.')
    receipt = preview.search(actor=request.user, profile_id=profile_id, item_id=item_id,
                             **form.cleaned_data)
    # Selected session authority is also rechecked after provider work.
    _profile(request, profile_id, 'select')
    return redirect('obtain_preview_results', profile_id=profile_id, preview_id=receipt.pk)


@login_required
@require_GET
@guard
def results(request, profile_id, preview_id):
    profile = _profile(request, profile_id, 'select')
    receipt = preview.owned_preview(request.user, profile_id, preview_id)
    return page(request, profile, obtain_preview=preview.context(receipt))


@login_required
@require_GET
@guard
def review(request, profile_id, preview_id, result_id):
    profile = _profile(request, profile_id, 'select')
    receipt, result = preview.review(actor=request.user, profile_id=profile_id,
                                    preview_id=preview_id, result_id=result_id)
    base = f'/profiles/{profile_id}/saved/previews/{preview_id}/results/{result_id}/'
    return page(request, profile, obtain_selected_result={**result.metadata, 'id': str(result.pk)},
        obtain_preview=preview.context(receipt), obtain_result_confirm_url=base + 'confirm/',
        obtain_result_form=preview.SelectionForm(initial={
            'review_token': preview.review_token(receipt, result), 'scope': receipt.scope,
            'edition': result.metadata['title'], 'storage': 'local_library'}))


@login_required
@require_POST
@guard
def confirm(request, profile_id, preview_id, result_id):
    _profile(request, profile_id, 'select')
    data = fields(request, set(preview.SelectionForm.base_fields))
    job, _ = preview.confirm(actor=request.user, profile_id=profile_id,
        preview_id=preview_id, result_id=result_id, data=data)
    return redirect('acquisition_status', profile_id=profile_id, job_id=job.pk)


class SubmissionForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput, max_length=8192)
    exact_selection = forms.BooleanField(label='I confirm this exact listing and edition.')
    rights = forms.BooleanField(label='I have the legal right to obtain this content, without access or DRM bypass.')
    scope = forms.BooleanField(label='I confirm the displayed full film, full audiobook or exact single-episode scope.')
    size = forms.BooleanField(label='I approve the displayed size limit and local storage destination.')
    duplicates = forms.BooleanField(label='I have checked my other downloads and library for this selection.')


class _CurrentAuthority:
    """Recheck selected session authority at each service authority boundary."""
    def __init__(self, request, context):
        self.request, self.context = request, context

    def require(self, actor, profile_id):
        _profile(self.request, profile_id, 'select')
        self.context.require(actor, profile_id)


@login_required
@require_POST
@guard
def submission_review(request, profile_id, job_id):
    profile = _profile(request, profile_id, 'select')
    fields(request, set())
    job = acquire.owned_job(request.user, profile_id, job_id)
    try:
        services = obtain_runtime.submission_factory(actor=request.user, profile_id=profile_id, job=job)
        review = acquire.review_submission(actor=request.user, profile_id=profile_id, job_id=job_id,
            context=_CurrentAuthority(request, services.context), evidence=services.evidence,
            max_bytes=services.max_bytes, retriever=services.retriever,
            complete_root=services.complete_root, test_only=services.test_only,
            completion_intent=services.completion_intent)
    except ContractError:
        return HttpResponse('Submission unavailable. Trusted operator setup and a fresh exact review are required. No enqueue was attempted.\n',
                            status=409, content_type='text/plain')
    _profile(request, profile_id, 'select')
    return page(request, profile, obtain_submission=review['binding'],
        acquisition_job=saved.job_context(job), obtain_submission_enabled=True,
        obtain_submit_url=f'/profiles/{profile_id}/saved/jobs/{job_id}/submit/',
        obtain_submit_form=SubmissionForm(initial={'token': review['token']}))


@login_required
@require_POST
@guard
def submit(request, profile_id, job_id):
    _profile(request, profile_id, 'select')
    data = fields(request, set(SubmissionForm.base_fields))
    form = SubmissionForm(data)
    if not form.is_valid():
        raise saved.SavedError('A fresh review and all explicit confirmations are required.', 409)
    job = acquire.owned_job(request.user, profile_id, job_id)
    try:
        services = obtain_runtime.submission_factory(actor=request.user, profile_id=profile_id, job=job)
        acquire.submit(actor=request.user, profile_id=profile_id, job_id=job_id,
            context=_CurrentAuthority(request, services.context), token=form.cleaned_data['token'],
            confirmed=True, retriever=services.retriever, backend=services.backend,
            max_bytes=services.max_bytes, complete_root=services.complete_root,
            test_only=services.test_only, completion_intent=services.completion_intent)
    except ContractError:
        return HttpResponse('Submission unavailable or review changed. Reload persisted request status; do not retry an uncertain enqueue.\n',
                            status=409, content_type='text/plain')
    return redirect('acquisition_status', profile_id=profile_id, job_id=job_id)


@login_required
@require_GET
@guard
def local_handoff(request, profile_id, job_id):
    _profile(request, profile_id, 'select')
    try:
        result = acquire.export_local(actor=request.user, profile_id=profile_id, job_id=job_id)
    except ContractError:
        raise saved.SavedError('Verified local handoff unavailable.', 409) from None
    response = JsonResponse(result)
    response['Cache-Control'] = 'private, no-store'
    return response
