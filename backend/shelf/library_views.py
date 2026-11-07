"""Owned acquired files, with local receipt reconciliation on each visit."""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from shelf import completion_consumer, obtain_acquisition as acquire, saved
from shelf.acquisition_contract import ContractError
from shelf.models import AcquisitionJob, PostprocessReceipt
from shelf.provider_views import _profile
from shelf.saved_views import guard


@login_required
@require_GET
@guard
def index(request, profile_id):
    profile = _profile(request, profile_id, 'select')
    saved.authority(request.user, profile_id)
    jobs = AcquisitionJob.objects.filter(actor=request.user, profile_id=profile_id,
        obtaintransfer__isnull=False).order_by('-created_at', '-pk')
    page = Paginator(jobs, 20).get_page(request.GET.get('page'))
    rows = []
    for job in page:
        transfer = acquire.transfer_for(job)
        row = {'job': job, 'state': 'Current files unavailable', 'files': []}
        if transfer.state == 'local_verified':
            receipt = acquire.export_local(actor=request.user, profile_id=profile_id, job_id=job.pk)
            row.update(state='Legacy local verification receipt', files=[receipt])
        elif completion_consumer.available():
            local = PostprocessReceipt.objects.filter(transfer=transfer,
                                                      state='postprocess_verified').first()
            if local:
                row.update(state='Postprocessing files verified', files=local.receipt['handoff'],
                           handoff_url=f'/profiles/{profile_id}/acquired-files/{job.pk}/handoff/')
        rows.append(row)
    response = render(request, 'cabinet/acquired_files.html',
                      {'selected_profile': profile, 'rows': rows, 'page': page})
    response['Cache-Control'] = 'private, no-store'
    return response


@login_required
@require_GET
@guard
def handoff(request, profile_id, job_id):
    _profile(request, profile_id, 'select')
    try:
        result = completion_consumer.export(actor=request.user, profile_id=profile_id, job_id=job_id)
    except ContractError:
        raise saved.SavedError('Current verified files unavailable.', 409) from None
    response = JsonResponse(result)
    response['Cache-Control'] = 'private, no-store'
    return response
