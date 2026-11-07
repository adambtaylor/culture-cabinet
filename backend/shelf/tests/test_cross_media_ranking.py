"""Real durable event shapes replayed against synthetic, source-only snapshots."""
from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import patch

from shelf.cross_media import Cache, InvalidData, node
from shelf.cross_media_ranking import ContextMismatch, IMPORT_PROVENANCE, rank, replay
from shelf.tests.test_cross_media import edge, fixture, graph

WHEN = '2026-01-01T00:00:00+00:00'
CONTEXT = {'actor_id': 7001, 'profile_id': 7002, 'run_id': 'synthetic-run', 'expected_revision': 5}


def synthetic_run():
    return deepcopy(fixture()['synthetic']['run'])


def event(key, choice='enjoyed', operation='choose', *, origin='editorial'):
    provider = key.split(':')[0]
    item = node(key, 'SYNTHETIC human example', fetched_at=WHEN, provenance='synthetic',
                slug='synthetic-only' if provider == 'hardcover' else None)
    card = {k: item[k] for k in ('key', 'provider', 'medium', 'source_id', 'title', 'url', 'fetched_at')}
    card.update(lane='synthetic-test', origin=origin, root='')
    return {'card': card, 'choice': choice, 'operation': operation, 'responded_at': WHEN,
            'recorded_at': WHEN, 'provenance': 'authenticated-human-post'}


class ReplayTests(TestCase):
    def test_exact_durable_shape_latest_correction_and_undo_clears(self):
        run = synthetic_run()
        key = run['history'][0]['card']['key']
        correction = dict(deepcopy(run['history'][0]), operation='correct', choice='not_for_me')
        run['history'].append(correction)
        effective = replay(run, **CONTEXT)
        self.assertEqual(effective[key]['choice'], 'not_for_me')
        run['history'].append(dict(deepcopy(correction), operation='undo', choice=None))
        self.assertNotIn(key, replay(run, **CONTEXT))
        self.assertEqual(len(run['history']), 7)

    def test_imported_history_keeps_provenance_and_unknown_original_time(self):
        run = synthetic_run()
        imported = run['history'][0]
        imported.update(operation='import', responded_at=None, provenance=IMPORT_PROVENANCE)
        effective = replay(json.loads(json.dumps(run)), **CONTEXT)
        result = effective[imported['card']['key']]
        self.assertIsNone(result['responded_at'])
        self.assertEqual(result['provenance'], IMPORT_PROVENANCE)
        cache = Cache(fixture()['synthetic']['cache'])
        lane = rank(run, cache, **CONTEXT)['lanes'][0]
        self.assertEqual(lane['provenance'], IMPORT_PROVENANCE)
        self.assertIsNone(lane['responded_at'])

    def test_import_cannot_be_relabelled_or_fabricate_response_time(self):
        for changes in ({'operation': 'import', 'responded_at': None},
                        {'operation': 'import', 'provenance': IMPORT_PROVENANCE}):
            run = synthetic_run()
            run['history'][0].update(changes)
            with self.assertRaises(InvalidData):
                replay(run, **CONTEXT)

    def test_supplied_actor_profile_run_revision_and_archived_checks(self):
        for field, value in (('actor_id', 7009), ('profile_id', 7009), ('id', 'other-run'),
                             ('revision', 6), ('actor_id', True), ('revision', True), ('archived_at', WHEN)):
            run = synthetic_run()
            run[field] = value
            with self.subTest(field=field), self.assertRaises(ContextMismatch):
                replay(run, **CONTEXT)
        for field, value in (('actor_id', 7009), ('profile_id', 7009), ('run_id', 'other-run'), ('expected_revision', 4)):
            with self.subTest(field=field), self.assertRaises(ContextMismatch):
                replay(synthetic_run(), **dict(CONTEXT, **{field: value}))

    def test_card_schema_identity_url_and_timestamp_match_actual_durable_snapshot(self):
        for changes in ({'key': 'tmdb:tv:81001'}, {'source_id': True}, {'url': 'https://evil.test/private'},
                        {'fetched_at': None}, {'fetched_at': '2026-01-01'}, {'origin': 'inferred'}):
            run = synthetic_run()
            run['history'][0]['card'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(InvalidData):
                replay(run, **CONTEXT)

    def test_nonhuman_save_open_skip_and_exposure_are_not_durable_positives(self):
        for operation in ('save', 'open', 'skip', 'exposure', 'related-version'):
            run = synthetic_run()
            run['history'][0]['operation'] = operation
            with self.subTest(operation=operation), self.assertRaises(InvalidData):
                replay(run, **CONTEXT)
        run = synthetic_run()
        run['history'][0]['provenance'] = 'inferred'
        with self.assertRaises(InvalidData):
            replay(run, **CONTEXT)

    def test_history_bound_and_null_only_for_undo(self):
        run = synthetic_run()
        run['history'] *= 401
        with self.assertRaises(InvalidData):
            replay(run, **CONTEXT)
        for choice, operation in ((None, 'choose'), ('enjoyed', 'undo'), ('saved', 'correct')):
            run = synthetic_run()
            run['history'][0].update(choice=choice, operation=operation)
            with self.assertRaises(InvalidData):
                replay(run, **CONTEXT)


class RankingTests(TestCase):
    def cache(self):
        return Cache(fixture()['synthetic']['cache'])

    def test_all_five_lanes_provider_rank_round_robin_and_membership(self):
        result = rank(synthetic_run(), self.cache(), **CONTEXT)
        self.assertEqual(len(result['lanes']), 5)
        self.assertEqual([card['key'] for card in result['selection'][:5]], [f'tmdb:movie:{82001+i}' for i in range(5)])
        shared = result['selection'][5]
        self.assertEqual(shared['key'], 'tmdb:movie:83001')
        self.assertEqual(len(shared['memberships']), 5)
        for lane in result['lanes']:
            self.assertEqual([member['path'][0]['rank'] for member in lane['members']], [0, 1])
        self.assertEqual(len(result['selection']), len({c['key'] for c in result['selection']}))

    def test_sixth_root_is_not_sixth_lane(self):
        run = synthetic_run()
        run['history'].append(event('tmdb:movie:84001', 'interested'))
        self.assertEqual(len(rank(run, self.cache(), **CONTEXT)['lanes']), 5)

    def test_experienced_and_anticipated_are_distinct(self):
        result = rank(synthetic_run(), self.cache(), **CONTEXT)
        self.assertEqual([lane['signal'] for lane in result['lanes']], ['experienced']*3 + ['anticipated']*2)
        self.assertEqual([lane['choice'] for lane in result['lanes']], ['enjoyed']*3 + ['interested']*2)

    def test_neighbor_origin_actual_human_reaction_is_new_root(self):
        run = synthetic_run()
        root = run['history'][1]
        self.assertEqual(root['card']['origin'], 'neighbor')
        result = rank(run, self.cache(), **CONTEXT)
        self.assertIn(root['card']['key'], [lane['root']['key'] for lane in result['lanes']])

    def test_suggestions_never_become_roots_until_new_human_post(self):
        run = synthetic_run()
        run['history'] = run['history'][:1]
        data = self.cache().snapshot()
        data['nodes'].append(node('tmdb:movie:85001', 'SIMULATED second neighbor', provenance='simulated'))
        data['edges'].append(edge('tmdb:movie:82001', 'tmdb:movie:85001', 'tmdb-recommendation'))
        cache = Cache(data)
        first = rank(run, cache, **CONTEXT)
        self.assertNotIn('tmdb:movie:85001', [c['key'] for c in first['selection']])
        run['history'].append(event('tmdb:movie:82001', origin='neighbor'))
        second = rank(run, cache, **CONTEXT)
        self.assertEqual(len(second['lanes']), 2)
        self.assertIn('tmdb:movie:85001', [c['key'] for c in second['selection']])

    def test_cold_start_exposure_save_metadata_unknown_do_not_invent_feedback(self):
        run = synthetic_run()
        run['history'] = []
        run.update(saved=['tmdb:movie:81001'], opens=['tmdb:movie:81002'], exposed=['tmdb:movie:81003'])
        for history in ([], [event('tmdb:movie:81001', 'unknown')], [event('tmdb:movie:81001', 'not_for_me')]):
            run['history'] = history
            result = rank(run, self.cache(), **CONTEXT)
            self.assertEqual(result['status'], 'needs-feedback')
            self.assertEqual(result['lanes'], [])
            self.assertEqual(result['selection'], [])
            self.assertIn('Broad exploration', result['exploration'])

    def test_correction_negative_undo_and_clear_recompute_after_restart(self):
        run = synthetic_run()
        run['history'] = run['history'][:1]
        original = deepcopy(run['history'][0])
        for correction in ('not_for_me', 'unknown', None):
            changed = deepcopy(run)
            changed['history'].append(dict(original, operation='undo' if correction is None else 'correct', choice=correction))
            result = rank(json.loads(json.dumps(changed)), Cache.from_json(self.cache().to_json()), **CONTEXT)
            self.assertEqual(result['status'], 'needs-feedback')
            self.assertEqual(result['lanes'], [])
        run['history'].extend([dict(original, operation='correct', choice='not_for_me'), dict(original, operation='correct', choice='interested')])
        self.assertEqual(rank(run, self.cache(), **CONTEXT)['lanes'][0]['signal'], 'anticipated')

    def test_judged_negative_and_unknown_candidates_not_recommended(self):
        run = synthetic_run()
        run['history'].append(event('tmdb:movie:83001', 'not_for_me'))
        run['history'].append(event('tmdb:movie:82001', 'unknown'))
        result = rank(run, self.cache(), **CONTEXT)
        keys = [c['key'] for c in result['selection']]
        self.assertNotIn('tmdb:movie:83001', keys)
        self.assertNotIn('tmdb:movie:82001', keys)
        self.assertEqual(len(result['lanes']), 5)

    def test_no_mutation_or_io_and_restart_is_deterministic(self):
        run, cache = synthetic_run(), self.cache()
        original_run, original_cache = deepcopy(run), cache.to_json()
        with patch('builtins.open', side_effect=AssertionError('no file I/O')), \
             patch('socket.socket', side_effect=AssertionError('no network')):
            first = rank(run, cache, **CONTEXT)
            second = rank(json.loads(json.dumps(run)), Cache.from_json(cache.to_json()), **CONTEXT)
        self.assertEqual(first, second)
        self.assertEqual(run, original_run)
        self.assertEqual(cache.to_json(), original_cache)
        first['lanes'][0]['root']['title'] = 'mutated output'
        self.assertEqual(rank(run, cache, **CONTEXT), second)

    def test_source_cache_is_independent_of_profile_and_revision(self):
        cache = Cache.from_json(self.cache().to_json())
        run = synthetic_run()
        original = rank(run, cache, **CONTEXT)
        run.update(profile_id=7999, revision=6)
        moved = rank(run, cache, **dict(CONTEXT, profile_id=7999, expected_revision=6))
        self.assertEqual(original['selection'], moved['selection'])
        with self.assertRaises(ContextMismatch):
            rank(run, cache, **CONTEXT)
        self.assertNotIn('actor_id', cache.to_json())

    def test_size_limits_and_empty_source_pool(self):
        self.assertEqual(rank(synthetic_run(), self.cache(), **CONTEXT, size=0)['selection'], [])
        self.assertEqual(len(rank(synthetic_run(), self.cache(), **CONTEXT, size=3)['selection']), 3)
        for size in (-1, 51, True):
            with self.assertRaises(InvalidData):
                rank(synthetic_run(), self.cache(), **CONTEXT, size=size)
        result = rank(synthetic_run(), Cache(), **CONTEXT)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['selection'], [])
        self.assertEqual(len(result['lanes']), 5)

    def test_exact_explanation_path_mixed_neighbor_and_facts(self):
        run = synthetic_run()
        run['history'] = run['history'][:1]
        keys = ['tmdb:movie:81001', 'tmdb:movie:86001', 'wikidata:entity:Q990000001',
                'wikidata:entity:Q990000002', 'tmdb:tv:86002', 'tmdb:tv:86003', 'tmdb:tv:86004']
        cache = graph(keys, [edge(keys[0], keys[1], 'tmdb-recommendation'), edge(keys[1], keys[2], 'identity'),
                            edge(keys[2], keys[3]), edge(keys[3], keys[4], 'identity'),
                            edge(keys[4], keys[5]), edge(keys[5], keys[6])])
        selected = rank(run, cache, **CONTEXT)['selection']
        by_key = {c['key']: c for c in selected}
        self.assertEqual(by_key[keys[5]]['fact_hops'], 2)
        self.assertEqual(by_key[keys[5]]['neighbor_hops'], 1)
        self.assertNotIn(keys[6], by_key)
        self.assertEqual([step['kind'] for step in by_key[keys[5]]['path']],
                         ['tmdb-recommendation', 'identity', 'based-on', 'identity', 'based-on'])

    def test_ol_author_is_never_a_book_suggestion(self):
        run = synthetic_run()
        run['history'] = run['history'][:1]
        keys = ['tmdb:movie:81001', 'wikidata:entity:Q990000001', 'openlibrary:author:OL990000001A',
                'openlibrary:work:OL990000001W', 'openlibrary:edition:OL990000001M']
        cache = graph(keys, [edge(keys[0], keys[1], 'identity'), edge(keys[1], keys[2], 'identity'),
                            edge(keys[1], keys[3], 'identity'), edge(keys[4], keys[3], 'edition-of')])
        selected = rank(run, cache, **CONTEXT)['selection']
        self.assertNotIn(keys[2], [c['key'] for c in selected])
        self.assertEqual({c['medium'] for c in selected}, {'work', 'edition'})
