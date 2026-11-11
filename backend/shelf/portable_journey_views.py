"""Closed native journey forms; presentation receives only safe projections."""
from functools import wraps
from urllib.parse import urlencode

from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from . import grant_authority as ga
from .card_action_views import fields
from .guided_journey_presentation import render_guided_journey
from .portable_journey import PortableJourney, PROVIDER_ADAPTERS

COMMON = {'profile_ref', 'journey_ref', 'expected_revision', 'command_id'}


def _render(request, profile_ref=None, *, status=200, notice=None):
    projection = PortableJourney(request.portable_authority).project(
        request.session.get('portable_session'), profile_ref=profile_ref)
    if notice is not None:
        projection['completion_receipts']=[{'message':notice}]
    return render_guided_journey(projection, csrf_token=get_token(request), status=status)


def _errors(view):
    @wraps(view)
    @sensitive_variables()
    @sensitive_post_parameters()
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except ga.AuthorityFault as fault:
            # Rebuild from current authority after a rejected command. Submitted
            # secrets, stale titles and command fields never enter the renderer.
            profile_ref = request.POST.get('profile_ref') if len(request.POST.getlist('profile_ref')) == 1 else None
            try:
                if profile_ref is not None:
                    profile_ref = str(ga._uuid(profile_ref))
                notice=('This favourite keeps its original title. To replace it, remove it and add a new favourite.'
                    if fault.code=='FAVOURITE_REPLACEMENT_REQUIRED' else None)
                return _render(request, profile_ref, status=fault.status,notice=notice)
            except ga.AuthorityFault:
                return HttpResponse('Please sign in again to continue.', status=fault.status,
                                    headers={'Cache-Control': 'no-store'})
    return wrapped


def _values(request, names):
    value = fields(request, names)
    from .portable_source_views import _revision
    for key in ('expected_revision', 'expected_owner_revision', 'base_revision', 'slot'):
        if key in value:
            value[key] = _revision(value[key])
    for key in ('profile_ref', 'journey_ref', 'command_id', 'entry_ref', 'mutation_id',
                'connection_ref', 'client_key', 'refresh_nonce'):
        if key in value:
            value[key] = str(ga._uuid(value[key]))
    return value


def _redirect(profile_ref):
    return HttpResponse(status=303, headers={'Location': '/setup/journey?' + urlencode(
        {'profile_ref': profile_ref}), 'Cache-Control': 'no-store'})


@require_http_methods(['GET','HEAD'])
@_errors
def page(request):
    ga._require(set(request.GET) <= {'profile_ref'} and all(
        len(request.GET.getlist(key)) == 1 for key in request.GET), 'CONTRACT_INVALID', 422)
    profile_ref = request.GET.get('profile_ref')
    if profile_ref is not None:
        profile_ref = str(ga._uuid(profile_ref))
    else:
        # A cold returning-owner GET resumes the one already owned profile. It
        # does not create, rename or adopt an unbound profile.
        from .models import ProfileCabinetState
        authority = request.portable_authority
        with ga.recovery_guard(authority, request.session.get('portable_session')), authority._lock:
            installation, session = ga._owner(authority, request.session.get('portable_session'))
            row = ProfileCabinetState.objects.filter(installation=installation,
                actor_id=session.owner_id, owner_revision=session.owner_revision).first()
            if row is not None:
                profile_ref = str(row.profile_ref)
    return _render(request, profile_ref)


@require_POST
@csrf_protect
@_errors
def name(request):
    result = PortableJourney(request.portable_authority).name(request.session.get('portable_session'),
        **_values(request, {'expected_owner_revision', 'label', 'command_id'}))
    return _redirect(result['profile_ref'])


@require_POST
@csrf_protect
@_errors
def step(request):
    result = PortableJourney(request.portable_authority).step(request.session.get('portable_session'),
        **_values(request, COMMON | {'step', 'action'}))
    return _redirect(result['profile_ref'])


@require_POST
@csrf_protect
@_errors
@sensitive_post_parameters()
@sensitive_variables()
def provider(request, provider_id, operation):
    ga._require(provider_id in PROVIDER_ADAPTERS and operation in ('configure', 'save', 'test'),
                'CONTRACT_INVALID', 422)
    extra = {'secret', 'storage'} if operation == 'save' else {'confirmed'}
    value = _values(request, COMMON | extra)
    journey = PortableJourney(request.portable_authority)
    ref = request.session.get('portable_session')
    if operation == 'save':
        result = journey.save_provider(ref, provider=provider_id, **value)
    else:
        ga._require(value['confirmed'] == 'yes', 'CONTRACT_INVALID', 422)
        value['confirmed'] = True
        if operation == 'configure':
            result = journey.configure(ref, provider=provider_id, **value)
        else:
            from .source_connection_validation import SourceConnectionValidation
            authority = request.portable_authority
            with ga.current_writer(authority, ref, value['profile_ref']):
                _, state = journey._state(ref, value['profile_ref'], value['journey_ref'],
                    sensitive=True, expected_revision=value['expected_revision'])
                ga._require(state.revision == value['expected_revision'], 'REVISION_CONFLICT', 409)
                checks = getattr(authority, '_guided_source_checks', None)
                if checks is None:
                    checks = SourceConnectionValidation(authority)
                    authority._guided_source_checks = checks
                checks.check(ref, profile_ref=value['profile_ref'], provider=provider_id,
                    command_id=value['command_id'], confirmed=True)
            result = value
    return _redirect(result['profile_ref'])


@require_POST
@csrf_protect
@_errors
def favourite(request, remove=False):
    value = _values(request, COMMON | {'category', 'slot', 'entry_ref', 'mutation_id',
        'base_revision'} | (set() if remove else {'title'}))
    result = PortableJourney(request.portable_authority).favourite(
        request.session.get('portable_session'), remove=remove, **value)
    return _redirect(result['profile_ref'])


@require_POST
@csrf_protect
@_errors
def location(request):
    action=request.POST.get('action')
    ga._require(action in ('save','skip','remove'),'CONTRACT_INVALID',422)
    names=COMMON|{'action'}
    if action=='save':
        names|={'country','postcode'}
        if 'radius_km' in request.POST:names|={'radius_km'}
    value=_values(request,names)
    if 'radius_km' in value:
        from .portable_source_views import _revision
        value['radius_km']=_revision(value['radius_km'])
    result=PortableJourney(request.portable_authority).location(request.session.get('portable_session'),**value)
    return _redirect(result['profile_ref'])


@require_POST
@csrf_protect
@_errors
def prepare(request):
    value=_values(request, COMMON | {'connection_ref','client_key','refresh_nonce'} |
        ({'noise'} if 'noise' in request.POST else set()))
    if 'noise' in value:
        from .enhance_selection import parse_noise
        value['noise']=parse_noise(value['noise'])
    result=PortableJourney(request.portable_authority).prepare(request.session.get('portable_session'),**value)
    return HttpResponse(status=303, headers={'Location': '/enhance?' + urlencode({
        key: result[key] for key in ('profile_ref', 'connection_ref', 'run_ref')}),
        'Cache-Control': 'no-store'})
