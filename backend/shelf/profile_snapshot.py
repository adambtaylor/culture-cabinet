"""Trusted fresh-portable owner history, detached from mutable ORM objects.

This is not a household/import adapter. Unsupported legacy streams fail closed;
their presence must never quietly become an incomplete portable taste profile.
"""
from uuid import UUID, uuid5

from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from . import grant_authority as authority
from .disclosure import REACTIONS, canonical_bytes, read_json
from .models import (AuthoredDiscoveryState, DiscoveryState, FeedbackEvent,
                     FeedbackProjection, IntakeRun, SavedItem, SocialDiscoveryState)
from .taste_selection import project


def _id(view, value):
    return str(uuid5(UUID(view.installation_id), value))


@sensitive_variables()
def _node(view, source, card, basis, value, *, origin='authenticated-human-post'):
    # Source relationship roots are not D3 node identities. Without an admitted
    # crosswalk they remain missing ancestors, rather than guessed public lineage.
    parents = [_id(view, 'unresolved-source-root:' + root) for root in card['roots']]
    return {'id': _id(view, source + ':' + basis), 'subject_ref': view.subject_ref,
        'key': card['key'], 'title': card['title'], 'medium': card['medium'],
        'basis': basis, 'value': value,
        'data_classes': ['raw_notes'] if basis == 'feedback_note' else ['canonical_seed'],
        'source_ref': source, 'identity_status': 'confirmed', 'author_ref': view.actor_ref,
        'applicable_subjects': [view.subject_ref], 'origin': origin, 'sensitivity': 'ordinary',
        'purposes': ['inference', 'catalogue_lookup'], 'direct_parent_ids': parents}


@sensitive_variables()
def _history(view, row):
    try:
        project(row.history)  # Validate the established typed values and experience acknowledgement.
    except (ValueError, TypeError, KeyError):
        raise authority.AuthorityFault('CONTRACT_INVALID', 422) from None
    stack = []
    for index, event in enumerate(row.history):
        if event['operation'] == 'undo':
            if stack:
                stack.pop()
        elif event['operation'] == 'restart':
            # Keep the restart on the stack: undo of it restores the preceding history.
            stack.append((index, event))
        else:
            stack.append((index, event))
    start = max((i for i, (_, event) in enumerate(stack) if event['operation'] == 'restart'), default=-1)
    current = {}
    for index, event in stack[start + 1:]:
        operation, key = event['operation'], event.get('key', '')
        if operation == 'rate':
            slot = (key, event['basis'])
            if event['value'] is None:
                current.pop(slot, None)
            else:
                current[slot] = (index, event, event['basis'], event['value'])
        elif operation == 'respond':
            # A replacement response replaces its compound note, too. Neither a
            # denied note nor a later source card can rewrite a separate rating.
            for basis in ('anticipated_reaction', 'legacy_choice', 'feedback_note'):
                current.pop((key, basis), None)
            basis = 'anticipated_reaction' if event['choice'] in REACTIONS else 'legacy_choice'
            current[(key, basis)] = (index, event, basis, event['choice'])
            if 'note' in event:
                current[(key, 'feedback_note')] = (index, event, 'feedback_note',
                    {'reaction': event['choice'], 'text': event['note']})
        elif operation == 'save':
            current[(key, 'favourite')] = (index, event, 'favourite', True)
        elif operation == 'unsave':
            current.pop((key, 'favourite'), None)
    nodes = []
    for index, event, basis, value in current.values():
        # Old records without the original judged identity or authenticated human
        # provenance are not adopted. The complete local revision still binds them.
        card = event.get('card')
        if card is None or event.get('origin') != 'authenticated-human-post':
            continue
        nodes.append(_node(view, f'discovery:{row.pk}:{index}', card, basis, value))
    return nodes


@sensitive_variables()
def _feedback(view):
    """Reduce original event/reversal history; do not invent a modern scale.

    The complete lineage remains local in the v2 snapshot. The sole disclosure
    codec exports only a permitted current typed value, never these local IDs.
    """
    from .feedback import reduce_events, validate_body, FeedbackError
    from .models import FeedbackSubject, Grant
    subject = FeedbackSubject.objects.filter(profile_id=view.profile_id).first()
    groups, evidence, excluded = {}, [], []
    for event in FeedbackEvent.objects.filter(profile_id=view.profile_id).select_related('target').order_by('revision', 'id'):
        groups.setdefault((event.target_id, event.signal, event.scope), []).append(event)
    for (_, signal, scope), events in groups.items():
        effective, body, erased = reduce_events(events)
        if erased:
            continue
        relevant = [event for event in events if event.pk == effective]
        if not relevant:
            continue  # Reversed history has no currently asserted preference.
        current = relevant[0]
        authentic = (subject is not None and str(subject.subject_id) == view.subject_ref
            and all(event.origin == event.original_origin == 'direct' and event.actor_id == view.actor_id
                and event.attribution == {'subject_id': view.subject_ref}
                and Grant.objects.filter(pk=event.authority_grant_id, user_id=event.actor_id,
                                         profile_id=view.profile_id).exists() for event in events))
        if not authentic or current.target.deleted:
            excluded.append({'reason': 'unattributed_feedback', 'count': len(events),
                             'blocks_use': signal == 'constraint'})
            continue
        try:
            validate_body(signal, body, imported=signal.startswith(('legacy_', 'seed_')))
        except FeedbackError:
            raise authority.AuthorityFault('CONTRACT_INVALID', 422) from None
        classes = {'enjoyment': ['experienced_enjoyment'], 'interest': ['anticipated_reactions'],
            'saved': ['explicit_favourites'], 'legacy_rating': ['legacy_choices'],
            'legacy_state': ['legacy_choices'], 'constraint': ['raw_notes', 'free_text_entry'],
            'note': ['raw_notes', 'free_text_entry'],
            'seed_statement': ['free_text_entry', 'unresolved_personal_text']}.get(signal, ['reaction'])
        card = {'key': 'feedback:' + str(current.target_id), 'title': current.target.title,
                'medium': current.target.category, 'roots': []}
        node = _node(view, 'feedback:' + str(effective), card, 'typed_feedback',
                     {'signal': signal, 'scope': scope, 'value': body})
        if current.target.source_namespace in ('owner-stated-favourite','owner-stated-visual-artist'):
            # A title supplied by the owner is personal text, never a provider
            # identity. Its separate text permission remains necessary.
            node['identity_status']='unresolved_personal'
            classes=[*classes,'unresolved_personal_text']
        elif current.target.source_namespace=='enhance-current' and signal in ('interest','saved'):
            from .models import PortableJourneyCommand
            interest=groups.get((current.target_id,'interest','item'),[])
            saved=groups.get((current.target_id,'saved','item'),[])
            expected=(interest[-1].revision if interest else 0,saved[-1].revision if saved else 0)
            opaque=str(uuid5(UUID(view.profile_ref),
                'candidate:'+current.target.source_key))
            for parent in PortableJourneyCommand.objects.filter(cabinet_id=view.profile_id,
                    kind__in=['card.action','card.undo'],state='committed').order_by('-created_at','-id')[:1000]:
                if (parent.result.get('target_ref')==opaque and
                    (parent.result.get('interest_revision'),parent.result.get('saved_revision'))==expected and
                    parent.proof.get('schema')=='card-action-continuity/v2' and
                    parent.proof.get('child_event_refs')==parent.result.get('child_event_refs')):
                    # Only a proven native command supplies this local identity
                    # crosswalk. No title matching or new provider fact is used.
                    node['key']='candidate-key-sha256:'+current.target.source_key
                    node['source_ref']='card-action:'+str(parent.command_id)
                    break
        node['data_classes'] = classes
        node['event_lineage'] = [{'event_ref': _id(view, 'event:' + str(e.pk)), 'revision': e.revision,
            'operation': e.operation, 'reverses_ref': _id(view, 'event:' + str(e.reverses_id)) if e.reverses_id else None,
            'corrects_ref': _id(view, 'event:' + str(e.corrects_id)) if e.corrects_id else None,
            'author_ref': view.actor_ref, 'subject_ref': view.subject_ref, 'origin': e.original_origin,
            'recorded_at': authority._stamp(e.recorded_at),
            'source_ref': current.target.source_namespace + ':' + current.target.source_key}
            for e in events]
        evidence.append(node)
    return evidence, excluded


@sensitive_variables()
def capture(authority_view):
    """Return a current D3 snapshot only for a sealed, authenticated owner view."""
    authority._require(type(authority_view) is authority.AuthorityView)
    view = authority_view
    # Hold the SAME recovery barrier across all reads, not just each authority
    # check. Its exit can refuse the return if witness continuity is lost late.
    with authority.recovery_guard(view._authority, view._session, profile_ref=view.profile_ref) as checkpoint:
        authority.assert_current(view)
        with view._authority._lock, transaction.atomic():
            authority.assert_current(view)
            # Existing import/derived/three-valued-feedback formats have no admitted
            # lossless portable lineage adapter. Never reinterpret or silently omit them.
            state, _ = authority._bound(view._authority, view._session, view.profile_ref)
            version2 = state.data_schema_version == 2 or FeedbackEvent.objects.filter(profile_id=view.profile_id).exists()
            excluded = []
            for model in (IntakeRun, AuthoredDiscoveryState, SocialDiscoveryState):
                count = model.objects.filter(profile_id=view.profile_id).count()
                if count and not version2:
                    raise authority.AuthorityFault('CAPABILITY_UNAVAILABLE', 503)
                if count:
                    excluded.append({'reason': 'unsupported_history', 'count': count, 'blocks_use': True})
            if not version2:
                authority._require(not FeedbackProjection.objects.filter(profile_id=view.profile_id).exists(),
                                   'CAPABILITY_UNAVAILABLE', 503)
            # The shared classifier excludes only guarded work-on-snapshot rows.
            # Run output is not an authenticated preference or a lineage adapter;
            # every legacy/unknown history row still blocks this fresh producer.
            legacy = authority.synthesis_history(state)
            if legacy and not version2:
                raise authority.AuthorityFault('CAPABILITY_UNAVAILABLE', 503)
            if legacy:
                excluded.append({'reason': 'unsupported_derived_history', 'count': len(legacy), 'blocks_use': True})
            evidence = []
            if version2:
                feedback_nodes, feedback_exclusions = _feedback(view)
                evidence.extend(feedback_nodes)
                excluded.extend(feedback_exclusions)
            for row in DiscoveryState.objects.filter(profile_id=view.profile_id).order_by('pk'):
                if row.actor_id == view.actor_id and row.mode == 'household':
                    evidence.extend(_history(view, row))
                    if version2:
                        unknown = sum(1 for event in row.history if event.get('operation') not in ('undo', 'restart')
                            and (event.get('origin') != 'authenticated-human-post' or event.get('card') is None))
                        if unknown:
                            excluded.append({'reason': 'unattributed_discovery', 'count': unknown, 'blocks_use': False})
                elif version2:
                    excluded.append({'reason': 'foreign_history', 'count': len(row.history), 'blocks_use': False})
            for item in SavedItem.objects.filter(profile_id=view.profile_id,
                actor_id=view.actor_id, list_kind='favorites').order_by('pk'):
                card = {'key': f'{item.provider}:{item.medium}:{item.source_id}',
                        'title': item.title, 'medium': item.medium, 'roots': []}
                evidence.append(_node(view, f'favourite:{item.pk}', card, 'favourite', True))
            authority._require(len(evidence) <= 500, 'LIMIT_EXCEEDED', 422)
            snapshot = {'schema': 'profile-snapshot/v1', 'installation_id': view.installation_id,
                'actor_ref': view.actor_ref, 'profile_ref': view.profile_ref, 'profile_kind': view.profile_kind,
                'applicability_ref': view.subject_ref, 'profile_revision_digest': view.profile_revision_digest,
                'source_revisions': view.source_revisions, 'policy_epoch': view.policy_epoch,
                'restriction_watermark': view.restriction_watermark, 'evidence': evidence,
                'derived_traits': [], 'hard_exclusions': [], 'seen_keys': [],
                'category_registry_revision': 1, 'hard_constraints': []}
            if version2:
                snapshot['schema'] = 'profile-snapshot/v2'
                snapshot['excluded_history'] = excluded
                from .category_registry import KEYS
                snapshot['hard_constraints'] = [{'id': node['id'], 'subject_ref': view.subject_ref,
                    'categories': list(KEYS), 'kind': 'explicit_exclusion',
                    'rule': 'Unresolved authored constraint; verify before recommendation.',
                    'source_ref': node['source_ref'], 'verification_required': True}
                    for node in evidence if node['basis'] == 'typed_feedback'
                    and node['value']['signal'] == 'constraint' and node['value']['value']
                    and node['value']['value']['text']]
            authority.assert_current(view)
            checkpoint.check()
            return read_json(canonical_bytes(snapshot))
