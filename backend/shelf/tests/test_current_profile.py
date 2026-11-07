"""Persisted current-state/typed-value regressions on the real recovery fixture."""
import uuid
from django.contrib.auth import get_user_model
from django.db import DatabaseError, transaction
from shelf import grant_authority as ga, profile_snapshot, disclosure
from shelf.current_profile import CurrentProfile
from shelf.feedback import MutationCommand, mutate
from shelf.models import ProfileCabinetState, FeedbackTarget, DiscoveryState, IntakeRun
from .test_grant_authority import RecoveryCase


class CurrentProfileTests(RecoveryCase):
    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.state = ProfileCabinetState.objects.get()
        self.assertEqual(ga.bind_existing_profile(self.authority, self.ref,
            profile_id=self.state.profile_id, confirmed=True), self.profile_ref)
        self.state.refresh_from_db()
        self.consent(self.profile_ref, classes=sorted(ga.CLASSES))
        self.provider = CurrentProfile(self.authority)

    def post(self, signal, value, *, revision=0, context=None, operation='set', reverses=None):
        target = FeedbackTarget.objects.filter(kind='item').first()
        actor = get_user_model().objects.get(pk=self.state.actor_id)
        command = MutationCommand(str(uuid.uuid4()), str(target.pk), 'item', signal, 'item',
            operation, revision, context, value, reverses)
        with ga.current_writer(self.authority, self.ref, self.profile_ref):
            return mutate(actor=actor, profile_id=self.state.profile_id, command=command)

    def test_current_profile_keeps_original_typed_values_null_false_and_reversal_lineage(self):
        with self.controlled_recovery_state('clear'):
            enjoyed = self.post('enjoyment', {'value': 5, 'basis': 'experienced', 'scale': 'integer-1-5'})
            interested = self.post('interest', {'value': 'maybe', 'basis': 'anticipated'})
            self.post('saved', {'value': False})
            current = self.provider.current(self.authority, self.ref, profile_ref=self.profile_ref,
                                             connection_ref=str(self.config.pk))
            snapshot = profile_snapshot.capture(current.view)
            self.assertEqual(snapshot['schema'], 'profile-snapshot/v2')
            values = {node['value']['signal']: node['value']['value'] for node in snapshot['evidence']}
            self.assertEqual(values['enjoyment'], {'value': 5, 'basis': 'experienced', 'scale': 'integer-1-5'})
            self.assertEqual(values['interest'], {'value': 'maybe', 'basis': 'anticipated'})
            self.assertIs(values['saved']['value'], False)
            self.post('enjoyment', None, revision=1, context=enjoyed.event_id,
                      operation='reverse', reverses=enjoyed.event_id)
            self.fault('PREVIEW_STALE', ga.assert_current, current.view)
            fresh = self.provider.current(self.authority, self.ref, profile_ref=self.profile_ref,
                                         connection_ref=str(self.config.pk))
            self.assertFalse(any(node['value']['signal'] == 'enjoyment'
                for node in profile_snapshot.capture(fresh.view)['evidence']))
            self.post('interest', None, revision=1, context=interested.event_id)
            cleared = profile_snapshot.capture(self.view(self.profile_ref))
            self.assertIsNone(next(node['value']['value'] for node in cleared['evidence']
                                  if node['value']['signal'] == 'interest'))
            self.assertGreater(fresh.view.policy_epoch, current.view.policy_epoch)

    def test_explicit_binding_and_scoped_current_writers_refuse_raw_bulk_and_import_paths(self):
        self.fault('CONTRACT_INVALID', ga.bind_existing_profile, self.authority, self.ref,
                   profile_id=self.state.profile_id, confirmed=False)
        self.assertEqual(ga.bind_existing_profile(self.authority, self.ref,
                         profile_id=self.state.profile_id, confirmed=True), self.profile_ref)
        actor = get_user_model().objects.get(pk=self.state.actor_id)
        def create():
            return DiscoveryState.objects.create(actor=actor, profile_id=self.state.profile_id,
                mode='household', nonce='scoped-current')
        with self.assertRaises(DatabaseError), transaction.atomic():
            create()
        before = self.view(self.profile_ref)
        with ga.current_writer(self.authority, self.ref, self.profile_ref), transaction.atomic(), ga.writer_window(actor, self.state.profile_id):
            row = create()
        self.fault('PREVIEW_STALE', ga.assert_current, before)
        with self.assertRaises(DatabaseError), transaction.atomic():
            DiscoveryState.objects.filter(pk=row.pk).update(history=[])
        with self.assertRaises(DatabaseError), ga.current_writer(self.authority, self.ref, self.profile_ref), transaction.atomic(), ga.writer_window(actor, self.state.profile_id):
            IntakeRun.objects.create(actor=actor, profile_id=self.state.profile_id, nonce='disabled', history=[])

    def test_text_constraint_exact_value_is_classified_without_becoming_a_numeric_rating(self):
        with self.controlled_recovery_state('clear'):
            text = '  e\u0301 / \u00e9\t\r\n\u96ea  '
            self.post('constraint', {'text': text})
            view = self.view(self.profile_ref)
            snapshot = profile_snapshot.capture(view)
            node = snapshot['evidence'][0]
            self.assertEqual(node['value'], {'signal': 'constraint', 'scope': 'item', 'value': {'text': text}})
            self.assertEqual(set(node['data_classes']), {'raw_notes', 'free_text_entry'})
            prepared = disclosure.prepare(snapshot, view.grants, view.context, [], {}, {}, now=self.clock.wall)
            self.assertTrue(prepared.ok)
            self.assertIn(text, disclosure.read_json(prepared.profile_bytes)['profile']['signals'][0]['value']['value']['text'])
