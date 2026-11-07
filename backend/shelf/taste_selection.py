"""Pure explicit-rating replay and bounded, balanced discovery rounds.

Source cards are supplied by the caller. This module neither fetches sources nor
turns a save, skip, qualitative response or source relationship into a rating.
"""
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit

from shelf.feedback_scale import (
    ALL_CHOICES, LEGACY_CHOICES, clean_note, weight, is_positive, is_negative,
)

MEDIA = ('movie', 'tv', 'book', 'music', 'event')
BASES = ('interest', 'enjoyment')
MAX_HISTORY = 2000
MAX_CARDS = 2000
CARD_FIELDS = ('key', 'title', 'medium', 'source', 'url', 'provenance', 'lane', 'roots', 'fetched_at')


def _text(value, limit=300, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit
            or (not empty and not value.strip())
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Invalid bounded text.')
    return value


def _keys(values, limit=MAX_CARDS):
    if not isinstance(values, list) or len(values) > limit:
        raise ValueError('Invalid identity list.')
    for key in values:
        _text(key, 160)
    if len(set(values)) != len(values):
        raise ValueError('Duplicate identities.')
    return list(values)


def _timestamp(value):
    if value is None:
        return
    _text(value, 50)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Invalid source timestamp.') from None
    if parsed.utcoffset() is None:
        raise ValueError('Timestamp needs an offset.')


def _card(value):
    if not isinstance(value, dict) or any(field not in value for field in CARD_FIELDS):
        raise ValueError('Incomplete source card.')
    card = {field: deepcopy(value[field]) for field in CARD_FIELDS}
    _text(card['key'], 160)
    for field in ('title', 'source', 'provenance'):
        _text(card[field])
    _text(card['lane'], 160, empty=True)
    if card['medium'] not in MEDIA or card['provenance'] == 'fixture':
        raise ValueError('Unsupported household card.')
    if card['url'] is not None:
        _text(card['url'], 2048)
        try:
            url = urlsplit(card['url'])
            safe = (url.scheme == 'https' and url.hostname and url.username is None
                    and url.password is None and url.port in (None, 443)
                    and '\\' not in card['url'])
        except ValueError:
            safe = False
        if not safe:
            raise ValueError('Source link must be HTTPS without credentials.')
    _timestamp(card['fetched_at'])
    card['roots'] = _keys(card['roots'], 64)
    return card


def _event(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid history event.')
    event = deepcopy(value)
    operation = event.get('operation')
    if operation not in ('rate', 'funnel_skip', 'funnel_next', 'save', 'unsave', 'respond', 'undo', 'restart'):
        raise ValueError('Unknown history operation.')
    modern = operation in ('rate', 'funnel_skip', 'funnel_next')
    for field, expected in (('mode', 'household'), ('origin', 'authenticated-human-post')):
        if (modern or field in event) and event.get(field) != expected:
            raise ValueError('History is not an explicit household action.')
    if modern and event.get('at') is None:
        raise ValueError('Human action timestamp required.')
    if 'at' in event:
        _timestamp(event['at'])
    key = event.get('key', '')
    _text(key, 160, empty=operation in ('undo', 'restart', 'funnel_next'))
    if operation in ('undo', 'restart', 'funnel_next') and key:
        raise ValueError('Unexpected action identity.')
    choice = event.get('choice', '')
    if operation == 'respond':
        if choice not in ALL_CHOICES:
            raise ValueError('Invalid qualitative response.')
    elif choice:
        raise ValueError('Unexpected qualitative response.')
    if 'note' in event:
        if operation != 'respond':
            raise ValueError('Notes require an explicit response.')
        event['note'] = clean_note(event['note'])
    if 'card' in event:
        event['card'] = _card(event['card'])
        if event['card']['key'] != key:
            raise ValueError('Snapshot identity disagrees with action.')
    if operation in ('rate', 'funnel_skip') and 'card' not in event:
        raise ValueError('Judged identity snapshot required.')
    if operation == 'rate':
        if (event.get('basis') not in BASES or 'value' not in event
                or type(event.get('experienced')) is not bool):
            raise ValueError('Invalid rating basis or experience acknowledgement.')
        rating = event['value']
        if rating is not None and (type(rating) is not int or not 1 <= rating <= 5):
            raise ValueError('Rating must be 1 to 5 or an explicit clear.')
        if event['basis'] == 'enjoyment' and not event['experienced']:
            raise ValueError('Enjoyment requires explicit experience or attendance.')
    if operation == 'funnel_next':
        event['shown'] = _keys(event.get('shown'), 30)
    return event


def project(history):
    """Replay edits exactly; undo pops one action, including next or restart.

    Resolving the effective action stack first avoids copying an ever-growing
    projection for each event. Legacy history can lack snapshots and remains
    qualitative. A clear removes only one rating basis, never a favourite.
    """
    if not isinstance(history, list) or len(history) > MAX_HISTORY:
        raise ValueError('History budget exceeded.')
    effective = []
    for value in history:
        event = _event(value)
        if event['operation'] == 'undo':
            if effective:
                effective.pop()
        else:
            effective.append(event)
    state = {'ratings': {}, 'saved': [], 'skipped': [], 'cards': {},
             'seen': [], 'round': 0, 'legacychoices': {}, 'notes': {}, 'feedback': {}}
    start = max((i for i, e in enumerate(effective) if e['operation'] == 'restart'), default=-1)
    for event in effective[start + 1:]:
        operation, key = event['operation'], event.get('key', '')
        if 'card' in event:
            state['cards'][key] = event['card']
        if operation == 'rate':
            ratings = state['ratings'].setdefault(key, {})
            if event['value'] is None:
                ratings.pop(event['basis'], None)
            else:
                ratings[event['basis']] = event['value']
            if not ratings:
                state['ratings'].pop(key, None)
        elif operation == 'respond':
            state['legacychoices'].pop(key, None)
            state['legacychoices'][key] = event['choice']
            state['notes'].pop(key, None)
            evidence = {'choice': event['choice'], 'origin': event.get('origin', 'authenticated-human-post')}
            if 'note' in event:
                state['notes'][key] = event['note']
                evidence['note'] = event['note']
            state['feedback'][key] = evidence
        elif operation == 'save' and key not in state['saved']:
            state['saved'].append(key)
        elif operation == 'unsave' and key in state['saved']:
            state['saved'].remove(key)
        elif operation == 'funnel_skip' and key not in state['skipped']:
            state['skipped'].append(key)
        elif operation == 'funnel_next':
            state['seen'] = list(dict.fromkeys(state['seen'] + event['shown']))
            state['skipped'] = []
            state['round'] += 1
    return dict(state, can_undo=bool(effective))


def select(cards, projection, size=12):
    """Balance media, retain distinct positives and reserve exploration space.

    Ratings below three affect only their item, never its genre or neighbours.
    Positive ratings give mild exact-lane affinity; supplied root identities are
    kept distinct. Seen cards never recycle automatically: exhaustion requires
    an explicit caller action. The selector does not mutate cards or history.
    """
    if type(size) is not int or not 1 <= size <= 30:
        raise ValueError('Invalid round size.')
    if not isinstance(cards, list) or len(cards) > MAX_CARDS or not isinstance(projection, dict):
        raise ValueError('Invalid candidate pool or projection.')
    round_number = projection.get('round')
    if type(round_number) is not int or not 0 <= round_number <= MAX_HISTORY:
        raise ValueError('Invalid round number.')
    ratings, legacy = projection.get('ratings'), projection.get('legacychoices')
    snapshots = projection.get('cards')
    if not isinstance(ratings, dict) or not isinstance(legacy, dict) or not isinstance(snapshots, dict):
        raise ValueError('Invalid projected history.')
    for key, values in ratings.items():
        _text(key, 160)
        if (not isinstance(values, dict) or not values or any(b not in BASES or type(v) is not int
                or not 1 <= v <= 5 for b, v in values.items())):
            raise ValueError('Invalid projected ratings.')
    for key, choice in legacy.items():
        _text(key, 160)
        if choice not in ALL_CHOICES:
            raise ValueError('Invalid projected legacy response.')
    blocked = set(ratings) | set(legacy)
    for name in ('saved', 'skipped', 'seen'):
        keys = _keys(projection.get(name))
        if name != 'saved':
            blocked.update(keys)
    by_key = {}
    for value in cards:
        card = _card(value)
        if card['key'] in by_key and by_key[card['key']] != card:
            raise ValueError('Conflicting candidate identity.')
        by_key[card['key']] = card
    negative_roots = {key for key, choice in legacy.items() if is_negative(choice)}
    pool = [c for key, c in sorted(by_key.items()) if key not in blocked
            and not negative_roots.intersection(c['roots'])]
    roots = {}
    for key, values in sorted(ratings.items()):
        if any(value >= 4 for value in values.values()) and key in snapshots:
            root = _card(snapshots[key])
            if root['key'] != key:
                raise ValueError('Projected identity mismatch.')
            roots[key] = root
    for key, choice in legacy.items():
        if is_positive(choice) and key in snapshots:
            root = _card(snapshots[key])
            if root['key'] != key:
                raise ValueError('Projected identity mismatch.')
            roots[key] = root
    roots = dict(sorted(roots.items(), key=lambda item: (-weight(legacy[item[0]]) if item[0] in legacy else -3, item[0])))
    lane_weights = {}
    for key, choice in legacy.items():
        if key in snapshots:
            lane = snapshots[key]['lane']
            lane_weights[lane] = lane_weights.get(lane, 0) + weight(choice)
    lanes = {root['lane'] for root in roots.values() if root['lane']}

    def memberships(card):
        return [key for key in sorted(card['roots']) if key in roots]

    def affinity(card):
        return bool(memberships(card)) or bool(card['lane'] and card['lane'] in lanes)

    selected, reasons = [], {}
    media_counts = dict.fromkeys(MEDIA, 0)
    lane_counts = {}

    def take(card, reason):
        if card['key'] in reasons or len(selected) >= size:
            return
        selected.append(card)
        reasons[card['key']] = [reason]
        media_counts[card['medium']] += 1
        lane_counts[card['lane']] = lane_counts.get(card['lane'], 0) + 1

    def remaining():
        return [c for c in pool if c['key'] not in reasons]

    def balance(card):
        return (media_counts[card['medium']], -lane_weights.get(card['lane'], 0),
                lane_counts.get(card['lane'], 0), card['key'])

    media_order = MEDIA[round_number % len(MEDIA):] + MEDIA[:round_number % len(MEDIA)]
    for medium in media_order:
        options = [c for c in remaining() if c['medium'] == medium]
        if options:
            take(min(options, key=lambda c: (not affinity(c), balance(c))), 'Balanced media coverage.')
    represented = set()
    for key, root in roots.items():
        # Separate explicit root identities even when two seeds share a lane.
        members = [c for c in pool if key in c['roots']]
        if not members and root['lane']:
            members = [c for c in pool if c['lane'] == root['lane']]
        if any(c['key'] in reasons for c in members):
            represented.add(key)
        elif members and len(represented) < 2:
            take(min(members, key=balance), 'Room for a distinct explicit positive starting point.')
            if any(c['key'] in reasons for c in members):
                represented.add(key)
        if len(represented) >= 2:
            break
    exploration_target = max(1, size // 3)
    while sum(not affinity(c) for c in selected) < exploration_target and len(selected) < size:
        options = [c for c in remaining() if not affinity(c)]
        if not options:
            break
        take(min(options, key=balance), 'Exploration outside your positive lanes; no rating inferred.')
    while remaining() and len(selected) < size:
        card = min(remaining(), key=lambda c: (media_counts[c['medium']], not affinity(c),
                                               -lane_weights.get(c['lane'], 0), lane_counts.get(c['lane'], 0), c['key']))
        take(card, 'Balanced discovery; exact-lane affinity is only a mild preference.')
    for card in selected:
        for key in memberships(card):
            positive = legacy[key] if key in legacy else ', '.join(
                f'{basis} {value}/5' for basis, value in ratings[key].items() if value >= 4)
            reasons[card['key']].append(f'Source-supplied link to {roots[key]["title"]} ({positive}).')
    return {'selection': [{'card': c, 'why': reasons[c['key']]} for c in selected],
            'round': round_number, 'exhausted': not pool,
            'coverage': [medium for medium in MEDIA if any(c['medium'] == medium for c in pool)]}
