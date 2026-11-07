"""Synthetic human ratings and source cards; no stores, Django or network."""
from copy import deepcopy
from unittest import TestCase

from shelf.taste_selection import project, select

NOW = '2026-10-30T10:00:00+00:00'


def card(key, medium='movie', lane='explore', roots=()):
    return dict(key=key, title='Synthetic ' + key, medium=medium, source='Synthetic source',
                url='https://example.org/' + key, provenance='live-automated', lane=lane,
                roots=list(roots), fetched_at=NOW)


def action(operation, key='', **fields):
    return dict(operation=operation, key=key, choice='', mode='household',
                origin='authenticated-human-post', at=NOW, **fields)


def rate(item, value=4, basis='interest', experienced=False):
    return action('rate', item['key'], card=item, basis=basis, value=value, experienced=experienced)


def keys(result):
    return [row['card']['key'] for row in result['selection']]


class RatingProjectionTests(TestCase):
    def test_interest_and_experienced_enjoyment_are_separate_and_clear_is_local(self):
        item = card('one')
        history = [rate(item, 5), rate(item, 2, 'enjoyment', True), action('save', 'one')]
        state = project(history)
        self.assertEqual(state['ratings'], {'one': {'interest': 5, 'enjoyment': 2}})
        self.assertEqual(state['saved'], ['one'])
        cleared = project(history + [rate(item, None)])
        self.assertEqual(cleared['ratings'], {'one': {'enjoyment': 2}})
        self.assertEqual(cleared['saved'], ['one'])
        self.assertEqual(project(history + [rate(item, None), action('undo')]), state)

    def test_enjoyment_and_event_attendance_require_explicit_acknowledgement(self):
        for medium in ('movie', 'event'):
            item = card('one', medium)
            for value in (4, None):
                with self.assertRaises(ValueError):
                    project([rate(item, value, 'enjoyment')])
            self.assertEqual(project([rate(item, 4, 'enjoyment', True)])['ratings']['one'], {'enjoyment': 4})
        self.assertEqual(project([rate(card('interest'), 5)])['ratings']['interest'], {'interest': 5})

    def test_legacy_choices_remain_qualitative_without_invented_numeric_ratings(self):
        history = [dict(operation='respond', key='one', choice='enjoyed'),
                   dict(operation='respond', key='two', choice='unknown')]
        state = project(history)
        self.assertEqual(state['ratings'], {})
        self.assertEqual(state['legacychoices'], {'one': 'enjoyed', 'two': 'unknown'})
        self.assertEqual(state['cards'], {})
        self.assertEqual(keys(select([card('one'), card('two'), card('three')], state)), ['three'])

    def test_next_round_skip_and_exact_undo(self):
        item = card('one')
        before = project([action('funnel_skip', 'one', card=item)])
        history = [action('funnel_skip', 'one', card=item), action('funnel_next', shown=['two', 'three'])]
        after = project(history)
        self.assertEqual(after['round'], 1)
        self.assertEqual(after['seen'], ['two', 'three'])
        self.assertEqual(after['skipped'], [])
        self.assertEqual(project(history + [action('undo')]), before)
        self.assertEqual(project(history + [action('undo'), action('undo')]), project([]))

    def test_restart_and_undo_restore_the_whole_projection(self):
        history = [rate(card('one')), action('save', 'one'), action('funnel_next', shown=['two'])]
        before = project(history)
        cleared = project(history + [action('restart')])
        self.assertEqual({k: v for k, v in cleared.items() if k != 'can_undo'},
                         {k: v for k, v in project([]).items() if k != 'can_undo'})
        self.assertTrue(cleared['can_undo'])
        self.assertEqual(project(history + [action('restart'), action('undo')]), before)

    def test_favourites_and_skip_do_not_create_ratings_or_legacy_choices(self):
        item = card('one')
        state = project([action('save', 'one', card=item), action('funnel_skip', 'one', card=item)])
        self.assertEqual(state['ratings'], {})
        self.assertEqual(state['legacychoices'], {})
        self.assertEqual(state['saved'], ['one'])
        self.assertEqual(state['skipped'], ['one'])
        self.assertEqual(project([action('save', 'one'), action('unsave', 'one')])['saved'], [])

    def test_minimal_snapshots_and_input_immutability(self):
        item = dict(card('one'), description='Do not persist this', details={'address': 'Do not retain'})
        history = [rate(item)]
        original = deepcopy(history)
        projected = project(history)
        self.assertNotIn('description', projected['cards']['one'])
        self.assertNotIn('details', projected['cards']['one'])
        projected['cards']['one']['roots'].append('changed')
        self.assertEqual(history, original)

    def test_invalid_ratings_and_shapes_are_rejected_even_when_undone(self):
        valid = rate(card('one'))
        for changes in ({'value': True}, {'value': 0}, {'value': 6}, {'value': '4'},
                        {'basis': 'taste'}, {'experienced': 1}, {'mode': 'sample'},
                        {'origin': 'machine'}, {'at': '2026-01-01'}, {'choice': 'enjoyed'},
                        {'key': 'other'}, {'card': {}}, {'operation': 'fetch'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                project([dict(valid, **changes), action('undo')])
        for event in (action('funnel_next', shown=['one', 'one']), action('funnel_next', shown='one'),
                      action('funnel_next', 'one', shown=[]), action('undo', 'one')):
            with self.assertRaises(ValueError):
                project([event])


class RatingRoundTests(TestCase):
    def test_media_balance_and_two_positive_clusters_with_exploration(self):
        a, b = card('root-a', lane='a'), card('root-b', 'music', lane='b')
        state = project([rate(a, 5), rate(b, 4, 'enjoyment', True)])
        candidates = [card(f'a-{i}', lane='a', roots=['root-a']) for i in range(15)]
        candidates += [card(f'b-{i}', 'music', lane='b', roots=['root-b']) for i in range(10)]
        candidates += [card(f'x-{medium}-{i}', medium, lane=f'explore-{medium}')
                       for medium in ('movie', 'tv', 'book', 'music', 'event') for i in range(3)]
        result = select(candidates, state)
        selected = [r['card'] for r in result['selection']]
        self.assertEqual(len(selected), 12)
        self.assertEqual({c['medium'] for c in selected}, {'movie', 'tv', 'book', 'music', 'event'})
        self.assertTrue(any('root-a' in c['roots'] for c in selected))
        self.assertTrue(any('root-b' in c['roots'] for c in selected))
        self.assertGreaterEqual(sum(c['lane'].startswith('explore') for c in selected), 4)
        self.assertLessEqual(sum(c['medium'] == 'movie' for c in selected), 4)
        self.assertIn('interest 5/5', repr(result))
        self.assertIn('enjoyment 4/5', repr(result))

    def test_two_same_medium_roots_remain_distinct(self):
        a, b = card('root-a', lane='shared'), card('root-b', lane='shared')
        state = project([rate(a), rate(b)])
        cards = [card('a', lane='shared', roots=['root-a']), card('b', lane='shared', roots=['root-b'])]
        cards += [card(f'other-{i}', lane='other') for i in range(12)]
        self.assertTrue({'a', 'b'}.issubset(keys(select(cards, state))))

    def test_negative_and_neutral_items_do_not_penalize_their_lane(self):
        cards = [card('a', lane='same'), card('b', lane='other'), card('c', lane='same')]
        empty = select(cards, project([]))
        for rating in (1, 2, 3):
            state = project([rate(card('judged', lane='same'), rating)])
            self.assertEqual(select(cards, state), empty)

    def test_favourite_does_not_boost_or_exclude_a_card(self):
        cards = [card('a'), card('b'), card('c')]
        self.assertEqual(select(cards, project([action('save', 'c', card=cards[2])])),
                         select(cards, project([])))

    def test_judged_cards_are_excluded_and_clearing_makes_them_available(self):
        cards = [card('a'), card('b')]
        history = [rate(cards[0], 3)]
        self.assertEqual(keys(select(cards, project(history))), ['b'])
        self.assertEqual(set(keys(select(cards, project(history + [rate(cards[0], None)])))), {'a', 'b'})

    def test_skip_is_current_round_only_and_not_dislike(self):
        a, b = card('a'), card('b')
        history = [action('funnel_skip', 'a', card=a)]
        self.assertEqual(keys(select([a, b], project(history))), ['b'])
        state = project(history + [action('funnel_next', shown=['b'])])
        self.assertEqual(keys(select([a, b], state)), ['a'])
        self.assertEqual(state['ratings'], {})

    def test_previous_rounds_do_not_repeat_and_finite_pool_does_not_recycle(self):
        cards = [card(str(n)) for n in range(5)]
        history = []
        all_shown = []
        for expected in (2, 2, 1):
            result = select(cards, project(history), size=2)
            shown = keys(result)
            self.assertEqual(len(shown), expected)
            self.assertTrue(set(shown).isdisjoint(all_shown))
            all_shown += shown
            history.append(action('funnel_next', shown=shown))
        final = select(cards, project(history), size=2)
        self.assertTrue(final['exhausted'])
        self.assertEqual(final['selection'], [])
        self.assertEqual(final['round'], 3)
        self.assertEqual(final['coverage'], [])

    def test_identical_duplicates_collapse_and_conflicting_identity_fails(self):
        item = card('a')
        self.assertEqual(keys(select([item, deepcopy(item)], project([]))), ['a'])
        with self.assertRaises(ValueError):
            select([item, dict(item, title='Different source identity')], project([]))

    def test_deterministic_input_order_independent_and_no_mutation(self):
        cards = [card(str(n), medium=('movie', 'book', 'music')[n % 3]) for n in range(20)]
        state = project([rate(card('root', lane='explore'))])
        original = deepcopy((cards, state))
        self.assertEqual(select(cards, state), select(list(reversed(cards)), state))
        self.assertEqual((cards, state), original)

    def test_small_round_rotates_media_and_empty_pool_is_explicit(self):
        cards = [card(medium, medium) for medium in ('movie', 'tv', 'book', 'music', 'event')]
        seen_media = set()
        for round_number in range(5):
            state = dict(project([]), round=round_number)
            seen_media.add(select(cards, state, size=1)['selection'][0]['card']['medium'])
        self.assertEqual(len(seen_media), 5)
        self.assertTrue(select([], project([]))['exhausted'])

    def test_bounds_and_sample_cards_refused(self):
        for size in (0, 31, True):
            with self.assertRaises(ValueError):
                select([], project([]), size=size)
        for changes in ({'provenance': 'fixture'}, {'medium': 'podcast'}, {'roots': ['x', 'x']},
                        {'title': 'x' * 301}, {'fetched_at': 'yesterday'},
                        {'url': 'javascript:alert(1)'}, {'url': 'https://user:secret@example.org/x'}):
            with self.assertRaises(ValueError):
                select([dict(card('one'), **changes)], project([]))
