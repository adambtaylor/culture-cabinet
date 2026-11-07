"""Pure human-history replay and deterministic, bounded seed-lane selection."""
from copy import deepcopy

from shelf.feedback_scale import (
    ALL_CHOICES, REACTION_LABELS, is_positive, is_negative, weight, clean_note,
)

CHOICES = ALL_CHOICES
CHOICE_LABELS = REACTION_LABELS + (
    ('enjoyed', 'Experienced enjoyment'),
    ('interested', 'Anticipated interest'),
    ('not_for_me', 'Not for me'),
    ('unknown', 'Unknown / not experienced'),
    ('skipped', 'Skipped (no taste preference)'),
)
MAX_HISTORY = 500


def replay(history):
    """Undo reverses the last effective operation, including corrections/restarts.

    Suggestions, source ranks and opens are never responses. Saved is independent
    of the exclusive response states. Restart clears the current projection,
    retaining audit history and allowing an explicit undo.
    """
    state = {'choices': {}, 'saved': [], 'notes': {}}
    stack = []
    for event in history:
        operation = event['operation']
        if operation == 'undo':
            if stack:
                state = stack.pop()
            continue
        stack.append(deepcopy(state))
        if operation == 'restart':
            state = {'choices': {}, 'saved': [], 'notes': {}}
        elif operation in ('rate','funnel_skip','funnel_next'):
            continue
        elif operation == 'respond':
            if event['choice'] not in CHOICES:
                raise ValueError('Unknown human response.')
            state['choices'].pop(event['key'], None)
            state['choices'][event['key']] = event['choice']
            state['notes'].pop(event['key'], None)
            if 'note' in event:
                state['notes'][event['key']] = clean_note(event['note'])
        elif operation == 'save':
            if event['key'] not in state['saved']:
                state['saved'].append(event['key'])
        elif operation == 'unsave':
            if event['key'] in state['saved']:
                state['saved'].remove(event['key'])
        else:
            raise ValueError('Unknown human operation.')
    return dict(state, can_undo=bool(stack))


def select(cards, state, *, size=12, category='all', unjudged_only=False):
    """Optionally keep response identities as roots without spending output slots."""
    if type(size) is not int or not 1 <= size <= 30:
        raise ValueError('Selection budget exceeded.')
    if type(unjudged_only) is not bool:
        raise ValueError('Expected an explicit response filter.')
    choices = state['choices']
    by_key = {card.key: card for card in cards}
    # An explicit new HUMAN response to a suggestion can itself become a seed.
    roots = [key for key, choice in choices.items()
             if is_positive(choice) and key in by_key]
    roots.sort(key=lambda key: -weight(choices[key]))
    roots = roots[:5]
    candidates = [card for card in cards if not is_negative(choices.get(card.key, 'unknown'))
                  and (not unjudged_only or card.key not in choices)
                  and (category == 'all' or card.kind != 'event' or card.category == category)]
    members = {root: sorted([c for c in candidates if root in c.roots or c.venue_key == root],
                           key=lambda c: (c.rank, c.key)) for root in roots}
    memberships = {c.key: [root for root in roots if c in members[root]] for c in candidates}
    exploration = sorted(candidates, key=lambda c: (bool(memberships[c.key]), c.rank, c.key))
    lanes = [{'key': root, 'title': by_key[root].title, 'choice': choices[root],
              'count': len(members[root])} for root in roots]
    output, seen = [], set()
    queues = [list(members[root]) for root in roots]
    cursor = 0
    while len(output) < size:
        use_exploration = not queues or len(output) % 3 == 2
        if not use_exploration:
            cursor %= len(queues)
        pool = exploration if use_exploration else queues[cursor]
        while pool and pool[0].key in seen:
            pool.pop(0)
        if not pool:
            if pool is not exploration:
                # The next lane shifts into this slot; do not skip its turn.
                del queues[cursor]
                continue
            if not queues:
                break
            # Exploration includes every candidate, so exhausted means done.
            break
        card = pool.pop(0)
        if not use_exploration:
            cursor += 1
        seen.add(card.key)
        reasons = [f'Seed: {by_key[root].title} ({choices[root]})' for root in memberships[card.key]]
        if use_exploration or not reasons:
            reasons.append('Exploration — other sources remain eligible')
        output.append({'card': card, 'memberships': memberships[card.key], 'why': reasons})
    return {'lanes': lanes, 'selection': output}
