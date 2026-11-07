"""Unsaved synthetic FeedbackEvent instances; SimpleTestCase forbids DB reads."""
from datetime import datetime
from uuid import UUID

from django.test import SimpleTestCase

from shelf.cross_media_feedback import from_feedback_events
from shelf.cross_media_ranking import ContextMismatch
from shelf.feedback import FeedbackError
from shelf.models import FeedbackEvent
from shelf.tests.test_cross_media_feedback import CONTEXT, WHEN, card, fact, ranked, selected

TARGET = UUID('00000000-0000-4000-8000-000000000001')


def event(revision, *, signal='enjoyment', value=5, operation='set', **kwargs):
    body = {'value': value, 'basis': 'experienced', 'scale': 'integer-1-5'} if signal == 'enjoyment' else value
    if operation != 'set':
        body = None
    return FeedbackEvent(id=UUID(int=revision + 100), target_id=TARGET,
        profile_id=CONTEXT['profile_id'], actor_id=CONTEXT['actor_id'], signal=signal,
        scope='item', revision=revision, operation=operation, body=body,
        origin='direct', original_origin='direct', recorded_at=datetime.fromisoformat(WHEN), **kwargs)


def adapt(events, key, *, signal='enjoyment', attended=False, revision=None):
    return from_feedback_events(events, **CONTEXT, target_id=TARGET, signal=signal, scope='item',
        key=key, expected_revision=revision if revision is not None else len(events), attended=attended)


class TypedFeedbackAdapterTests(SimpleTestCase):
    def setUp(self):
        self.artist = card('artist', 'synthetic-artist')
        self.music = card('recording', 'synthetic-recording')
        self.cards = [self.artist, self.music]
        self.edges = [fact(self.music, self.artist, 'recording-artist')]

    def test_numeric_enjoyment_thresholds_keep_neutral_and_negatives_separate(self):
        for value, response in ((1, 'not_for_me'), (2, 'not_for_me'), (3, 'neutral'), (4, 'enjoyed'), (5, 'enjoyed')):
            with self.subTest(value=value):
                snapshot = adapt([event(1, value=value)], self.artist.key)
                self.assertEqual(snapshot.reactions[0].response, response)
                self.assertEqual(snapshot.reactions[0].original_value, value)
                rows = selected(ranked(self.cards, self.edges, [snapshot]))
                self.assertEqual(self.music.key in rows, value >= 4)

    def test_correction_reverse_clear_and_erase_reconstruct_without_queries(self):
        positive = event(1)
        negative = event(2, value=1, corrects_id=positive.pk)
        snapshot = adapt([positive, negative], self.artist.key)
        self.assertEqual(ranked(self.cards, self.edges, [snapshot])['lanes'], [])
        reverse = event(3, value=None, operation='reverse', reverses_id=negative.pk)
        restored = adapt([reverse, positive, negative], self.artist.key)
        self.assertEqual(restored.revision, 3)
        self.assertEqual(restored.reactions[0].revision, 1)
        self.assertEqual(restored.reactions[0].event_id, str(positive.pk))
        self.assertEqual(restored.reactions[0].responded_at, WHEN)
        original_rows = ranked(self.cards, self.edges, [adapt([positive], self.artist.key)])['selection']
        self.assertEqual(ranked(self.cards, self.edges, [restored])['selection'], original_rows)
        clear = event(4)
        clear.body = None
        self.assertEqual(adapt([positive, negative, reverse, clear], self.artist.key).reactions, ())
        erase = event(5, operation='erase', erased=True)
        self.assertEqual(adapt([positive, negative, reverse, clear, erase], self.artist.key).reactions, ())

    def test_reverse_does_not_reveal_imported_or_source_redacted_enjoyment(self):
        imported = event(1)
        imported.origin = 'imported_statement'
        direct = event(2, corrects_id=imported.pk)
        reverse = event(3, operation='reverse', reverses_id=direct.pk)
        self.assertEqual(adapt([imported], self.artist.key).reactions, ())
        self.assertEqual(adapt([imported, direct, reverse], self.artist.key).reactions, ())
        imported.body = None
        self.assertEqual(adapt([imported, direct, reverse], self.artist.key).reactions, ())

    def test_actions_familiarity_completion_and_interest_never_mean_enjoyment(self):
        cases = [('saved', {'value': True}, 'saved'), ('skipped', {'value': True}, 'skipped'),
                 ('opened', {'value': True}, 'opened'), ('saved', {'value': False}, 'unsaved'),
                 ('interest', {'value': 'yes', 'basis': 'anticipated'}, 'interested'),
                 ('interest', {'value': 'maybe', 'basis': 'anticipated'}, 'interested'),
                 ('interest', {'value': 'no', 'basis': 'anticipated'}, 'uninterested'),
                 ('familiarity', {'value': 'unknown'}, 'unknown'),
                 ('completion', {'value': 'completed'}, 'completed')]
        for signal, value, response in cases:
            with self.subTest(signal=signal, value=value):
                snapshot = adapt([event(1, signal=signal, value=value)], self.artist.key, signal=signal)
                self.assertEqual(snapshot.reactions[0].response, response)
                self.assertEqual(ranked(self.cards, self.edges, [snapshot])['lanes'], [])

    def test_constraint_text_cannot_generate_features_or_dietary_preferences(self):
        snapshot = adapt([event(1, signal='constraint', value={'text':'Synthetic food-themed art note'})],
                         self.artist.key, signal='constraint')
        self.assertEqual(snapshot.reactions, ())
        self.assertEqual(snapshot.revision, 1)
        self.assertEqual(ranked(self.cards, self.edges, [snapshot])['selection'], [])

    def test_attendance_required_separately_for_typed_event_enjoyment(self):
        past = card('event', 'past', starts_at=WHEN)
        cards = [past, self.artist]
        edges = [fact(past, self.artist, 'lineup')]
        self.assertEqual(ranked(cards, edges, [adapt([event(1)], past.key)])['selection'], [])
        self.assertIn(self.artist.key, selected(ranked(cards, edges,
                           [adapt([event(1)], past.key, attended=True)])))

    def test_stale_incomplete_or_cross_profile_stream_refused(self):
        for events, revision in (([event(1)], 2), ([event(2)], 2)):
            with self.assertRaises(ContextMismatch):
                adapt(events, self.artist.key, revision=revision)
        wrong = event(1)
        wrong.profile_id += 1
        with self.assertRaises(ContextMismatch):
            adapt([wrong], self.artist.key)
        self.assertEqual(adapt([], self.artist.key, revision=0).revision, 0)

    def test_existing_body_validator_rejects_anticipated_enjoyment(self):
        wrong = event(1)
        wrong.body['basis'] = 'anticipated'
        with self.assertRaises(FeedbackError):
            adapt([wrong], self.artist.key)
