"""Pure durable-event replay and bounded seed lanes, separate from live intake."""
from copy import deepcopy

from shelf.cross_media import (
    Cache, InvalidData, MAX_PATH_STATES, canonical_url, identity, paths_from, positive_id,
    split_identity, stamp, _json, _list, _text,
)

MAX_HISTORY = 2000
MAX_LANES = 5
MAX_MEMBERS = 100
MAX_SELECTION = 50
CHOICES = {'enjoyed', 'interested', 'not_for_me', 'unknown'}
IMPORT_PROVENANCE = 'server-session-v1; original response time unknown'
CARD_FIELDS = {'key', 'provider', 'medium', 'source_id', 'title', 'url',
               'fetched_at', 'lane', 'origin', 'root'}


class ContextMismatch(InvalidData):
    """Supplied ownership/run context or optimistic revision does not match."""


def _card(card):
    if type(card) is not dict or set(card) != CARD_FIELDS:
        raise InvalidData('Expected an actual durable card snapshot.')
    if (card['provider'], card['medium']) not in {('hardcover', 'book'), ('tmdb', 'movie'), ('tmdb', 'tv')}:
        raise InvalidData('Unsupported durable identity.')
    if card['key'] != identity(card['provider'], card['medium'], card['source_id']):
        raise InvalidData('Durable key does not match its namespace.')
    url = card['url']
    slug = url.removeprefix('https://hardcover.app/books/') if type(url) is str else None
    if canonical_url(card['key'], slug) != url or url is None:
        raise InvalidData('Noncanonical durable URL.')
    _text(card['title'])
    _text(card['lane'], 160)
    if (card['origin'] not in ('editorial', 'search', 'neighbor')
            or type(card['root']) is not str or len(card['root']) > 100):
        raise InvalidData('Invalid durable discovery metadata.')
    if stamp(card['fetched_at']) is None:
        raise InvalidData('Durable snapshots require known fetched_at.')
    return card


def replay(run, *, actor_id, profile_id, run_id, expected_revision):
    """Latest per key, null clears. Supplied checks do not perform authorization."""
    _json(run)
    positive_id(actor_id)
    positive_id(profile_id)
    _text(run_id, 100)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ContextMismatch('Invalid expected revision.')
    if (type(run) is not dict or type(run.get('actor_id')) is not int
            or type(run.get('profile_id')) is not int or type(run.get('revision')) is not int
            or run.get('actor_id') != actor_id or run.get('profile_id') != profile_id
            or run.get('id') != run_id or run.get('revision') != expected_revision
            or run.get('archived_at') is not None):
        raise ContextMismatch('Stale or different actor/profile/run.')
    effective = {}
    for index, event in enumerate(_list(run.get('history'), MAX_HISTORY)):
        if type(event) is not dict or set(event) != {'card', 'choice', 'operation', 'responded_at', 'recorded_at', 'provenance'}:
            raise InvalidData('Expected an actual durable event.')
        card = _card(event['card'])
        operation, choice = event['operation'], event['choice']
        if operation not in ('choose', 'correct', 'undo', 'import'):
            raise InvalidData('Only durable human response operations are supported.')
        if ((operation == 'undo' and choice is not None)
                or (operation != 'undo' and (type(choice) is not str or choice not in CHOICES))):
            raise InvalidData('Invalid durable choice.')
        if stamp(event['recorded_at']) is None:
            raise InvalidData('Missing recording time.')
        if operation == 'import':
            if event['provenance'] != IMPORT_PROVENANCE or event['responded_at'] is not None:
                raise InvalidData('Imported history must retain unknown response time and provenance.')
        elif event['provenance'] != 'authenticated-human-post' or stamp(event['responded_at']) is None:
            raise InvalidData('Missing human response provenance.')
        key = card['key']
        effective.pop(key, None)
        if choice is not None:
            effective[key] = dict(deepcopy(event), event_index=index)
    return effective


def rank(run, cache, *, actor_id, profile_id, run_id, expected_revision, size=15):
    """At most five current human seeds; source-ranked lanes share output slots."""
    if not isinstance(cache, Cache):
        raise InvalidData('Expected a validated source cache.')
    if type(size) is not int or not 0 <= size <= MAX_SELECTION:
        raise InvalidData('Selection budget exceeded.')
    effective = replay(run, actor_id=actor_id, profile_id=profile_id,
                       run_id=run_id, expected_revision=expected_revision)
    roots = [event for event in effective.values() if event['choice'] in ('enjoyed', 'interested')]
    roots.sort(key=lambda event: (event['choice'] != 'enjoyed', event['event_index'], event['card']['key']))
    nodes = {item['key']: item for item in cache.snapshot()['nodes']}
    lanes = []
    for event in roots[:MAX_LANES]:
        root = event['card']['key']
        candidates = paths_from(cache, root)
        path_budget_reached = len(candidates) >= MAX_PATH_STATES

        def order(result):
            neighbors = [step['rank'] for step in result['path'] if step['kind'] in
                         ('hardcover-similar', 'tmdb-recommendation', 'tmdb-similar')]
            return (0 if neighbors else 1, neighbors[0] if neighbors else 0,
                    result['fact_hops'], len(result['path']), result['key'])

        candidates.sort(key=order)
        members, seen = [], set()
        for result in candidates:
            key = result['key']
            if (key in seen or key in effective
                    or split_identity(key)[1] not in ('book', 'movie', 'tv', 'work', 'edition')):
                continue
            members.append(dict(deepcopy(nodes[key]), **{k: v for k, v in result.items() if k != 'key'}))
            seen.add(key)
            if len(members) == MAX_MEMBERS:
                break
        lanes.append({'id': 'seed:' + root, 'root': deepcopy(event['card']), 'choice': event['choice'],
                      'signal': 'experienced' if event['choice'] == 'enjoyed' else 'anticipated',
                      'provenance': event['provenance'], 'responded_at': event['responded_at'],
                      'members': members, 'path_budget_reached': path_budget_reached})
    memberships = {}
    for lane in lanes:
        for member in lane['members']:
            memberships.setdefault(member['key'], []).append({'lane_id': lane['id'], 'path': deepcopy(member['path'])})
    selection, seen = [], set()
    for offset in range(MAX_MEMBERS):
        for lane in lanes:
            if len(selection) >= size:
                break
            if offset >= len(lane['members']):
                continue
            member = lane['members'][offset]
            if member['key'] not in seen:
                selection.append(dict(deepcopy(member), memberships=memberships[member['key']]))
                seen.add(member['key'])
        if len(selection) >= size:
            break
    return {'schema': 'cross-media-ranking-v1', 'status': 'ready' if roots else 'needs-feedback',
            'exploration': 'Broad exploration; an explicit human positive enables seed lanes.' if not roots else None,
            'context': {'actor_id': actor_id, 'profile_id': profile_id, 'run_id': run_id, 'revision': expected_revision},
            'lanes': lanes, 'selection': selection, 'effective_history': deepcopy(list(effective.values())),
            'limits': {'lanes': MAX_LANES, 'members_per_lane': MAX_MEMBERS, 'selection': size,
                       'neighbor_hops': 1, 'fact_hops': 2}}
