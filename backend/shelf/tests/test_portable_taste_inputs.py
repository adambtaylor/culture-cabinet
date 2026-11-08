"""Direct synthetic artist statements, real writer/events/snapshot and Undo."""
import uuid

from shelf import grant_authority as ga, profile_snapshot
from shelf.models import FeedbackEvent, FeedbackTarget, ProfileCabinetState
from shelf.portable_taste_inputs import NAMESPACE, PortableTasteInputs
from .test_grant_authority import RecoveryCase
from .test_portable_authority import PASSWORD


class ArtistInputTests(RecoveryCase):
    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.consent(self.profile_ref, classes=['anticipated_reactions', 'experienced_enjoyment'])
        self.state = ProfileCabinetState.objects.get()
        self.inputs = PortableTasteInputs(self.authority)
        self.entry = str(uuid.uuid4())
        self.mutation = str(uuid.uuid4())

    def submit(self, **changes):
        arguments = dict(entry_ref=self.entry, mutation_id=self.mutation, base_revision=0,
            operation='set', signal='interest', title='Fictional Artist', value='yes')
        arguments.update(changes)
        return self.inputs.mutate(self.ref, self.profile_ref, **arguments)

    def snapshot(self):
        return profile_snapshot.capture(self.view(self.profile_ref))

    def test_fresh_v2_creation_direct_statement_repeat_creation_and_undo_preserve_meaning(self):
        self.assertEqual(self.state.data_schema_version, 2)
        with self.controlled_recovery_state('clear'):
            result = self.submit()
            events = list(FeedbackEvent.objects.values_list('pk', 'revision'))
            self.state.refresh_from_db()
            identity = (self.state.profile_ref, self.state.subject_ref, self.state.policy_epoch,
                        self.state.restriction_watermark)
            self.assertEqual(self.bind(), self.profile_ref)
            self.state.refresh_from_db()
            self.assertEqual((self.state.profile_ref, self.state.subject_ref, self.state.policy_epoch,
                              self.state.restriction_watermark), identity)
            self.assertEqual(list(FeedbackEvent.objects.values_list('pk', 'revision')), events)
            nodes = [node for node in self.snapshot()['evidence'] if node.get('medium') == 'artist']
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0]['data_classes'], ['anticipated_reactions'])
            self.assertEqual(nodes[0]['value']['value'], {'value': 'yes', 'basis': 'anticipated'})
            self.assertTrue(nodes[0]['key'].startswith('feedback:'))
            self.assertTrue(nodes[0]['event_lineage'][0]['source_ref'].startswith(NAMESPACE + ':'))
            read = self.inputs.read(self.ref, self.profile_ref)
            self.assertEqual((read[0]['title'], read[0]['signals'][0]['value']), ('Fictional Artist', 'yes'))
            self.submit(mutation_id=str(uuid.uuid4()), base_revision=1, operation='reverse',
                        title=None, value=None, reverses_id=result['event_ref'])
            self.assertFalse(any(node.get('medium') == 'artist' for node in self.snapshot()['evidence']))

    def test_experienced_rating_needs_explicit_ack_and_keeps_original_scale(self):
        with self.controlled_recovery_state('clear'):
            self.fault('CONTRACT_INVALID', self.submit, signal='enjoyment', value=4)
            self.assertFalse(FeedbackTarget.objects.filter(source_namespace=NAMESPACE).exists())
            self.submit(signal='enjoyment', value=4, experienced_ack=True)
            node = next(node for node in self.snapshot()['evidence'] if node.get('medium') == 'artist')
            self.assertEqual(node['data_classes'], ['experienced_enjoyment'])
            self.assertEqual(node['value']['value'], {'value': 4, 'basis': 'experienced', 'scale': 'integer-1-5'})
            self.assertEqual(FeedbackEvent.objects.get().origin, 'direct')

    def test_exact_replay_after_newer_history_and_changed_reuse_conflict(self):
        with self.controlled_recovery_state('clear'):
            first = self.submit()
            self.submit(mutation_id=str(uuid.uuid4()), base_revision=1, value='maybe')
            self.assertEqual(self.submit(), first)
            self.assertEqual(self.inputs.read(self.ref, self.profile_ref)[0]['signals'][0]['value'], 'maybe')
            self.fault('REVISION_CONFLICT', self.submit, value='no')
            self.fault('REVISION_CONFLICT', self.submit, title='Changed artist')
            self.fault('AUTHORITY_DENIED', self.inputs.read, self.ref, str(uuid.uuid4()))
            self.assertEqual(FeedbackEvent.objects.count(), 2)

    def test_recent_auth_and_pending_recovery_prevent_writes(self):
        with self.controlled_recovery_state('clear') as coordinator:
            self.clock.advance(300)
            self.fault('UNAUTHENTICATED', self.submit)
            self.authority.reauthenticate(self.ref, PASSWORD)
            self.submit()
            before = list(FeedbackEvent.objects.values_list('pk', 'revision'))
            self.prepare_restriction(coordinator)
            self.fault('CAPABILITY_UNAVAILABLE', self.submit, mutation_id=str(uuid.uuid4()), base_revision=1)
            self.assertEqual(list(FeedbackEvent.objects.values_list('pk', 'revision')), before)

class ExistingV1ArtistTests(RecoveryCase):
    def test_existing_v1_state_is_not_upgraded_by_creation_or_artist_post(self):
        from django.db import transaction
        from shelf.models import Profile, Grant
        # Model-default v1 remains a supported historical state. Construct that
        # state through real guards, rather than downgrading a new v2 profile.
        with transaction.atomic(), ga._write_window():
            profile = Profile.objects.create(kind='owner', label='Historical fixture', active=True)
            Grant.objects.create(profile=profile, user_id=self.authority.authorize(self.ref).owner_id,
                                 can_view=True, can_select=True, can_mutate=True, can_export=True)
            state = ProfileCabinetState.objects.create(profile=profile,
                installation_id=self.boundary.installation_id, actor_id=self.authority.authorize(self.ref).owner_id,
                owner_revision=1)
        self.assertEqual(self.bind(), str(state.profile_ref))
        state.refresh_from_db()
        self.assertEqual(state.data_schema_version, 1)
        self.fault('CAPABILITY_UNAVAILABLE', PortableTasteInputs(self.authority).mutate,
            self.ref, str(state.profile_ref), entry_ref=str(uuid.uuid4()), mutation_id=str(uuid.uuid4()),
            base_revision=0, operation='set', signal='interest', title='Fictional Artist', value='yes')
        self.assertFalse(FeedbackTarget.objects.filter(source_namespace=NAMESPACE).exists())
