"""Actual native state/publication checks; cards are explicitly fictional."""
from dataclasses import replace
import uuid
from django.db import transaction
from shelf import grant_authority as ga
from shelf.card_actions import CardActions, CardAction, CardUndo
from shelf.models import PortableCabinetManifest, PortableJourneyCommand, FeedbackEvent
from shelf.portable_authority import AuthorityFault
from .test_portable_feedback_views import NativeFeedbackOutcomeTests


class CardPublicationTests(NativeFeedbackOutcomeTests):
    def prepare(self):
        self.consent(self.profile_ref)
        target = self.publish_card()
        self.actions = CardActions(self.authority, service=self.enhance)
        current = self.enhance.current_for_interaction(self.ref, profile_ref=self.profile_ref)
        return CardAction(self.profile_ref, current['publication_ref'], current['publication_revision'],
                          target, 0, 0, str(uuid.uuid4()), 'like')

    def test_multiple_current_actions_and_undo_keep_sources_without_stale_send_authority(self):
        command = self.prepare()
        self.actions.apply(self.ref, command)
        current = self.enhance.current_for_interaction(self.ref, profile_ref=self.profile_ref)
        self.assertEqual(current['ranking_state'], 'feedback_pending')
        self.assertEqual(len(next(row for row in current['manifest']['categories'] if row['category']=='movie')['cards']),1)
        self.assertEqual(current['manifest']['for_you'], [])
        with self.assertRaises(AuthorityFault):self.enhance.current(self.ref, profile_ref=self.profile_ref)
        self.assertIsNone(PortableCabinetManifest.objects.get().content_redacted_at)
        second = replace(command, action='favourite', command_id=str(uuid.uuid4()), interest_revision=1)
        self.actions.apply(self.ref, second)
        current = self.enhance.current_for_interaction(self.ref, profile_ref=self.profile_ref)
        self.assertEqual(current['ranking_state'], 'feedback_pending')
        self.actions.undo(self.ref, CardUndo(self.profile_ref, second.command_id, str(uuid.uuid4()), 2, 1))
        self.assertEqual(self.enhance.current_for_interaction(self.ref, profile_ref=self.profile_ref)['ranking_state'],
                         'feedback_pending')
        self.assertEqual(self.actions.apply(self.ref, command)['selected_state'], 'like')

    def test_other_feedback_breaks_chain_and_commits_redaction(self):
        command = self.prepare()
        self.actions.apply(self.ref, command)
        fields = self.note_fields()
        self.assertEqual(self.post('/cabinet/taste', fields).status_code,303)
        with self.assertRaises(AuthorityFault):self.enhance.current_for_interaction(self.ref, profile_ref=self.profile_ref)
        publication = PortableCabinetManifest.objects.get()
        self.assertIsNotNone(publication.content_redacted_at)
        self.assertEqual(publication.manifest, {})
        # Its historical action receipt remains readable without source facts.
        self.assertEqual(self.actions.receipt(self.ref,self.profile_ref,command.command_id)['selected_state'],'like')

    def test_missing_proof_receipt_or_expiry_cannot_resurrect_cards(self):
        command = self.prepare()
        self.actions.apply(self.ref, command)
        # An incomplete restore missing the coordinating proof fails closed even
        # though the original typed feedback event remains valid personal data.
        with ga.current_writer(self.authority,self.ref,self.profile_ref), transaction.atomic(),ga._write_window():
            PortableJourneyCommand.objects.all().delete()
        with self.assertRaises(AuthorityFault):self.enhance.current_for_interaction(self.ref,profile_ref=self.profile_ref)
        self.assertIsNotNone(PortableCabinetManifest.objects.get().content_redacted_at)

    def test_revocation_or_original_expiry_still_denies_feedback_view(self):
        command=self.prepare()
        self.actions.apply(self.ref,command)
        self.clock.advance(601)
        with self.assertRaises(AuthorityFault):self.enhance.current_for_interaction(self.ref,profile_ref=self.profile_ref)
        self.assertIsNotNone(PortableCabinetManifest.objects.get().content_redacted_at)

    def test_extra_grant_change_during_child_write_aborts_all_feedback_and_proof(self):
        from unittest.mock import patch
        from shelf.feedback import mutate_in_transaction
        from shelf.models import Grant
        command=self.prepare();count=FeedbackEvent.objects.count()
        def injected(**kwargs):
            result=mutate_in_transaction(**kwargs)
            Grant.objects.filter(user_id=self.state.actor_id,profile_id=self.state.profile_id).update(can_export=False)
            return result
        with patch('shelf.feedback.mutate_in_transaction',side_effect=injected),self.assertRaises(AuthorityFault):
            self.actions.apply(self.ref,command)
        self.assertEqual(FeedbackEvent.objects.count(),count)
        self.assertTrue(Grant.objects.get(user_id=self.state.actor_id,profile_id=self.state.profile_id).can_export)
        self.assertFalse(PortableJourneyCommand.objects.filter(command_id=command.command_id).exists())

    def test_arbitrary_extra_floor_increment_is_not_a_bridge_even_with_normal_children(self):
        from unittest.mock import patch
        from django.db.models import F
        from shelf.feedback import mutate_in_transaction
        from shelf.models import ProfileCabinetState
        command=self.prepare();self.state.refresh_from_db();before=(self.state.policy_epoch,self.state.restriction_watermark)
        def injected(**kwargs):
            result=mutate_in_transaction(**kwargs)
            ProfileCabinetState.objects.filter(pk=self.state.pk).update(
                policy_epoch=F('policy_epoch')+4,restriction_watermark=F('restriction_watermark')+4)
            return result
        with patch('shelf.feedback.mutate_in_transaction',side_effect=injected),self.assertRaises(AuthorityFault):
            self.actions.apply(self.ref,command)
        self.state.refresh_from_db();self.assertEqual((self.state.policy_epoch,self.state.restriction_watermark),before)
        self.assertFalse(PortableJourneyCommand.objects.filter(command_id=command.command_id).exists())

    def test_restore_grammar_accepts_actual_children_and_rejects_missing_receipt_or_bad_proof(self):
        from django.db import connection
        from shelf.card_actions import validate_journey_history
        from shelf.models import MutationReceipt
        command=self.prepare();self.actions.apply(self.ref,command)
        raw=connection.connection
        validate_journey_history(raw)
        with ga.current_writer(self.authority,self.ref,self.profile_ref),transaction.atomic(),ga._write_window():
            MutationReceipt.objects.filter(event_id=FeedbackEvent.objects.get().pk).delete()
        with self.assertRaises(ValueError):validate_journey_history(raw)
        with self.assertRaises(AuthorityFault):self.enhance.current_for_interaction(self.ref,profile_ref=self.profile_ref)
        self.assertIsNotNone(PortableCabinetManifest.objects.get().content_redacted_at)

    def test_obtain_favourite_keeps_real_native_tv_origin_deadline_and_navigates_without_send(self):
        from shelf.source_authority import configure_source
        from shelf.models import PortableConnection,SynthesisRun
        from shelf.disclosure import canonical_bytes
        from shelf.enhance_contract import EnhanceIntent
        from shelf.portable_reactions import target_ref
        from shelf.portable_saved_authority import selected_context,portable_saved_scope
        from shelf.portable_obtain_origin import resolve_current_origin
        from .test_owner_tv_entry import OwnerPolicy
        self.consent(self.profile_ref)
        identifier=configure_source(self.authority,self.ref,confirmed=True,adapter='tmdb_tv')
        source=PortableConnection.objects.get(pk=identifier)
        self.vault.replace(self.ref,source.pk,source.config_revision,b'SYNTHETIC-TV')
        ga.change_grant(self.authority,self.ref,profile_ref=self.profile_ref,connection_ref=identifier,
            purpose='catalogue_lookup',allowed_data_classes=['canonical_seed'],expected_revision=0,
            expires_at=self.expires,action='grant')
        self.enhance._policy_provider=OwnerPolicy(self.clock.wall)
        store=self.enhance._store(self.ref,self.profile_ref,str(self.config.pk))
        row=store.admit(EnhanceIntent(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4()),store.current().bindings))
        card=dict(key='tmdb:tv:1399',category='tv',title='Fictional TV evidence',medium='tv',provider='tmdb',
            source_id='1399',source_url='https://www.themoviedb.org/tv/1399',identity_status='confirmed',
            evidence_type='cached_lookup',observed_at=self.clock.wall.isoformat(),valid_until=self.expires.isoformat(),
            rights_ref='synthetic-owner-tv',source_roots=['synthetic-tv-evidence'],facts=[])
        outcomes=[dict(category=k,state='fresh' if k=='tv' else 'unavailable',candidate_count=int(k=='tv'),
            code='OK' if k=='tv' else 'CAPABILITY_UNAVAILABLE') for k in self.enhance.registry.categories]
        row=store.checkpoint(str(row.pk),stage='awaiting_preview',outcomes=outcomes,
            metadata={'phase':'inference','submitted_refs':{'signals':[],
                'candidates':[{'key':card['key'],'evidence_refs':card['source_roots']}]}},
            collection_digest='c'*64,body_digest='b'*64,sealed_run_digest='d'*64)
        row=store.reserve(str(row.pk),body_digest=row.body_digest,expected_revision=row.revision)
        store.record_result(str(row.pk),canonical_bytes(dict(schema='cabinet-inference-result/v1',associations=None,
            candidate_assessments=[dict(candidate_key=card['key'],decision='uncertain',signal_keys=[],
                evidence_refs=card['source_roots'],hypothesis='Fictional attribution test only.')])))
        self.state.refresh_from_db()
        store.publish(str(row.pk),manifest=dict(schema='cabinet-manifest/v1',categories=[dict(category=k,
            state='fresh' if k=='tv' else 'unavailable',code='OK' if k=='tv' else 'CAPABILITY_UNAVAILABLE',
            cards=[card] if k=='tv' else []) for k in self.enhance.registry.categories],for_you=[]),
            expected_publication_revision=self.state.publication_revision,allowed_candidates=[card])
        current=self.enhance.current_for_interaction(self.ref,profile_ref=self.profile_ref)
        operations=CardActions(self.authority,service=self.enhance)
        command=CardAction(self.profile_ref,current['publication_ref'],current['publication_revision'],
            target_ref(self.profile_ref,card['key']),0,0,str(uuid.uuid4()),'obtain')
        before_calls=self.fake.calls
        result=operations.apply(self.ref,command)
        self.assertEqual(result['selected_state'],'favourite')
        self.assertEqual(result['obtain']['state'],'entry_ready')
        self.assertTrue(result['obtain']['href'].startswith('/cabinet/obtain/options/?'))
        self.assertEqual(FeedbackEvent.objects.count(),1)
        self.assertEqual(self.fake.calls,before_calls)
        self.assertEqual(SynthesisRun.objects.count(),1)
        token=selected_context(self.authority,self.ref,operations.selection)
        with portable_saved_scope(self.authority,self.ref,profile_ref=self.profile_ref,selection_token=token) as access:
            origin,_=resolve_current_origin(access,publication_revision=current['publication_revision'],
                candidate_key=card['key'],scope='series_s01e01',now=self.authority.now())
        publication=PortableCabinetManifest.objects.get()
        from datetime import datetime
        self.assertLessEqual(datetime.fromisoformat(origin['valid_until'].replace('Z','+00:00')),publication.expires_at)
        self.assertEqual(operations.apply(self.ref,command),result)
        self.assertEqual(FeedbackEvent.objects.count(),1)
