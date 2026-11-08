"""Closed native intake/card forms over the existing typed event engine."""
import hashlib
from urllib.parse import urlencode
import uuid

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from . import grant_authority as ga
from .category_registry import CATEGORIES, KEYS
from .enhancement_views import _native_cabinet_html, _portable_errors, _cabinet_url
from .portable_authority import AuthorityFault
from .portable_setup_views import _form
from .portable_source_views import _revision
from .portable_taste_inputs import PortableTasteInputs, ENTRY_SIGNALS, signal_rows, typed_body
from .portable_reactions import PortableReactions


_PENDING_OUTCOME = '_portable_feedback_outcome'
_OUTCOME_OPERATIONS = {'RESPONSE_SAVED': 'set', 'RESPONSE_UNDONE': 'reverse'}


def _remember_feedback_outcome(request, *, profile_ref, mutation_id, operation, result):
    """A committed result supplies identifiers only; never retain the command."""
    session_ref = request.session.get('portable_session')
    request.session[_PENDING_OUTCOME] = dict(profile_ref=profile_ref,
        session_digest=hashlib.sha256(session_ref.encode()).hexdigest(),
        mutation_id=str(ga._uuid(mutation_id)), event_ref=str(ga._uuid(result['event_ref'])),
        accepted_revision=result['revision'],
        code='RESPONSE_SAVED' if operation == 'set' else 'RESPONSE_UNDONE')


@sensitive_variables()
def consume_feedback_outcome(request, *, profile_ref):
    """One-use historical completion, independent of publication currentness.

    Recheck the durable receipt under today's owner/recovery guard. The event
    projection deliberately excludes its value/body, so a stale card is never
    restored just to acknowledge a successful earlier command.
    """
    pending = request.session.pop(_PENDING_OUTCOME, None)
    if type(pending) is not dict or set(pending) != {
            'profile_ref', 'session_digest', 'mutation_id', 'event_ref', 'accepted_revision', 'code'}:
        return None
    session_ref = request.session.get('portable_session')
    if (type(session_ref) is not str or pending['profile_ref'] != profile_ref
            or pending['session_digest'] != hashlib.sha256(session_ref.encode()).hexdigest()
            or type(pending['code']) is not str or pending['code'] not in _OUTCOME_OPERATIONS
            or type(pending['accepted_revision']) is not int or pending['accepted_revision'] < 1):
        return None
    try:
        mutation_id, event_ref = (ga._uuid(pending[k]) for k in ('mutation_id', 'event_ref'))
    except AuthorityFault:
        return None
    from .models import MutationReceipt, FeedbackEvent
    authority = request.portable_authority
    with ga.recovery_guard(authority, session_ref, profile_ref=profile_ref), authority._lock:
        state, _ = ga._bound(authority, session_ref, profile_ref)
        operation = _OUTCOME_OPERATIONS[pending['code']]
        receipt = MutationReceipt.objects.filter(actor_id=state.actor_id, profile_id=state.profile_id,
            mutation_id=mutation_id, event_id=event_ref, accepted_revision=pending['accepted_revision'],
            operation_family='feedback.'+operation, state='accepted').only('target_id').first()
        if receipt is None or not FeedbackEvent.objects.filter(pk=event_ref, actor_id=state.actor_id,
                profile_id=state.profile_id, target_id=receipt.target_id,
                revision=pending['accepted_revision'], operation=operation).exists():
            return None
        return {'schema': 'feedback-action-outcome/v1', 'code': pending['code']}


@sensitive_variables()
def feedback_context(authority, session_ref, profile_ref, *, category=None):
    """Safe owner projection, not a capability or a promise of source coverage."""
    category = KEYS[0] if category is None else category
    ga._require(category in KEYS, 'CONTRACT_INVALID', 422)
    profile_ref = str(ga._uuid(profile_ref))
    with ga.recovery_guard(authority, session_ref, profile_ref=profile_ref), authority._lock:
        state, _ = ga._bound(authority, session_ref, profile_ref)
        return {'feedback_version': 'workflow-taste/v1', 'taste_input': True,
            'profile_ref': profile_ref, 'profile_label': str(state.profile.label)[:80],
            'feedback_categories': [vars(row).copy() for row in CATEGORIES],
            'feedback_category': category, 'feedback_entries': PortableTasteInputs(authority).read_entries(
                session_ref, profile_ref),
            'new_entry': {'entry_ref': str(uuid.uuid4()), 'base_revision': 0,
                'signals': [{'signal': signal, 'mutation_id': str(uuid.uuid4())} for signal in ENTRY_SIGNALS]},
            'taste_action': '/cabinet/taste', 'feedback_action': '/cabinet/feedback',
            'cabinet_url': _cabinet_url(profile_ref), 'feedback_code': ''}


@sensitive_variables()
def card_feedback_context(authority, session_ref, profile_ref, *, target_ref):
    profile_ref, target_ref = str(ga._uuid(profile_ref)), str(ga._uuid(target_ref))
    service = getattr(authority, '_enhance_service', None)
    ga._require(service is not None, 'CAPABILITY_UNAVAILABLE', 503)
    reactions = PortableReactions(authority, service=service)
    with ga.current_writer(authority, session_ref, profile_ref) as state:
        target = reactions._target(session_ref, profile_ref, target_ref, require_current=True)
        return {'target_ref': target_ref, 'profile_ref': profile_ref,
            'signals': signal_rows(state, target), 'actions': list(ENTRY_SIGNALS),
            'feedback_action': '/cabinet/feedback'}


def _fields(request, *, card):
    base = {'profile_ref', 'mutation_id', 'base_revision', 'operation', 'signal',
            'target_ref' if card else 'entry_ref'}
    operation, signal = request.POST.get('operation'), request.POST.get('signal')
    ga._require(operation in ('set', 'reverse') and signal in ENTRY_SIGNALS, 'CONTRACT_INVALID', 422)
    if operation == 'reverse':
        return _form(request, base | {'reverses_id'})
    fields = base | ({'text'} if signal == 'note' else {'value'})
    if signal == 'enjoyment':
        fields.add('experienced_ack')
    if not card:
        fields |= {'entry_kind', 'category'}
        if request.POST.get('entry_kind') == 'item':
            fields.add('title')
    return _form(request, fields)


def _value(fields):
    value = fields.get('value')
    if fields['signal'] == 'enjoyment' and fields['operation'] == 'set':
        ga._require(value in ('1', '2', '3', '4', '5') and fields.get('experienced_ack') == 'yes',
                    'CONTRACT_INVALID', 422)
        value = int(value)
    return value


def _redirect(profile_ref, category=None):
    values = {'profile_ref': profile_ref}
    if category is not None:
        values['category'] = category
    return HttpResponse(status=303, headers={'Location': '/cabinet/taste?' + urlencode(values),
                                            'Cache-Control': 'no-store'})


def _conflict(request, profile_ref, code):
    # Reconstruct only current bindings after a failed command. Never retain
    # submitted text, before-images or a command for automatic resubmission.
    context = feedback_context(request.portable_authority, request.session.get('portable_session'), profile_ref)
    return _native_cabinet_html(request, {**context, 'feedback_code': code}, status=409)


@require_GET
@_portable_errors
@sensitive_variables()
def taste(request):
    ga._require(set(request.GET) <= {'profile_ref', 'category'} and 'profile_ref' in request.GET
        and all(len(request.GET.getlist(k)) == 1 for k in request.GET), 'CONTRACT_INVALID', 422)
    context = feedback_context(request.portable_authority, request.session.get('portable_session'),
        request.GET['profile_ref'], category=request.GET.get('category'))
    return _native_cabinet_html(request, context)


@require_POST
@csrf_protect
@_portable_errors
@sensitive_variables()
def taste_mutate(request):
    fields = _fields(request, card=False)
    profile_ref = str(ga._uuid(fields['profile_ref']))
    try:
        PortableTasteInputs(request.portable_authority).mutate_entry(
            request.session.get('portable_session'), profile_ref,
            entry_ref=fields['entry_ref'], mutation_id=fields['mutation_id'],
            base_revision=_revision(fields['base_revision']), operation=fields['operation'],
            signal=fields['signal'], entry_kind=fields.get('entry_kind'), category=fields.get('category'),
            title=fields.get('title'), value=_value(fields), text=fields.get('text'),
            experienced_ack=fields.get('experienced_ack') == 'yes', reverses_id=fields.get('reverses_id'))
    except AuthorityFault as error:
        if error.status != 409:
            raise
        return _conflict(request, profile_ref, error.code)
    return _redirect(profile_ref, fields.get('category'))


@require_POST
@csrf_protect
@_portable_errors
@sensitive_variables()
def card_mutate(request):
    request.session.pop(_PENDING_OUTCOME, None)
    fields = _fields(request, card=True)
    profile_ref = str(ga._uuid(fields['profile_ref']))
    operation, signal = fields['operation'], fields['signal']
    body = (typed_body(signal, value=_value(fields), text=fields.get('text'),
        experienced_ack=fields.get('experienced_ack') == 'yes') if operation == 'set' else None)
    authority = request.portable_authority
    service = getattr(authority, '_enhance_service', None)
    ga._require(service is not None, 'CAPABILITY_UNAVAILABLE', 503)
    try:
        result = PortableReactions(authority, service=service).mutate(request.session.get('portable_session'),
            profile_ref, fields['target_ref'], fields['mutation_id'], _revision(fields['base_revision']),
            operation, {'signal': signal, 'value': body}, fields.get('reverses_id'))
    except AuthorityFault as error:
        if error.status != 409:
            raise
        return _conflict(request, profile_ref, error.code)
    _remember_feedback_outcome(request, profile_ref=profile_ref, mutation_id=fields['mutation_id'],
        operation=operation, result=result)
    return HttpResponse(status=303, headers={'Location': _cabinet_url(profile_ref),
                                            'Cache-Control': 'no-store'})


def intake(request):
    """One fixed URL accepts only the separately decorated native GET/POST ports."""
    return taste_mutate(request) if request.method == 'POST' else taste(request)
