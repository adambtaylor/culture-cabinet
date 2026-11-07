"""Bounded, actor-owned message drafts with write-ahead dispatch and exact consent.

The only transport is a fictional fixture. A sending checkpoint is uncertain
after interruption: only evidence reconciliation is allowed, never a second send.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import secrets

from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone

from shelf.acquisition_contract import Blocked
from shelf.calendar_handoff import digest, instant
from shelf.discovery_state import Conflict, public_authorized
from shelf.event_plan_provider import Quote, RehearsalProvider, session
from shelf.message_provider import ACCESS_STATES, SCENARIOS, access_ready, chat, provider, selected_message
from shelf.models import MessageDraft

MAX_BODY = 2800
MAX_ACTIONS = 200
APPROVAL_LIFETIME = timedelta(minutes=15)


def available():
    return MessageDraft._meta.db_table in connection.introspection.table_names()


def owned(request, profile_id, draft_id):
    public_authorized(request, profile_id)
    draft = MessageDraft.objects.filter(pk=draft_id, actor=request.user, profile_id=profile_id).first()
    if draft is None or draft.mode != 'sample':
        raise Http404
    return draft


def preview_digest(draft):
    return digest([str(draft.pk), draft.actor_id, draft.profile_id, draft.mode,
                   draft.state, draft.version, draft.nonce, draft.data])


def content_digest(data):
    # Operational state is separate; consent covers the exact identity, purpose,
    # selected context, event source/price and outbound text that could be sent.
    return digest({key: data[key] for key in ('kind', 'chat', 'context', 'event', 'quote', 'body')})


def body_text(value):
    if type(value) is not str:
        raise Blocked('Message must contain 1–2800 characters of plain text.')
    # HTML textarea submissions use CRLF; store and approve one canonical text.
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    if (not value.strip() or len(value) > MAX_BODY
            or any(ord(c) < 32 and c != '\n' for c in value)):
        raise Blocked('Message must contain 1–2800 characters of plain text.')
    return value


def invitation(quote):
    event = quote.offer.event
    return (f'Would you like to join me for {event.title}?\n'
            f'Exact session: {event.session_id}\n'
            f'Starts: {event.start}\nEnds: {event.end}\nTimezone: {event.timezone}\n'
            f'Place: {event.location}\n'
            f'Price for one: {quote.offer.currency} {quote.offer.total} including '
            f'{quote.fees} fees. Quote expires: {quote.expires_at}.\n'
            f'Source: {event.official_source}\n'
            'This is a fictional rehearsal invitation. No tickets have been reserved. '
            'This chat message does not add a calendar event. Let me know if you are interested.')


def _event_data(session_id):
    event = session(session_id)
    quote = RehearsalProvider().quote(event, 1, timezone.now())
    return {'event': asdict(event), 'quote': quote.to_dict(), 'body': invitation(quote)}


def create(request, profile_id, *, kind, chat_id, session_id, message_id, scenario, access):
    public_authorized(request, profile_id)
    recipient = chat(chat_id)
    if scenario not in SCENARIOS or access not in ACCESS_STATES:
        raise Blocked('Choose a displayed rehearsal scenario.')
    data = {'kind': kind, 'chat': recipient, 'scenario': scenario, 'access': access,
            'context': None, 'event': None, 'quote': None, 'approval': None,
            'dispatch': None, 'evidence': None, 'timeline': []}
    if kind == 'invitation' and session_id and not message_id:
        data.update(_event_data(session_id))
        subject = session_id
    elif kind == 'reply' and message_id and not session_id:
        data['context'] = selected_message(chat_id, message_id)
        # Incoming text is data. It never chooses a recipient, action or authority.
        data['body'] = 'Thanks for your message. I will check the details and get back to you.'
        subject = message_id
    else:
        raise Blocked('Choose an exact session for an invitation or one selected message for a reply.')
    active_key = digest(['message-v1', request.user.pk, profile_id, 'sample', kind, chat_id, subject])
    with transaction.atomic():
        public_authorized(request, profile_id)
        existing = MessageDraft.objects.filter(active_key=active_key).first()
        if existing:
            if (existing.data['scenario'], existing.data['access']) != (scenario, access):
                raise Conflict('This exact intent already exists. Open its retained draft and evidence.')
            return existing
        return MessageDraft.objects.create(actor=request.user, profile_id=profile_id,
            active_key=active_key, nonce=secrets.token_urlsafe(24), data=data)


def validate(draft):
    try:
        _validate_data(draft)
    except (KeyError, TypeError, AttributeError):
        raise Blocked('Saved message data is incomplete. No dispatch is allowed.') from None


def _validate_data(draft):
    data = draft.data
    if draft.mode != 'sample':
        raise Blocked('Live messaging is unavailable.')
    if data['chat'] != chat(data['chat']['id']):
        raise Blocked('Observed chat identity changed. No dispatch is allowed.')
    if body_text(data['body']) != data['body']:
        raise Blocked('Saved message line endings changed. Review the exact text again.')
    if data['kind'] == 'invitation':
        event = session(data['event']['session_id'])
        quote = Quote.from_dict(data['quote'])
        if (data['event'] != asdict(event) or quote.offer.event != event
                or not event.source_verified or quote.offer.quantity != 1
                or quote != RehearsalProvider().quote(event, 1, instant(quote.quoted_at))
                or data['body'] != invitation(quote) or data['context'] is not None):
            raise Blocked('The exact verified session or invitation changed.')
    elif data['kind'] == 'reply':
        context = data['context']
        if (context != selected_message(data['chat']['id'], context['id'])
                or data['event'] is not None or data['quote'] is not None):
            raise Blocked('Selected context changed. Review the original chat.')
    else:
        raise Blocked('Unknown message purpose.')
    if data['dispatch']:
        dispatch, approval = data['dispatch'], data['approval']
        if (dispatch['key'] != 'message-' + str(draft.pk) or dispatch['attempts'] != 1
                or dispatch['content_digest'] != content_digest(data)
                or not approval or approval['actor_id'] != draft.actor_id
                or approval['content_digest'] != dispatch['content_digest']):
            raise Blocked('Saved content no longer matches the approved dispatch. Preserve its uncertain evidence.')


def _ready(draft):
    validate(draft)
    access_ready(draft.data['access'])
    if draft.data['kind'] == 'invitation' and not Quote.from_dict(draft.data['quote']).current(timezone.now()):
        raise Blocked('Session price quote expired. Refresh the invitation and approve it again.')


def _save(draft, data, state, receipts=None):
    # Include the complete previous state as well as the version/nonce, so an
    # out-of-band content change cannot inherit a previously issued approval.
    changed = MessageDraft.objects.filter(pk=draft.pk, version=draft.version, nonce=draft.nonce,
        state=draft.state, data=draft.data, receipts=draft.receipts).update(
            data=data, state=state, receipts=draft.receipts if receipts is None else receipts,
            version=draft.version + 1, nonce=secrets.token_urlsafe(24), updated_at=timezone.now())
    if changed != 1:
        raise Conflict('Draft changed concurrently. Reload its retained state.')
    draft.refresh_from_db()
    return draft


def _record(data, event, *, reconcile=False):
    if not reconcile and len(data['timeline']) >= MAX_ACTIONS:
        raise Blocked('New message actions reached their limit; dispatch evidence is retained.')
    data['timeline'].append({'event': event, 'at': timezone.now().isoformat()})
    if reconcile:
        data['timeline'] = data['timeline'][-MAX_ACTIONS:]


def apply(request, profile_id, draft_id, fields):
    if fields.get('confirmed') is not True:
        raise Blocked('Explicit confirmation is required.')
    external = None
    with transaction.atomic():
        draft = owned(request, profile_id, draft_id)
        fingerprint, nonce = digest(fields), fields['nonce']
        if nonce in draft.receipts:
            if draft.receipts[nonce] != fingerprint:
                raise Conflict('This form was already used for a different action.')
            return draft
        if (fields['version'] != draft.version or nonce != draft.nonce
                or fields['preview'] != preview_digest(draft)):
            raise Conflict('The recipient or preview changed. Reload before approving.')
        action = fields['action']
        if action != 'reconcile' and len(draft.receipts) >= MAX_ACTIONS:
            raise Blocked('New message actions reached their limit; dispatch evidence is retained.')
        validate(draft)
        if action == 'dispatch' and len(draft.data['timeline']) > MAX_ACTIONS - 2:
            raise Blocked('Not enough receipt capacity for a new dispatch; no dispatch occurred.')
        if ((fields.get('body') and action != 'edit')
                or ((fields.get('sensitive_confirmed') or fields.get('recipient_confirmation'))
                    and action != 'approve')):
            raise Blocked('Unexpected action fields.')
        data, state = deepcopy(draft.data), draft.state
        if action in ('edit', 'refresh'):
            if state not in ('draft', 'queued') or data['dispatch']:
                raise Blocked('A dispatched message is immutable. Reconcile its evidence.')
            if action == 'edit' and data['kind'] == 'reply':
                data['body'] = body_text(fields['body'])
            elif action == 'refresh' and data['kind'] == 'invitation':
                data.update(_event_data(data['event']['session_id']))
            else:
                raise Blocked('This edit does not apply to the selected message purpose.')
            state, data['approval'] = 'draft', None
        elif action == 'approve':
            if state not in ('draft', 'queued') or data['dispatch']:
                raise Blocked('This intent already has dispatch evidence. It cannot be approved again.')
            _ready(draft)
            sensitive = data['kind'] == 'reply'
            if sensitive and (not fields['sensitive_confirmed']
                    or fields['recipient_confirmation'] != data['chat']['identity']):
                raise Blocked('Replies need the separate sensitive-content check and exact typed recipient identity.')
            data['approval'] = {'actor_id': request.user.pk, 'at': timezone.now().replace(microsecond=0).isoformat(),
                'content_digest': content_digest(data), 'sensitive_confirmed': sensitive,
                'recipient_confirmation': data['chat']['identity'] if sensitive else '',
                'origin': 'explicit-authenticated-preview-approval'}
            state = 'queued'
        elif action == 'dispatch':
            if state != 'queued' or data['dispatch']:
                raise Blocked('Only an approved queued message may dispatch once. Reconcile uncertain evidence.')
            _ready(draft)
            approval = data['approval']
            if (not approval or approval['actor_id'] != request.user.pk
                    or approval['content_digest'] != content_digest(data)
                    or not instant(approval['at']) <= timezone.now() < instant(approval['at']) + APPROVAL_LIFETIME):
                raise Blocked('Approval is stale or changed. Review and approve the exact message again.')
            data['dispatch'] = {'key': 'message-' + str(draft.pk),
                'content_digest': content_digest(data), 'attempts': 1, 'at': timezone.now().isoformat()}
            state, external = 'sending', 'send'
        elif action == 'reconcile':
            if state not in ('sending', 'unknown') or not data['dispatch']:
                raise Blocked('Only an uncertain dispatch needs evidence reconciliation.')
            access_ready(data['access'])
            external = 'reconcile'
        else:
            raise Blocked('Unknown message action.')
        _record(data, action, reconcile=action == 'reconcile')
        receipts = {**draft.receipts, nonce: fingerprint}
        if action == 'reconcile':
            # Old forms remain stale by version/nonce after their replay receipt
            # is pruned. Original approval, dispatch and evidence live separately.
            receipts = dict(list(receipts.items())[-MAX_ACTIONS:])
        public_authorized(request, profile_id)
        _save(draft, data, state, receipts=receipts)
    if external:
        return _resolve(request, profile_id, draft, external)
    return draft


def _resolve(request, profile_id, reserved, operation):
    # No database transaction spans adapter I/O. Check authority and reservation
    # again immediately before I/O, and refuse late results after any change.
    current = owned(request, profile_id, reserved.pk)
    if preview_digest(current) != preview_digest(reserved):
        raise Conflict('Dispatch checkpoint changed. Reconcile the retained receipt.')
    validate(current)
    adapter = provider(current.mode)
    try:
        evidence = adapter.send(current) if operation == 'send' else adapter.reconcile(current)
    except OSError:
        # Exception details may contain private provider data; retain no raw error.
        evidence = None
    with transaction.atomic():
        draft = owned(request, profile_id, current.pk)
        if preview_digest(draft) != preview_digest(current):
            raise Conflict('Draft changed while checking evidence. Reconcile its retained receipt.')
        data = deepcopy(draft.data)
        if evidence is None:
            state = 'unknown'
        else:
            dispatch = data['dispatch']
            if (type(evidence) is not dict or set(evidence) != {'fixture', 'state', 'dispatch_key',
                    'content_digest', 'chat_id', 'reference', 'delivered', 'read'}
                    or evidence.get('fixture') is not True or evidence.get('state') not in ('sent', 'failed', 'unknown')
                    or evidence.get('chat_id') != data['chat']['id']
                    or evidence.get('dispatch_key') != dispatch['key']
                    or evidence.get('content_digest') != dispatch['content_digest']
                    or evidence.get('reference') != 'fixture-message-' + digest(str(draft.pk))[:24]
                    or evidence.get('delivered') != 'unknown' or evidence.get('read') != 'unknown'):
                raise Blocked('Evidence does not match this exact dispatch. Outcome remains uncertain.')
            state = evidence['state']
            data['evidence'] = evidence
        _record(data, state, reconcile=operation == 'reconcile')
        return _save(draft, data, state)
