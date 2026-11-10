"""Named fresh identity/progress and typed favourites, no provider operation."""
import uuid
from shelf.portable_journey import PortableJourney, favourite_slot, journey_actions
from shelf.models import ProfileCabinetState, PortableJourneyState, FeedbackEvent
from shelf.portable_taste_inputs import PortableTasteInputs
from shelf.portable_authority import AuthorityFault
from .test_grant_authority import RecoveryCase


class JourneyPrimitiveTests(RecoveryCase):
    def test_name_receipt_exact_replay_never_renames_or_adopts_an_existing_profile(self):
        journey=PortableJourney(self.authority)
        with self.controlled_recovery_state('clear'):
            values=dict(expected_owner_revision=self.authority.authorize(self.ref).owner_revision,
                label='Fictional Owner 雪',command_id=str(uuid.uuid4()))
            created=journey.name(self.ref,**values)
            self.assertEqual(ProfileCabinetState.objects.get().profile.label,'Fictional Owner 雪')
            self.assertEqual(journey.name(self.ref,**values),created)
            with self.assertRaises(AuthorityFault):journey.name(self.ref,**{**values,'label':'Changed'})
            with self.assertRaises(AuthorityFault):journey.name(self.ref,**{**values,'command_id':str(uuid.uuid4())})
            self.assertEqual(PortableJourneyState.objects.count(),1)

    def test_skip_back_and_exact_progress_replay_never_send_or_change_taste_floors(self):
        journey=PortableJourney(self.authority)
        with self.controlled_recovery_state('clear'):
            created=journey.name(self.ref,expected_owner_revision=self.authority.authorize(self.ref).owner_revision,
                label='Fixture',command_id=str(uuid.uuid4()))
            state=ProfileCabinetState.objects.get();floors=(state.policy_epoch,state.restriction_watermark)
            values=dict(profile_ref=created['profile_ref'],journey_ref=created['journey_ref'],
                expected_revision=1,step='providers',action='skip',command_id=str(uuid.uuid4()))
            advanced=journey.step(self.ref,**values)
            self.assertEqual(advanced['step'],'favourites')
            self.assertEqual(journey.step(self.ref,**values),advanced)
            state.refresh_from_db();self.assertEqual((state.policy_epoch,state.restriction_watermark),floors)
            self.assertFalse(FeedbackEvent.objects.exists())
            back=journey.step(self.ref,**{**values,'expected_revision':2,'step':'favourites',
                'action':'back','command_id':str(uuid.uuid4())})
            self.assertEqual(back['step'],'providers')

    def test_stated_favourite_preserves_typed_history_and_clear_is_not_a_dislike(self):
        with self.controlled_recovery_state('clear'):
            profile=self.bind();inputs=PortableTasteInputs(self.authority);entry=str(uuid.uuid4())
            values=dict(entry_ref=entry,mutation_id=str(uuid.uuid4()),base_revision=0,category='book',title='Fixture title')
            first=inputs.mutate_favourite(self.ref,profile,**values)
            self.assertEqual(inputs.mutate_favourite(self.ref,profile,**values),first)
            self.assertEqual(FeedbackEvent.objects.get().body,{'value':True})
            inputs.mutate_favourite(self.ref,profile,**{**values,'mutation_id':str(uuid.uuid4()),
                'base_revision':1,'title':None,'remove':True})
            self.assertEqual(list(FeedbackEvent.objects.order_by('revision').values_list('body',flat=True)),[{'value':True},None])
            self.assertFalse(FeedbackEvent.objects.exclude(signal='saved').exists())

    def test_five_slots_closed_forms_and_sensitive_existing_witness_absence(self):
        from shelf import recovery
        self.assertFalse(recovery.native_clean_start_matches(None,self.boundary))
        self.assertFalse(recovery.native_clean_start_matches(object(),self.boundary))
        for slot in range(1,6):
            row=favourite_slot(profile_ref=str(uuid.uuid4()),journey_ref=str(uuid.uuid4()),expected_revision=1,
                category='book',slot=slot)
            self.assertEqual(row['slot'],slot)
            self.assertEqual(set(row['actions']['add']['fields']),{'profile_ref','journey_ref','expected_revision',
                'command_id','category','slot','entry_ref','base_revision','mutation_id','title'})
        with self.assertRaises(AuthorityFault):favourite_slot(profile_ref=str(uuid.uuid4()),journey_ref=str(uuid.uuid4()),
            expected_revision=1,category='book',slot=6)
