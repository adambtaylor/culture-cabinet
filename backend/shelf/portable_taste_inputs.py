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
                    ga._require(FeedbackTarget.objects.filter(source_namespace=NAMESPACE,
                        source_key__startswith=profile_ref + ':').count() < 100, 'CAPABILITY_UNAVAILABLE', 503)
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
