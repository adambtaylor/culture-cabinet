"""Reference checkpoints and atomic local restore; no inference credential needed."""
import json
import uuid
from unittest.mock import patch
from django.db import transaction
from shelf import grant_authority as ga
from shelf.models import FeedbackEvent,FeedbackProjection,PortableJourneyCommand
from shelf.portable_taste_history import TasteHistory
from shelf.portable_taste_inputs import PortableTasteInputs
from .test_portable_feedback_views import NativeFeedbackCase


class TasteHistoryTests(NativeFeedbackCase):
    def setUp(self):
        super().setUp()
        self.history=TasteHistory(self.authority)

    def note(self,text,**changes):
        values=self.note_fields(text=text);values.update(changes)
        self.assertEqual(self.post('/cabinet/taste',values).status_code,303)
        return values

    def point(self,command):
        return self.history.capture(self.ref,profile_ref=self.profile_ref,origin_command_ref=command)

    def older_and_current(self):
        old=self.note('  Original exact note\n雪  ')
        point=self.point(old['mutation_id'])
        self.note('Later exact note',entry_ref=old['entry_ref'],base_revision=1)
        inputs=PortableTasteInputs(self.authority)
        favourite=inputs.mutate_favourite(self.ref,self.profile_ref,entry_ref=str(uuid.uuid4()),
            mutation_id=str(uuid.uuid4()),base_revision=0,category='book',title='Fictional later favourite')
        return point,old

    def preview(self,point):
        comparison=self.history.compare(self.ref,profile_ref=self.profile_ref,checkpoint_ref=point['checkpoint_ref'])
        return self.history.preview(self.ref,profile_ref=self.profile_ref,checkpoint_ref=point['checkpoint_ref'],
            expected_state_digest=comparison['current_state_digest'],command_id=str(uuid.uuid4()))

    def restore(self,preview,**changes):
        fields=dict(profile_ref=self.profile_ref,preview_ref=preview['preview_ref'],
            preview_digest=preview['preview_digest'],command_id=str(uuid.uuid4()),confirmed='restore_taste_choices')
        fields.update(changes)
        return self.history.restore(self.ref,**fields)

    def test_actual_checkpoints_are_reference_only_get_is_read_only_and_work_without_inference_key(self):
        self.assertEqual(self.history.index(self.ref,profile_ref=self.profile_ref)['checkpoints'],[])
        self.assertFalse(PortableJourneyCommand.objects.filter(kind='taste.checkpoint').exists())
        first=self.note('  Private fictional exact note\n雪  ')
        point=self.point(first['mutation_id'])
        self.assertEqual(self.point(first['mutation_id']),point)
        stored=PortableJourneyCommand.objects.get(kind='taste.checkpoint').result
        self.assertNotIn('Private fictional exact note',json.dumps(stored))
        self.assertEqual(len(stored['stream_heads']),1)
        self.vault.remove(self.ref,self.config.pk,self.config.config_revision,disable=True)
        page=self.history.index(self.ref,profile_ref=self.profile_ref)
        self.assertEqual(len(page['checkpoints']),1);self.assertTrue(page['checkpoints'][0]['current'])
        comparison=self.history.compare(self.ref,profile_ref=self.profile_ref,checkpoint_ref=point['checkpoint_ref'])
        self.assertEqual(comparison['changes'],[])

    def test_restore_appends_clears_and_exact_note_then_undo_restores_current_without_touching_location_noise(self):
        from shelf.portable_journey import PortableJourney
        from shelf.models import PortableJourneyState
        point,_=self.older_and_current()
        journey=PortableJourney(self.authority);initial=journey.project(self.ref,profile_ref=self.profile_ref)
        journey.location(self.ref,profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],expected_revision=0,
            command_id=str(uuid.uuid4()),action='save',country='AU',postcode='3000',radius_km=50)
        with ga.current_writer(self.authority,self.ref,self.profile_ref),transaction.atomic(),ga._write_window():
            PortableJourneyState.objects.update(next_noise=75)
        self.vault.remove(self.ref,self.config.pk,self.config.config_revision,disable=True)
        preview=self.preview(point)
        self.assertEqual(len(preview['changes']),2)
        parent=self.restore(preview)
        self.assertEqual(len(parent['child_event_refs']),2)
        self.assertEqual(self.history.compare(self.ref,profile_ref=self.profile_ref,
            checkpoint_ref=point['checkpoint_ref'])['changes'],[])
        fields=dict(profile_ref=self.profile_ref,restore_command_ref=parent['restore_command_ref'],
            expected_state_digest=parent['after_state_digest'],command_id=str(uuid.uuid4()),confirmed='undo_taste_restore')
        undone=self.history.undo(self.ref,**fields)
        self.assertEqual(self.history.undo(self.ref,**fields),undone)
        self.assertEqual(FeedbackEvent.objects.count(),7)
        projections=list(FeedbackProjection.objects.values_list('value',flat=True))
        self.assertIn({'text':'Later exact note'},projections);self.assertIn({'value':True},projections)
        settings=PortableJourneyState.objects.get()
        self.assertEqual((settings.home_postcode,settings.radius_km,settings.next_noise),('3000',50,75))
        self.assertNotIn('Original exact note',json.dumps(list(PortableJourneyCommand.objects.filter(
            kind__startswith='taste.').values('descriptor','result','proof'))))

    def test_second_child_fault_rolls_back_every_restore_event_parent_and_monotonic_floor(self):
        from shelf.feedback import mutate_in_transaction
        point,_=self.older_and_current();preview=self.preview(point)
        before=list(FeedbackEvent.objects.values());self.state.refresh_from_db()
        floors=(self.state.policy_epoch,self.state.restriction_watermark);calls=[]
        def fail(**kwargs):
            calls.append(kwargs['command'].signal)
            if len(calls)==2:raise RuntimeError('Synthetic second child fault')
            return mutate_in_transaction(**kwargs)
        with patch('shelf.feedback.mutate_in_transaction',side_effect=fail),self.assertRaises(RuntimeError):
            self.restore(preview)
        self.assertEqual(list(FeedbackEvent.objects.values()),before)
        self.state.refresh_from_db();self.assertEqual((self.state.policy_epoch,self.state.restriction_watermark),floors)
        self.assertFalse(PortableJourneyCommand.objects.filter(kind='taste.restore').exists())

    def test_restore_replay_noop_and_later_choice_conflict_never_override_new_input(self):
        point,old=self.older_and_current();preview=self.preview(point)
        fields=dict(profile_ref=self.profile_ref,preview_ref=preview['preview_ref'],
            preview_digest=preview['preview_digest'],command_id=str(uuid.uuid4()),confirmed='restore_taste_choices')
        restored=self.history.restore(self.ref,**fields);count=FeedbackEvent.objects.count()
        self.assertEqual(self.history.restore(self.ref,**fields),restored)
        self.assertEqual(FeedbackEvent.objects.count(),count)
        noop=self.restore(self.preview(point))
        self.assertEqual(noop['outcome'],'unchanged');self.assertFalse(noop['child_event_refs'])
        self.assertEqual(FeedbackEvent.objects.count(),count)
        self.note('A later independent choice',entry_ref=old['entry_ref'],base_revision=3)
        with self.assertRaises(ga.AuthorityFault) as fault:
            self.history.undo(self.ref,profile_ref=self.profile_ref,
                restore_command_ref=restored['restore_command_ref'],expected_state_digest=restored['after_state_digest'],
                command_id=str(uuid.uuid4()),confirmed='undo_taste_restore')
        self.assertEqual(fault.exception.status,409)
        self.assertIn({'text':'A later independent choice'},list(FeedbackProjection.objects.values_list('value',flat=True)))

    def test_real_erasure_retires_reference_metadata_and_grammar_accepts_only_closed_owned_vectors(self):
        from django.db import connection
        from django.contrib.auth import get_user_model
        from shelf.card_actions import validate_journey_history
        from shelf.feedback import EraseCommand,erase_feedback,key_token
        point,_=self.older_and_current();preview=self.preview(point);self.restore(preview)
        with ga.recovery_guard(self.authority,self.ref,profile_ref=self.profile_ref):
            validate_journey_history(connection.connection)
        row=PortableJourneyCommand.objects.get(kind='taste.checkpoint',command_id=point['checkpoint_ref'])
        altered={**row.result,'unexpected_note_copy':'must be rejected'}
        altered['digest']=__import__('shelf.portable_taste_history',fromlist=['_digest'])._digest(
            {key:value for key,value in altered.items() if key!='digest'})
        with ga.current_writer(self.authority,self.ref,self.profile_ref),transaction.atomic(),ga._write_window():
            PortableJourneyCommand.objects.filter(pk=row.pk).update(result=altered)
        with self.assertRaises(ValueError):validate_journey_history(connection.connection)
        with ga.current_writer(self.authority,self.ref,self.profile_ref),transaction.atomic(),ga._write_window():
            PortableJourneyCommand.objects.filter(pk=row.pk).update(result=row.result)
        projection=FeedbackProjection.objects.get(signal='note')
        event=FeedbackEvent.objects.get(profile_id=self.state.profile_id,target_id=projection.target_id,
                                       signal='note',revision=projection.revision)
        token=key_token(str(projection.target_id),'note','item')
        command=EraseCommand(str(uuid.uuid4()),str(projection.target_id),'item',('note',),
            {token:projection.revision},{token:str(event.pk)},'ERASE_FICTIONAL_FEEDBACK')
        actor=get_user_model().objects.get(pk=self.state.actor_id)
        erase_feedback(actor=actor,profile_id=self.state.profile_id,command=command,recovery=self.coordinator)
        retired=PortableJourneyCommand.objects.filter(kind__startswith='taste.')
        self.assertTrue(retired.exists());self.assertTrue(all(row.state=='retired' and row.result=={} for row in retired))
        validate_journey_history(connection.connection)
        with self.assertRaises(ga.AuthorityFault) as fault:
            self.history.compare(self.ref,profile_ref=self.profile_ref,checkpoint_ref=point['checkpoint_ref'])
        # The established native recovery guard conservatively blocks all reads
        # after a committed restriction. History must keep that stricter policy.
        self.assertEqual(fault.exception.status,503)
        with self.assertRaises(ga.AuthorityFault):
            self.history.index(self.ref,profile_ref=self.profile_ref)


from .test_portable_feedback_views import NativeFeedbackOutcomeTests


class TasteHistoryPublicationTests(NativeFeedbackOutcomeTests):
    def test_two_child_card_has_one_point_restore_keeps_canonical_identity_but_never_bridges_publication(self):
        import hashlib
        from dataclasses import replace
        from shelf import profile_snapshot
        from shelf.card_actions import CardActions,CardAction
        from shelf.models import PortableCabinetManifest
        self.consent(self.profile_ref)
        target=self.publish_card();history=TasteHistory(self.authority)
        actions=CardActions(self.authority,service=self.enhance)
        current=self.enhance.current_for_interaction(self.ref,profile_ref=self.profile_ref)
        like=CardAction(self.profile_ref,current['publication_ref'],current['publication_revision'],
            target,0,0,str(uuid.uuid4()),'like')
        actions.apply(self.ref,like)
        point=PortableJourneyCommand.objects.get(kind='taste.checkpoint',result__origin_command_ref=like.command_id).result
        before=PortableJourneyCommand.objects.filter(kind='taste.checkpoint').count()
        favourite=replace(like,action='favourite',command_id=str(uuid.uuid4()),interest_revision=1)
        parent=actions.apply(self.ref,favourite)
        self.assertEqual(len(parent['child_event_refs']),2)
        self.assertEqual(PortableJourneyCommand.objects.filter(kind='taste.checkpoint').count(),before+1)
        comparison=history.compare(self.ref,profile_ref=self.profile_ref,checkpoint_ref=point['checkpoint_ref'])
        preview=history.preview(self.ref,profile_ref=self.profile_ref,checkpoint_ref=point['checkpoint_ref'],
            expected_state_digest=comparison['current_state_digest'],command_id=str(uuid.uuid4()))
        history.restore(self.ref,profile_ref=self.profile_ref,preview_ref=preview['preview_ref'],
            preview_digest=preview['preview_digest'],command_id=str(uuid.uuid4()),confirmed='restore_taste_choices')
        snapshot=profile_snapshot.capture(self.view(self.profile_ref))
        canonical='candidate-key-sha256:'+hashlib.sha256(b'fixture:movie:81001').hexdigest()
        nodes=[row for row in snapshot['evidence'] if row.get('key')==canonical]
        self.assertTrue(any(row['value']['signal']=='interest' and row['value']['value']==
            {'value':'yes','basis':'anticipated'} for row in nodes))
        with self.assertRaises(ga.AuthorityFault):
            self.enhance.current_for_interaction(self.ref,profile_ref=self.profile_ref)
        self.assertIsNotNone(PortableCabinetManifest.objects.get().content_redacted_at)

    def test_admission_captures_real_current_vector_once_without_backfilling_legacy_empty_snapshot(self):
        from shelf.enhance_contract import EnhanceIntent
        from shelf.models import SynthesisRun
        self.consent(self.profile_ref)
        values=self.note_fields(text='Current real native note')
        self.assertEqual(self.post('/cabinet/taste',values).status_code,303)
        point=PortableJourneyCommand.objects.get(kind='taste.checkpoint').result
        store=self.enhance._store(self.ref,self.profile_ref,str(self.config.pk))
        intent=EnhanceIntent(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4()),store.current().bindings)
        row=store.admit(intent)
        self.assertEqual(store.admit(intent).pk,row.pk)
        self.assertEqual(PortableJourneyCommand.objects.filter(kind='taste.checkpoint').count(),1)
        self.assertEqual(row.input_snapshot,{})
        self.assertEqual(TasteHistory(self.authority).index(self.ref,profile_ref=self.profile_ref)['checkpoints'][0]
                         ['checkpoint_ref'],point['checkpoint_ref'])
        self.assertEqual(SynthesisRun.objects.count(),1)
