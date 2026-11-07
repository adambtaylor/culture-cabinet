"""Synthetic cross-media observations enter the existing supervised snapshot."""
from copy import deepcopy
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from shelf import authored_discovery, authored_observations, synthesis
from shelf.models import AuthoredDiscoveryState, Grant, Profile
from shelf.tests.test_authored_observations import observation


class ObservationSynthesisTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username='SYNTHETIC graph owner')
        self.profile = Profile.objects.create(kind='owner', active=True, label='SYNTHETIC graph')
        Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.state = AuthoredDiscoveryState.objects.create(actor=self.actor, profile=self.profile,
            mode='household', nonce='synthetic', data=authored_discovery.empty_data())

    def store(self, rows):
        self.state.data['observations'] = authored_observations.validate_packet({'observations': rows}, self.actor.pk)
        self.state.save()
        return synthesis.snapshot(self.actor, self.profile.pk)

    def test_distinct_media_qualitative_intent_and_origin_reach_shared_snapshot(self):
        rows = [observation(id='graphic-invincible', medium='graphic_novel', title='Invincible'),
            observation(id='animated-invincible', medium='animation', title='Invincible'),
            observation(id='next-book', title='Lonesome Dove', reaction='selected_next', experience='not_established'),
            observation(id='uncertain-court', raw_title='Council of Owls', identity_status='provisional',
                        uncertainty='Court of Owls is a candidate; not owner confirmed'),
            observation(id='assistant-only', reaction='assistant_suggestion')]
        value = self.store(rows)
        signals = {row['key']: row for row in value['signals']}
        self.assertEqual(len(signals), 4)
        self.assertEqual(signals['authored_observation:graphic-invincible']['medium'], 'graphic_novel')
        self.assertEqual(signals['authored_observation:animated-invincible']['medium'], 'animation')
        self.assertIn('selected_next', signals['authored_observation:next-book']['value'])
        self.assertIn('not_established', signals['authored_observation:next-book']['value'])
        self.assertEqual(signals['authored_observation:uncertain-court']['title'], 'Council of Owls')
        self.assertFalse(any(row['basis'] in ('interest', 'enjoyment', 'favourite') for row in signals.values()))
        output = {'basis': synthesis.ASSOCIATION, 'clusters': [{'label': 'SYNTHETIC hypothesis',
            'members': list(signals)[:2], 'explanation': 'A possible cross-medium connection.'}],
            'links': [{'from': 'authored_observation:graphic-invincible',
                'to': 'authored_observation:animated-invincible', 'explanation': 'Possible association, not a shared viewing record.'}]}
        self.assertEqual(synthesis.validate_output(output, list(signals.values())), output)

    def test_source_only_edits_invalidate_existing_snapshot_without_rewriting_seed_data(self):
        seed = authored_discovery.seed_record(authored_discovery.DEFAULT_SEEDS[0], self.actor.pk, 'synthetic-capture')
        self.state.data['seeds'] = [seed]
        first = self.store([observation()])
        original = deepcopy(self.state.data['seeds'])
        self.state.data['observations'][0]['provenance']['source_id'] = 'synthetic-corrected-source'
        self.state.save()
        second = synthesis.snapshot(self.actor, self.profile.pk)
        self.assertEqual(first['signals'], second['signals'])
        self.assertNotEqual(first['input_digest'], second['input_digest'])
        self.assertEqual(self.state.data['seeds'], original)
        self.assertEqual(len([s for s in second['signals'] if s['basis'] == 'discovery_seed']), 1)
