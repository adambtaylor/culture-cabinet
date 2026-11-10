"""Atomic typed feedback/replay under real native SQL/owner/recovery guards."""
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace
import uuid
from django.db import DatabaseError, transaction
from shelf import grant_authority as ga
from shelf.card_actions import CardAction, CardUndo, CardActions, desired_values, action_forms
from shelf.models import FeedbackEvent, FeedbackProjection, PortableJourneyCommand, ProfileCabinetState
from shelf.portable_authority import AuthorityFault
from shelf.portable_reactions import target_ref
from .test_grant_authority import RecoveryCase


class CardActionTests(RecoveryCase):
    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.state = ProfileCabinetState.objects.get()
        ga.bind_existing_profile(self.authority, self.ref, profile_id=self.state.profile_id, confirmed=True)
        self.state.refresh_from_db()
        self.publication = str(uuid.uuid4())
        self.card = {'key': 'fixture:book:1', 'title': 'Fictional exact card', 'category': 'book'}
        self.opaque = target_ref(self.profile_ref, self.card['key'])
        self.current = {'publication_ref': self.publication, 'publication_revision': 1,
            'manifest': {'categories': [{'cards': [self.card]}]}, 'ranking_state': 'fresh'}
        self.actions = CardActions(self.authority, service=SimpleNamespace(authority=self.authority,
            current_for_interaction=lambda *a, **k:self.current))

    def command(self, action='like', interest=0, saved=0):
        return CardAction(self.profile_ref, self.publication, 1, self.opaque, interest, saved,
                          str(uuid.uuid4()), action)

    def test_four_actions_switch_clear_pair_and_exact_replay_after_newer_action(self):
        with self.controlled_recovery_state('clear'):
            first = self.command()
            a = self.actions.apply(self.ref, first)
            self.assertEqual((a['selected_state'], a['interest_revision'], a['saved_revision']), ('like', 1, 0))
            favourite = self.actions.apply(self.ref, self.command('favourite', 1, 0))
            self.assertEqual((favourite['selected_state'], favourite['interest_revision'], favourite['saved_revision']),
                             ('favourite', 2, 1))
            self.assertEqual(len(favourite['child_event_refs']), 2)
            count = FeedbackEvent.objects.count()
            obtain = self.actions.apply(self.ref, self.command('obtain', 2, 1))
            self.assertEqual(obtain['preference_outcome'], 'unchanged')
            self.assertFalse(obtain['undo_available'])
            self.assertEqual(obtain['obtain']['state'], 'unavailable')
            self.assertEqual(FeedbackEvent.objects.count(), count)
            dislike = self.actions.apply(self.ref, self.command('dislike', 2, 1))
            self.assertEqual(dislike['selected_state'], 'dislike')
            self.assertEqual(self.actions.apply(self.ref, first), a)
            with self.assertRaises(AuthorityFault) as error:
                self.actions.apply(self.ref, replace(first, action='favourite'))
            self.assertEqual(error.exception.status, 409)
            self.assertFalse(FeedbackEvent.objects.exclude(signal__in=['interest', 'saved']).exists())

    def test_second_child_failure_rolls_back_first_and_receipt(self):
        with self.controlled_recovery_state('clear'):
            self.actions.apply(self.ref, self.command('like'))
            before = list(FeedbackEvent.objects.values())
            from shelf.feedback import mutate_in_transaction
            calls = []
            def fail(**kwargs):
                calls.append(kwargs['command'].signal)
                if len(calls) == 2:raise RuntimeError('Owned failure after first child')
                return mutate_in_transaction(**kwargs)
            command = self.command('favourite', 1, 0)
            with patch('shelf.feedback.mutate_in_transaction', side_effect=fail), self.assertRaises(RuntimeError):
                self.actions.apply(self.ref, command)
            self.assertEqual(list(FeedbackEvent.objects.values()), before)
            self.assertFalse(PortableJourneyCommand.objects.filter(command_id=command.command_id).exists())

    def test_pair_cas_and_foreign_target_or_owner_never_write(self):
        with self.controlled_recovery_state('clear'):
            self.actions.apply(self.ref, self.command('favourite'))
            before = FeedbackEvent.objects.count()
            for command in [self.command('like', 0, 0), replace(self.command(), target_ref=str(uuid.uuid4())),
                            replace(self.command(), publication_ref=str(uuid.uuid4()))]:
                with self.assertRaises(AuthorityFault):self.actions.apply(self.ref, command)
            with self.assertRaises(AuthorityFault):self.actions.apply('foreign', self.command())
            self.assertEqual(FeedbackEvent.objects.count(), before)
            with self.assertRaises(DatabaseError), transaction.atomic():
                PortableJourneyCommand.objects.all().update(result={})

    def test_undo_reverses_changed_children_and_refuses_newer_affected_revision(self):
        with self.controlled_recovery_state('clear'):
            first = self.command('favourite')
            self.actions.apply(self.ref, first)
            undo = CardUndo(self.profile_ref, first.command_id, str(uuid.uuid4()), 0, 1)
            result = self.actions.undo(self.ref, undo)
            self.assertEqual(result['selected_state'], 'none')
            self.assertEqual(self.actions.undo(self.ref, undo), result)
            later = self.command('like', 0, 2)
            self.actions.apply(self.ref, later)
            with self.assertRaises(AuthorityFault):
                self.actions.undo(self.ref, replace(undo, command_id=str(uuid.uuid4()), interest_revision=1, saved_revision=2))
            self.assertEqual(FeedbackEvent.objects.filter(operation='reverse').count(), 1)

    def test_closed_forms_exclude_supplied_actor_score_title_and_numeric_action(self):
        command = self.command()
        self.assertEqual(CardAction.parse({**command.descriptor(), 'command_id': command.command_id}), command)
        for extra in ['actor', 'score', 'title']:
            with self.assertRaises(AuthorityFault):CardAction.parse({**vars(command), extra:'untrusted'})
        with self.assertRaises(AuthorityFault):replace(command, action=1)
        forms = action_forms(**{k:v for k,v in vars(command).items() if k not in ['action', 'command_id']})
        self.assertEqual([r['action'] for r in forms], ['like', 'dislike', 'favourite', 'obtain'])
        self.assertTrue(all(set(r['fields']) == set(vars(command)) for r in forms))
        self.assertEqual(desired_values('obtain'), desired_values('favourite'))

    def test_shared_form_first_choice_binds_one_command_and_conflicting_choice_refuses(self):
        with self.controlled_recovery_state('clear'):
            base = self.command()
            rows = action_forms(**{k:v for k,v in vars(base).items() if k not in ['action','command_id']})
            self.assertEqual(len({row['fields']['command_id'] for row in rows}),1)
            first = CardAction.parse(rows[0]['fields'])
            committed = self.actions.apply(self.ref,first)
            self.assertEqual(self.actions.apply(self.ref,first),committed)
            with self.assertRaises(AuthorityFault) as error:
                self.actions.apply(self.ref,CardAction.parse(rows[2]['fields']))
            self.assertEqual(error.exception.status,409)
            self.assertEqual(FeedbackEvent.objects.count(),1)


from django.test import SimpleTestCase


class GroundedCardStrengthTests(SimpleTestCase):
    def material(self):
        from .test_enhance_selection import GroundedSelectionTests
        submitted,output,outcomes=GroundedSelectionTests().material()
        submitted['profile']['signals']=[dict(key=key,medium='book',basis='typed_feedback',
            value=dict(signal=signal,scope='item',value=value)) for key,signal,value in [
                ('like1','interest',{'value':'yes','basis':'anticipated'}),
                ('like2','interest',{'value':'yes','basis':'anticipated'}),
                ('favourite1','saved',{'value':True})]]
        from .test_inference_contract import graph
        output['associations']=graph(['like1','like2','favourite1'])
        output['candidate_assessments'][0]['signal_keys']=['like1','like2']
        output['candidate_assessments'][1]['signal_keys']=['favourite1']
        return submitted,output,outcomes

    def test_favourite_beats_two_likes_without_stacking_or_replacing_evidence_tier(self):
        from shelf.enhance_selection import GroundedSelector
        submitted,output,outcomes=self.material()
        result=GroundedSelector().build(output,submitted,outcomes)
        self.assertEqual(result['categories'][0]['cards'][0]['key'],'source:b')
        self.assertIn('favourite',result['for_you'][0]['text'])
        output['candidate_assessments'][1]['decision']='uncertain'
        result=GroundedSelector().build(output,submitted,outcomes)
        self.assertEqual(result['categories'][0]['cards'][0]['key'],'source:c')

    def test_rejected_or_ungrounded_favourite_and_unpermitted_reference_never_promote(self):
        from shelf.enhance_selection import GroundedSelector
        from shelf.inference_contract import ContractError
        submitted,output,outcomes=self.material()
        output['candidate_assessments'][1]['decision']='reject'
        self.assertNotIn('source:b',[c['key'] for c in GroundedSelector().build(output,submitted,outcomes)['categories'][0]['cards']])
        output['candidate_assessments'][1]['decision']='retain'
        output['candidate_assessments'][1]['evidence_refs']=[]
        self.assertNotIn('source:b',[c['key'] for c in GroundedSelector().build(output,submitted,outcomes)['categories'][0]['cards']])
        output['candidate_assessments'][0]['signal_keys']=['PRIVATE-DENIED']
        with self.assertRaises(ContractError):GroundedSelector().build(output,submitted,outcomes)
