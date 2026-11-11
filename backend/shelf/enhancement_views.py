"""Authenticated enhancement lifecycle; never starts a model or invents source inventory."""
from functools import wraps
from copy import copy, deepcopy
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, OperationalError, transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from shelf import pick_enhancement as enhancement
from shelf.acquisition_contract import Blocked
from shelf.discovery_state import Conflict


def destination(profile_id, category):
    if category == 'artist':
        return f'/profiles/{profile_id}/discovery/?mode=household&queue=artist#enhance-heading'
    return f'/profiles/{profile_id}/?' + urlencode({'queue': category}) + '#enhance-heading'


def errors(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except Conflict as error:
            return JsonResponse({'state': 'stale', 'message': str(error)}, status=409)
        except Blocked as error:
            return JsonResponse({'state': 'blocked', 'message': str(error)}, status=400)
        except (IntegrityError, OperationalError):
            return JsonResponse({'state': 'unavailable', 'message': 'Enhancement storage is busy. Check the existing request before retrying.'}, status=503)
    return wrapped


def scalar_fields(request, allowed):
    try:
        size = int(request.META.get('CONTENT_LENGTH') or 0)
    except ValueError:
        raise Blocked('A bounded scalar form is required.') from None
    if (not 0 <= size <= 16384 or request.FILES or set(request.POST) - allowed - {'csrfmiddlewaretoken'}
            or any(len(request.POST.getlist(key)) != 1 for key in request.POST)):
        raise Blocked('Only the exact bounded enhancement form may be submitted.')


def receipt(request, profile_id, run, consumer):
    category = consumer['category']
    try:
        binding = enhancement.preview(request, profile_id, category)
    except (Blocked, Conflict):
        binding = None
    result = enhancement.consumer_progress(run, consumer, binding)
    kwargs = {'profile_id': profile_id, 'category': category, 'request_id': consumer['request_id']}
    result.update({name + '_url': reverse('enhancement_' + name, kwargs=kwargs)
                   for name in ('status', 'apply', 'cancel')})
    labels = {
        'awaiting_authorized_worker': ('Enhancement queued', 'Your explicit request is waiting for the authorized Astra worker. No result has been recorded.'),
        'running': ('Astra worker reserved', 'One supervised attempt is reserved. The actual result is still pending.'),
        'completed': ('Taste snapshot ready', 'The actual Astra result is recorded. Only verified cached picks can be applied.'),
        'stale': ('Preferences or sources changed', 'This application preview is historical. The existing model receipt is retained; reconcile any uncertain attempt before further model work.'),
        'failed': ('Enhancement failed', 'No automatic retry will launch. Check the existing attempt with your operator.'),
        'unknown': ('Outcome uncertain', 'Reconcile the existing attempt; do not launch a replacement blindly.'),
        'cancelled': ('Application stopped', 'This round will not be applied. An already reserved external turn may continue.'),
    }
    result['label'], result['explanation'] = labels.get(result['state'], ('Review this request', 'No new result is claimed.'))
    if consumer.get('state') == 'applied' and result['state'] == 'completed':
        result['outcome'] = consumer.get('outcome')
        result['explanation'] = {'supported': 'Your verified cached picks now use the recorded taste associations.',
            'unchanged': 'The recorded associations did not justify changing this cached selection.',
            'no_candidates': 'No verified cached candidates are available in this category. Fetch sources explicitly before trying a new pool.'}.get(consumer.get('outcome'), result['explanation'])
    return result


def materialize(request, profile_id, consumer):
    """Publish only a current bound order, after rendering succeeds in a copied session.

    Applied evidence can survive a failed response/session save. Recovery still
    requires the same taste, pool, actual result and approved pre/post order.
    """
    from shelf import provider_views, discovery_views
    from shelf.provider_views import _profile
    category, request_id = consumer['category'], consumer['request_id']
    with transaction.atomic():
        _, consumer = enhancement.validate_application(request, profile_id, category, request_id)
        staged = copy(request)
        staged.session = deepcopy(request.session)
        profile = _profile(staged, profile_id, 'select')
        ids = consumer.get('after_ids', [])
        result = {}
        if category == 'artist':
            if ids:
                discovery_views.apply_enhancement_order(staged, profile_id, ids)
            row = discovery_views.storage.owned(staged, profile_id, 'household')
            cards = discovery_views.candidates(staged, profile_id, 'household', row.history if row else [])
            context = discovery_views._context(staged, profile, 'household', 'artist', 'all', row, cards)
            result['deck_html'] = ''.join(render_to_string('cabinet/discovery_card.html', {**context, 'row': item}, request=staged)
                                         for item in context['music'])
            result.update(deck_kind='artist', mode='household', queue='artist', revision=context['revision'], nonce=context['nonce'])
        else:
            state = enhancement.catalogue_state(staged, profile_id, category)
            known = state['candidates']
            if any(key not in known or key in state['choices'] or category not in ('all', known[key]['medium']) for key in ids):
                raise Conflict('An enhanced source identity is no longer in this queue.')
            if state['current'] != ids:
                state['current'] = list(ids)
                provider_views._store_durable(staged, state)
            context = provider_views._page_context(staged, profile, state)
            result['deck_html'] = ''.join(render_to_string('cabinet/provider_card.html', {**context, 'card': card, 'search_result': False}, request=staged)
                                         for card in context['cards'])
            result.update(deck_kind='catalogue', queue=category, run_id=state.get('run_id', ''), revision=state['revision'], nonce=state['nonce'])
        result['enhance_context'] = {key: context[key] for key in ('enhance_digest', 'enhance_token', 'enhance_url', 'active_queue', 'enhancement') if key in context}
        # Recheck real authority and source freshness after rendering, before
        # publishing any staged session changes to the response.
        enhancement.validate_application(request, profile_id, category, request_id)
        for key in set(request.session.keys()) | set(staged.session.keys()):
            if key not in staged.session:
                del request.session[key]
            elif key not in request.session or request.session[key] != staged.session[key]:
                request.session[key] = staged.session[key]
        return result


def response(request, profile_id, run, consumer, *, applied=False):
    result = receipt(request, profile_id, run, consumer)
    if applied and consumer.get('state') == 'applied':
        if result['state'] != 'completed':
            raise Conflict('This application preview is no longer current. The existing receipt is retained.')
        result.update(materialize(request, profile_id, consumer))
    if 'application/json' in request.headers.get('Accept', ''):
        return JsonResponse(result)
    return redirect(destination(profile_id, consumer['category']))


@login_required
@csrf_protect
@require_POST
@errors
def request_pick(request, profile_id, category):
    scalar_fields(request, {'preview_token', 'expected_digest', 'category'})
    if request.POST.get('category') != category:
        raise Blocked('Category must match the signed preview route.')
    run, consumer = enhancement.enroll(request, profile_id, category,
        request.POST.get('preview_token', ''), request.POST.get('expected_digest', ''))
    return response(request, profile_id, run, consumer)


@login_required
@require_GET
@errors
def status(request, profile_id, category, request_id):
    run, consumer = enhancement.lookup(request, profile_id, category, request_id)
    data = receipt(request, profile_id, run, consumer)
    if 'application/json' in request.headers.get('Accept', ''):
        return JsonResponse(data)
    from shelf.provider_views import _profile
    return render(request, 'cabinet/enhancement_page.html', {'selected_profile': _profile(request, profile_id, 'select'),
        'enhancement': data, 'catalogue_url': destination(profile_id, category), 'feedback_enabled': True})


@login_required
@csrf_protect
@require_POST
@errors
def apply(request, profile_id, category, request_id):
    scalar_fields(request, set())
    run, consumer = enhancement.apply(request, profile_id, category, request_id)
    return response(request, profile_id, run, consumer, applied=True)


@login_required
@csrf_protect
@require_POST
@errors
def cancel(request, profile_id, category, request_id):
    scalar_fields(request, set())
    run, consumer = enhancement.apply(request, profile_id, category, request_id, cancel=True)
    return response(request, profile_id, run, consumer)


# Portable adapters deliberately do not use legacy login_required, private IDs,
# cached-pool enrollment or source callbacks. The service resolves every opaque
# reference under the exact runtime authority and rechecks sensitive commands.
@sensitive_variables()
def _portable_service(request):
    from .portable_authority import AuthorityFault
    from .portable_enhance_service import get_service
    authority = getattr(request, 'portable_authority', None)
    if authority is None:
        raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503)
    session_ref = request.session.get('portable_session')
    authority.authorize(session_ref, sensitive=True)
    service = get_service(authority)
    if service.authority is not authority:
        raise AuthorityFault('AUTHORITY_DENIED', 403)
    return service, session_ref


def _portable_json(request):
    return 'application/json' in request.headers.get('Accept', '')


@sensitive_variables()
def _portable_actions(value):
    from .portable_connection_views import _uuid
    allowed = value.get('actions', [])
    result = {}
    if value.get('schema') != 'enhance/v1' or type(allowed) is not list:
        return result
    if value.get('run_ref'):
        ref = str(_uuid(value['run_ref']))
        for name in ('preview', 'confirm', 'cancel', 'status'):
            if name in allowed:
                result[name] = '/enhance/runs/' + ref + '/' + name
    return result


# A literal parent avoids importing authenticated legacy navigation into the
# separate portable URLconf. Existing callers still inherit provider_base.html.
_PORTABLE_ENHANCE_BASE = '''<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Enhance · Culture Cabinet</title><link rel="stylesheet" href="/enhance/style.css"><link rel="stylesheet" href="/enhance/presentation.css">
</head><body><a class="skip-link" href="#main">Skip to content</a>
<header class="masthead"><p class="wordmark">Culture Cabinet</p><nav><a href="/setup">Owner setup and Your model</a><a href="/setup/cabinet">Choose your cabinet</a></nav></header>
<main id="main" tabindex="-1">{% block content %}{% endblock %}</main></body></html>'''


@sensitive_variables()
def _portable_html(request, value, *, status=200, selection=None):
    import uuid
    from django.http import HttpResponse
    from django.middleware.csrf import get_token
    from django.template import Engine
    # No request/RequestContext, provider prose or arbitrary action URLs enter
    # presentation. Exact body text is supplied only by the service's preview.
    commands = _portable_actions(value)
    value = {**value}
    for name in ('category_outcomes', 'source_use'):
        if value.get(name):
            value[name] = [{**row, 'category_label': _CATEGORY_LABELS.get(row['category'], row['category'])}
                           for row in value[name]]
    context = {'enhancement_base': Engine.get_default().from_string(_PORTABLE_ENHANCE_BASE),
        'portable_ui': True, 'enhancement': value, 'portable_commands': commands,
        'csrf_token': get_token(request), 'disclosure': value.get('disclosure')}
    if value.get('resume_ref'):
        from .portable_connection_views import _uuid
        context['portable_return_url'] = '/enhance?' + urlencode({'run_ref': str(_uuid(value['resume_ref']))})
    if selection and value.get('available') is True and 'start' in value.get('actions', []):
        commands['start'] = '/enhance/start'
        context.update(selection, client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
    navigation = selection
    if navigation is None and value.get('run_ref') and status < 400:
        from .portable_authority import AuthorityFault
        try:
            navigation = _run_selection(request, value['run_ref'])
        except AuthorityFault:
            # No authority can be inferred from a status-shaped browser value.
            navigation = None
    if navigation:
        context['cabinet_url'] = _cabinet_url(navigation['profile_ref'], navigation.get('connection_ref'),
            run_ref=value.get('run_ref'))
        context['feedback_url'] = '/cabinet/taste?' + urlencode({'profile_ref': navigation['profile_ref']})
    html = render_to_string('cabinet/enhancement_page.html', context)
    return HttpResponse(html, status=status, headers={'Cache-Control': 'no-store',
                                                    'X-Content-Type-Options': 'nosniff'})


@sensitive_variables()
def _portable_reply(request, value, *, review=False):
    from django.http import HttpResponse
    if _portable_json(request):
        return JsonResponse(value, headers={'Cache-Control': 'no-store'})
    if request.method == 'POST':
        # PRG stores neither prompt nor confirmation in the browser session.
        # A following GET reauthorizes and reconstructs a requested preview;
        # refresh never repeats collection, inference or confirmation.
        from .portable_connection_views import _uuid
        ref = str(_uuid(value['run_ref']))
        location = '/enhance?' + urlencode({'run_ref': ref, **({'review': '1'} if review else {})})
        return HttpResponse(status=303, headers={'Location': location, 'Cache-Control': 'no-store'})
    return _portable_html(request, value)


def _portable_errors(view):
    from .portable_authority import AuthorityFault
    from .enhance_contract import SAFE_CODES
    from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
    @wraps(view)
    @sensitive_variables()
    @sensitive_post_parameters()
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except AuthorityFault as error:
            code = error.code if error.code in SAFE_CODES else 'CONTRACT_INVALID'
            if _portable_json(request):
                return JsonResponse({'ok': False, 'code': code}, status=error.status,
                                    headers={'Cache-Control': 'no-store'})
            value = {'schema': 'enhance/v1', 'state': 'blocked', 'stage': 'blocked',
                     'code': code, 'actions': [], 'category_outcomes': []}
            # Preserve only a navigation reference after a rejected command. A
            # fresh GET must authorize again; no body/preview/checkbox is replayed.
            from .portable_connection_views import _uuid
            ref = kwargs.get('run_ref') or (args[0] if args else None)
            if ref is None and len(request.GET.getlist('run_ref')) == 1:
                ref = request.GET['run_ref']
            if ref is not None:
                try:
                    value['resume_ref'] = str(_uuid(str(ref)))
                except AuthorityFault:
                    pass
            return _portable_html(request, value, status=error.status)
    return wrapped


@sensitive_variables()
def _portable_fields(request, fields):
    from .portable_authority import AuthorityFault
    from .portable_connection_views import _body
    from .portable_setup_views import _form
    # JSON booleans/integers are exact types; native fields have one documented
    # spelling. Reject duplicates, files, unlisted fields and authority/body dicts.
    try:
        size = int(request.META.get('CONTENT_LENGTH') or 0)
        if not 0 <= size <= 16384 or request.FILES:
            raise ValueError
    except (ValueError, TypeError):
        raise AuthorityFault('CONTRACT_INVALID', 422) from None
    if request.content_type == 'application/json':
        return _body(request, fields)
    value = _form(request, fields)
    for name, kind in fields.items():
        if kind is bool:
            value[name] = value[name] == 'yes'
        elif kind is int:
            raw = value[name]
            if len(raw) > 16 or not raw.isascii() or not raw.isdecimal() or str(int(raw)) != raw or not 0 < int(raw) <= 2**53 - 1:
                raise AuthorityFault('CONTRACT_INVALID', 422)
            value[name] = int(raw)
    return value


@require_GET
@_portable_errors
def portable_page(request):
    from .portable_authority import AuthorityFault
    from .portable_connection_views import _uuid
    service, session_ref = _portable_service(request)
    query = request.GET
    if any(len(query.getlist(k)) != 1 for k in query):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    if 'run_ref' in query:
        if set(query) - {'run_ref', 'review'} or query.get('review', '1') != '1':
            raise AuthorityFault('CONTRACT_INVALID', 422)
        ref = str(_uuid(query['run_ref']))
        value = service.status(session_ref, run_ref=ref)
        if 'review' in query and 'preview' in value.get('actions', []):
            value = service.preview(session_ref, run_ref=ref)
        return _portable_html(request, value)
    if not query:
        return _portable_html(request, {'schema': 'enhance/v1', 'stage': 'blocked', 'state': 'blocked',
            'code': 'CAPABILITY_UNAVAILABLE', 'actions': [], 'category_outcomes': []})
    if set(query) != {'profile_ref', 'connection_ref'}:
        raise AuthorityFault('CONTRACT_INVALID', 422)
    selection = {key: str(_uuid(query[key])) for key in ('profile_ref', 'connection_ref')}
    value = service.readiness(session_ref, **selection)
    value = {**value, 'schema': 'enhance/v1', 'state': 'authorized' if value['available'] else 'blocked',
             'stage': 'authorized' if value['available'] else 'blocked', 'category_outcomes': []}
    return _portable_html(request, value, selection=selection)


@require_POST
@csrf_protect
@_portable_errors
def portable_start(request):
    from .portable_connection_views import _uuid
    service, session_ref = _portable_service(request)
    value = _portable_fields(request, {k: str for k in
        ('profile_ref', 'connection_ref', 'client_key', 'refresh_nonce')})
    value = {key: str(_uuid(ref)) for key, ref in value.items()}
    return _portable_reply(request, service.start(session_ref, **value))


@require_GET
@_portable_errors
def portable_status(request, run_ref):
    service, session_ref = _portable_service(request)
    return _portable_reply(request, service.status(session_ref, run_ref=str(run_ref)))


@require_POST
@csrf_protect
@_portable_errors
def portable_preview(request, run_ref):
    service, session_ref = _portable_service(request)
    _portable_fields(request, {})
    from .portable_authority import AuthorityFault
    current = service.status(session_ref, run_ref=str(run_ref))
    if 'preview' not in current.get('actions', []):
        raise AuthorityFault('AUTHORITY_DENIED', 403)
    return _portable_reply(request, service.preview(session_ref, run_ref=str(run_ref)), review=True)


@require_POST
@csrf_protect
@_portable_errors
def portable_confirm(request, run_ref):
    from .portable_connection_views import _uuid
    from .portable_authority import AuthorityFault
    service, session_ref = _portable_service(request)
    value = _portable_fields(request, {'preview_ref': str, 'body_digest': str, 'confirmed': bool})
    value['preview_ref'] = str(_uuid(value['preview_ref']))
    if (len(value['body_digest']) != 64 or any(c not in '0123456789abcdef' for c in value['body_digest'])
            or value['confirmed'] is not True):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    return _portable_reply(request, service.confirm(session_ref, run_ref=str(run_ref), **value))


@require_POST
@csrf_protect
@_portable_errors
def portable_cancel(request, run_ref):
    from .portable_authority import AuthorityFault
    service, session_ref = _portable_service(request)
    value = _portable_fields(request, {'expected_revision': int})
    if not 0 < value['expected_revision'] <= 2**53 - 1:
        raise AuthorityFault('CONTRACT_INVALID', 422)
    return _portable_reply(request, service.cancel(session_ref, run_ref=str(run_ref), **value))


@require_GET
@_portable_errors
def portable_current(request, profile_ref):
    service, session_ref = _portable_service(request)
    value = service.current(session_ref, profile_ref=str(profile_ref))
    # This is a filtered machine read, not a guessed card renderer. Its manifest
    # is never copied into the legacy session deck or claimed as rendered picks.
    return JsonResponse(value, headers={'Cache-Control': 'no-store'})


@require_GET
def portable_style(request):
    from pathlib import Path
    from django.http import HttpResponse
    source = Path(__file__).resolve().parents[2] / 'static' / 'cabinet.css'
    return HttpResponse(source.read_bytes(), content_type='text/css; charset=utf-8',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


def _cabinet_url(profile_ref, connection_ref=None, *, category=None, run_ref=None):
    from .portable_connection_views import _uuid
    values = {'profile_ref': str(_uuid(profile_ref))}
    if connection_ref:
        values['connection_ref'] = str(_uuid(connection_ref))
    if category:
        values['category'] = category
    if run_ref:
        values['run_ref'] = str(_uuid(run_ref))
    return '/cabinet?' + urlencode(values)


@sensitive_variables()
def _run_selection(request, run_ref):
    """Read only durable scope metadata, never run input/output or source stores.

    A browser return URL cannot assert which profile owns a run. Resolve its
    installation/owner/profile from stored bindings and reauthorize before
    building navigation; each destination rechecks again under current policy.
    """
    from . import grant_authority as ga
    from .models import ProfileCabinetState, SynthesisRun
    authority = request.portable_authority
    ref = request.session.get('portable_session')
    with ga.recovery_guard(authority, ref), authority._lock:
        installation, session = ga._owner(authority, ref, sensitive=True)
        row = SynthesisRun.objects.only('profile_id', 'portable_connection_id').filter(
            pk=ga._uuid(run_ref), installation=installation, actor_id=session.owner_id,
            contract_version='enhance/v1').first()
        if row is None:
            raise ga.AuthorityFault('AUTHORITY_DENIED', 403)
        bound = ProfileCabinetState.objects.filter(profile_id=row.profile_id,
            installation=installation, actor_id=session.owner_id).first()
        if bound is None:
            raise ga.AuthorityFault('AUTHORITY_DENIED', 403)
        ga._bound(authority, ref, str(bound.profile_ref), sensitive=True)
        return {'profile_ref': str(bound.profile_ref), 'connection_ref': str(row.portable_connection_id)}


def _display_text(value, maximum):
    from .portable_authority import AuthorityFault
    if type(value) is not str or len(value) > maximum:
        raise AuthorityFault('CONTRACT_INVALID', 422)
    return value


def _source_link(value):
    from urllib.parse import urlsplit
    text = _display_text(value, 2048)
    try:
        parsed = urlsplit(text)
        safe = (parsed.scheme == 'https' and parsed.hostname and parsed.username is None
                and parsed.password is None and parsed.port in (None, 443)
                and '\\' not in text and not any(ord(c) <= 32 or ord(c) == 127 for c in text))
    except ValueError:
        safe = False
    # An invalid optional source link is withheld, never repaired or guessed.
    return text if safe else ''


from .category_registry import CATEGORIES as _FEEDBACK_CATEGORIES
_CATEGORY_LABELS = {row.key: row.label for row in _FEEDBACK_CATEGORIES}
_CATEGORY_LABELS.update(music='Music', events='Events', outings='Outings')
_CATEGORY_COPY = {'fresh': 'Published picks', 'empty_bounded': 'No candidates within the collection limits.',
    'unavailable': 'Source unavailable; no fresh picks.', 'failed': 'Collection failed; not an empty result.',
    'cancelled': 'Collection cancelled; no fresh picks.',
    'retained_previous_stale': 'Older picks retained by current policy; not refreshed by this round.'}
_REASON_LABELS = {'explicit': 'Explicit taste', 'source': 'Source evidence', 'inferred': 'Inferred connection'}
_SAVED_ATTRIBUTION_UNAVAILABLE = (
    'Saving is unavailable because durable attribution permission is not established.')


@sensitive_variables()
def _cabinet_projection(current):
    """Allowlist a detached, display-only projection of current()'s filtered read.

    RC1 suppression belongs to the filtered producer: missing reasons are never
    rebuilt from retained personal/inferred text, including after Undo/replay.
    Never fill missing categories/cards/reasons from prior responses, run output
    or local caches. A refused/malformed publication is an error, not permission
    to reconstruct a previously visible card. This function grants no rights.
    """
    from .portable_authority import AuthorityFault
    from .enhance_contract import SAFE_CODES
    def require(condition):
        if not condition:
            raise AuthorityFault('CONTRACT_INVALID', 422)
    require(type(current) is dict and current.get('schema') == 'cabinet-current/v1')
    require(type(current.get('published')) is bool and current.get('code') in SAFE_CODES)
    if not current['published']:
        require(current.get('manifest') is None)
        return {'published': False, 'categories': [], 'publication_revision': None, 'code': current['code'],
                    **({'read_code': current['code']} if current['code'] != 'OK' else {})}
    revision = current.get('publication_revision')
    require(type(revision) is int and 0 < revision <= 2**53 - 1)
    manifest = current.get('manifest')
    require(type(manifest) is dict and manifest.get('schema') == 'cabinet-manifest/v1')
    categories, reasons = manifest.get('categories'), manifest.get('for_you')
    require(type(categories) is list and len(categories) <= 100 and type(reasons) is list and len(reasons) <= 100)
    result, cards, seen_categories = [], {}, set()
    for row in categories:
        require(type(row) is dict)
        category = _display_text(row.get('category'), 80)
        require(category and category not in seen_categories and row.get('state') in _CATEGORY_COPY
                and row.get('code') in SAFE_CODES and type(row.get('cards')) is list and len(row['cards']) <= 20)
        require((row['state'] == 'fresh') == bool(row['cards']))
        seen_categories.add(category)
        projected = {'category': category, 'label': _CATEGORY_LABELS.get(category, category),
            'state': row['state'], 'status': _CATEGORY_COPY[row['state']], 'code': row['code'], 'cards': []}
        if row['state'] == 'unavailable' and row['code'] == 'CAPABILITY_UNAVAILABLE':
            projected['status'] = 'Not available with the current connections or integration.'
        for card in row['cards']:
            require(type(card) is dict and card.get('category') == category)
            key = _display_text(card.get('key'), 300)
            require(key and key not in cards and len(cards) < 100)
            item = {'title': _display_text(card.get('title'), 300), 'reasons': [], 'facts': [],
                    'candidate_key': key,
                    'save_list_kind': {'book': 'read', 'movie': 'watch', 'tv': 'watch'}.get(category)}
            require(bool(item['title']))
            for name, limit in [('medium', 300), ('provider', 300), ('observed_at', 100), ('valid_until', 100)]:
                item[name] = _display_text(card.get(name, ''), limit)
            item['source_url'] = _source_link(card.get('source_url', ''))
            facts = card.get('facts', [])
            require(type(facts) is list and len(facts) <= 40)
            for fact in facts:
                require(type(fact) is dict)
                name = _display_text(fact.get('property'), 80)
                value = fact.get('value')
                require(type(value) in (str, int, bool))
                if type(value) is str:
                    value = _display_text(value, 1200)
                elif type(value) is bool:
                    value = 'Yes' if value else 'No'
                else:
                    require(abs(value) <= 2**53 - 1)
                    value = str(value)
                item['facts'].append({'name': name, 'value': value})
            cards[key] = item
            projected['cards'].append(item)
        result.append(projected)
    for reason in reasons:
        require(type(reason) is dict and reason.get('candidate_key') in cards
                and reason.get('kind') in _REASON_LABELS and type(reason.get('evidence_refs')) is list
                and len(reason['evidence_refs']) <= 100)
        refs = [_display_text(ref, 300) for ref in reason['evidence_refs']]
        cards[reason['candidate_key']]['reasons'].append({'text': _display_text(reason.get('text'), 1200),
            'label': _REASON_LABELS[reason['kind']], 'evidence_count': len(refs)})
    return {'published': True, 'publication_revision': revision, 'categories': result, 'code': current['code']}


def _feedback_signals(rows, *, actions=()):
    """Detach typed display state without normalizing the exact note text.

    Revisions/events remain opaque command bindings, never permission. Only the
    handler can authorize a write or decide whether an old event can be undone.
    """
    from .portable_authority import AuthorityFault
    from .portable_connection_views import _uuid
    def require(value):
        if not value:
            raise AuthorityFault('CONTRACT_INVALID', 422)
    require(type(rows) is list and len(rows) <= 3)
    result, seen = [], set()
    for row in rows:
        require(type(row) is dict)
        signal = row.get('signal')
        require(signal in ('interest', 'enjoyment', 'note') and signal not in seen)
        seen.add(signal)
        revision, undo = row.get('revision', 0), row.get('can_undo', False)
        # A shared null-clear keeps its event/Undo; authored text='' is still
        # a real empty note. Do not infer clear from a falsy value.
        cleared = row.get('cleared', False)
        require(type(cleared) is bool)
        require(type(revision) is int and 0 <= revision <= 2**53 - 1 and type(undo) is bool)
        event = row.get('event_ref', '')
        require(type(event) is str)
        if event:
            event = str(_uuid(event))
        require(not undo or bool(event))
        value = row.get('value')
        if signal == 'note':
            text = _display_text(row.get('text', ''), 500)
            require('\x00' not in text)
            value = text if event and not cleared else None
        elif signal == 'interest':
            require(value is None or type(value) is str and value in ('no', 'maybe', 'yes'))
        else:
            require(value is None or type(value) is int and 1 <= value <= 5)
        item = {'signal': signal, 'value': value, 'revision': revision, 'event_ref': event,
                'mutation_id': str(_uuid(row.get('mutation_id'))), 'can_undo': undo,
                'can_set': signal in actions, 'cleared': cleared}
        item['undo_mutation_id'] = str(_uuid(row.get('undo_mutation_id'))) if undo else ''
        result.append(item)
    return result


def _taste_display_context(value):
    """Map the frozen native handler projection into category-scoped forms.

    Never generate mutation IDs here. The one new-entry intent offers a choice
    of forms; after any successful POST, the handler issues fresh bindings.
    Category-note labels/text are display context, not source query material.
    """
    from .category_registry import KEYS
    from .portable_authority import AuthorityFault
    from .portable_connection_views import _uuid
    if value.get('feedback_version') != 'workflow-taste/v1':
        raise AuthorityFault('CONTRACT_INVALID', 422)
    rows, category = value['feedback_categories'], value['feedback_category']
    if (type(rows) is not list or len(rows) != len(KEYS) or any(type(r) is not dict for r in rows)
            or [r.get('key') for r in rows] != list(KEYS) or category not in KEYS):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    categories = [{'key': r['key'], 'label': _display_text(r['label'], 80)} for r in rows]
    new = value['new_entry']
    if type(new) is not dict or type(new.get('base_revision')) is not int or new['base_revision'] != 0:
        raise AuthorityFault('CONTRACT_INVALID', 422)
    signals = _feedback_signals(new.get('signals'))
    if {row['signal'] for row in signals} != {'interest', 'enjoyment', 'note'}:
        raise AuthorityFault('CONTRACT_INVALID', 422)
    entry_ref = str(_uuid(new.get('entry_ref')))
    groups = [{'entry_ref': entry_ref, 'entry_kind': 'item', 'is_new': True,
               'title': '', 'signals': signals},
              {'entry_ref': entry_ref, 'entry_kind': 'category_note', 'is_new': True,
               'title': '', 'signals': [r for r in signals if r['signal'] == 'note']}]
    entries = value['feedback_entries']
    if type(entries) is not list or len(entries) > 100:
        raise AuthorityFault('CONTRACT_INVALID', 422)
    for row in entries:
        if type(row) is not dict or row.get('category') not in KEYS or row.get('entry_kind') not in ('item', 'category_note'):
            raise AuthorityFault('CONTRACT_INVALID', 422)
        if row['category'] != category:
            continue
        signals = _feedback_signals(row.get('signals'))
        if row['entry_kind'] == 'category_note':
            signals = [r for r in signals if r['signal'] == 'note']
        groups.append({'entry_ref': str(_uuid(row.get('entry_ref'))), 'entry_kind': row['entry_kind'],
            'title': _display_text(row['title'], 200), 'is_new': False, 'signals': signals})
    return {'taste_categories': categories, 'taste_category': category, 'taste_groups': groups,
            'feedback_error_code': _display_text(value.get('feedback_code', ''), 80)}


def _card_feedback_display(value, *, profile_ref, target_ref):
    from .portable_authority import AuthorityFault
    if (type(value) is not dict or value.get('profile_ref') != profile_ref
            or value.get('target_ref') != target_ref or value.get('feedback_action') != '/cabinet/feedback'
            or type(value.get('actions')) is not list
            or any(a not in ('interest', 'enjoyment', 'note') for a in value['actions'])):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    return {'target_ref': target_ref, 'signals': _feedback_signals(value['signals'], actions=value['actions'])}


def _source_setup_display(value):
    """Validate fixed native labels/actions before rendering source setup.

    Display metadata never grants source or model use. The route descriptor is
    shared with the native handlers, so a substituted action cannot solicit a key.
    """
    from .portable_authority import AuthorityFault
    from .portable_connection_views import _uuid
    from .source_authority import NATIVE_SETUP, MEDIA, source_identity
    adapter = value.get('source_adapter', 'musicbrainz_recording')
    if type(adapter) is not str or adapter not in NATIVE_SETUP:
        raise AuthorityFault('CONTRACT_INVALID', 422)
    category, label, base = NATIVE_SETUP[adapter]
    page = base + '?' + urlencode({'profile_ref': str(_uuid(value['profile_ref']))})
    expected = {'source_category': category, 'source_label': label, 'source_page_url': page,
                'source_configure_action': base + '/configure', 'source_consent_action_url': base + '/consent',
                'source_credential_required': adapter in MEDIA,
                'source_credential_action': base + '/credential' if adapter in MEDIA else '',
                'source_origin': source_identity(adapter)[1], 'source_path': source_identity(adapter)[2]}
    if any(key in value and value[key] != item for key, item in expected.items()):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    if (any(key in value and type(value[key]) is not bool
            for key in ('source_credential_present', 'source_credential_required'))
            or 'source_readiness' in value and value['source_readiness'] not in
                ('not_configured', 'credential_required', 'permission_required', 'ready')):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    return {'source_adapter': adapter, **expected}


def _feedback_outcome_display(value):
    """Display only the consumed backend receipt's closed historical outcome.

    A committed response and a noncurrent publication can coexist. This notice
    cannot certify current card values or restore a publication; its absence on
    reload likewise says nothing about whether the durable write happened.
    """
    if value is None:
        return ''
    from .portable_authority import AuthorityFault
    messages = {'RESPONSE_SAVED': 'Your response was saved.',
                'RESPONSE_UNDONE': 'Your response was undone.'}
    if (type(value) is not dict or set(value) != {'schema', 'code'}
            or value.get('schema') != 'feedback-action-outcome/v1'
            or type(value.get('code')) is not str or value['code'] not in messages):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    return messages[value['code']]


@sensitive_variables()
def _native_cabinet_html(request, context, *, status=200):
    from django.http import HttpResponse
    from django.middleware.csrf import get_token
    from django.template import Engine
    if context.get('taste_input') is True:
        context = {**context, **_taste_display_context(context)}
    if context.get('source_setup') is True:
        context = {**context, **_source_setup_display(context)}
    context = {**context,
               'feedback_outcome_message': _feedback_outcome_display(context.get('feedback_outcome')),
               'csrf_token': get_token(request),
               'enhancement_base': Engine.get_default().from_string(_PORTABLE_ENHANCE_BASE)}
    return HttpResponse(render_to_string('cabinet/portable_cabinet.html', context), status=status,
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


def render_portable_saved(request, *, template, context, status=200):
    """Render N1's safe projection without request or authority in the template.

    A selection token is command context, never cached permission. N1 rechecks
    its live generation and PA1/PS1 boundaries on every command. The page
    allowlist prevents a route value choosing a legacy authentication surface.
    """
    from django.http import HttpResponse
    from django.middleware.csrf import get_token
    from django.template import Engine
    allowed = {'cabinet/portable_saved_items.html', 'cabinet/portable_obtain_preview.html',
               'cabinet/portable_obtain_submission.html', 'cabinet/portable_obtain_job.html',
               'cabinet/portable_obtain_tv.html', 'cabinet/portable_obtain_options.html'}
    if template not in allowed:
        raise ValueError('Unsupported portable saved template')
    values = {**context, 'csrf_token': get_token(request),
              'enhancement_base': Engine.get_default().from_string(_PORTABLE_ENHANCE_BASE)}
    return HttpResponse(render_to_string(template, values), status=status,
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@require_GET
@_portable_errors
def portable_entry(request):
    from .portable_setup_views import cabinet_choices
    from .portable_connection_views import _uuid
    from .portable_authority import AuthorityFault
    if set(request.GET) - {'profile_ref'} or any(len(request.GET.getlist(k)) != 1 for k in request.GET):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    choices = cabinet_choices(request)
    selected = request.GET.get('profile_ref', '')
    if selected:
        selected = str(_uuid(selected))
        if selected not in {row['profile_ref'] for row in choices['profiles']}:
            raise AuthorityFault('AUTHORITY_DENIED', 403)
    return _native_cabinet_html(request, {**choices, 'entry': True, 'selected_profile_ref': selected})


@require_GET
@_portable_errors
def portable_cabinet(request):
    from .portable_setup_views import cabinet_choices
    from .portable_connection_views import _uuid
    from .portable_authority import AuthorityFault
    query = request.GET
    if (set(query) - {'profile_ref', 'connection_ref', 'category', 'run_ref', 'action_receipt'} or 'profile_ref' not in query
            or any(len(query.getlist(k)) != 1 for k in query)):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    choices = cabinet_choices(request)
    profile_ref = str(_uuid(query['profile_ref']))
    profile = next((p for p in choices['profiles'] if p['profile_ref'] == profile_ref), None)
    if profile is None:
        raise AuthorityFault('AUTHORITY_DENIED', 403)
    connection_ref = str(_uuid(query['connection_ref'])) if 'connection_ref' in query else None
    if connection_ref and connection_ref not in {row['connection_ref'] for row in choices['connections']}:
        raise AuthorityFault('AUTHORITY_DENIED', 403)
    service, session_ref = _portable_service(request)
    context = {'entry': False, 'profile_label': profile['label'], 'profile_ref': profile_ref,
               'connection_ref': connection_ref, 'categories': [], 'published': False,
               # C4: the current producer has no independent durable-attribution
               # signal. Do not offer success until its accepted contract lands;
               # backend refusal remains mandatory even for a fabricated POST.
               'save_unavailable_reason': _SAVED_ATTRIBUTION_UNAVAILABLE}
    from .portable_obtain_views import cabinet_saved_context
    # GET navigation cannot select a profile. Only N1's fresh read-only PA1
    # projection can supply command context for an already selected profile.
    context.update(cabinet_saved_context(request, profile_ref=profile_ref))
    if connection_ref:
        context['enhance_url'] = '/enhance?' + urlencode({'profile_ref': profile_ref, 'connection_ref': connection_ref})
        context['inference_consent_url'] = '/cabinet/consent?' + urlencode({'profile_ref': profile_ref, 'connection_ref': connection_ref})
    status = 200
    try:
        interaction = service.current_for_interaction(session_ref, profile_ref=profile_ref)
        context.update(_cabinet_projection(interaction))
        context['ranking_state'] = interaction.get('ranking_state','none')
    except AuthorityFault as fault:
        # Render only the refusal. Never keep the last manifest in a session or
        # merge previously displayed cards back into a current-policy failure.
        status, context['read_code'] = fault.status, fault.code
    category = query.get('category', '')
    if category and (len(category) > 80 or category not in {r['category'] for r in context['categories']}):
        if 'read_code' not in context:
            raise AuthorityFault('CONTRACT_INVALID', 422)
    run_ref = None
    if 'run_ref' in query:
        run_ref = str(_uuid(query['run_ref']))
        selection = _run_selection(request, run_ref)
        if selection['profile_ref'] != profile_ref:
            raise AuthorityFault('AUTHORITY_DENIED', 403)
        progress = service.status(session_ref, run_ref=run_ref)
        context['round_url'] = '/enhance?' + urlencode({'run_ref': run_ref})
        context['round_pending'] = progress.get('stage') != 'published'
        from .enhance_contract import STAGES
        context['round_blocks_start'] = progress.get('stage') == 'unknown' or progress.get('stage') in STAGES[:-1]
        if context['round_blocks_start']:
            context.pop('enhance_url', None)
    context['active_category'] = category
    # A category change is navigation within this publication/round, never a
    # new run or a way to drop the known unresolved-attempt barrier from the UI.
    context['all_url'] = _cabinet_url(profile_ref, connection_ref, run_ref=run_ref)
    for row in context['categories']:
        row['url'] = _cabinet_url(profile_ref, connection_ref, category=row['category'], run_ref=run_ref)
        row['visible'] = not category or row['category'] == category
    from .card_actions import CardActions
    from .portable_reactions import target_ref as feedback_target_ref
    context['feedback_available'] = True
    context['feedback_url'] = '/cabinet/taste?' + urlencode({'profile_ref': profile_ref,
        **({'category': category} if category else {})})
    # Every fresh feedback-read refusal propagates, including recovery mapped
    # to 503. Status/code alone cannot identify optional missing UI capability.
    # Even a later-card failure must discard all earlier cards and bindings.
    for row in context['categories']:
        if not row['visible']:
            continue
        for card in row['cards']:
            target = feedback_target_ref(profile_ref, card['candidate_key'])
            local_display=interaction.get('local_event_display',{}).get(card['candidate_key'])
            if local_display is not None:
                card['local_event_display']=local_display
            from .card_action_presentation import card_action_context
            card['card_actions'] = card_action_context(CardActions(request.portable_authority,service=service).project(
                session_ref,profile_ref=profile_ref,target_ref=target))
            context['card_actions_available'] = True
            from .portable_obtain_views import cabinet_card_obtain_context
            card.update(cabinet_card_obtain_context(request, profile_ref=profile_ref,
                publication_revision=context.get('publication_revision'), card=card))
    if 'action_receipt' in query:
        command_id=str(_uuid(query['action_receipt']))
        context['action_receipt']=CardActions(request.portable_authority,service=service).receipt(
            session_ref,profile_ref,command_id)
    from .portable_feedback_views import consume_feedback_outcome
    outcome = consume_feedback_outcome(request, profile_ref=profile_ref)
    if outcome is not None:
        context['feedback_outcome'] = outcome
    return _native_cabinet_html(request, context, status=status)


@require_GET
def portable_enhancement_style(request):
    from pathlib import Path
    from django.http import HttpResponse
    source = Path(__file__).resolve().parents[2] / 'static' / 'enhancement.css'
    return HttpResponse(source.read_bytes(), content_type='text/css; charset=utf-8',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


# Existing disclosure classes only. New typed-feedback classes and source-purpose
# grants belong to Taste's accepted producer contract, not this HTTP allowlist.
_INFERENCE_CHOICES = (
    ('canonical_seed', 'Confirmed seed titles'), ('explicit_favourites', 'Explicit favourites'),
    ('reaction', 'Saved reactions'), ('anticipated_reactions', 'Anticipated interest'),
    ('experienced_enjoyment', 'Experienced enjoyment'), ('legacy_choices', 'Original legacy choices'),
    ('raw_notes', 'Exact notes (may contain personal information)'),
    ('free_text_entry', 'Free text you entered (also required to share typed preference notes)'),
    ('inferred_traits', 'Inferred traits with permitted ancestors'))


@sensitive_variables()
def _inference_consent_scope(request, profile_ref, connection_ref):
    from . import grant_authority as ga
    from .models import PortableDisclosureGrant
    authority = request.portable_authority
    session_ref = request.session.get('portable_session')
    with ga.recovery_guard(authority, session_ref), authority._lock:
        state, session = ga._bound(authority, session_ref, profile_ref, sensitive=True)
        _, (_, binding, _) = ga._connection(authority, session_ref, connection_ref)
        grant = PortableDisclosureGrant.objects.filter(cabinet=state,
            connection_id=ga._uuid(connection_ref), purpose='inference').first()
        now = authority.now()
        active = (grant is not None and grant.state == 'active' and now < grant.expires_at
                  and grant.config_digest == binding['config_digest'])
        return {'profile_ref': str(state.profile_ref), 'profile_label': _display_text(state.profile.label, 80),
                'connection_ref': str(ga._uuid(connection_ref)), 'consent_active': active,
                'consent_record_active': grant is not None and grant.state == 'active',
                'consent_revision': grant.revision if grant else 0,
                'consent_action': 'regrant' if grant is not None else 'grant',
                'consent_groups': [dict(_INFERENCE_CHOICES).get(_display_text(code, 80), code.replace('_', ' '))
                                   for code in grant.allowed_data_classes] if grant else [],
                'consent_expires': grant.expires_at.isoformat() if grant else '',
                'consent_destination': _display_text(binding['origin'] + binding['path'], 400)}


@require_GET
@_portable_errors
def portable_inference_consent(request):
    from .portable_authority import AuthorityFault
    from .portable_connection_views import _uuid
    if (set(request.GET) != {'profile_ref', 'connection_ref'}
            or any(len(request.GET.getlist(key)) != 1 for key in request.GET)):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    profile_ref, connection_ref = (str(_uuid(request.GET[key])) for key in ('profile_ref', 'connection_ref'))
    context = _inference_consent_scope(request, profile_ref, connection_ref)
    context.update(consent=True, inference_choices=[{'code': code, 'label': label} for code, label in _INFERENCE_CHOICES],
                   cabinet_url=_cabinet_url(profile_ref, connection_ref))
    return _native_cabinet_html(request, context)


@require_POST
@csrf_protect
@_portable_errors
@sensitive_variables()
def portable_inference_consent_change(request):
    from datetime import timedelta
    from django.http import HttpResponse
    from . import grant_authority as ga
    from .portable_setup_views import _form
    from .portable_connection_views import _uuid
    from .portable_authority import AuthorityFault
    required = {'profile_ref', 'connection_ref', 'expected_revision', 'action', 'confirmed'}
    flags = {'allow_' + code: code for code, _ in _INFERENCE_CHOICES}
    supplied = set(request.POST) - {'csrfmiddlewaretoken'}
    if not required <= supplied or supplied - required - set(flags):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    value = _form(request, supplied)
    raw = value['expected_revision']
    if (len(raw) > 16 or not raw.isascii() or not raw.isdecimal() or str(int(raw)) != raw
            or int(raw) > 2**53 - 1 or value['confirmed'] != 'yes'
            or value['action'] not in ('grant', 'regrant', 'revoke')):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    if any(value[key] != 'yes' for key in supplied & set(flags)):
        raise AuthorityFault('CONTRACT_INVALID', 422)
    classes = [flags[key] for key in supplied & set(flags)]
    profile_ref, connection_ref = (str(_uuid(value[key])) for key in ('profile_ref', 'connection_ref'))
    authority, session_ref = request.portable_authority, request.session.get('portable_session')
    # Never use an OpenAI-bound grant as source permission. The purpose is fixed
    # server-side; source consent must use the dedicated source issuer contract.
    with ga.recovery_guard(authority, session_ref), authority._lock:
        _, session = ga._bound(authority, session_ref, profile_ref, sensitive=True)
        expiry = min(authority.now() + timedelta(minutes=5), ga._end(authority, session))
        ga.change_grant(authority, session_ref, profile_ref=profile_ref, expected_revision=int(raw),
            purpose='inference', connection_ref=connection_ref, allowed_data_classes=classes,
            expires_at=expiry, action=value['action'])
    # A refreshed GET shows current permission only. It cannot approve a body,
    # collect APIs, send inference, or restore a prior note/confirmation payload.
    return HttpResponse(status=303, headers={'Location': '/cabinet/consent?' + urlencode({
        'profile_ref': profile_ref, 'connection_ref': connection_ref}), 'Cache-Control': 'no-store'})
