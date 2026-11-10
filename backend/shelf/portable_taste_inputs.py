"""Direct owner-stated artists through the existing feedback event engine."""
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from . import grant_authority as ga
from .feedback import MutationCommand, mutate as record_mutation, reduce_events, validate_body, Conflict, FeedbackError
from .models import FeedbackEvent, FeedbackTarget, MutationReceipt
from .portable_authority import AuthorityFault

NAMESPACE = 'owner-stated-artist'
SIGNALS = ('interest', 'enjoyment')
ITEM_NAMESPACE = 'owner-stated-item'
NOTE_NAMESPACE = 'owner-stated-category-note'
FAVOURITE_NAMESPACE = 'owner-stated-favourite'
NATIVE_NAMESPACES = (NAMESPACE, ITEM_NAMESPACE, NOTE_NAMESPACE, FAVOURITE_NAMESPACE)
ENTRY_SIGNALS = ('interest', 'enjoyment', 'note')
MAX_ENTRIES = 100


def typed_body(signal, *, value=None, text=None, experienced_ack=False):
    """One native meaning for intake and card controls; never trim authored text."""
    ga._require(signal in ENTRY_SIGNALS, 'CONTRACT_INVALID', 422)
    if signal == 'note':
        ga._require(value is None and experienced_ack is False, 'CONTRACT_INVALID', 422)
        body = {'text': text}
    else:
        ga._require(text is None and (signal != 'enjoyment' or experienced_ack is True),
                    'CONTRACT_INVALID', 422)
        body = ({'value': value, 'basis': 'anticipated'} if signal == 'interest' else
                {'value': value, 'basis': 'experienced', 'scale': 'integer-1-5'})
    try:
        validate_body(signal, body)
    except FeedbackError:
        raise AuthorityFault('CONTRACT_INVALID', 422) from None
    return body


def signal_rows(state, target, signals=ENTRY_SIGNALS):
    """Expose effective statements for Undo and the latest per-signal revision."""
    rows = []
    for signal in signals:
        events = list(FeedbackEvent.objects.filter(profile_id=state.profile_id,
            actor_id=state.actor_id, target=target, signal=signal, scope='item').order_by('revision'))
        effective, body, erased = reduce_events(events) if events else (None, None, False)
        rows.append({'signal': signal, 'revision': events[-1].revision if events else 0,
            'value': body.get('value') if body is not None and signal != 'note' else None,
            'text': body['text'] if body is not None and signal == 'note' else '',
            'event_ref': str(effective) if effective else '',
            'can_undo': effective is not None and not erased,
            'cleared': effective is not None and body is None and not erased,
            'mutation_id': str(uuid.uuid4()), 'undo_mutation_id': str(uuid.uuid4())})
    return rows


class PortableTasteInputs:
    def __init__(self, authority):
        ga._require(type(authority) is ga.PortableAuthority)
        self.authority = authority

    @sensitive_variables()
    def read(self, session_ref, profile_ref):
        with ga.recovery_guard(self.authority, session_ref, profile_ref=profile_ref), self.authority._lock:
            state, _ = ga._bound(self.authority, session_ref, profile_ref)
            targets = FeedbackTarget.objects.filter(source_namespace=NAMESPACE, kind='item',
                category='artist', deleted=False, source_key__startswith=str(state.profile_ref) + ':',
                feedbackevent__profile_id=state.profile_id, feedbackevent__actor_id=state.actor_id).distinct().order_by('pk')[:100]
            entries = []
            for target in targets:
                signals = []
                for signal in SIGNALS:
                    events = list(FeedbackEvent.objects.filter(profile_id=state.profile_id,
                        actor_id=state.actor_id, target=target, signal=signal, scope='item').order_by('revision'))
                    if not events:
                        continue
                    effective, body, erased = reduce_events(events)
                    signals.append({'signal': signal, 'revision': events[-1].revision,
                        'value': body['value'] if body is not None else None,
                        'event_ref': str(effective) if effective else '',
                        'can_undo': body is not None and not erased,
                        'mutation_id': str(uuid.uuid4()), 'undo_mutation_id': str(uuid.uuid4())})
                entries.append({'entry_ref': target.source_key.split(':', 1)[1],
                                'title': target.title, 'signals': signals})
            return entries

    @sensitive_variables()
    def mutate(self, session_ref, profile_ref, *, entry_ref, mutation_id, base_revision,
               operation, signal, title=None, value=None, experienced_ack=False, reverses_id=None):
        profile_ref, entry_ref = str(ga._uuid(profile_ref)), str(ga._uuid(entry_ref))
        mutation_id = str(ga._uuid(mutation_id))
        ga._revision(base_revision)
        ga._require(signal in SIGNALS and operation in ('set', 'reverse'), 'CONTRACT_INVALID', 422)
        ga._require((operation == 'reverse') == (reverses_id is not None), 'CONTRACT_INVALID', 422)
        body = None
        if operation == 'set':
            ga._require(type(title) is str and 0 < len(title) <= 200 and bool(title.strip())
                and not any(ord(c) < 32 or ord(c) == 127 for c in title), 'CONTRACT_INVALID', 422)
            ga._require(signal != 'enjoyment' or experienced_ack is True, 'CONTRACT_INVALID', 422)
            body = ({'value': value, 'basis': 'anticipated'} if signal == 'interest' else
                    {'value': value, 'basis': 'experienced', 'scale': 'integer-1-5'})
            try:
                validate_body(signal, body)
            except FeedbackError:
                raise AuthorityFault('CONTRACT_INVALID', 422) from None
        else:
            ga._require(title is None and value is None, 'CONTRACT_INVALID', 422)
            reverses_id = str(ga._uuid(reverses_id))
        target_id = uuid.uuid5(ga._uuid(profile_ref), NAMESPACE + ':' + entry_ref)
        key = profile_ref + ':' + entry_ref
        with ga.current_writer(self.authority, session_ref, profile_ref) as state:
            actor = get_user_model().objects.get(pk=state.actor_id)
            receipt = MutationReceipt.objects.filter(actor=actor, mutation_id=mutation_id).first()
            if receipt is not None:
                ga._require(receipt.profile_id == state.profile_id and receipt.target_id == target_id,
                            'REVISION_CONFLICT', 409)
            # The feedback engine must own its IMMEDIATE transaction. Create
            # only scoped target identity in this preceding short writer window.
            with transaction.atomic(), ga.writer_window(actor, state.profile_id):
                target = FeedbackTarget.objects.filter(pk=target_id).first()
                if target is not None:
                    ga._require((target.source_namespace, target.source_key, target.kind,
                        target.category, target.deleted) == (NAMESPACE, key, 'item', 'artist', False),
                        'AUTHORITY_DENIED', 403)
                if target is None:
                    ga._require(operation == 'set', 'AUTHORITY_DENIED', 403)
                    ga._require(FeedbackTarget.objects.filter(source_namespace__in=NATIVE_NAMESPACES,
                        source_key__startswith=profile_ref + ':').count() < MAX_ENTRIES, 'CAPABILITY_UNAVAILABLE', 503)
                    target = FeedbackTarget.objects.create(id=target_id, kind='item', category='artist',
                        title=title, source_namespace=NAMESPACE, source_key=key)
                if operation == 'set':
                    ga._require(target.title == title, 'REVISION_CONFLICT', 409)
            events = FeedbackEvent.objects.filter(profile_id=state.profile_id, target=target,
                signal=signal, scope='item').order_by('revision')
            last = events.last()
            if receipt is not None and receipt.event_id:
                accepted = FeedbackEvent.objects.get(pk=receipt.event_id)
                ga._require(accepted.signal == signal and accepted.actor_id == actor.pk, 'REVISION_CONFLICT', 409)
                previous = events.filter(revision=accepted.revision - 1).first()
                context = str(previous.pk) if previous else None
            else:
                context = str(last.pk) if last else None
            command = MutationCommand(mutation_id, str(target.pk), 'item', signal, 'item', operation,
                                      base_revision, context, body, reverses_id)
            try:
                result = record_mutation(actor=actor, profile_id=state.profile_id, command=command)
            except Conflict:
                raise AuthorityFault('REVISION_CONFLICT', 409) from None
            except FeedbackError as error:
                raise AuthorityFault('CAPABILITY_UNAVAILABLE' if error.status == 503 else 'AUTHORITY_DENIED',
                                     503 if error.status == 503 else 403) from None
            return {'entry_ref': entry_ref, 'event_ref': result.event_id, 'revision': result.accepted_revision}

    @sensitive_variables()
    def read_entries(self, session_ref, profile_ref):
        with ga.recovery_guard(self.authority, session_ref, profile_ref=profile_ref), self.authority._lock:
            state, _ = ga._bound(self.authority, session_ref, profile_ref)
            targets = FeedbackTarget.objects.filter(source_namespace__in=NATIVE_NAMESPACES,
                kind='item', deleted=False, source_key__startswith=str(state.profile_ref) + ':',
                feedbackevent__profile_id=state.profile_id, feedbackevent__actor_id=state.actor_id
                ).distinct().order_by('pk')[:MAX_ENTRIES]
            return [{'entry_ref': target.source_key.split(':', 1)[1],
                'entry_kind': 'category_note' if target.source_namespace == NOTE_NAMESPACE else 'item',
                'category': target.category, 'title': target.title,
                'signals': signal_rows(state, target,
                    ('note',) if target.source_namespace == NOTE_NAMESPACE else ENTRY_SIGNALS)}
                for target in targets]

    @sensitive_variables()
    def mutate_entry(self, session_ref, profile_ref, *, entry_ref, mutation_id, base_revision,
                     operation, signal, entry_kind=None, category=None, title=None, value=None,
                     text=None, experienced_ack=False, reverses_id=None):
        from .category_registry import CATEGORIES, KEYS
        profile_ref, entry_ref = str(ga._uuid(profile_ref)), str(ga._uuid(entry_ref))
        mutation_id = str(ga._uuid(mutation_id))
        ga._revision(base_revision)
        ga._require(operation in ('set', 'reverse') and signal in ENTRY_SIGNALS
            and (operation == 'reverse') == (reverses_id is not None), 'CONTRACT_INVALID', 422)
        if operation == 'set':
            ga._require(category in KEYS and entry_kind in ('item', 'category_note'), 'CONTRACT_INVALID', 422)
            if entry_kind == 'category_note':
                ga._require(signal == 'note' and title is None, 'CONTRACT_INVALID', 422)
                title = next(row.label for row in CATEGORIES if row.key == category) + ' preference note'
            ga._require(type(title) is str and 0 < len(title) <= 200 and bool(title.strip())
                and not any(ord(c) < 32 or ord(c) == 127 for c in title), 'CONTRACT_INVALID', 422)
            body = typed_body(signal, value=value, text=text, experienced_ack=experienced_ack)
        else:
            ga._require(all(v is None for v in (entry_kind, category, title, value, text))
                and experienced_ack is False, 'CONTRACT_INVALID', 422)
            reverses_id, body = str(ga._uuid(reverses_id)), None
        key = profile_ref + ':' + entry_ref
        with ga.current_writer(self.authority, session_ref, profile_ref) as state:
            actor = get_user_model().objects.get(pk=state.actor_id)
            receipt = MutationReceipt.objects.filter(actor=actor, mutation_id=mutation_id).first()
            # Resolve persisted identity before accepting kind/category from a
            # set form. Undo carries only the entry UUID and its original event.
            with transaction.atomic(), ga.writer_window(actor, state.profile_id):
                targets = list(FeedbackTarget.objects.filter(source_namespace__in=NATIVE_NAMESPACES,
                    source_key=key)[:2])
                ga._require(len(targets) <= 1, 'REVISION_CONFLICT', 409)
                target = targets[0] if targets else None
                if target is None:
                    ga._require(operation == 'set' and base_revision == 0 and receipt is None,
                                'REVISION_CONFLICT', 409)
                    ga._require(FeedbackTarget.objects.filter(source_namespace__in=NATIVE_NAMESPACES,
                        source_key__startswith=profile_ref + ':').count() < MAX_ENTRIES,
                        'CAPABILITY_UNAVAILABLE', 503)
                    namespace = NOTE_NAMESPACE if entry_kind == 'category_note' else ITEM_NAMESPACE
                    target = FeedbackTarget.objects.create(id=uuid.uuid5(ga._uuid(profile_ref),
                        namespace + ':' + entry_ref), kind='item', category=category, title=title,
                        source_namespace=namespace, source_key=key)
                ga._require(not target.deleted and target.kind == 'item', 'AUTHORITY_DENIED', 403)
                if operation == 'set':
                    kind = 'category_note' if target.source_namespace == NOTE_NAMESPACE else 'item'
                    ga._require((kind, target.category, target.title) == (entry_kind, category, title),
                                'REVISION_CONFLICT', 409)
                ga._require(target.source_namespace != NOTE_NAMESPACE or signal == 'note', 'CONTRACT_INVALID', 422)
                if receipt:
                    ga._require(receipt.profile_id == state.profile_id and receipt.target_id == target.pk,
                                'REVISION_CONFLICT', 409)
            events = FeedbackEvent.objects.filter(profile_id=state.profile_id, target=target,
                signal=signal, scope='item').order_by('revision')
            if receipt and receipt.event_id:
                accepted = FeedbackEvent.objects.get(pk=receipt.event_id)
                ga._require(accepted.signal == signal and accepted.actor_id == actor.pk, 'REVISION_CONFLICT', 409)
                previous = events.filter(revision=accepted.revision - 1).first()
            else:
                previous = events.last()
            command = MutationCommand(mutation_id, str(target.pk), 'item', signal, 'item', operation,
                base_revision, str(previous.pk) if previous else None, body, reverses_id)
            try:
                result = record_mutation(actor=actor, profile_id=state.profile_id, command=command)
            except Conflict:
                raise AuthorityFault('REVISION_CONFLICT', 409) from None
            except FeedbackError as error:
                raise AuthorityFault('CAPABILITY_UNAVAILABLE' if error.status == 503 else 'AUTHORITY_DENIED',
                                     503 if error.status == 503 else 403) from None
            return {'entry_ref': entry_ref, 'event_ref': result.event_id, 'revision': result.accepted_revision}

    def mutate_favourite(self, session_ref, profile_ref, *, entry_ref, mutation_id,
                         base_revision, category, title=None, remove=False):
        from .category_registry import KEYS
        ga._require(category in KEYS and type(remove) is bool, 'CONTRACT_INVALID', 422)
        profile_ref, entry_ref = str(ga._uuid(profile_ref)), str(ga._uuid(entry_ref))
        mutation_id = str(ga._uuid(mutation_id)); ga._revision(base_revision)
        ga._require(remove or type(title) is str and 1 <= len(title) <= 200 and bool(title.strip())
            and not any(ord(c) < 32 or ord(c) == 127 for c in title), 'CONTRACT_INVALID', 422)
        with ga.current_writer(self.authority, session_ref, profile_ref) as state:
            actor = get_user_model().objects.get(pk=state.actor_id)
            with transaction.atomic(), ga.writer_window(actor, state.profile_id):
                target, created = FeedbackTarget.objects.get_or_create(
                    source_namespace=FAVOURITE_NAMESPACE, source_key=profile_ref+':'+entry_ref,
                    defaults=dict(id=uuid.uuid5(ga._uuid(profile_ref), FAVOURITE_NAMESPACE+':'+entry_ref),
                                  kind='item', category=category, title=title or ''))
                ga._require(not target.deleted and target.category == category and (remove or target.title == title)
                    and (not created or not remove), 'REVISION_CONFLICT', 409)
            events = FeedbackEvent.objects.filter(profile_id=state.profile_id,target=target,
                signal='saved',scope='item').order_by('revision')
            receipt = MutationReceipt.objects.filter(actor=actor,mutation_id=mutation_id).first()
            last = (events.filter(revision=receipt.accepted_revision-1).first() if receipt else events.last())
            command = MutationCommand(mutation_id,str(target.pk),'item','saved','item','set',base_revision,
                str(last.pk) if last else None,None if remove else {'value':True})
            try:result = record_mutation(actor=actor,profile_id=state.profile_id,command=command)
            except Conflict:raise AuthorityFault('REVISION_CONFLICT',409) from None
            return {'entry_ref':entry_ref,'event_ref':result.event_id,'revision':result.accepted_revision}
