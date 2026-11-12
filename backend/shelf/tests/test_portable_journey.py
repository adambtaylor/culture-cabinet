"""Named fresh identity/progress and typed favourites, no provider operation."""
import uuid
from shelf.portable_journey import PortableJourney, favourite_slot, journey_actions
from shelf.models import ProfileCabinetState, PortableJourneyState, FeedbackEvent, PortableJourneyCommand
from shelf import grant_authority as ga
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

from .test_portable_feedback_views import NativeFeedbackCase


class JourneyNativeCallerTests(NativeFeedbackCase):
    def test_returning_owner_get_optional_skip_and_rejected_secret_form_use_native_renderer(self):
        from shelf.models import PortableJourneyCommand
        path='/setup/journey?profile_ref='+self.profile_ref
        page=self.http.get(path,**self.headers)
        self.assertEqual(page.status_code,200)
        self.assertContains(page,'/setup/journey/style.css')
        self.assertFalse(PortableJourneyState.objects.exists())
        journey_ref=str(uuid.uuid5(uuid.UUID(self.profile_ref),'guided-owner-journey:v2'))
        fields=dict(profile_ref=self.profile_ref,journey_ref=journey_ref,expected_revision=0,
            command_id=str(uuid.uuid4()),step='providers',action='skip')
        self.assertEqual(self.post('/cabinet/journey/step',fields,csrf=False).status_code,403)
        response=self.post('/cabinet/journey/step',fields)
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.http.get(response['Location'],**self.headers).status_code,200)
        self.assertEqual(PortableJourneyState.objects.get().step,'favourites')
        self.assertEqual(self.post('/cabinet/journey/step',fields).status_code,303)
        self.assertEqual(PortableJourneyCommand.objects.count(),1)
        invalid=self.post('/cabinet/journey/providers/tmdb/save',dict(profile_ref=self.profile_ref,
            journey_ref=journey_ref,expected_revision=1,command_id=str(uuid.uuid4()),secret='PRIVATE_CANARY',
            storage='persistent',extra='rejected'))
        self.assertEqual(invalid.status_code,422)
        self.assertNotIn(b'PRIVATE_CANARY',invalid.content)
        self.assertContains(invalid,'/setup/journey/style.css',status_code=422)
        self.assertFalse(FeedbackEvent.objects.exists())

    def test_normal_favourite_form_replay_and_clear_preserve_history(self):
        journey=PortableJourney(self.authority)
        initial=journey.project(self.ref,profile_ref=self.profile_ref)
        journey.step(self.ref,profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],
            expected_revision=0,step='providers',action='skip',command_id=str(uuid.uuid4()))
        fields=dict(profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],expected_revision=1,
            command_id=str(uuid.uuid4()),category='book',slot=1,entry_ref=str(uuid.uuid4()),
            mutation_id=str(uuid.uuid4()),base_revision=0,title='A fictional favourite')
        self.assertEqual(self.post('/cabinet/journey/favourites/add',fields).status_code,303)
        self.assertEqual(self.post('/cabinet/journey/favourites/add',fields).status_code,303)
        self.assertEqual(FeedbackEvent.objects.count(),1)
        page=self.http.get('/setup/journey',**self.headers)
        self.assertEqual(page.status_code,200)
        self.assertContains(page,'A fictional favourite')
        remove={key:value for key,value in fields.items() if key!='title'}
        remove.update(expected_revision=2,command_id=str(uuid.uuid4()),mutation_id=str(uuid.uuid4()),base_revision=1)
        self.assertEqual(self.post('/cabinet/journey/favourites/remove',remove).status_code,303)
        self.assertEqual(list(FeedbackEvent.objects.order_by('revision').values_list('body',flat=True)),
            [{'value':True},None])
        self.assertEqual(PortableJourneyState.objects.get().slots,{})


class JourneyFreshNativeTests(RecoveryCase):
    claim_owner=NativeFeedbackCase.claim_owner
    csrf=NativeFeedbackCase.csrf
    post=NativeFeedbackCase.post

    def test_cold_native_name_csrf_exact_replay_and_existing_owner_resume(self):
        with self.controlled_recovery_state('clear'):
            page=self.http.get('/setup/journey',**self.headers)
            self.assertEqual(page.status_code,200)
            self.assertContains(page,'/cabinet/journey/name')
            self.assertFalse(ProfileCabinetState.objects.exists())
            values=dict(expected_owner_revision=self.authority.authorize(self.ref).owner_revision,
                label='Fictional Owner 雪',command_id=str(uuid.uuid4()))
            self.assertEqual(self.post('/cabinet/journey/name',values,csrf=False).status_code,403)
            created=self.post('/cabinet/journey/name',values)
            self.assertEqual(created.status_code,303)
            self.assertEqual(self.post('/cabinet/journey/name',values).status_code,303)
            self.assertEqual(ProfileCabinetState.objects.get().profile.label,'Fictional Owner 雪')
            self.assertContains(self.http.get('/setup/journey',**self.headers),'Connect your sources')
            self.assertEqual(self.post('/cabinet/journey/name',{**values,'label':'Replacement'}).status_code,409)
            self.assertEqual(ProfileCabinetState.objects.count(),1)

from .test_portable_enhance_service import ServiceCase


class JourneyPrepareTests(ServiceCase):
    def test_prepare_reuses_existing_reviewable_plan_without_collection_or_inference(self):
        from django.db import transaction
        from shelf import grant_authority as ga
        from shelf.models import SynthesisRun,PortableJourneyCommand
        journey=PortableJourney(self.authority)
        with ga.current_writer(self.authority,self.ref,self.profile_ref) as cabinet,transaction.atomic(),ga._write_window():
            state=PortableJourneyState.objects.create(id=uuid.uuid5(cabinet.profile_ref,'guided-owner-journey:v2'),
                cabinet=cabinet,connections={'openai':[str(self.config.pk)]},created_at=self.authority.now(),
                updated_at=self.authority.now())
        fields=dict(profile_ref=self.profile_ref,journey_ref=str(state.pk),expected_revision=1,
            command_id=str(uuid.uuid4()),connection_ref=str(self.config.pk),client_key=str(uuid.uuid4()),
            refresh_nonce=str(uuid.uuid4()))
        result=journey.prepare(self.ref,**fields)
        self.assertEqual(journey.prepare(self.ref,**fields),result)
        self.assertEqual(SynthesisRun.objects.count(),1)
        self.assertEqual((self.catalogue.calls,self.transport.calls),(0,0))
        self.assertIn('preview',self.enhance.status(self.ref,run_ref=result['run_ref'])['actions'])
        self.assertEqual(PortableJourneyCommand.objects.get().state,'committed')
        state.refresh_from_db();self.assertEqual(str(state.run_ref),result['run_ref'])
        with self.assertRaises(AuthorityFault):journey.prepare(self.ref,**{**fields,'refresh_nonce':str(uuid.uuid4())})
        self.assertEqual((self.catalogue.calls,self.transport.calls),(0,0))


class JourneyReservationRegressionTests(NativeFeedbackCase):
    def test_normal_changed_title_refusal_does_not_strand_remove_or_replacement(self):
        journey=PortableJourney(self.authority)
        initial=journey.project(self.ref,profile_ref=self.profile_ref)
        journey.step(self.ref,profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],expected_revision=0,
            step='providers',action='skip',command_id=str(uuid.uuid4()))
        projected=journey.project(self.ref,profile_ref=self.profile_ref)
        slot=next(row for row in projected['favourite_categories'] if row['category']=='book')['slots'][0]
        first={**slot['actions']['add']['fields'],'title':'Original fictional favourite'}
        self.assertEqual(self.post('/cabinet/journey/favourites/add',first).status_code,303)
        page=self.http.get('/setup/journey',{'profile_ref':self.profile_ref},**self.headers)
        self.assertContains(page,'Original fictional favourite')
        projected=journey.project(self.ref,profile_ref=self.profile_ref)
        slot=next(row for row in projected['favourite_categories'] if row['category']=='book')['slots'][0]
        changed={**slot['actions']['add']['fields'],'title':'Replacement fictional favourite'}
        refused=self.post('/cabinet/journey/favourites/add',changed)
        self.assertEqual(refused.status_code,409)
        self.assertContains(refused,'remove it and add a new favourite',status_code=409)
        self.assertFalse(PortableJourneyCommand.objects.filter(command_id=changed['command_id']).exists())
        self.assertEqual(FeedbackEvent.objects.count(),1)
        self.assertEqual(self.post('/cabinet/journey/favourites/remove',slot['actions']['remove']['fields']).status_code,303)
        projected=journey.project(self.ref,profile_ref=self.profile_ref)
        empty=next(row for row in projected['favourite_categories'] if row['category']=='book')['slots'][0]
        self.assertNotEqual(empty['entry_ref'],first['entry_ref'])
        self.assertEqual(self.post('/cabinet/journey/favourites/add',
            {**empty['actions']['add']['fields'],'title':'Replacement fictional favourite'}).status_code,303)
        self.assertEqual(list(FeedbackEvent.objects.order_by('recorded_at','id').values_list('body',flat=True)).count(None),1)
        self.assertEqual(FeedbackEvent.objects.count(),3)

    def test_interrupted_committed_child_resumes_its_receipt_once_and_keeps_pending_guard(self):
        from unittest.mock import patch
        from shelf.portable_taste_inputs import PortableTasteInputs
        journey=PortableJourney(self.authority)
        initial=journey.project(self.ref,profile_ref=self.profile_ref)
        slot=next(row for row in initial['favourite_categories'] if row['category']=='book')['slots'][0]
        fields={**slot['actions']['add']['fields'],'title':'Interrupted fictional favourite'}
        original=PortableTasteInputs.mutate_favourite
        def interrupted(instance,*args,**kwargs):
            original(instance,*args,**kwargs)
            raise RuntimeError('Fixture interruption after child commit')
        with patch.object(PortableTasteInputs,'mutate_favourite',interrupted),self.assertRaises(RuntimeError):
            self.post('/cabinet/journey/favourites/add',fields)
        parent=PortableJourneyCommand.objects.get(command_id=fields['command_id'])
        self.assertEqual(parent.state,'reserved');self.assertEqual(FeedbackEvent.objects.count(),1)
        blocked=self.post('/cabinet/journey/providers/apple/configure',dict(
            profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],expected_revision=0,
            command_id=str(uuid.uuid4()),confirmed='yes'))
        self.assertEqual(blocked.status_code,409)
        self.assertEqual(self.post('/cabinet/journey/favourites/add',fields).status_code,303)
        self.assertEqual(self.post('/cabinet/journey/favourites/add',fields).status_code,303)
        parent.refresh_from_db();self.assertEqual(parent.state,'committed')
        self.assertEqual(FeedbackEvent.objects.count(),1)

    def test_prepare_known_no_run_refusal_is_replayable_and_does_not_block_next_command(self):
        self.enterContext(self.settings(PORTABLE_CONNECTIONS=self.service))
        from shelf.models import SynthesisRun
        journey=PortableJourney(self.authority)
        initial=journey.project(self.ref,profile_ref=self.profile_ref)
        configured=journey.configure(self.ref,profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],
            expected_revision=0,command_id=str(uuid.uuid4()),provider='openai',confirmed=True)
        fields=dict(profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],
            expected_revision=configured['revision'],command_id=str(uuid.uuid4()),
            connection_ref=configured['connection_refs'][0],client_key=str(uuid.uuid4()),refresh_nonce=str(uuid.uuid4()))
        refused=self.post('/cabinet/journey/prepare',fields)
        self.assertGreaterEqual(refused.status_code,400)
        parent=PortableJourneyCommand.objects.get(command_id=fields['command_id'])
        self.assertEqual(parent.state,'committed')
        self.assertEqual(parent.result['schema'],'journey-prepare-refusal/v1')
        self.assertFalse(SynthesisRun.objects.exists())
        self.assertEqual(self.post('/cabinet/journey/step',dict(profile_ref=self.profile_ref,
            journey_ref=initial['journey_ref'],expected_revision=configured['revision'],
            command_id=str(uuid.uuid4()),step='providers',action='skip')).status_code,303)
        self.assertEqual(self.post('/cabinet/journey/prepare',fields).status_code,refused.status_code)
        self.assertFalse(SynthesisRun.objects.exists())


class JourneyPrepareInterruptionTests(ServiceCase):
    def test_admitted_prepare_child_is_not_settled_as_no_effect_after_interruption(self):
        from django.db import transaction
        from unittest.mock import patch
        from shelf.models import PortableJourneyCommand,SynthesisRun
        journey=PortableJourney(self.authority)
        with ga.current_writer(self.authority,self.ref,self.profile_ref) as cabinet,transaction.atomic(),ga._write_window():
            state=PortableJourneyState.objects.create(id=uuid.uuid5(cabinet.profile_ref,'guided-owner-journey:v2'),
                cabinet=cabinet,connections={'openai':[str(self.config.pk)]},created_at=self.authority.now(),
                updated_at=self.authority.now())
        fields=dict(profile_ref=self.profile_ref,journey_ref=str(state.pk),expected_revision=1,
            command_id=str(uuid.uuid4()),connection_ref=str(self.config.pk),client_key=str(uuid.uuid4()),
            refresh_nonce=str(uuid.uuid4()))
        original=self.enhance.start
        def interrupted(*args,**kwargs):
            original(*args,**kwargs)
            raise AuthorityFault('CAPABILITY_UNAVAILABLE',503)
        with patch.object(self.enhance,'start',side_effect=interrupted),self.assertRaises(AuthorityFault):
            journey.prepare(self.ref,**fields)
        # Admission also records a history point; inspect this exact parent,
        # rather than treating all coordination metadata as one singleton.
        parent=PortableJourneyCommand.objects.get(kind='journey.prepare',command_id=fields['command_id'])
        self.assertEqual(parent.state,'reserved')
        self.assertEqual(SynthesisRun.objects.count(),1)
        resumed=journey.prepare(self.ref,**fields)
        parent.refresh_from_db();self.assertEqual(parent.state,'committed')
        self.assertEqual(str(SynthesisRun.objects.get().pk),resumed['run_ref'])
        self.assertEqual((self.catalogue.calls,self.transport.calls),(0,0))
