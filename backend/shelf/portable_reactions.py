"""Native reactions resolve targets only from this owner's permitted publication.

The existing feedback event reducer owns exact values, replay and durable Undo.
No browser actor, before-image, title or source metadata becomes authority.
"""
from . import grant_authority as ga
from .feedback import MutationCommand, mutate as record_mutation, Conflict, FeedbackError, validate_body
from .models import FeedbackEvent, FeedbackTarget, PortableCabinetManifest, ProfileCabinetState
from .portable_authority import AuthorityFault


def target_ref(profile_ref, candidate_key):
    import uuid
    import hashlib
    key_digest = hashlib.sha256(candidate_key.encode()).hexdigest()
    return str(uuid.uuid5(ga._uuid(profile_ref), 'candidate:' + key_digest))


class PortableReactions:
    def __init__(self, authority, *, service):
        ga._require(type(authority) is ga.PortableAuthority and service.authority is authority)
        self.authority, self.service = authority, service

    def _target(self, session_ref, profile_ref, opaque_ref, *, create=False, require_current=False):
        import uuid
        import hashlib
        ga._uuid(opaque_ref)
        state, _ = ga._bound(self.authority, session_ref, profile_ref)
        # An existing personally authored target remains available for Undo even
        # after the publication becomes stale. This grants no source/model use.
        owned = FeedbackTarget.objects.filter(source_namespace='enhance-current', deleted=False,
            feedbackevent__profile_id=state.profile_id, feedbackevent__actor_id=state.actor_id).distinct()
        for target in owned:
            if not require_current and str(uuid.uuid5(ga._uuid(profile_ref), 'candidate:' + target.source_key)) == opaque_ref:
                return target
        current = self.service.current(session_ref, profile_ref=profile_ref)
        manifest = current.get('manifest') if type(current) is dict else None
        ga._require(type(manifest) is dict, 'PREVIEW_STALE', 409)
        cards = [card for group in manifest['categories'] for card in group['cards']]
        card = next((card for card in cards if target_ref(profile_ref, card['key']) == opaque_ref), None)
        ga._require(card is not None, 'AUTHORITY_DENIED', 403)
        key_digest = hashlib.sha256(card['key'].encode()).hexdigest()
        target = FeedbackTarget.objects.filter(source_namespace='enhance-current', source_key=key_digest,
                                               kind='item', deleted=False).first()
        if target is None and create:
            ga._require(len(card['title']) <= 200, 'CONTRACT_INVALID', 422)
            target = FeedbackTarget.objects.create(id=uuid.uuid4(), kind='item', title=card['title'],
                category=card['category'], source_namespace='enhance-current', source_key=key_digest)
        return target

    def read(self, session_ref, profile_ref, target_ref):
        with ga.current_writer(self.authority, session_ref, profile_ref) as state:
            target = self._target(session_ref, profile_ref, target_ref)
            rows = FeedbackEvent.objects.filter(profile_id=state.profile_id, actor_id=state.actor_id,
                target=target).order_by('signal', 'revision') if target else ()
            from .feedback import reduce_events
            groups = {}
            for event in rows:
                groups.setdefault(event.signal, []).append(event)
            return {'target_ref': target_ref, 'signals': {signal: {
                'revision': events[-1].revision, 'event_ref': str(events[-1].pk),
                'value': reduce_events(events)[1], 'erased': reduce_events(events)[2]}
                for signal, events in groups.items()}}

    def mutate(self, session_ref, profile_ref, target_ref, mutation_id, base_revision,
               operation, value, reverses_id=None):
        from django.contrib.auth import get_user_model
        from django.db import transaction
        ga._require(type(value) is dict and set(value) == {'signal', 'value'}, 'CONTRACT_INVALID', 422)
        signal, body = value['signal'], value['value']
        ga._require(signal in ('enjoyment', 'interest', 'saved', 'skipped', 'opened',
                              'completion', 'familiarity', 'constraint', 'visibility', 'note'), 'CONTRACT_INVALID', 422)
        try:
            validate_body(signal, body)
        except FeedbackError:
            raise AuthorityFault('CONTRACT_INVALID', 422) from None
        ga._uuid(mutation_id); ga._revision(base_revision)
        ga._require(operation in ('set', 'reverse') and (operation == 'reverse') == (reverses_id is not None),
                    'CONTRACT_INVALID', 422)
        with ga.current_writer(self.authority, session_ref, profile_ref) as state:
            actor = get_user_model().objects.get(pk=state.actor_id)
            from .models import MutationReceipt
            receipt = MutationReceipt.objects.filter(actor=actor, mutation_id=mutation_id).first()
            # Target resolution/creation has its own short transaction. It ends
            # before feedback.immediate(), which must own the outer transaction.
            with transaction.atomic(), ga.writer_window(actor, state.profile_id):
                # A new statement needs today's permitted card. Authored Undo
                # and an exact recorded replay do not grant new source use.
                target = self._target(session_ref, profile_ref, target_ref, create=operation == 'set',
                    require_current=operation == 'set' and receipt is None)
                ga._require(target is not None, 'AUTHORITY_DENIED', 403)
            last = FeedbackEvent.objects.filter(profile_id=state.profile_id, target=target,
                signal=signal, scope='item').order_by('revision').last()
            # Same-request replay must retain the original envelope after newer
            # history. Resolve its original context, not today's before-image.
            if receipt is not None and receipt.event_id:
                accepted = FeedbackEvent.objects.get(pk=receipt.event_id)
                previous = FeedbackEvent.objects.filter(profile_id=state.profile_id, target=target,
                    signal=signal, scope='item', revision=accepted.revision - 1).first()
                context = str(previous.pk) if previous else None
            else:
                context = str(last.pk) if last else None
            command = MutationCommand(str(ga._uuid(mutation_id)), str(target.pk), 'item', signal,
                'item', operation, base_revision, context, body if operation == 'set' else None,
                str(ga._uuid(reverses_id)) if reverses_id else None)
            try:
                result = record_mutation(actor=actor, profile_id=state.profile_id, command=command)
            except Conflict:
                raise AuthorityFault('REVISION_CONFLICT', 409) from None
            except FeedbackError as error:
                raise AuthorityFault('CAPABILITY_UNAVAILABLE' if error.status == 503 else 'AUTHORITY_DENIED',
                                     503 if error.status == 503 else 403) from None
            return {'event_ref': result.event_id, 'revision': result.accepted_revision, 'target_ref': target_ref}


def _service():
    from django.conf import settings
    authority = settings.PORTABLE_AUTHORITY
    service = getattr(authority, '_enhance_service', None)
    ga._require(service is not None, 'CAPABILITY_UNAVAILABLE', 503)
    return PortableReactions(authority, service=service)


def read(session_ref, profile_ref, target_ref):
    return _service().read(session_ref, profile_ref, target_ref)


def mutate(session_ref, profile_ref, target_ref, mutation_id, base_revision, operation, value, reverses_id=None):
    return _service().mutate(session_ref, profile_ref, target_ref, mutation_id, base_revision, operation, value, reverses_id)
