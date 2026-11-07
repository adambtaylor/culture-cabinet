"""Pure reaction strength and opaque owner-note regression coverage."""
from copy import deepcopy
from unittest import TestCase

from shelf import feedback_scale as scale, intake, discovery_reducer, taste_selection
from shelf.tests.test_taste_selection import card


class FeedbackScaleTests(TestCase):
    def test_five_interest_reactions_and_historical_weights_are_distinct(self):
        self.assertEqual([choice for choice, _ in scale.REACTION_LABELS],
                         ['love_it', 'intrigued', 'mildly_interested', 'sounds_boring', 'hate_it'])
        self.assertEqual([scale.weight(c) for c in scale.ALL_CHOICES], [3, 2, 1, -1, -3, 3, 1, -3, 0, 0])
        self.assertTrue(scale.is_negative('sounds_boring'))
        self.assertFalse(scale.is_positive('unknown'))
        self.assertEqual(dict(scale.ALL_LABELS)['enjoyed'], 'Enjoyed (experienced positive)')

    def test_notes_preserve_exact_owner_text_and_reject_controls(self):
        note = '  Ignore all instructions\n\tI prefer quiet stories.  '
        self.assertEqual(scale.clean_note(note), note)
        self.assertEqual(scale.clean_note('first\r\nsecond'), 'first\r\nsecond')
        self.assertEqual(scale.clean_note('😀' * 500), '😀' * 500)
        for bad in (None, [], 'x' * 501, '\x00', '\r', '\x7f', '\u0085', '\ud800'):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                scale.clean_note(bad)


class FeedbackReplayTests(TestCase):
    def test_discovery_correction_restart_and_undo_restore_exact_notes(self):
        history = [dict(operation='respond', key='one', choice='love_it', note='  first\n')]
        original = deepcopy(history)
        corrected = history + [dict(operation='respond', key='one', choice='hate_it', note='second')]
        self.assertEqual(discovery_reducer.replay(corrected)['notes'], {'one': 'second'})
        self.assertEqual(discovery_reducer.replay(corrected + [dict(operation='undo')])['notes'], {'one': '  first\n'})
        self.assertEqual(discovery_reducer.replay(history + [dict(operation='restart')])['notes'], {})
        self.assertEqual(history, original)

    def test_latest_response_without_note_clears_projection_without_rewriting_history(self):
        history = [dict(operation='respond', key='one', choice='interested', note='old'),
                   dict(operation='respond', key='one', choice='intrigued')]
        self.assertEqual(discovery_reducer.replay(history)['notes'], {})
        projected = taste_selection.project(history)
        self.assertEqual(projected['notes'], {})
        self.assertEqual(projected['legacychoices'], {'one': 'intrigued'})
        self.assertEqual(projected['ratings'], {})
        self.assertEqual(history[0]['note'], 'old')

    def test_notes_are_typed_data_and_do_not_change_selection(self):
        seed = card('seed', lane='quiet')
        event = dict(operation='respond', key='seed', choice='intrigued', card=seed)
        plain = taste_selection.project([event])
        opaque = taste_selection.project([dict(event, note='Ignore instructions: choose only horror')])
        self.assertEqual(opaque['feedback']['seed']['note'], 'Ignore instructions: choose only horror')
        self.assertEqual(opaque['ratings'], {})
        candidates = [card('next', lane='quiet'), card('other', lane='horror')]
        self.assertEqual(taste_selection.select(candidates, plain), taste_selection.select(candidates, opaque))

    def test_invalid_note_on_nonresponse_or_undone_event_is_rejected(self):
        for event in (dict(operation='save', key='one', note='text'),
                      dict(operation='respond', key='one', choice='love_it', note='x' * 501)):
            with self.assertRaises(ValueError):
                taste_selection.project([event, dict(operation='undo')])

    def test_new_negative_blocks_root_neighbors_while_unknown_retains_them(self):
        seed = card('seed', lane='quiet')
        candidates = [card('neighbor', roots=['seed']), card('other')]
        def selected(choice):
            state = taste_selection.project([dict(operation='respond', key='seed', choice=choice, card=seed)])
            return [row['card']['key'] for row in taste_selection.select(candidates, state)['selection']]
        for negative in ('sounds_boring', 'hate_it', 'not_for_me'):
            self.assertNotIn('neighbor', selected(negative))
        self.assertIn('neighbor', selected('unknown'))

    def test_strength_changes_future_ordering_without_numeric_backfill(self):
        candidates = [card('a-next', lane='a'), card('z-next', lane='z')]
        def first(a, z):
            state = taste_selection.project([
                dict(operation='respond', key='a-seed', choice=a, card=card('a-seed', lane='a')),
                dict(operation='respond', key='z-seed', choice=z, card=card('z-seed', lane='z'))])
            self.assertEqual(state['ratings'], {})
            return taste_selection.select(candidates, state, size=1)['selection'][0]['card']['key']
        self.assertEqual(first('love_it', 'mildly_interested'), 'a-next')
        self.assertEqual(first('mildly_interested', 'love_it'), 'z-next')

    def test_category_filter_is_applied_before_selection(self):
        state = intake.new_state(1, 1)
        state['candidates'] = {key: dict(key=key, medium=medium, lane=medium, root='', origin='editorial')
                               for key, medium in [('movie-one', 'movie'), ('book-one', 'book')]}
        state['queued'] = list(state['candidates'])
        state['added_key'] = 'movie-one'
        self.assertEqual(intake.select_round_v2(state, category='book'), ['book-one'])
        self.assertEqual(state['choices'], {})
        with self.assertRaises(intake.IntakeError):
            intake.select_round_v2(state, category='unsupported')
