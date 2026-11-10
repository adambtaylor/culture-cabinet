"""Dormant connection commands; transient owner input never becomes a prompt.

Keep frame-level sensitive_variables on JSON/credential/provider helpers:
POST-field filtering alone does not redact parsed JSON locals. Unexpected
exceptions remain diagnosable with sensitive values removed through the supported
Django reporter; these annotations cannot guarantee arbitrary logging is safe.
"""
import uuid

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from .disclosure import DisclosureError, read_json
from .portable_authority import AuthorityFault
from .portable_setup_views import _reply


def _service(request):
    service = getattr(settings, 'PORTABLE_CONNECTIONS', None)
    if service is None or service.authority is not request.portable_authority:
        raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503)
    return service


@sensitive_variables()
def _body(request, fields, optional=None):
    try:
        if request.content_type != 'application/json' or len(request.body) > 16384:
            raise DisclosureError()
        value = read_json(request.body)
        if type(value) is not dict:
            raise DisclosureError()
        if optional is not None and set(value) == set(fields) | set(optional):
            fields = {**fields, **optional}
        if set(value) != set(fields):
            raise DisclosureError()
        for key, kind in fields.items():
            if type(value[key]) is not kind:
                raise DisclosureError()
            if kind is str and len(value[key]) > 8192:
                raise DisclosureError()
        return value
    except DisclosureError:
        raise AuthorityFault('CONTRACT_INVALID', 422) from None


def _uuid(value):
    try:
        parsed = uuid.UUID(value)
        if str(parsed) != value:
            raise ValueError
        return parsed
    except (ValueError, AttributeError, TypeError):
        raise AuthorityFault('CONTRACT_INVALID', 422) from None


@require_POST
@_reply
@sensitive_variables()
def configure(request):
    service = _service(request)
    fields = {'adapter': str, 'model': str, 'reasoning': str}
    # Creation and replacement are distinct closed command shapes.
    value = _body(request, fields, {'connection_ref': str, 'revision': int})
    options = ({'connection_id': _uuid(value['connection_ref']), 'revision': value['revision']}
               if 'connection_ref' in value else {})
    row = service.vault.configure(request.session.get('portable_session'),
        {key: value[key] for key in ('adapter', 'model', 'reasoning')}, **options)
    return JsonResponse({'ok': True, 'connection_ref': str(row.pk), 'revision': row.config_revision,
                         'validation': 'UNPROVEN'})


@require_POST
@_reply
@sensitive_variables()
def credential(request):
    value = _body(request, {'connection_ref': str, 'revision': int, 'secret': str}, {'storage': str})
    service = _service(request)
    status = service.vault.replace(request.session.get('portable_session'), _uuid(value['connection_ref']),
                                   value['revision'], value['secret'].encode('utf-8'),
                                   storage=value.get('storage'))
    return JsonResponse({'ok': True, **status})


@require_POST
@_reply
@sensitive_variables()
def remove(request):
    value = _body(request, {'connection_ref': str, 'revision': int, 'disable': bool})
    service = _service(request)
    result = service.vault.remove(request.session.get('portable_session'), _uuid(value['connection_ref']),
                                 value['revision'], disable=value['disable'])
    return JsonResponse({'ok': True, **(result if type(result) is dict else {})})


@require_POST
@_reply
@sensitive_variables()
def preview(request):
    value = _body(request, {'connection_ref': str, 'revision': int})
    return JsonResponse({'ok': True, **_service(request).preview(request.session.get('portable_session'),
        _uuid(value['connection_ref']), value['revision'])})


@require_POST
@_reply
@sensitive_variables()
def confirm(request):
    value = _body(request, {'preview_ref': str, 'intent_ref': str, 'confirm_billable_test': bool})
    return JsonResponse({'ok': True, **_service(request).confirm(request.session.get('portable_session'),
        _uuid(value['preview_ref']), _uuid(value['intent_ref']), value['confirm_billable_test'])})


@require_GET
@_reply
@sensitive_variables()
def status(request):
    if set(request.GET) != {'connection_ref', 'revision'} or any(len(request.GET.getlist(k)) != 1 for k in request.GET):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    revision = request.GET['revision']
    if not revision.isascii() or not revision.isdigit() or not 1 <= len(revision) <= 12:
        raise AuthorityFault('CONTRACT_INVALID', 422)
    return JsonResponse({'ok': True, **_service(request).status(request.session.get('portable_session'),
        _uuid(request.GET['connection_ref']), int(revision))})


# HTML commands stay in the security adapter: the Experience renderer never
# receives the request, secret bytes, owner-session reference or credential lease.
from .portable_setup_views import _after_success, _form, _html_reply, _page


def _revision(value):
    if not value.isascii() or not value.isdigit() or not 1 <= len(value) <= 12 or int(value) < 1:
        raise AuthorityFault('CONTRACT_INVALID', 422)
    return int(value)


@_html_reply
@sensitive_variables()
def html_configure(request):
    fields = {'adapter', 'model', 'reasoning'}
    if 'connection_ref' in request.POST or 'revision' in request.POST:
        fields |= {'connection_ref', 'revision'}
    value = _form(request, fields)
    options = ({'connection_id': _uuid(value['connection_ref']), 'revision': _revision(value['revision'])}
               if 'connection_ref' in value else {})
    row = _service(request).vault.configure(request.session.get('portable_session'),
        {k: value[k] for k in ['adapter', 'model', 'reasoning']}, **options)
    return _after_success(request, notice='CONFIG_SAVED', selected=str(row.pk))


@_html_reply
@sensitive_variables()
def html_credential(request):
    fields = {'connection_ref', 'revision', 'secret'} | ({'storage'} if 'storage' in request.POST else set())
    value = _form(request, fields)
    _service(request).vault.replace(request.session.get('portable_session'), _uuid(value['connection_ref']),
        _revision(value['revision']), value['secret'].encode('utf-8'), storage=value.get('storage'))
    return _after_success(request, notice='KEY_SAVED' if value.get('storage') == 'persistent' else 'KEY_ADDED',
                          selected=value['connection_ref'])


@_html_reply
@sensitive_variables()
def html_remove(request):
    value = _form(request, {'connection_ref', 'revision'})
    result = _service(request).vault.remove(request.session.get('portable_session'), _uuid(value['connection_ref']),
        _revision(value['revision']))
    notice = 'KEY_REMOVAL_PENDING' if type(result) is dict and result.get('removal_pending') else 'KEY_REMOVED'
    return _after_success(request, notice=notice, selected=value['connection_ref'])


@_html_reply
@sensitive_variables()
def html_preview(request):
    # The reviewed neutral body is an explicit 200 exception to command PRG.
    # Never flash/persist/replay this preview or automatically confirm it.
    value = _form(request, {'connection_ref', 'revision'})
    preview = _service(request).preview(request.session.get('portable_session'), _uuid(value['connection_ref']),
        _revision(value['revision']))
    preview['intent_ref'] = str(uuid.uuid4())
    return _page(request, preview=preview, selected=value['connection_ref'])


@_html_reply
@sensitive_variables()
def html_confirm(request):
    value = _form(request, {'preview_ref', 'intent_ref', 'confirm_billable_test'})
    if value['confirm_billable_test'] != 'yes':
        raise AuthorityFault('CONTRACT_INVALID', 422)
    outcome = _service(request).confirm(request.session.get('portable_session'), _uuid(value['preview_ref']),
        _uuid(value['intent_ref']), True)
    # Selection comes from the authorized attempt, never a redirect target or
    # latest timestamp. Canonical GET then rechecks current status, not history.
    from .models import PortableProbeAttempt
    selected = PortableProbeAttempt.objects.filter(pk=_uuid(outcome['attempt_ref']),
        connection__installation_id=request.portable_authority.boundary.installation_id
        ).values_list('connection_id', flat=True).first()
    if selected is None:
        raise AuthorityFault('AUTHORITY_DENIED')
    return _after_success(request, notice='TEST_' + outcome['outcome'].upper(), selected=str(selected))
