"""Actual feedback/replay/Undo under real owner, SQL and recovery guards.

The filtered-publication read is a fixture port; it is not source/publisher proof.
"""
from types import SimpleNamespace
import uuid
from django.db import DatabaseError, transaction
from shelf import grant_authority as ga
from shelf.models import ProfileCabinetState, FeedbackTarget
from shelf.portable_authority import AuthorityFault
from shelf.portable_reactions import PortableReactions, target_ref
from .test_grant_authority import RecoveryCase


class PortableReactionTests(RecoveryCase):
    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.state = ProfileCabinetState.objects.get()
        ga.bind_existing_profile(self.authority, self.ref, profile_id=self.state.profile_id, confirmed=True)
        self.state.refresh_from_db()
        self.opaque = target_ref(self.profile_ref, 'synthetic:book:81')
        self.card = {'key': 'synthetic:book:81', 'title': 'Synthetic judged book', 'category': 'book'}
        self.stale = False
        def current(*args, **kwargs):
            if self.stale:
                raise AuthorityFault('PREVIEW_STALE', 409)
            return {'manifest': {'categories': [{'cards': [self.card]}]}}
        self.reactions = PortableReactions(self.authority,
            service=SimpleNamespace(authority=self.authority, current=current))

    def test_exact_replay_changed_intent_and_undo_survive_stale_publication(self):
        with self.controlled_recovery_state('clear'):
            mutation = str(uuid.uuid4())
            value = {'signal': 'enjoyment', 'value': {'value': 4, 'basis': 'experienced', 'scale': 'integer-1-5'}}
            result = self.reactions.mutate(self.ref, self.profile_ref, self.opaque, mutation, 0, 'set', value)
            self.assertEqual(self.reactions.mutate(self.ref, self.profile_ref, self.opaque, mutation, 0, 'set', value), result)
            with self.assertRaises(AuthorityFault):
                self.reactions.mutate(self.ref, self.profile_ref, self.opaque, mutation, 0, 'set',
                    {'signal': 'enjoyment', 'value': {'value': 1, 'basis': 'experienced', 'scale': 'integer-1-5'}})
            self.stale = True
            read = self.reactions.read(self.ref, self.profile_ref, self.opaque)
            self.assertEqual(read['signals']['enjoyment']['value'], value['value'])
            undone = self.reactions.mutate(self.ref, self.profile_ref, self.opaque, str(uuid.uuid4()), 1,
                'reverse', {'signal': 'enjoyment', 'value': None}, result['event_ref'])
            self.assertEqual(undone['revision'], 2)
            self.assertIsNone(self.reactions.read(self.ref, self.profile_ref, self.opaque)['signals']['enjoyment']['value'])

    def test_opaque_current_target_scope_and_raw_identity_guards_are_required(self):
        with self.controlled_recovery_state('clear'):
            with self.assertRaises(AuthorityFault):
                self.reactions.read(self.ref, self.profile_ref, str(uuid.uuid4()))
            value = {'signal': 'saved', 'value': {'value': False}}
            self.reactions.mutate(self.ref, self.profile_ref, self.opaque, str(uuid.uuid4()), 0, 'set', value)
            self.assertIs(self.reactions.read(self.ref, self.profile_ref, self.opaque)['signals']['saved']['value']['value'], False)
            self.fault('UNAUTHENTICATED', self.reactions.read, 'foreign-session', self.profile_ref, self.opaque)
            with self.assertRaises(DatabaseError), transaction.atomic():
                FeedbackTarget.objects.filter(source_namespace='enhance-current').update(title='Forged title')

    def test_pending_recovery_denies_even_personally_authored_undo(self):
        with self.controlled_recovery_state('clear') as current:
            result = self.reactions.mutate(self.ref, self.profile_ref, self.opaque, str(uuid.uuid4()), 0, 'set',
                {'signal': 'interest', 'value': {'value': 'maybe', 'basis': 'anticipated'}})
            self.prepare_restriction(current)
            before = self.recovery_rows()
            self.fault('CAPABILITY_UNAVAILABLE', self.reactions.mutate, self.ref, self.profile_ref,
                self.opaque, str(uuid.uuid4()), 1, 'reverse', {'signal': 'interest', 'value': None}, result['event_ref'])
            self.assertEqual(self.recovery_rows(), before)
