"""Deterministic fixtures: titles are fictional; no source or model calls."""
from unittest import TestCase
from shelf import intake
from shelf.provider_catalogue import Candidate


def fixture_cards():
    cards = []
    for lane, medium in (('a', 'movie'), ('b', 'movie'), ('c', 'tv'), ('d', 'book')):
        for i in range(12):
            cards.append(Candidate(f'{lane}{i:02}', 'hardcover' if medium == 'book' else 'tmdb',
                i+1, medium, f'Fiction {lane}{i}', '', 'https://www.themoviedb.org/movie/1',
                '2026-01-01T00:00:00+00:00', lane, 'Public editorial lane'))
    return cards


class IntakeTests(TestCase):
    def state(self):
        state = intake.new_state(1, 1, now=100)
        intake.merge(state, fixture_cards())
        intake.select_round(state)
        return state

    def test_deterministic_diverse_round_and_narrowing(self):
        state = self.state()
        self.assertEqual(state['current'], self.state()['current'])
        self.assertEqual({state['candidates'][k]['medium'] for k in state['current']}, {'movie', 'tv', 'book'})
        intake.choose(state, 'a00', 'enjoyed')
        self.assertEqual(intake.lane_weights(state)['a'], 3)
        self.assertGreater(sum(state['candidates'][k]['lane'] == 'a' for k in state['current']), 4)
        self.assertGreaterEqual(len({state['candidates'][k]['lane'] for k in state['current']}), 2)
        self.assertTrue(any('Exploration slot' in reason for reason in state['reasons'].values()))

    def test_unknown_neutral_and_interested_distinct_from_enjoyed(self):
        state = self.state()
        intake.choose(state, 'a00', 'unknown')
        self.assertEqual(dict(intake.lane_weights(state)), {'a': 0})
        self.assertEqual(intake.positive_roots(state), [])
        intake.choose(state, 'a01', 'interested')
        self.assertEqual(intake.lane_weights(state)['a'], 1)
        self.assertEqual(intake.summary(state)['enjoyed'], 0)
        self.assertEqual(intake.summary(state)['interested'], 1)

    def test_two_lanes_preserved_even_with_one_medium_and_equal_weights(self):
        state = intake.new_state(1, 1)
        intake.merge(state, [c for c in fixture_cards() if c.medium == 'movie'])
        intake.select_round(state)
        self.assertEqual({state['candidates'][k]['lane'] for k in state['current']}, {'a', 'b'})

    def test_multiple_positive_lanes_preserved_and_negative_avoids_neighbor_root(self):
        state = self.state()
        intake.choose(state, 'a00', 'enjoyed')
        key = next(k for k in state['current'] if state['candidates'][k]['lane'] == 'c')
        intake.choose(state, key, 'interested')
        self.assertEqual({c['lane'] for c in intake.positive_roots(state)}, {'a', 'c'})
        self.assertTrue({'a', 'c'}.issubset({state['candidates'][k]['lane'] for k in state['current']}))
        root = state['current'][0]
        card = Candidate('neighbor', 'tmdb', 99, 'movie', 'Fiction neighbor', '', '', '',
                         'root:'+root, 'Neighbors', 'neighbor', root)
        intake.merge(state, [card])
        intake.choose(state, root, 'not_for_me')
        self.assertNotIn('neighbor', state['current'])

    def test_no_machine_roots_or_implicit_likes(self):
        state = self.state()
        intake.merge(state, [Candidate('n', 'tmdb', 99, 'movie', 'Fiction', '', '', '',
                                      'root:a00', 'Neighbors', 'neighbor', 'a00')])
        self.assertEqual(intake.summary(state), dict.fromkeys(intake.CHOICES, 0))
        state['current'] = ['n']
        intake.choose(state, 'n', 'enjoyed')
        self.assertEqual(intake.positive_roots(state), [])
        self.assertNotIn('a00', state['choices'])

    def test_distinct_neighbors_of_two_roots_in_one_editorial_lane_survive(self):
        state = self.state()
        intake.choose(state, 'a00', 'enjoyed')
        intake.choose(state, 'a01', 'enjoyed')
        for root in ('a00', 'a01'):
            intake.merge(state, [Candidate(root+'neighbor', 'tmdb', 99, 'movie',
                'Fiction', '', '', '', 'root:'+root, 'Neighbors', 'neighbor', root)])
        intake.select_round(state)
        self.assertIn('a00neighbor', state['current'])
        self.assertIn('a01neighbor', state['current'])

    def test_stale_replay_and_noncurrent_choice(self):
        state = self.state()
        nonce = state['nonce']
        intake.check_revision(state, '0', nonce)
        intake.advance(state)
        with self.assertRaises(intake.IntakeError) as caught:
            intake.check_revision(state, '0', nonce)
        self.assertEqual(caught.exception.status, 409)
        for key, choice in (('unknown-id', 'enjoyed'), (state['current'][0], 'machine_guess')):
            with self.assertRaises(intake.IntakeError):
                intake.choose(state, key, choice)

    def test_expiry_actor_profile_and_no_sliding_ttl(self):
        for actor, profile, now in ((2, 1, 101), (1, 2, 101), (1, 1, 3700)):
            session = {intake.SESSION_KEY: self.state()}
            self.assertIsNone(intake.load(session, actor, profile, now=now))
            self.assertNotIn(intake.SESSION_KEY, session)
        state = self.state()
        expiry = state['expires_at']
        intake.advance(state)
        self.assertEqual(state['expires_at'], expiry)

    def test_search_add_requires_existing_result_and_no_automatic_signal(self):
        state = self.state()
        state['search_results'] = ['d11']
        with self.assertRaises(intake.IntakeError):
            intake.add_search_result(state, 'invented')
        intake.add_search_result(state, 'd11')
        self.assertIn('d11', state['current'])
        self.assertEqual(state['choices'], {})

    def test_unselected_results_never_automatically_enter_later_rounds(self):
        state = intake.new_state(1, 1)
        cards = fixture_cards()[:3]
        intake.merge(state, cards, queue=False)
        state['search_results'] = [c.key for c in cards]
        intake.add_search_result(state, cards[0].key)
        # A later search replaces the displayed result list, not the human queue.
        state['search_results'] = []
        intake.select_round(state)
        self.assertEqual(state['current'], [cards[0].key])
