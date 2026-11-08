"""Owner JSON and native-form adapters; presentation receives safe context only."""
import json
import uuid
from urllib.parse import urlencode

from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token, rotate_token
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from .portable_authority import AuthorityFault


def _reply(function):
    @sensitive_variables()
    @sensitive_post_parameters()
    def view(request):
        try:
            return function(request)
        except AuthorityFault as fault:
            return JsonResponse({'ok': False, 'code': fault.code}, status=fault.status)
    return view


@sensitive_variables()
def _body(request, fields):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        if request.content_type != 'application/json' or len(request.body) > 4096:
            raise ValueError
        data = json.loads(request.body.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        if type(data) is not dict or set(data) != fields or any(type(v) is not str for v in data.values()):
            raise ValueError
        for value in data.values():
            value.encode('utf-8', 'strict')
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise AuthorityFault('CONTRACT_INVALID', 422) from None


@sensitive_variables()
def _rotate_session(request, ref):
    previous = request.session.get('portable_session')
    if previous:
        request.portable_authority.logout(previous)
    request.session.flush()
    request.session['portable_session'] = ref
    request.session.set_expiry(1800)
    rotate_token(request)


@sensitive_variables()
def _rotate(request, ref):
    # JSON and HTML must share fixation protection and old-vault revocation.
    _rotate_session(request, ref)
    return JsonResponse({'ok': True, 'code': 'OK'})


@require_GET
@_reply
def status(request):
    authority = request.portable_authority
    authenticated = False
    try:
        authority.authorize(request.session.get('portable_session'))
        authenticated = True
        request.session.set_expiry(1800)
    except AuthorityFault:
        pass
    return JsonResponse({'ok': True, 'code': 'OK',
        'claimed': authority._installation().owner_id is not None,
        'authenticated': authenticated, 'csrf_token': get_token(request)})


@require_POST
@_reply
@sensitive_variables()
def claim(request):
    data = _body(request, {'code', 'passphrase'})
    return _rotate(request, request.portable_authority.claim(data['code'], data['passphrase']))


@require_POST
@_reply
@sensitive_variables()
def login(request):
    data = _body(request, {'passphrase'})
    return _rotate(request, request.portable_authority.login(data['passphrase']))


@require_POST
@_reply
@sensitive_variables()
def reauthenticate(request):
    data = _body(request, {'passphrase'})
    request.portable_authority.reauthenticate(request.session.get('portable_session'), data['passphrase'])
    request.session.cycle_key()
    request.session.set_expiry(1800)
    rotate_token(request)
    return JsonResponse({'ok': True, 'code': 'OK'})


@require_POST
@_reply
def logout(request):
    _body(request, set())
    request.portable_authority.logout(request.session.get('portable_session'))
    request.session.flush()
    rotate_token(request)
    return JsonResponse({'ok': True, 'code': 'OK'})


_SUCCESS_NOTICES = frozenset({'OWNER_CREATED', 'OWNER_UNLOCKED', 'SIGNED_OUT',
    'CONFIG_SAVED', 'KEY_ADDED', 'KEY_REMOVED', 'TEST_PASSED'})
_FLASH = '_portable_notice'


@sensitive_variables()
def _after_success(request, *, notice, selected=None):
    """POST/redirect/GET prevents refresh from resubmitting a successful command.

    The redirect carries only an owned canonical connection UUID. Flash storage
    holds one fixed code, never input, preview, intent or result text. The next
    GET reconstructs current authority/readiness; history is not current proof.
    """
    query = ''
    if selected is not None:
        from .models import PortableConnection
        try:
            key = uuid.UUID(selected)
            if str(key) != selected:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise AuthorityFault('CONTRACT_INVALID', 422) from None
        request.portable_authority.authorize(request.session.get('portable_session'), touch=False)
        if not PortableConnection.objects.filter(pk=key, removed=False,
                installation_id=request.portable_authority.boundary.installation_id).exists():
            raise AuthorityFault('AUTHORITY_DENIED')
        query = '?' + urlencode({'connection_ref': str(key)})
    request.session.pop(_FLASH, None)
    if type(notice) is str and notice in _SUCCESS_NOTICES:
        request.session[_FLASH] = notice
    response = HttpResponse(status=303)
    response['Location'] = '/setup' + query
    return response


# HTML rendering gets a deliberately closed nonsecret projection. Passing a
# request/RequestContext to the template would expose transient POST fields to a
# presentation author; command handlers retain that trust boundary here instead.
def setup_context(request, *, notice='', preview=None, selected=None):
    from .models import PortableConnection
    from .portable_connection_views import _service, _uuid
    authority = request.portable_authority
    if request.method == 'GET':
        stored_notice = request.session.pop(_FLASH, None)
        if not notice and type(stored_notice) is str and stored_notice in _SUCCESS_NOTICES:
            notice = stored_notice
    context = dict(state='claim' if authority._installation().owner_id is None else 'login',
        configured=False, credential_configured=False, connection_ready=False, run_ready=False,
        connection_ref='', revision=0, adapter='openai_responses', model='gpt-6-astra', reasoning='high',
        connections=[], outcome='missing_credential', notice=notice, preview=None,
        reauthentication_required=False,
        csrf_token=get_token(request), actions={name: '/setup/form/' + name for name in
            ['claim', 'login', 'logout', 'reauthenticate', 'configure', 'credential', 'remove', 'preview', 'confirm']})
    try:
        authority.authorize(request.session.get('portable_session'))
    except AuthorityFault:
        return context
    context['state'] = 'model'
    try:
        authority.authorize(request.session.get('portable_session'), sensitive=True, touch=False)
    except AuthorityFault:
        # Ordinary authorization above proved a live session. Only recent
        # password confirmation is missing; preserve its vault/session identity.
        context['reauthentication_required'] = True
    request.session.set_expiry(1800)
    rows = list(PortableConnection.objects.filter(installation_id=authority.boundary.installation_id,
                                                 removed=False).order_by('id'))
    context['connections'] = [{'connection_ref': str(r.pk), 'model': r.model, 'revision': r.config_revision} for r in rows]
    try:
        if selected is None:
            if set(request.GET) - {'connection_ref'} or any(len(request.GET.getlist(k)) != 1 for k in request.GET):
                raise AuthorityFault('CONTRACT_INVALID', 422)
            selected = request.GET.get('connection_ref')
        row = next((r for r in rows if r.pk == _uuid(selected)), None) if selected else (rows[0] if len(rows) == 1 else None)
        if selected and row is None:
            raise AuthorityFault('AUTHORITY_DENIED')
        if row is not None:
            context.update(_service(request).status(request.session.get('portable_session'), row.pk, row.config_revision))
            context.update(configured=True, connection_ref=str(row.pk), revision=row.config_revision,
                           adapter=row.adapter, model=row.model, reasoning=row.reasoning)
            # Never project service internals/bindings, even if future status adds
            # fields. This explicit selection is the presentation contract.
            allowed_preview = {'preview_ref', 'body', 'body_digest', 'destination', 'max_output_tokens',
                               'maximum_sends', 'retry_sends', 'requires_billable_confirmation', 'intent_ref'}
            if preview is not None:
                context['preview'] = {k: preview[k] for k in allowed_preview}
    except AuthorityFault as fault:
        context['notice'] = fault.code
    allowed = {'state', 'configured', 'credential_configured', 'connection_ready', 'run_ready',
        'connection_ref', 'revision', 'adapter', 'model', 'reasoning', 'connections', 'outcome',
        'notice', 'preview', 'csrf_token', 'actions', 'reauthentication_required'}
    return {k: v for k, v in context.items() if k in allowed}


@sensitive_variables()
def _form(request, fields):
    """Closed native-form shapes; duplicate keys and JSON never reach services."""
    if request.content_type != 'application/x-www-form-urlencoded':
        raise AuthorityFault('CONTRACT_INVALID', 422)
    value = request.POST
    keys = set(value) - {'csrfmiddlewaretoken'}
    if keys != set(fields) or any(len(value.getlist(k)) != 1 for k in value):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    if any(len(value[k]) > 8192 for k in keys):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    return {k: value[k] for k in keys}


def _page(request, *, notice='', preview=None, selected=None, status=200):
    from .onboarding_experience_views import render_setup
    return render_setup(setup_context(request, notice=notice, preview=preview, selected=selected), status=status)


def _html_reply(function):
    @require_POST
    @sensitive_variables()
    @sensitive_post_parameters()
    def view(request):
        try:
            return function(request)
        except AuthorityFault as fault:
            request.session.pop(_FLASH, None)
            # Fixed codes only: no error string may interpolate owner-form data.
            notice = fault.code
            if fault.code == 'UNAUTHENTICATED':
                try:
                    request.portable_authority.authorize(request.session.get('portable_session'), touch=False)
                except AuthorityFault:
                    pass
                else:
                    notice = 'REAUTHENTICATION_REQUIRED'
            # A hidden nonsecret selection may survive the error, but no secret
            # POST/preview/confirmation is replayed after password confirmation.
            selected = request.POST.get('connection_ref') if len(request.POST.getlist('connection_ref')) == 1 else None
            return _page(request, notice=notice, selected=selected, status=fault.status)
    return view


@_html_reply
@sensitive_variables()
def html_claim(request):
    value = _form(request, {'code', 'passphrase'})
    _rotate_session(request, request.portable_authority.claim(value['code'], value['passphrase']))
    return _after_success(request, notice='OWNER_CREATED')


@_html_reply
@sensitive_variables()
def html_login(request):
    value = _form(request, {'passphrase'})
    _rotate_session(request, request.portable_authority.login(value['passphrase']))
    return _after_success(request, notice='OWNER_UNLOCKED')


@_html_reply
@sensitive_variables()
def html_reauthenticate(request):
    fields = {'passphrase'} | ({'connection_ref'} if 'connection_ref' in request.POST else set())
    value = _form(request, fields)
    request.portable_authority.reauthenticate(request.session.get('portable_session'), value['passphrase'])
    request.session.cycle_key()
    request.session.set_expiry(1800)
    rotate_token(request)
    return _after_success(request, notice='OWNER_UNLOCKED', selected=value.get('connection_ref'))


@_html_reply
def html_logout(request):
    _form(request, set())
    request.portable_authority.logout(request.session.get('portable_session'))
    request.session.flush()
    rotate_token(request)
    return _after_success(request, notice='SIGNED_OUT')


@require_GET
@_reply
def enhance_status(request):
    """Readiness is separate from a successful neutral connection test.

    An explicit profile and connection may be checked; absence never selects a
    household/default profile. No setup Run flag or source/send grant is raised.
    """
    from .enhancement_views import _portable_service
    from .portable_connection_views import _uuid
    query = request.GET
    if (set(query) not in (set(), {'profile_ref', 'connection_ref'})
            or any(len(query.getlist(key)) != 1 for key in query)):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    service, session_ref = _portable_service(request)
    selection = {key: str(_uuid(query[key])) for key in query}
    return JsonResponse(service.readiness(session_ref, **selection))


@sensitive_variables()
def cabinet_choices(request):
    """Display only already-bound choices under the existing recovery boundary.

    Labels never identify authority. Membership is rechecked on each destination;
    the create primitive alone decides whether a genuinely empty store may bind.
    No first profile/connection is selected, even when there is only one choice.
    """
    from . import grant_authority as ga
    from .models import Profile, ProfileCabinetState, PortableConnection
    from .portable_connection_views import _service
    authority = request.portable_authority
    ref = request.session.get('portable_session')
    with ga.recovery_guard(authority, ref), authority._lock:
        installation, session = ga._owner(authority, ref, sensitive=True)
        profiles = []
        for row in ProfileCabinetState.objects.filter(installation=installation,
                actor_id=session.owner_id, owner_revision=session.owner_revision, blocked=False,
                data_schema_version__in=(1, 2), profile__kind='owner', profile__active=True,
                profile__grant__user_id=session.owner_id, profile__grant__can_view=True,
                profile__grant__can_select=True, profile__grant__can_mutate=True
                ).select_related('profile').order_by('profile_ref'):
            try:
                state, _ = ga._bound(authority, ref, str(row.profile_ref), sensitive=True)
            except AuthorityFault as error:
                if error.code == 'AUTHORITY_DENIED':
                    continue
                raise
            if type(state.profile.label) is not str or len(state.profile.label) > 80:
                raise AuthorityFault('CONTRACT_INVALID', 422)
            profiles.append({'profile_ref': str(state.profile_ref), 'label': state.profile.label})
        connections = []
        for row in PortableConnection.objects.filter(installation=installation, removed=False).order_by('id'):
            try:
                # Configuration eligibility is not inference readiness or consent.
                _service(request)._current(ref, row.pk, row.config_revision)
            except AuthorityFault as error:
                if error.code in ('AUTHORITY_DENIED', 'CAPABILITY_UNAVAILABLE', 'REVISION_CONFLICT'):
                    continue
                raise
            if type(row.model) is not str or len(row.model) > 128:
                raise AuthorityFault('CONTRACT_INVALID', 422)
            connections.append({'connection_ref': str(row.pk), 'model': row.model,
                                'revision': row.config_revision})
        # D2 technical approval is not permission to adopt a real owner's
        # history. Keep this binding surface synthetic until separate owner
        # adoption authority and normal-store activation are actually admitted.
        bind_profiles = []
        if ga._fixture_database() and callable(getattr(ga, 'bind_existing_profile', None)):
            for profile in Profile.objects.filter(kind='owner', active=True,
                    grant__user_id=session.owner_id, grant__can_view=True,
                    grant__can_select=True, grant__can_mutate=True,
                    profilecabinetstate__isnull=True).order_by('id'):
                if type(profile.label) is not str or len(profile.label) > 80:
                    raise AuthorityFault('CONTRACT_INVALID', 422)
                bind_profiles.append({'profile_id': profile.pk, 'label': profile.label})
        return {'profiles': profiles, 'connections': connections, 'bind_profiles': bind_profiles,
                'can_create': not Profile.objects.exists() and not ProfileCabinetState.objects.exists(),
                'owner_revision': session.owner_revision}


@require_GET
def cabinet_entry(request):
    # Import lazily: setup's command/rendering boundary remains independent of
    # legacy enhancement authentication and its template/navigation URLconf.
    from .enhancement_views import portable_entry
    return portable_entry(request)


@csrf_protect
@_html_reply
@sensitive_variables()
def create_cabinet(request):
    from . import grant_authority as ga
    from .enhancement_views import _portable_fields
    value = _portable_fields(request, {'expected_owner_revision': int})
    ref = ga.create_owner_profile(request.portable_authority,
        request.session.get('portable_session'), expected_owner_revision=value['expected_owner_revision'])
    # Only the opaque result is navigation. No grant, default profile or original
    # form is cached/replayed; the chooser rechecks the binding after this GET.
    return HttpResponse(status=303, headers={'Location': '/setup/cabinet?' + urlencode({'profile_ref': ref}),
                                            'Cache-Control': 'no-store'})


@csrf_protect
@_html_reply
@sensitive_variables()
def bind_cabinet(request):
    """Explicit synthetic D2 binding, never household/store adoption by HTTP."""
    from . import grant_authority as ga
    from .enhancement_views import _portable_fields
    from .portable_connection_views import _uuid
    function = getattr(ga, 'bind_existing_profile', None)
    if not ga._fixture_database() or not callable(function):
        raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503)
    request.portable_authority.authorize(request.session.get('portable_session'), sensitive=True)
    value = _portable_fields(request, {'profile_id': int, 'confirmed': bool})
    if value['confirmed'] is not True:
        raise AuthorityFault('AUTHORITY_DENIED', 403)
    ref = function(request.portable_authority, request.session.get('portable_session'),
                   profile_id=value['profile_id'], confirmed=True)
    return HttpResponse(status=303, headers={'Location': '/setup/cabinet?' + urlencode({
        'profile_ref': str(_uuid(ref))}), 'Cache-Control': 'no-store'})
