"""Authored discovery remains distinct from ratings and existing taste history."""
from copy import deepcopy
from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import TestCase
from django.utils import timezone
from shelf import authored_discovery, synthesis, taste_funnel
from shelf.models import AuthoredDiscoveryState, Grant, Profile, DiscoveryState, SavedItem


class AuthoredSynthesisTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-authored-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='SYNTHETIC owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        data = authored_discovery.empty_data()
        data['seeds'] = [authored_discovery.seed_record(seed, 1, timezone.now().isoformat())
                         for seed in authored_discovery.DEFAULT_SEEDS]
        self.state = AuthoredDiscoveryState.objects.create(actor=self.actor, profile=self.profile,
            mode='household', nonce='synthetic', data=data)

    def test_five_authored_topics_are_exploration_not_ratings_or_endorsements(self):
        value = synthesis.snapshot(self.actor, 1)
        self.assertEqual(len(value['signals']), 5)
        self.assertEqual({row['basis'] for row in value['signals']}, {'discovery_seed'})
        self.assertEqual({row['medium'] for row in value['signals']}, {'topic'})
        self.assertEqual({row['value'] for row in value['signals']}, {'explore'})
        self.assertFalse(DiscoveryState.objects.exists())
        self.assertFalse(SavedItem.objects.exists())
        self.assertIn('authored_discovery', value['revision_map'])
        self.assertFalse(synthesis.signal_value('discovery_seed', 5))
        self.assertFalse(synthesis.signal_value('interest', 'explore'))

    def test_provenance_only_edit_invalidates_supervised_snapshot(self):
        before = taste_funnel.synthesis_snapshot(self.actor, 1)
        changed = deepcopy(self.state.data)
        changed['seeds'][0]['source_text'] += ' Additional explicit source context.'
        self.state.data = changed
        self.state.revision += 1
        self.state.save(update_fields=['data', 'revision'])
        after = taste_funnel.synthesis_snapshot(self.actor, 1)
        self.assertEqual(before['signals'], after['signals'])
        self.assertNotEqual(before['input_digest'], after['input_digest'])

    def test_sample_topics_and_revoked_grants_are_not_owner_inputs(self):
        self.state.mode = 'sample'
        self.state.save(update_fields=['mode'])
        self.assertEqual(taste_funnel.synthesis_snapshot(self.actor, 1)['signals'], [])
        self.grant.can_select = False
        self.grant.save(update_fields=['can_select'])
        with self.assertRaises(Http404):
            taste_funnel.synthesis_snapshot(self.actor, 1)
