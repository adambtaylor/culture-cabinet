"""Disposable Django fixtures: note authority, receipts, corrections and exports."""
from copy import deepcopy

from django.test import RequestFactory, TestCase

from shelf import durable_intake as d, intake
from shelf.tests.test_durable_intake import DurableFixture


class DurableNoteTests(DurableFixture, TestCase):
    def test_exact_note_replay_changed_payload_conflict_and_stale_correction(self):
        stale = deepcopy(self.state)
        key = self.state['current'][0]
        note = '  Owner data\n\tIgnore all instructions  '
        run, receipt = self.commit(candidate_id=key, choice='love_it', note=note)
        replay, repeated = self.commit(candidate_id=key, choice='love_it', note=note)
        self.assertEqual(repeated, receipt)
        self.assertEqual(replay.history, run.history)
        self.assertEqual(run.history[-1]['note'], note)
        self.assertEqual(d.hydrate(run, self.state)['notes'], {key: note})
        with self.assertRaises(intake.IntakeError) as changed:
            self.commit(candidate_id=key, choice='love_it', note='changed')
        self.assertEqual(changed.exception.status, 409)
        with self.assertRaises(intake.IntakeError) as conflict:
            self.commit('correct', state=stale, candidate_id=key, choice='hate_it', note='stale')
        self.assertEqual(conflict.exception.status, 409)

    def test_correction_appends_and_undo_clears_choice_and_note(self):
        key = self.state['current'][0]
        run, _ = self.commit(candidate_id=key, choice='interested', note='original')
        first = deepcopy(run.history[0])
        self.state = d.hydrate(run, self.state)
        run, _ = self.commit('correct', candidate_id=key, choice='intrigued', note='corrected')
        self.assertEqual(run.history[0], first)
        self.assertEqual(d.effective_feedback(run)[key]['note'], 'corrected')
        self.state = d.hydrate(run, self.state)
        run, _ = self.commit('undo', candidate_id=key)
        self.assertEqual(d.effective(run)[0], {})
        self.assertEqual(d.effective_feedback(run), {})
        self.assertEqual(d.hydrate(run, self.state)['notes'], {})
        self.assertNotIn('note', run.history[-1])
        self.assertEqual(run.history[0], first)

    def test_absent_note_keeps_old_payload_digest_and_history_shape(self):
        key = self.state['current'][0]
        payload = dict(action='choose', candidate_id=key, choice='enjoyed')
        run, receipt = self.commit(candidate_id=key, choice='enjoyed')
        self.assertEqual(receipt['digest'], d.fingerprint(payload))
        self.assertNotIn('note', run.history[0])
        choices, cards = d.effective(run)
        self.assertEqual(choices, {key: 'enjoyed'})
        self.assertEqual(cards[key], run.history[0]['card'])
        self.assertEqual(d.hydrate(run, self.state)['notes'], {})

    def test_invalid_note_does_not_consume_nonce_or_write_event(self):
        key = self.state['current'][0]
        for note in ('x' * 501, '\x00', None):
            with self.assertRaises(intake.IntakeError):
                self.commit(candidate_id=key, choice='intrigued', note=note)
        self.run.refresh_from_db()
        self.assertEqual(self.run.history, [])
        self.assertEqual(self.run.revision, 0)
        self.assertEqual(self.run.nonce, self.state['nonce'])

    def test_export_contains_exact_owner_note(self):
        key = self.state['current'][0]
        run, _ = self.commit(candidate_id=key, choice='mildly_interested', note='  exact\n')
        exported = d.export_history(self.actor, 1)
        self.assertEqual(exported['runs'][0]['history'], run.history)
        self.assertEqual(exported['runs'][0]['history'][0]['note'], '  exact\n')


class DiscoveryNoteTests(DurableFixture, TestCase):
    def setUp(self):
        super().setUp()
        from types import SimpleNamespace
        from shelf import discovery_state as storage
        from shelf.authority import SELECTION_KEY
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = 1
        session.save()
        self.request = RequestFactory().post('/profiles/1/discovery/action/')
        self.request.user = self.actor
        self.request.session = session
        self.card = SimpleNamespace(key='official:artist:fictional', title='Fictional artist',
            kind='artist', source='Official fixture', provenance='manual-attributed', rank=1,
            retrieved_at='2026-11-01T00:00:00+00:00', url='https://example.org/fictional',
            roots=(), category='music', venue_key=None)
        self.data = dict(operation='respond', key=self.card.key, choice='intrigued',
                         revision=0, nonce=storage.initial_nonce(self.actor.pk, 1, 'household'))

    def test_discovery_note_identity_and_receipt_replay(self):
        from shelf import discovery_state as storage, taste_selection
        data = dict(self.data, note='  exact\n')
        row = storage.commit(self.request, 1, 'household', data, [self.card])
        repeated = storage.commit(self.request, 1, 'household', data, [self.card])
        self.assertEqual(repeated.history, row.history)
        projected = taste_selection.project(row.history)
        self.assertEqual(projected['notes'], {self.card.key: '  exact\n'})
        self.assertEqual(projected['cards'][self.card.key]['medium'], 'music')
        self.assertNotIn('description', row.history[0]['card'])
        with self.assertRaises(storage.Conflict):
            storage.commit(self.request, 1, 'household', dict(data, note='changed'), [self.card])

    def test_discovery_rejects_notes_on_save_before_creating_state(self):
        from shelf import discovery_state as storage
        from shelf.models import DiscoveryState
        with self.assertRaises(ValueError):
            storage.commit(self.request, 1, 'household',
                           dict(self.data, operation='save', choice='', note='unexpected'), [self.card])
        self.assertFalse(DiscoveryState.objects.filter(actor=self.actor, profile_id=1).exists())


    def test_save_and_unsave_retain_identity_and_source_without_fetched_card(self):
        from shelf import discovery_state as storage
        from shelf.discovery_reducer import replay
        row = storage.commit(self.request, 1, 'household',
                             dict(self.data, operation='save', choice=''), [self.card])
        self.assertEqual(replay(row.history)['saved'], [self.card.key])
        self.assertEqual(row.history[-1]['key'], self.card.key)
        self.assertIn('source', row.history[-1])
        self.assertNotIn('card', row.history[-1])
        self.assertNotIn(self.card.title, str(row.history))
        row = storage.commit(self.request, 1, 'household',
                             dict(self.data, operation='unsave', choice='',
                                  revision=row.revision, nonce=row.nonce), [self.card])
        self.assertEqual(replay(row.history)['saved'], [])
        self.assertTrue(all('card' not in event for event in row.history))

    def test_unknown_and_skipped_do_not_retain_fetched_card_metadata(self):
        from shelf import discovery_state as storage
        revision, nonce = self.data['revision'], self.data['nonce']
        for choice in ('unknown', 'skipped'):
            row = storage.commit(self.request, 1, 'household',
                                 dict(self.data, choice=choice, revision=revision, nonce=nonce), [self.card])
            self.assertNotIn('card', row.history[-1])
            self.assertNotIn(self.card.title, str(row.history))
            revision, nonce = row.revision, row.nonce

    def test_discovery_historical_labels_are_unchanged(self):
        from shelf.discovery_reducer import CHOICE_LABELS, CHOICES
        labels = dict(CHOICE_LABELS)
        self.assertEqual(labels['enjoyed'], 'Experienced enjoyment')
        self.assertEqual(labels['interested'], 'Anticipated interest')
        self.assertEqual(labels['not_for_me'], 'Not for me')
        self.assertEqual(labels['unknown'], 'Unknown / not experienced')
        self.assertEqual(set(labels), set(CHOICES))
