"""Pure, supplied-feedback read model. No queries, persistence or provider I/O.

Callers authorize and materialize complete histories before adapting them. Source
labels are provenance, not proof of humanity. Never feed ranking output back as
human history. See docs/cross-media-feedback.md for the integration boundary.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Literal

from shelf.cross_media import Cache, InvalidData, MAX_EDGES, MAX_NODES, MAX_PATH_STATES, positive_id, stamp
from shelf.cross_media_ranking import ContextMismatch, MAX_LANES, MAX_MEMBERS, MAX_SELECTION, replay as replay_intake
from shelf.discovery_reducer import CHOICES, MAX_HISTORY, replay as replay_discovery
from shelf.discovery_sources import Candidate, timestamp
from shelf.taste_selection import project as project_taste

NARRATIVE = {'book', 'movie', 'tv', 'work', 'edition'}
ARTS = {'gallery', 'art_fair'}
THEMES = {'visual-art', 'live-performance', 'speculative-fiction', 'history', 'nature'}
MAX_SNAPSHOTS = 128
ROOT_CAP = 500
FEATURE_CAP = 100
TOTAL_FEATURE_CAP = 150
TOTAL_CAP = 1000


@dataclass(frozen=True)
class Reaction:
    key: str
    response: str
    event_id: str
    revision: int | None
    provenance: str
    responded_at: str | None
    attended: bool = False
    original_value: int | str | bool | None = None
    basis: str | None = None
    experienced: bool = False


@dataclass(frozen=True)
class FeedbackSnapshot:
    actor_id: int
    profile_id: int
    mode: str
    source: str
    revision: int
    reactions: tuple[Reaction, ...]


@dataclass(frozen=True)
class FactualEdge:
    source_key: str
    target_key: str
    relation: Literal['lineup', 'recording-artist', 'adaptation', 'shared-work']
    source: str
    evidence: str
    confidence: float


@dataclass(frozen=True)
class FeatureEdge:
    source_key: str
    target_key: str
    feature: str
    source: str
    evidence: str
    confidence: float


def _text(value, limit=300):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise InvalidData('Expected bounded nonempty text.')
    return value


def _sequence(value, limit):
    # In particular, do not accidentally evaluate an ORM QuerySet or generator.
    if type(value) not in (list, tuple) or len(value) > limit:
        raise InvalidData('Expected bounded materialized input.')
    return value


def _context(actor_id, profile_id, mode):
    positive_id(actor_id)
    positive_id(profile_id)
    if mode not in ('sample', 'household'):
        raise ContextMismatch('Unknown feedback mode.')


def _revision(actual, expected):
    if type(actual) is not int or type(expected) is not int or actual < 0 or actual != expected:
        raise ContextMismatch('Stale feedback revision.')


def from_intake(run, *, actor_id, profile_id, run_id, expected_revision, mode):
    """Reuse durable replay: correction replaces; undo clears that item's choice.

    Durable history has event positions, not per-event revisions. Keep those
    positions and the original run revision; never call index + 1 a revision.
    """
    _context(actor_id, profile_id, mode)
    effective = replay_intake(run, actor_id=actor_id, profile_id=profile_id,
                              run_id=run_id, expected_revision=expected_revision)
    reactions = tuple(Reaction(key, event['choice'], f'{run_id}:history:{event["event_index"]}',
                               None, event['provenance'], event['responded_at'],
                               original_value=event['choice'])
                      for key, event in effective.items())
    return FeedbackSnapshot(actor_id, profile_id, mode, 'intake:' + run_id,
                            run['revision'], reactions)


def from_discovery(snapshot, *, actor_id, profile_id, state_id, expected_revision,
                   mode, attended_keys=()):
    """Replay actual DiscoveryState history, retaining original event positions.

    attended_keys must come from explicitly supplied human attendance evidence,
    not event dates, source metadata, completion, opens or inferred graph paths.
    Numeric event enjoyment has its own explicit experience/attendance checkbox;
    interest never supplies that evidence. Legacy reactions keep their meanings.
    """
    _context(actor_id, profile_id, mode)
    _text(state_id, 100)
    if (type(snapshot) is not dict or snapshot.get('actor_id') != actor_id
            or snapshot.get('profile_id') != profile_id or snapshot.get('id') != state_id
            or snapshot.get('mode') != mode
            or type(snapshot.get('actor_id')) is not int or type(snapshot.get('profile_id')) is not int):
        raise ContextMismatch('Different actor/profile/state/mode.')
    _revision(snapshot.get('revision'), expected_revision)
    for key in _sequence(attended_keys, MAX_NODES):
        _text(key)
    attended = set(attended_keys)
    history = _sequence(snapshot.get('history'), MAX_HISTORY)
    active = []
    for index, event in enumerate(history):
        if (type(event) is not dict or event.get('origin') != 'authenticated-human-post'
                or event.get('mode') != mode or stamp(event.get('at')) is None):
            raise InvalidData('Expected supplied authenticated human history.')
        operation = event.get('operation')
        operations = ('respond', 'save', 'unsave', 'undo', 'restart')
        if mode == 'household':
            operations += ('rate', 'funnel_skip', 'funnel_next')
        if operation not in operations:
            raise InvalidData('Not a human feedback operation.')
        if operation in ('respond', 'save', 'unsave', 'rate', 'funnel_skip'):
            _text(event.get('key'))
        elif event.get('key') or event.get('choice'):
            raise InvalidData('Unexpected undo/restart fields.')
        if operation == 'respond' and event.get('choice') not in CHOICES:
            raise InvalidData('Unknown human response.')
        if operation != 'respond' and event.get('choice'):
            raise InvalidData('Actions are not responses.')
        # Track references only; the existing reducer owns response semantics.
        if operation == 'undo':
            if active:
                active.pop()
        else:
            active.append((index, event))
    if mode == 'household':
        try:
            projected = project_taste(list(history))
        except ValueError as error:
            raise InvalidData('Invalid supplied household rating history.') from error
        state = {'choices': projected['legacychoices'], 'saved': projected['saved']}
    else:
        projected = {'ratings': {}}
        state = replay_discovery(history)
    references = {}
    for index, event in active:
        if event['operation'] == 'restart':
            references.clear()
        elif event['operation'] in ('respond', 'save'):
            references[event['operation'], event['key']] = (index, event)
        elif event['operation'] == 'rate':
            reference = ('rate', event['key'], event['basis'])
            if event['value'] is None:
                references.pop(reference, None)
            else:
                references[reference] = (index, event)
    reactions = []
    for operation, values in (('respond', state['choices'].items()),
                              ('save', ((key, 'saved') for key in state['saved']))):
        for key, response in values:
            index, event = references[operation, key]
            reactions.append(Reaction(key, response, f'{state_id}:history:{index}', None,
                                      event['origin'], event['at'], key in attended, response))
    for key, ratings in projected['ratings'].items():
        for basis, value in ratings.items():
            index, event = references['rate', key, basis]
            positive, negative = ('enjoyed', 'not_for_me') if basis == 'enjoyment' else ('interested', 'uninterested')
            response = positive if value >= 4 else negative if value <= 2 else 'neutral'
            experienced = basis == 'enjoyment' and event['experienced']
            reactions.append(Reaction(key, response, f'{state_id}:history:{index}', None,
                event['origin'], event['at'], experienced and event['card']['medium'] == 'event',
                value, basis, experienced))
    return FeedbackSnapshot(actor_id, profile_id, mode, 'discovery:' + state_id,
                            snapshot['revision'], tuple(reactions))


def from_feedback_events(events, *, actor_id, profile_id, target_id, signal, scope,
                         key, expected_revision, mode, attended=False):
    """Adapt a complete, already-authorized, materialized feedback signal stream.

    Uses the existing reducer/validator under Django, without querying models.
    Only an effective direct human statement can supply reinforcement. Legacy,
    imported and attributed values keep their revision but supply no reaction.
    """
    from shelf.feedback import reduce_events, validate_body

    _context(actor_id, profile_id, mode)
    _text(key)
    if type(attended) is not bool:
        raise InvalidData('Attendance must be an explicit Boolean assertion.')
    rows = sorted(_sequence(events, 2000), key=lambda event: event.revision)
    for revision, event in enumerate(rows, 1):
        if (event.revision != revision or event.profile_id != profile_id
                or str(event.target_id) != str(target_id) or event.signal != signal or event.scope != scope):
            raise ContextMismatch('Incomplete or mixed feedback stream.')
    actual_revision = rows[-1].revision if rows else 0
    _revision(actual_revision, expected_revision)
    effective_id, body, erased = reduce_events(rows)
    current = next((event for event in rows if event.pk == effective_id), None)
    reactions = ()
    if current is not None and not erased and body is not None and current.origin == 'direct':
        validate_body(signal, body)
        response = None
        if signal == 'enjoyment':
            response = 'enjoyed' if body['value'] >= 4 else 'not_for_me' if body['value'] <= 2 else 'neutral'
        elif signal == 'interest':
            response = 'uninterested' if body['value'] == 'no' else 'interested'
        elif signal in ('saved', 'skipped', 'opened'):
            response = signal if body['value'] else 'un' + signal
        elif signal in ('familiarity', 'completion', 'visibility'):
            response = body['value']
        if response is not None:
            reactions = (Reaction(key, response, str(current.pk), current.revision, 'direct',
                                  current.recorded_at.isoformat(), attended, body['value']),)
    return FeedbackSnapshot(actor_id, profile_id, mode, f'feedback:{target_id}:{scope}:{signal}',
                            actual_revision, reactions)


def catalogue_candidates(cache):
    """Reuse validated cross-media identities as Candidate read models."""
    if not isinstance(cache, Cache):
        raise InvalidData('Expected a validated cache.')
    return tuple(Candidate(item['key'], item['title'], item['medium'], item['provider'],
                           item['provenance'], 0, item['fetched_at'], url=item['url'])
                 for item in cache.snapshot()['nodes'] if item['medium'] in NARRATIVE)


def _adjacency(cards, edges):
    adjacent = {key: [] for key in cards}
    seen = set()
    for edge in _sequence(edges, MAX_EDGES):
        if type(edge) not in (FactualEdge, FeatureEdge):
            raise InvalidData('Expected a typed factual or inferred edge.')
        _text(edge.source_key)
        _text(edge.target_key)
        _text(edge.feature if isinstance(edge, FeatureEdge) else edge.relation, 80)
        if edge.source_key not in cards or edge.target_key not in cards or edge.source_key == edge.target_key:
            raise InvalidData('Missing or identical edge endpoints.')
        _text(edge.source, 160)
        _text(edge.evidence, 500)
        if (type(edge.confidence) not in (int, float) or not math.isfinite(edge.confidence)
                or not 0 < edge.confidence <= 1):
            raise InvalidData('Expected finite confidence in (0, 1].')
        if edge in seen:
            continue
        seen.add(edge)
        source, target = cards[edge.source_key], cards[edge.target_key]
        if isinstance(edge, FeatureEdge):
            if (edge.feature not in THEMES or source.kind not in NARRATIVE
                    or target.kind != 'event' or target.category not in ARTS):
                raise InvalidData('Only explicit narrative-to-arts theme features are supported.')
        else:
            kinds = (source.kind, target.kind)
            valid = (edge.relation == 'lineup' and kinds == ('event', 'artist')
                     or edge.relation == 'recording-artist' and kinds == ('recording', 'artist')
                     or edge.relation in ('adaptation', 'shared-work') and set(kinds) <= NARRATIVE)
            if not valid:
                raise InvalidData('Factual relation and endpoint kinds disagree.')
            adjacent[edge.target_key].append((edge.source_key, edge, 'reverse'))
        adjacent[edge.source_key].append((edge.target_key, edge, 'forward'))
    for values in adjacent.values():
        values.sort(key=lambda item: (isinstance(item[1], FeatureEdge), -item[1].confidence,
                                     item[0], repr(item[1]), item[2]))
    return adjacent


def _paths(root, adjacent, blocked):
    """At most two facts and one terminal feature; cap the allocated frontier."""
    if root not in adjacent:
        return {}, False
    queue = deque([(root, (), frozenset({root}), 0, TOTAL_CAP)])
    results, allocated = {}, 1
    truncated = False
    while queue:
        current, path, visited, facts, score = queue.popleft()
        for target, edge, direction in adjacent[current]:
            inferred = isinstance(edge, FeatureEdge)
            if target in visited or target in blocked or (not inferred and facts == 2):
                continue
            if allocated == MAX_PATH_STATES:
                truncated = True
                continue
            allocated += 1
            points = min(FEATURE_CAP if inferred else ROOT_CAP,
                         int(score * edge.confidence * (0.1 if inferred else 0.5)))
            if points == 0:
                continue
            step = {'from_key': current, 'to_key': target, 'traversal': direction,
                    'kind': 'inferred-feature' if inferred else 'factual',
                    'relation': edge.feature if inferred else edge.relation,
                    'source': edge.source, 'evidence': edge.evidence, 'confidence': edge.confidence}
            next_path = path + (step,)
            result = {'key': target, 'points': points, 'inferred': inferred,
                      'fact_hops': facts + (not inferred), 'feature_hops': int(inferred), 'path': next_path}
            previous = results.get(target)
            if previous is None or points > previous['points']:
                results[target] = result
            if not inferred:
                queue.append((target, next_path, visited | {target}, facts + 1, points))
    return results, truncated


def rank(cards, edges, snapshots, *, actor_id, profile_id, mode, expected_revisions, now, size=15):
    """Reconstruct a bounded ranking; its scores are never human preferences.

    Scores are integer thousandths. Each original item is one root even when
    supplied twice or reached by many paths. Independent human judgments of a
    suggested item may become a new root; provider lineage alone cannot.
    """
    _context(actor_id, profile_id, mode)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise InvalidData('Supply an aware evaluation time.')
    if type(size) is not int or not 0 <= size <= MAX_SELECTION:
        raise InvalidData('Selection budget exceeded.')
    if type(expected_revisions) is not dict:
        raise ContextMismatch('Expected current feedback revisions.')
    by_key = {}
    for card in _sequence(cards, MAX_NODES):
        if not isinstance(card, Candidate):
            raise InvalidData('Expected Candidate identities.')
        _text(card.key)
        if card.key in by_key:
            raise InvalidData('Expected unique Candidate identities.')
        if type(card.rank) is not int or card.rank < 0:
            raise InvalidData('Invalid source rank.')
        by_key[card.key] = card
    adjacent = _adjacency(by_key, edges)
    sources = {}
    for snapshot in _sequence(snapshots, MAX_SNAPSHOTS):
        if (not isinstance(snapshot, FeedbackSnapshot)
                or (snapshot.actor_id, snapshot.profile_id, snapshot.mode) != (actor_id, profile_id, mode)):
            raise ContextMismatch('Mixed feedback ownership or mode.')
        _context(snapshot.actor_id, snapshot.profile_id, snapshot.mode)
        _text(snapshot.source)
        if type(expected_revisions) is not dict or snapshot.source not in expected_revisions:
            raise ContextMismatch('Missing current feedback revision.')
        _revision(snapshot.revision, expected_revisions[snapshot.source])
        if snapshot.source in sources and sources[snapshot.source] != snapshot:
            raise ContextMismatch('Conflicting snapshots of one stream.')
        _sequence(snapshot.reactions, 2000)
        sources[snapshot.source] = snapshot
    if set(expected_revisions) != set(sources):
        raise ContextMismatch('Incomplete feedback snapshot set.')
    feedback = tuple(sources[source] for source in sorted(sources))
    by_root, blocked = {}, set()
    for snapshot in feedback:
        for reaction in snapshot.reactions:
            if not isinstance(reaction, Reaction):
                raise InvalidData('Expected an adapted human reaction.')
            _text(reaction.key)
            if type(reaction.attended) is not bool:
                raise InvalidData('Attendance must be an explicit Boolean assertion.')
            if reaction.response in ('not_for_me', 'uninterested', 'hidden'):
                blocked.add(reaction.key)
            if reaction.response == 'enjoyed':
                card = by_key.get(reaction.key)
                if card is None or card.kind != 'event' or reaction.attended:
                    by_root.setdefault(reaction.key, []).append((snapshot.source, reaction))
    roots = sorted(set(by_root) - blocked)[:MAX_LANES]
    judged = {reaction.key for snapshot in feedback for reaction in snapshot.reactions
              if reaction.response in ('enjoyed', 'not_for_me', 'uninterested')}
    lanes, memberships = [], {}
    for root in roots:
        paths, truncated = _paths(root, adjacent, blocked)
        members = []
        for key, result in paths.items():
            card = by_key[key]
            if key in judged or key in blocked:
                continue
            if card.kind == 'event':
                starts = timestamp(card.starts_at)
                if starts is None or starts <= now:
                    continue
            members.append(result)
        members.sort(key=lambda row: (-row['points'], by_key[row['key']].rank, row['key']))
        members = members[:MAX_MEMBERS]
        lanes.append({'root': root, 'feedback': tuple(by_root[root]), 'members': members,
                      'path_budget_reached': truncated})
        for member in members:
            memberships.setdefault(member['key'], []).append(dict(member, root=root))
    # Each lane gets its next unseen member, even after shared/empty pools.
    queues = [deque(lane['members']) for lane in lanes]
    selection, seen = [], set()
    while any(queues) and len(selection) < size:
        for queue in queues:
            while queue and queue[0]['key'] in seen:
                queue.popleft()
            if not queue or len(selection) == size:
                continue
            key = queue.popleft()['key']
            seen.add(key)
            contributions = memberships[key]
            factual = sum(row['points'] for row in contributions if not row['inferred'])
            inferred = min(TOTAL_FEATURE_CAP, sum(row['points'] for row in contributions if row['inferred']))
            selection.append({'card': by_key[key], 'points': min(TOTAL_CAP, factual + inferred),
                              'memberships': contributions})
    return {'status': 'ready' if roots else 'needs-enjoyment', 'feedback': feedback,
            'lanes': lanes, 'selection': selection,
            'limits': {'lanes': MAX_LANES, 'facts': 2, 'features': 1, 'path_states': MAX_PATH_STATES,
                       'per_root': ROOT_CAP, 'per_feature': FEATURE_CAP, 'total_features': TOTAL_FEATURE_CAP,
                       'total': TOTAL_CAP, 'members': MAX_MEMBERS, 'selection': size}}
