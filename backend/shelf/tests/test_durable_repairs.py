"""Offline regressions for A1 capacity and A2 ranked Hardcover hydration."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase, override_settings

from shelf import intake, durable_intake as d, provider_catalogue as p
from shelf.authority import SELECTION_KEY
from shelf.models import Profile, Grant


def card(n):
    return p.parse_candidates('tmdb', 'movie',
        [{'id': n, 'title': f'Fictional capacity probe {n}'}],
        lane='fixture', lane_label='Fictional lane')[0]


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class DurableCapacityRepairs(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='fictional-capacity-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Fictional capacity')
        Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.run = d.new_run(self.actor, 1)
        self.state = d.hydrate(self.run)

    def commit(self, action, **extra):
        self.run, _ = d.commit(actor=self.actor, profile_id=1, run_id=self.state['run_id'],
            revision=self.state['revision'], nonce=self.state['nonce'], state=self.state,
            payload={'action': action, **extra})
        self.state = d.hydrate(self.run, self.state)

    def respond_120(self, *, clear=False):
        intake.merge(self.state, [card(n) for n in range(1, 121)])
        self.state['started'] = True
        intake.select_round_v2(self.state)
        keys = []
        for _ in range(120):
            key = self.state['current'][0]
            keys.append(key)
            self.commit('choose', candidate_id=key, choice='unknown')
        if clear:
            for key in keys:
                self.commit('undo', candidate_id=key)

    def login_with_state(self):
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = 1
        session[intake.SESSION_KEY] = self.state
        session.save()

    def post(self, action, **extra):
        state = self.client.session[intake.SESSION_KEY]
        response = self.client.post('/profiles/1/intake/action/',
            {'action': action, 'revision': state['revision'], 'nonce': state['nonce'], **extra})
        self.assertEqual(response.status_code, 302)
        return self.client.session[intake.SESSION_KEY]

    def test_real_refresh_after_120_unknown_responses(self):
        # A's exact boundary: real durable commits, real refresh POST and GET.
        self.respond_120()
        history = deepcopy(self.run.history)
        self.login_with_state()
        with patch.object(p, 'initial_deck', return_value=([card(500)], [])) as fetch:
            state = self.post('refresh')
        fetch.assert_called_once()
        self.run.refresh_from_db()
        self.assertEqual(self.run.history, history)
        self.assertEqual(len(history), 120)
        response = self.client.get('/profiles/1/')
        self.assertIn('tmdb:movie:500', [c['key'] for c in response.context['cards']])
        self.assertEqual(set(state['candidates']) - set(state['history_only']), {'tmdb:movie:500'})
        self.assertEqual(len(state['choices']), 120)

    def test_cleared_history_and_unselected_search_results_obey_cache_bound(self):
        self.respond_120(clear=True)
        history = deepcopy(self.run.history)
        self.state = d.hydrate(self.run)
        self.assertEqual(self.state['candidates'], {})
        self.assertEqual(self.state['current'], [])
        self.login_with_state()
        # A fresh provider response for a cleared historical identity is valid.
        fresh = replace(card(1), title='Fictional newly fetched title', description='Fresh metadata')
        with patch.object(p, 'search', return_value=[fresh]):
            state = self.post('search', medium='movie', query='fictional')
        self.assertEqual(state['search_results'], [fresh.key])
        self.assertEqual(state['candidates'][fresh.key], fresh.data())
        self.assertEqual(state['current'], [])
        for batch in range(14):
            cards = [card(n) for n in range(500 + 9*batch, 509 + 9*batch)]
            with patch.object(p, 'search', return_value=cards) as search:
                state = self.post('search', medium='movie', query='fictional')
            search.assert_called_once()
            self.assertLessEqual(len(state['candidates']), 120)
        self.assertEqual(len(state['candidates']), 120)
        with patch.object(p, 'search', return_value=[card(999)]):
            state = self.post('search', medium='movie', query='fictional overflow')
        self.assertNotIn(card(999).key, state['candidates'])
        self.assertEqual(state['search_results'], [])
        self.assertEqual(state['current'], [])
        self.assertEqual(state['choices'], {})
        self.assertEqual(state['history_only'], [])
        self.run.refresh_from_db()
        self.assertEqual(self.run.history, history)
        self.assertEqual(len(history), 240)

    def test_historical_snapshot_replaced_by_fresh_metadata_and_clear_stays_out_of_deck(self):
        intake.merge(self.state, [card(1)])
        intake.select_round_v2(self.state)
        self.commit('choose', candidate_id=card(1).key, choice='enjoyed')
        history = deepcopy(self.run.history)
        self.state = d.hydrate(self.run)
        self.assertEqual(self.state['history_only'], [card(1).key])
        fresh = replace(card(1), title='Fictional refreshed root', description='New source metadata')
        intake.merge(self.state, [fresh])
        self.state = d.hydrate(self.run, self.state)
        self.assertEqual(self.state['history_only'], [])
        self.assertEqual(self.state['candidates'][fresh.key], fresh.data())
        self.assertEqual(intake.positive_roots(self.state)[0]['title'], fresh.title)
        self.assertEqual(self.run.history, history)
        # Without a fresh fetch, clearing must not queue the historical snapshot.
        self.state = d.hydrate(self.run)
        self.commit('undo', candidate_id=fresh.key)
        self.assertEqual(self.state['candidates'], {})
        self.assertEqual(self.state['queued'], [])
        self.assertEqual(self.state['current'], [])
        self.assertEqual(self.run.history[0], history[0])


class TemporaryCapacityRepairs(SimpleTestCase):
    def test_legacy_pool_retains_original_limit(self):
        state = intake.new_state(1, 1)
        intake.merge(state, [card(n) for n in range(1, 122)], queue=False)
        self.assertEqual(len(state['candidates']), 120)
        self.assertNotIn('tmdb:movie:121', state['candidates'])
        self.assertEqual(state['queued'], [])
        intake.merge(state, [replace(card(1), title='Replacement')])
        self.assertEqual(state['candidates'][card(1).key]['title'], card(1).title)


class HardcoverRankRepairs(SimpleTestCase):
    root = {'provider': 'hardcover', 'medium': 'book', 'source_id': 1,
            'key': 'hardcover:book:1', 'origin': 'editorial', 'title': 'Fictional root'}

    def rows(self, ids):
        return [{'id': n, 'title': f'Fictional neighbor {n}', 'slug': f'fictional-{n}'} for n in ids]

    def test_neighbors_shuffled_details_preserve_rank(self):
        with patch.object(p, '_request', side_effect=[
            {'books_by_pk': {'cached_similar_book_ids': [9, 2, 7]}},
            {'books': self.rows([2, 7, 9])}]) as request:
            cards = p.neighbors(self.root, actor_id=1, profile_id=1)
        self.assertEqual([c.source_id for c in cards], [9, 2, 7])
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(c.root == self.root['key'] and c.origin == 'neighbor' for c in cards))

    def test_missing_and_duplicate_details_keep_surviving_source_order(self):
        rows = self.rows([7, 9, 9])
        rows[-1]['title'] = 'Fictional duplicate must not replace first'
        with patch.object(p, '_request', side_effect=[
            {'books_by_pk': {'cached_similar_book_ids': [1, 9, 2, 9, 7]}},
            {'books': rows}]) as request:
            cards = p.neighbors(self.root, actor_id=1, profile_id=1)
        self.assertEqual([c.source_id for c in cards], [9, 7])
        self.assertEqual(cards[0].title, 'Fictional neighbor 9')
        self.assertEqual(request.call_args_list[1].args, ('book_details', [9, 2, 7]))

    def test_search_uses_same_ranked_hydration_without_more_calls(self):
        with patch.object(p, '_request', side_effect=[
            {'search': {'ids': [9, 2, 9, 7], 'error': None}},
            {'books': self.rows([7, 9])}]) as request:
            cards = p.search('book', 'Fictional title', actor_id=1, profile_id=1)
        self.assertEqual([c.source_id for c in cards], [9, 7])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].args, ('book_details', [9, 2, 7]))

    def test_ranked_details_still_require_valid_provider_metadata(self):
        for rows in ([{'id': True, 'title': 'Fictional', 'slug': 'fictional'}],
                     self.rows([88]), self.rows([9]) + [{'id': 9, 'title': '', 'slug': 'fictional'}]):
            with self.subTest(rows=rows), patch.object(p, '_request', side_effect=[
                {'books_by_pk': {'cached_similar_book_ids': [9, 2, 7]}}, {'books': rows}]):
                with self.assertRaises(p.CatalogueError):
                    p.neighbors(self.root, actor_id=1, profile_id=1)
