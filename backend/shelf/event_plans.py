"""Durable orchestration of the existing ticket and calendar contracts.

Only the explicit rehearsal provider is enabled. Every outward command has a
write-ahead receipt; a crashed or uncertain command is reconciled, never resent.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import secrets
import uuid

from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone

from shelf.acquisition_contract import Blocked
from shelf.calendar_handoff import CalendarHandoff, digest, instant
from shelf.discovery_state import Conflict, public_authorized
from shelf.event_plan_provider import TARGET, SCENARIOS, Quote, provider, resolve_contacts, session
from shelf.models import EventPlan
from shelf.ticket_handoff import ManualOfficialTicketAdapter, TicketHandoff


def available():
    return EventPlan._meta.db_table in connection.introspection.table_names()


def owned(request, profile_id, plan_id):
    public_authorized(request, profile_id)
    plan = EventPlan.objects.filter(pk=plan_id, actor=request.user, profile_id=profile_id).first()
    if plan is None:
        raise Http404
    return plan


def contracts(plan):
    return (TicketHandoff.from_dict(plan.data['ticket']),
            CalendarHandoff.from_dict(plan.data['calendar']), Quote.from_dict(plan.data['quote']))


def session_event(plan):
    from shelf.outing_plans import anchor
    return anchor(plan)


def preview_digest(plan):
    # Consent binds the fees, terms, expiry, resolved mailboxes and current state,
    # in addition to the unchanged exact-offer contract used by existing callers.
    return digest([str(plan.pk), plan.actor_id, plan.profile_id, plan.mode, plan.version, plan.data])


def plan_key(actor_id, event):
    return digest(['event-plan-v1', actor_id, 'sample', event.event_id, event.session_id])


def create(request, profile_id, *, session_id, quantity, contacts, purchase_scenario, calendar_scenario):
    public_authorized(request, profile_id)
    event = session(session_id)
    recipients = resolve_contacts(contacts)
    if quantity < 1 + len(recipients):
        raise Blocked('Include one ticket for yourself and each selected friend.')
    if purchase_scenario not in SCENARIOS or calendar_scenario not in SCENARIOS:
        raise Blocked('Unknown rehearsal scenario.')
    active_key = plan_key(request.user.pk, event)
    selection = {'session_id': session_id, 'quantity': quantity, 'contacts': sorted(contacts),
                 'purchase_scenario': purchase_scenario, 'calendar_scenario': calendar_scenario}
    with transaction.atomic():
        public_authorized(request, profile_id)
        existing = EventPlan.objects.filter(active_key=active_key).first()
        if existing:
            if existing.profile_id != profile_id or existing.data['selection'] != selection:
                raise Conflict('This session already has a plan. Open it to preserve its receipts.')
            return existing
        now = timezone.now().replace(microsecond=0)
        quote = provider('sample').quote(event, quantity, now)
        identity = str(uuid.uuid4())
        ticket = TicketHandoff(str(request.user.pk), identity, quote.offer)
        calendar = CalendarHandoff(str(request.user.pk), identity, event, now.isoformat())
        return EventPlan.objects.create(id=identity, actor=request.user, profile_id=profile_id,
            mode='sample', active_key=active_key, nonce=secrets.token_urlsafe(24), data={
                'selection': selection, 'recipients': [asdict(r) for r in recipients],
                'quote': quote.to_dict(), 'ticket': ticket.to_dict(), 'calendar': calendar.to_dict(),
                'purchase_checks': 0, 'operations': {}, 'booking_status': 'scheduled'})


def _save(plan, data, *, receipts=None, active_key=None):
    updated = EventPlan.objects.filter(pk=plan.pk, version=plan.version, nonce=plan.nonce).update(
        data=data, receipts=plan.receipts if receipts is None else receipts,
        active_key=plan.active_key if active_key is None else active_key,
        version=plan.version + 1, nonce=secrets.token_urlsafe(24), updated_at=timezone.now())
    if updated != 1:
        raise Conflict('Concurrent change. Reload the plan.')
    plan.refresh_from_db()
    return plan


def _calendar_command(plan, calendar, recipient_id):
    if recipient_id:
        recipients = resolve_contacts(plan.data['selection']['contacts'])
        recipient = next((r for r in recipients if r.recipient_id == recipient_id), None)
        if recipient is None:
            raise Blocked('Recipient was not explicitly selected for this plan.')
        if calendar.event.status == 'cancelled' and not _previous_invite(calendar, recipient):
            raise Blocked('No confirmed prior invitation to cancel for this recipient.')
        if not any(r.operation == 'add' and r.revision == calendar.revision and r.state == 'confirmed'
                   for r in calendar.receipts):
            raise Blocked('Confirm the owner calendar update before inviting friends.')
        return 'invite', recipient
    return 'add', None


def _previous_invite(calendar, recipient):
    return any(r.operation == 'invite' and r.recipient == recipient and r.state == 'confirmed'
               and r.revision < calendar.revision for r in calendar.receipts)


def apply(request, profile_id, plan_id, fields):
    """Commit the exact POST and reservation before invoking a rehearsal adapter."""
    from shelf import outing_plans
    external = None
    with transaction.atomic():
        plan = owned(request, profile_id, plan_id)
        if plan.mode != 'sample':
            raise Blocked('Live booking and calendar adapters are not connected.')
        fingerprint = digest(fields)
        nonce = fields['nonce']
        if nonce in plan.receipts:
            if plan.receipts[nonce] != fingerprint:
                raise Conflict('This form was already used for a different action.')
            return plan
        if (fields['version'] != plan.version or nonce != plan.nonce
                or fields['preview'] != preview_digest(plan)):
            raise Conflict('The preview changed. Reload before approving.')
        action = fields['action']
        reconciling = action in ('reconcile_purchase', 'reconcile_calendar', 'reconcile_email')
        if len(plan.receipts) >= 200 and not reconciling:
            raise Blocked('Plan action limit reached; existing outcome checks remain available.')
        ticket, calendar, quote = contracts(plan)
        data = deepcopy(plan.data)
        actor = str(request.user.pk)
        recipient_id, receipt_key = fields.get('recipient', ''), fields.get('receipt', '')
        if ((recipient_id and action not in ('calendar','packet_email'))
                or (receipt_key and action not in ('reconcile_calendar','reconcile_email'))
                or (fields.get('packet_digest') and action != 'packet_email')):
            raise Blocked('Unexpected action fields.')
        now = timezone.now().replace(microsecond=0)
        if action == 'refresh_quote':
            if ticket.purchase_state not in ('requested', 'approved'):
                raise Blocked('Preserve the submitted purchase; reconcile its outcome.')
            quote = provider(plan.mode).quote(ticket.offer.event, ticket.offer.quantity, now)
            ticket = replace(ticket, offer=quote.offer, revision=ticket.revision + 1,
                             purchase_state='requested', approval_digest=None)
            data['quote'] = quote.to_dict()
        elif action in ('approve_purchase', 'handoff'):
            if not quote.current(now):
                raise Blocked('Quote expired. Refresh and review the full price and terms again.')
            if action == 'approve_purchase':
                ticket = ticket.approve(actor_id=actor, expected_revision=ticket.revision,
                    preview_digest=ticket.preview_digest, user_confirmed=True)
            else:
                ticket, instructions = ticket.reserve_submission(actor_id=actor, current_offer=quote.offer,
                    expected_revision=ticket.revision, preview_digest=ticket.preview_digest,
                    human_final_click_authorized=True, adapter=ManualOfficialTicketAdapter())
                # In rehearsal the URL is deliberately non-functional. This is
                # a durable handoff reservation, never proof of a seller order.
                if instructions:
                    data['handoff_reserved_at'] = now.isoformat()
        elif action == 'reconcile_purchase':
            if ticket.purchase_state != 'submitted':
                raise Blocked('Only an unknown purchase needs reconciliation.')
            external = ('purchase', '')
        elif action == 'calendar':
            if data.get('outing'):outing_plans.require_current(plan)
            if ticket.purchase_state != 'confirmed':
                raise Blocked('Confirmed booking evidence required before calendar actions.')
            operation, recipient = _calendar_command(plan, calendar, recipient_id)
            preview = calendar.preview(operation, target=TARGET, recipient=recipient)
            key = 'calendar-' + preview['digest'][:32]
            calendar, receipt = calendar.approve(actor_id=actor, expected_revision=calendar.revision,
                preview_digest=preview['digest'], key=key, operation=operation,
                target=TARGET, recipient=recipient, user_confirmed=True)
            calendar, receipt, fresh = calendar.reserve(receipt.key, actor_id=actor, adapter=provider(plan.mode))
            if fresh:
                data['operations'][receipt.key] = {'send_attempts': 1, 'state': 'unknown',
                    'event_uid': calendar.uid, 'revision': calendar.revision}
                external = ('calendar', receipt.key)
        elif action == 'reconcile_calendar':
            receipt = calendar._receipt(receipt_key, actor)
            if receipt.state != 'submitted_unknown':
                raise Blocked('Only an unknown calendar outcome needs reconciliation.')
            external = ('reconcile_calendar', receipt_key)
        elif action in ('reschedule', 'cancel_event'):
            outing_plans.require_settled(plan)
            if ticket.purchase_state != 'confirmed':
                raise Blocked('A confirmed booking is required.')
            if any(r.state == 'submitted_unknown' for r in calendar.receipts):
                raise Blocked('Reconcile unknown calendar outcomes before updating the event.')
            original = session_event(plan)
            if original.status == 'cancelled':
                raise Blocked('Event already cancelled; purchase receipts are retained.')
            if action == 'reschedule':
                event = replace(original,
                    start=(instant(original.start) + timedelta(hours=1)).isoformat(),
                    end=(instant(original.end) + timedelta(hours=1)).isoformat())
            else:
                event = replace(original, status='cancelled')
            if data.get('outing'):
                data['outing']['anchor'] = asdict(event)
                shadow = deepcopy(plan); shadow.data = data
                packet, calendar = outing_plans.build(shadow, data['outing']['choices'], now)
                data['outing'] = packet
            else:
                calendar = calendar.revise(event, actor_id=actor, expected_revision=calendar.revision,
                                           modified_at=now.isoformat())
            data['booking_status'] = event.status
        elif action == 'packet_email':
            if ticket.purchase_state != 'confirmed':
                raise Blocked('Confirmed booking evidence required before packet delivery.')
            payload = outing_plans.email_preview(plan, recipient_id or outing_plans.OWNER)
            if fields.get('packet_digest') != payload['digest']:
                raise Conflict('The addressed packet changed; preview it again.')
            if not any(r.operation=='add' and r.revision==calendar.revision and r.state=='confirmed' for r in calendar.receipts):
                raise Blocked('Confirm the current master calendar block before its email summary.')
            key = 'packet-email-' + digest([str(plan.pk), payload['digest']])[:32]
            if key not in data['operations']:
                if any(r.get('kind')=='packet_email' and r['state']=='unknown' and
                       r['payload']['recipient_id']==payload['recipient_id'] for r in data['operations'].values()):
                    raise Blocked('Reconcile the unknown addressed email before another delivery.')
                data['operations'][key] = {'kind':'packet_email','key':key,'state':'unknown',
                    'send_attempts':1,'payload':payload,'approved_plan_version':plan.version}
                external = ('packet_email', key)
        elif action == 'reconcile_email':
            receipt = data['operations'].get(receipt_key)
            if not receipt or receipt.get('kind')!='packet_email' or receipt['state']!='unknown':
                raise Blocked('Only an existing unknown email can be reconciled.')
            external = ('reconcile_email', receipt_key)
        else:
            raise Blocked('Unknown event-plan action.')
        data.update(ticket=ticket.to_dict(), calendar=calendar.to_dict())
        public_authorized(request, profile_id)
        receipts = {**plan.receipts, nonce: fingerprint}
        # Keep evidence checks possible after repeated unknown outcomes. Only
        # replay metadata rotates: order/dispatch/approval evidence stays intact.
        # Pruned forms still fail the current version and nonce checks above.
        if reconciling:
            receipts = dict(list(receipts.items())[-200:])
        _save(plan, data, receipts=receipts)
    if external:
        if external[0] in ('packet_email','reconcile_email'):
            return _resolve_email(request, profile_id, plan, *external)
        return _resolve(request, profile_id, plan, *external)
    return plan


def _resolve_email(request, profile_id, plan, kind, key):
    public_authorized(request, profile_id)
    receipt = plan.data['operations'][key]
    evidence = provider(plan.mode).packet_email_evidence(receipt,
        plan.data['selection']['calendar_scenario'], reconcile=kind=='reconcile_email')
    with transaction.atomic():
        current = owned(request, profile_id, plan.pk)
        if current.version != plan.version:
            raise Conflict('Plan changed during email delivery; reconcile its saved receipt.')
        if (evidence.get('fixture') is not True or evidence.get('key')!=key
                or evidence.get('payload_digest')!=receipt['payload']['digest']
                or evidence.get('state') not in ('confirmed','failed','unknown')
                or evidence.get('reference')!='fixture-email-'+digest(key)[:24]):
            raise Blocked('Email evidence does not match this exact packet; outcome remains unknown.')
        data=deepcopy(current.data)
        data['operations'][key].update(state=evidence['state'],evidence=evidence)
        return _save(current,data)


def _resolve(request, profile_id, plan, kind, key):
    """No transaction is held over provider I/O. Failure leaves durable unknown.

    A reservation already claims the single dispatch attempt. Reconciliation
    reads provider evidence and cannot call the send path again.
    """
    public_authorized(request, profile_id)
    ticket, calendar, _ = contracts(plan)
    adapter = provider(plan.mode)
    if kind == 'purchase':
        evidence = adapter.purchase_evidence(ticket, plan.data['selection']['purchase_scenario'],
                                             plan.data['purchase_checks'])
    else:
        receipt = calendar._receipt(key, str(request.user.pk))
        evidence = adapter.calendar_evidence(calendar, receipt, plan.data['selection']['calendar_scenario'],
                                             reconcile=kind == 'reconcile_calendar')
    with transaction.atomic():
        current = owned(request, profile_id, plan.pk)
        if current.version != plan.version:
            raise Conflict('Plan changed while checking evidence. Reconcile the retained receipt.')
        data = deepcopy(current.data)
        actor = str(request.user.pk)
        if evidence.get('fixture') is not True or evidence.get('status') not in ('confirmed', 'failed', 'unknown'):
            raise Blocked('Unrecognized provider evidence; outcome remains unknown.')
        if kind == 'purchase':
            if evidence.get('offer_digest') != ticket.approval_digest:
                raise Blocked('Seller evidence does not match the approved purchase.')
            if evidence['status'] == 'confirmed':
                ticket = ticket.confirm_purchase(actor_id=actor, offer_digest=evidence['offer_digest'],
                    order_reference=evidence['reference'], issuer_verified=True)
                ticket = ticket.record_ticket_issued(actor_id=actor, order_reference=evidence['reference'],
                    ticket_reference='fixture-ticket-' + ticket.request_id, issuer_verified=True)
            elif evidence['status'] == 'failed':
                ticket = ticket.confirm_failure(actor_id=actor, offer_digest=evidence['offer_digest'],
                    failure_reference=evidence['reference'], issuer_verified=True)
            data['purchase_checks'] += 1
            data['purchase_evidence'] = evidence
        else:
            if evidence['status'] == 'confirmed':
                calendar = calendar.confirm(key, actor_id=actor, externally_verified=True)
            elif evidence['status'] == 'failed':
                calendar = calendar.fail(key, actor_id=actor, externally_verified=True)
            data['operations'][key].update(state=evidence['status'], evidence=evidence)
        data.update(ticket=ticket.to_dict(), calendar=calendar.to_dict())
        return _save(current, data)


def context(plan):
    from shelf.restaurant_plans import context as restaurant_context
    from shelf.outing_plans import context as outing_context
    ticket, calendar, quote = contracts(plan)
    recipients = resolve_contacts(plan.data['selection']['contacts'])
    commands = []
    for recipient in (None, *recipients):
        operation = 'invite' if recipient else 'add'
        preview = calendar.preview(operation, target=TARGET, recipient=recipient)
        if recipient and calendar.event.status == 'cancelled' and not _previous_invite(calendar, recipient):
            preview['blockers'].append('No confirmed prior invitation to cancel for this recipient.')
        receipt = next((r for r in calendar.receipts if r.preview_digest == preview['digest']), None)
        commands.append({'recipient': recipient, 'preview': preview, 'receipt': receipt,
                         'state': receipt.state if receipt else 'pending approval'})
    return {**restaurant_context(plan), **outing_context(plan), 'plan': plan, 'ticket': ticket, 'quote': quote, 'event': session_event(plan),
            'calendar': calendar, 'commands': commands, 'target': TARGET,
            'recipients': recipients, 'preview_digest': preview_digest(plan),
            'quote_current': quote.current(timezone.now()),
            'unknown_receipts': [r for r in calendar.receipts if r.state == 'submitted_unknown'],
            'owner_current': any(r.operation == 'add' and r.revision == calendar.revision
                                 and r.state == 'confirmed' for r in calendar.receipts)}
