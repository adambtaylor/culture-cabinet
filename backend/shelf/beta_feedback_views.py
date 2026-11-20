"""Normal signed-in submission and portable-owner reference lookup."""
import uuid

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import DatabaseError
from django.http import Http404, HttpResponse
from django.template.response import TemplateResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .beta_feedback import (ReportConflict, ReportDenied, owner_report,
                            submit_report, tester_report)
from .recovery import RecoveryBlocked


class ReportForm(forms.Form):
    reference = forms.UUIDField(widget=forms.HiddenInput)
    body = forms.CharField(max_length=2000, strip=False,
                           widget=forms.Textarea(attrs={'rows': 6, 'maxlength': 2000}))

    def clean_body(self):
        body = self.cleaned_data['body']
        if not body.strip() or any(ord(ch) < 32 and ch not in '\n\r\t' for ch in body):
            raise forms.ValidationError('Enter a short plain-text report.')
        return body


def _render(request, *, status=200, **context):
    return TemplateResponse(request, 'cabinet/problem_report.html', context,
        status=status, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@login_required
@require_http_methods(['GET', 'POST'])
@csrf_protect
@sensitive_variables()
@sensitive_post_parameters()
def tester_page(request):
    if request.method == 'GET':
        return _render(request, form=ReportForm(initial={'reference': uuid.uuid4()}))
    if (request.content_type != 'application/x-www-form-urlencoded'
            or set(request.POST) - {'csrfmiddlewaretoken', 'reference', 'body'}
            or any(len(request.POST.getlist(key)) != 1 for key in request.POST)):
        return _render(request, message='This form could not be verified. Reload it and try again.',
                        form=ReportForm(initial={'reference': uuid.uuid4()}), status=400)
    form = ReportForm(request.POST)
    if not form.is_valid():
        return _render(request, form=form, message='Review the report text and try again.', status=400)
    try:
        report, _ = submit_report(tester=request.user, **form.cleaned_data)
    except ReportDenied:
        return _render(request, form=form, message='Report submission is unavailable for this account.', status=403)
    except ReportConflict:
        return _render(request, form=form,
            message='This reference is already used for another report. The saved report was not changed.', status=409)
    except (DatabaseError, RecoveryBlocked):
        return _render(request, form=form,
            message='The report store is unavailable. Keep this reference and retry the same text.', status=503)
    return HttpResponse(status=303, headers={'Location': f'/report/{report.reference}/',
                                             'Cache-Control': 'no-store'})


@login_required
@require_GET
@sensitive_variables()
def tester_status(request, reference):
    try:
        report = tester_report(tester=request.user, reference=reference)
    except RecoveryBlocked:
        return HttpResponse('Report status is unavailable.\n', status=503, content_type='text/plain')
    if report is None:
        raise Http404('Not available.')
    return _render(request, report=report, saved=True)


@require_http_methods(['GET', 'POST'])
@csrf_protect
@sensitive_variables()
@sensitive_post_parameters()
def owner_page(request):
    from . import grant_authority as ga
    authority = request.portable_authority
    session_ref = request.session.get('portable_session')
    try:
        with ga.recovery_guard(authority, session_ref), authority._lock:
            ga._owner(authority, session_ref)
        reference = None
        if request.method == 'POST':
            from .portable_setup_views import _form
            fields = _form(request, {'reference'})
            try:
                reference = uuid.UUID(fields['reference'])
            except (ValueError, TypeError, AttributeError):
                reference = None
        report = (owner_report(authority=authority, session_ref=session_ref,
                               reference=reference) if reference else None)
    except ga.AuthorityFault as fault:
        return HttpResponse('Owner session required.\n', status=fault.status,
                            content_type='text/plain', headers={'Cache-Control': 'no-store'})
    message = ('Report found.' if report else
        ('No report found for that reference.' if request.method == 'POST' else ''))
    form = None if request.method == 'GET' else {'reference': str(reference or '')}
    return _render(request, owner=True, report=report, form=form, message=message)
