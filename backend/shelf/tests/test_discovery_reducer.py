from dataclasses import replace
from unittest import TestCase

from shelf.discovery_sources import Candidate, sample_candidates
from shelf.discovery_reducer import replay, select


def event(key='', choice='', operation='respond'):
    return {'key': key, 'choice': choice, 'operation': operation}


def populated_lanes(counts):
    seeds = [Candidate(f'fixture:artist:seed-{i}', f'Synthetic seed {i}',
                       'artist', 'Synthetic', 'fixture', 100, None)
             for i in range(len(counts))]
    cards = seeds + [
        Candidate(f'fixture:recording:lane-{i}-{j}', f'Synthetic recording {i}-{j}',
                  'recording', 'Synthetic', 'fixture', j, None, roots=(seed.key,))
        for i, (seed, count) in enumerate(zip(seeds, counts)) for j in range(count)
    ]
    cards += [Candidate(f'fixture:recording:explore-{i}', f'Synthetic exploration {i}',
                        'recording', 'Synthetic', 'fixture', i, None) for i in range(6)]
    return cards, [seed.key for seed in seeds]


class DiscoveryReducerTests(TestCase):
    def setUp(self):
        self.cards = sample_candidates()
        self.a, self.b, self.venue = [c.key for c in self.cards[:3]]

    def test_four_choices_and_save_are_separate(self):
        for choice in ('enjoyed', 'interested', 'not_for_me', 'unknown'):
            state = replay([event(self.a, choice), event(self.a, operation='save')])
            self.assertEqual(state['choices'], {self.a: choice})
            self.assertEqual(state['saved'], [self.a])
            self.assertEqual(len(select(self.cards, state)['lanes']), int(choice in ('enjoyed', 'interested')))
        self.assertEqual(replay([event(self.a, operation='save')])['choices'], {})

    def test_correction_undo_restart_replay(self):
        history = [event(self.a, 'enjoyed'), event(self.a, 'not_for_me')]
        self.assertEqual(replay(history)['choices'][self.a], 'not_for_me')
        history.append(event(operation='undo'))
        self.assertEqual(replay(history)['choices'][self.a], 'enjoyed')
        history.append(event(operation='restart'))
        self.assertEqual(replay(history)['choices'], {})
        history.append(event(operation='undo'))
        self.assertEqual(replay(history)['choices'][self.a], 'enjoyed')
        self.assertEqual(replay(history), replay(history.copy()))

    def test_multimembership_interleaving_and_exploration(self):
        state = replay([event(self.a, 'enjoyed'), event(self.b, 'interested')])
        ranked = select(self.cards, state)
        self.assertEqual(ranked, select(self.cards, state))
        self.assertEqual(len(ranked['lanes']), 2)
        self.assertTrue(any(len(row['memberships']) == 2 for row in ranked['selection']))
        self.assertTrue(any('Exploration' in reason for row in ranked['selection'] for reason in row['why']))
        self.assertEqual(len({r['card'].key for r in ranked['selection']}), len(ranked['selection']))
        self.assertEqual(state['choices'], {self.a: 'enjoyed', self.b: 'interested'})

    def assert_populated_lane_rotation(self, lane_count, expected_order):
        cards, roots = populated_lanes([15] * lane_count)
        state = replay([event(root, 'interested') for root in roots])
        ranked = select(cards, state)
        rows = ranked['selection']
        self.assertEqual([lane['count'] for lane in ranked['lanes']], [15] * lane_count)
        self.assertEqual(len(rows), 12)
        self.assertEqual([row['memberships'] for row in rows if row['memberships']],
                         [[roots[i]] for i in expected_order])
        self.assertEqual([i for i, row in enumerate(rows) if not row['memberships']],
                         [2, 5, 8, 11])
        self.assertEqual([rows[i]['card'].key for i in (2, 5, 8, 11)],
                         [f'fixture:recording:explore-{i}' for i in range(4)])
        self.assertTrue(all(any('Exploration' in reason for reason in rows[i]['why'])
                            for i in (2, 5, 8, 11)))
        self.assertEqual(len({row['card'].key for row in rows}), 12)
        self.assertEqual(ranked, select(cards, state))
        self.assertEqual(state['choices'], {root: 'interested' for root in roots})

    def test_three_populated_lanes_rotate_between_exploration_slots(self):
        self.assert_populated_lane_rotation(3, [0, 1, 2, 0, 1, 2, 0, 1])

    def test_four_populated_lanes_rotate_between_exploration_slots(self):
        self.assert_populated_lane_rotation(4, [0, 1, 2, 3, 0, 1, 2, 3])

    def test_five_populated_lanes_rotate_between_exploration_slots(self):
        self.assert_populated_lane_rotation(5, [0, 1, 2, 3, 4, 0, 1, 2])

    def test_unequal_empty_and_exhausted_lanes_keep_the_next_lane_turn(self):
        cards, roots = populated_lanes([1, 0, 3, 4, 1])
        state = replay([event(root, 'interested') for root in roots])
        ranked = select(cards, state, size=30)
        rows = ranked['selection']
        self.assertEqual([lane['count'] for lane in ranked['lanes']], [1, 0, 3, 4, 1])
        self.assertEqual([row['card'].key for row in rows if row['memberships']], [
            'fixture:recording:lane-0-0', 'fixture:recording:lane-2-0',
            'fixture:recording:lane-3-0', 'fixture:recording:lane-4-0',
            'fixture:recording:lane-2-1', 'fixture:recording:lane-3-1',
            'fixture:recording:lane-2-2', 'fixture:recording:lane-3-2',
            'fixture:recording:lane-3-3',
        ])
        self.assertEqual(len(rows), len(cards))
        self.assertEqual({row['card'].key for row in rows}, {card.key for card in cards})

    def test_shared_candidate_dedup_does_not_skip_the_next_nonempty_lane(self):
        cards, roots = populated_lanes([0, 0, 3])
        shared = Candidate('fixture:recording:shared', 'Synthetic shared recording',
                           'recording', 'Synthetic', 'fixture', 0, None, roots=tuple(roots))
        cards.append(shared)
        rows = select(cards, replay([event(root, 'interested') for root in roots]))['selection']
        self.assertEqual(rows[0]['card'], shared)
        self.assertEqual(rows[0]['memberships'], roots)
        self.assertEqual(len([reason for reason in rows[0]['why'] if reason.startswith('Seed:')]), 3)
        self.assertEqual(rows[1]['card'].key, 'fixture:recording:lane-2-0')
        self.assertEqual([row['card'].key for row in rows if row['memberships']],
                         [shared.key] + [f'fixture:recording:lane-2-{i}' for i in range(3)])
        self.assertEqual(len({row['card'].key for row in rows}), len(rows))

    def test_negative_hides_candidate_unknown_does_not_boost(self):
        negative = select(self.cards, replay([event(self.a, 'not_for_me')]))
        self.assertNotIn(self.a, [r['card'].key for r in negative['selection']])
        self.assertEqual(select(self.cards, replay([event(self.a, 'unknown')]))['lanes'], [])

    def test_venue_boost_does_not_exclude_other_sources(self):
        ranked = select(self.cards, replay([event(self.venue, 'interested')]))
        self.assertEqual(ranked['selection'][0]['card'].venue_key, self.venue)
        self.assertTrue(any(r['card'].venue_key != self.venue for r in ranked['selection']))

    def test_five_lane_cap_and_new_human_response_is_evidence(self):
        state = replay([event(c.key, 'interested') for c in self.cards[:8]])
        self.assertEqual(len(select(self.cards, state)['lanes']), 5)
        suggestion = self.cards[3]
        ranked = select(self.cards, replay([event(suggestion.key, 'enjoyed')]))
        self.assertEqual(ranked['lanes'][0]['key'], suggestion.key)

    def test_broad_filter_and_empty_are_not_coverage_claims(self):
        state = replay([])
        ranked = select(self.cards, state, category='regional_day_trip')
        events = [r['card'] for r in ranked['selection'] if r['card'].kind == 'event']
        self.assertEqual([c.category for c in events], ['regional_day_trip'])
        self.assertEqual(select([], state)['selection'], [])
