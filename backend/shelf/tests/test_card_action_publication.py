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
