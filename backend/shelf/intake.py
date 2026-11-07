"""Deterministic, temporary human choices. No official feedback or machine roots."""
import secrets
import time
from collections import Counter

from shelf.feedback_scale import (
    ALL_CHOICES, ALL_LABELS, REACTION_LABELS, WEIGHTS, is_positive, is_negative,
)

SESSION_KEY = 'provider_intake_v1'
TTL = 3600
MAX_CANDIDATES = 120

CHOICES = ALL_CHOICES
CHOICE_LABELS = ALL_LABELS


class IntakeError(Exception):
    def __init__(self, status=400):
        self.status = status
        super().__init__('Temporary intake is invalid or stale.')


def new_state(actor_id, profile_id, *, now=None):
    now = time.time() if now is None else now
    return {'actor_id': actor_id, 'profile_id': profile_id, 'expires_at': now + TTL,
            'revision': 0, 'nonce': secrets.token_urlsafe(24), 'candidates': {},
            'choices': {}, 'current': [], 'queued': [], 'search_results': [], 'expanded': [],
            'started': False, 'failures': [], 'reasons': {}}


def load(session, actor_id, profile_id, *, now=None):
    state = session.get(SESSION_KEY)
    now = time.time() if now is None else now
    if state and (state.get('actor_id') != actor_id or state.get('profile_id') != profile_id
                  or state.get('expires_at', 0) <= now):
        session.pop(SESSION_KEY, None)
        state = None
    return state


def check_revision(state, revision, nonce):
    if (str(state['revision']) != revision or not isinstance(nonce, str)
            or not secrets.compare_digest(state['nonce'], nonce)):
        raise IntakeError(409)


def advance(state):
    state['revision'] += 1
    state['nonce'] = secrets.token_urlsafe(24)


def merge(state, candidates, *, queue=True):
    historical = set(state.get('history_only', [])) if state.get('run_id') else set()
    cached_count = len(state['candidates'].keys() - historical)
    for candidate in candidates:
        data = candidate.data()
        key = data['key']
        if (key not in state['candidates'] or key in historical) and cached_count < MAX_CANDIDATES:
            state['candidates'][key] = data
            historical.discard(key)
            cached_count += 1
        elif key in state['candidates'] and data.get('book_metadata') is not None:
            # Enrich old cached books on an explicit fetch, retaining identity,
            # original lane/root and the existing session expiry.
            for field in ('book_metadata', 'description', 'fetched_at'):
                state['candidates'][key][field] = data[field]
        if queue and key in state['candidates'] and key not in historical and key not in state['queued']:
            state['queued'].append(data['key'])
    if state.get('run_id'):
        state['history_only'] = sorted(historical)


def lane_weights(state):
    weights = Counter()
    for key, choice in state['choices'].items():
        card = state['candidates'][key]
        weights[card['lane']] += WEIGHTS[choice]
    # Neighbor inheritance is additive and independent of choice insertion order.
    for root in {c['root'] for c in state['candidates'].values() if c['root']}:
        weights['root:' + root] += WEIGHTS[state['choices'].get(root, 'unknown')]
    return weights


def positive_roots(state):
    return [state['candidates'][key] for key, choice in state['choices'].items()
            if is_positive(choice)
            and state['candidates'][key]['origin'] != 'neighbor']


def select_round(state, size=9):
    weights = lane_weights(state)
    negative = {k for k, c in state['choices'].items() if is_negative(c)}
    available = [c for k, c in state['candidates'].items()
                 if k in state['queued'] and k not in state['choices'] and c['root'] not in negative]
    available.sort(key=lambda c: (-weights[c['lane']], c['lane'], c['key']))
    selected, reasons = [], {}

    def take(card, reason):
        if card['key'] not in reasons and len(selected) < size:
            selected.append(card['key'])
            reasons[card['key']] = reason

    # Preserve each medium when available; these are topical exploration slots,
    # never an assertion of adaptation or a cross-media relationship.
    for medium in ('movie', 'tv', 'book'):
        card = next((c for c in available if c['medium'] == medium), None)
        if card:
            take(card, 'Medium coverage; exploratory editorial lane.')
    # At least one less-weighted lane remains when the pool permits.
    exploration = sorted(available, key=lambda c: (weights[c['lane']], c['lane'], c['key']))
    if exploration:
        selected_lanes = {state['candidates'][key]['lane'] for key in selected}
        card = next((c for c in exploration if c['lane'] not in selected_lanes), None)
        if card is None:
            card = next((c for c in exploration if c['key'] not in reasons), None)
        if card:
            take(card, 'Exploration slot; lower-weight lane retained.')
    positive_lanes = set()
    for root in positive_roots(state):
        neighbor_lane = 'root:' + root['key']
        # Distinct human roots keep their own provider-neighbor lanes, even
        # when both roots began in the same editorial genre lane.
        positive_lanes.add(neighbor_lane if any(c['lane'] == neighbor_lane for c in available)
                           else root['lane'])
    positive_lanes = sorted(positive_lanes)
    if positive_lanes:
        offset = state['revision'] % len(positive_lanes)
        positive_lanes = positive_lanes[offset:] + positive_lanes[:offset]
    for lane in positive_lanes:
        card = next((c for c in available if c['lane'] == lane and c['key'] not in reasons), None)
        if card:
            take(card, 'Preserves a distinct lane with an explicit human positive.')
    for card in available:
        take(card, f"Human lane weight {weights[card['lane']]} (explicit reaction strength; historical meanings preserved).")
    state['current'], state['reasons'] = selected, reasons
    return selected


def choose(state, key, choice):
    if key not in state['current'] or key in state['choices'] or choice not in CHOICES:
        raise IntakeError()
    state['choices'][key] = choice
    select_round(state)


def add_search_result(state, key):
    if key not in state['search_results'] or key not in state['candidates']:
        raise IntakeError()
    state['search_results'].remove(key)
    if key not in state['queued']:
        state['queued'].append(key)
    select_round(state)
    # A deliberately selected favorite must be visible for the next human choice.
    if key not in state['choices'] and key not in state['current']:
        state['current'] = [key] + state['current'][:8]
        state['reasons'][key] = 'Added explicitly from your source search; no taste signal yet.'


def summary(state):
    counts = Counter(state['choices'].values())
    return {choice: counts[choice] for choice in CHOICES}


SELECTOR_V2 = 'intake-v2.1'


def select_round_v2(state, size=9, *, category='all'):
    """Fixed deterministic human weights; capped lanes, two roots, exploration.

    No cross-media inference: provider lanes remain independent. A neighbor's
    own human response is valid but never promotes it into an expansion root.
    """
    if category not in ('all', 'book', 'movie', 'tv'):
        raise IntakeError()
    weights = lane_weights(state)
    negative = {k for k, c in state['choices'].items() if is_negative(c)}
    available = [c for k, c in state['candidates'].items()
                 if k in state['queued'] and k not in state['choices'] and c.get('root') not in negative
                 and (category == 'all' or c['medium'] == category)]
    available.sort(key=lambda c: (-weights[c['lane']], c['lane'], c['key']))
    selected, reasons = [], {}
    media, lanes = Counter(), Counter()
    medium_count = len({c['medium'] for c in available})
    medium_cap = max(3, (size + max(1, medium_count) - 1) // max(1, medium_count))
    counts = summary(state)

    def take(card, why, *, relax_medium=False):
        if (len(selected) >= size or card['key'] in reasons or lanes[card['lane']] >= 4
                or (not relax_medium and media[card['medium']] >= medium_cap)):
            return False
        selected.append(card['key'])
        media[card['medium']] += 1
        lanes[card['lane']] += 1
        reasons[card['key']] = (f"{SELECTOR_V2}: {why} Score {weights[card['lane']]}; "
            f"human choices {counts}. Explicit reaction strength; historical meanings preserved.")
        return True

    roots = sorted(positive_roots(state),
                   key=lambda c: (-WEIGHTS[state['choices'][c['key']]], c['key']))
    for root in roots[:2]:
        neighbors = [c for c in available if c.get('root') == root['key']]
        pool = neighbors or [c for c in available if c['lane'] == root['lane']]
        for card in pool:
            if take(card, 'Space for a distinct explicit human positive root.'):
                break
    for medium in ('movie', 'tv', 'book'):
        if not media[medium]:
            for card in available:
                if card['medium'] == medium and take(card, 'Balanced medium exploration.'):
                    break
    exploration = sorted(available, key=lambda c: (weights[c['lane']], c['lane'], c['key']))
    for card in exploration:
        if card['lane'] not in lanes and take(card, 'Exploration slot in a less-weighted lane.'):
            break
    while len(selected) < size:
        ranked = sorted(available, key=lambda c: (-weights[c['lane']], lanes[c['lane']], media[c['medium']], c['lane'], c['key']))
        if not any(take(card, 'Ranked by explicit human lane choices; tied lanes share space.') for card in ranked):
            break
    # Finite or uneven source pools may leave a medium short. Keep the lane cap
    # while allowing other media to fill unused slots; never duplicate a card.
    for card in available:
        take(card, 'Finite-pool coverage; lane dominance remains capped.', relax_medium=True)
    added = state.get('added_key')
    if (added and added in state['queued'] and added not in state['choices'] and added in state['candidates']
            and (category == 'all' or state['candidates'][added]['medium'] == category)):
        if added not in selected:
            selected = [added] + selected[:max(0, size-1)]
        reasons[added] = 'Added explicitly from your source search; no taste signal yet.'
    state['current'], state['reasons'] = selected, reasons
    return selected
