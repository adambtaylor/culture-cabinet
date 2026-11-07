"""Household social intent stays typed, scoped and bound to synthesis provenance."""
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import Http404
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase
from django.utils import timezone

from shelf import social_event_intake as events, synthesis, taste_funnel
from shelf.acquisition_contract import Blocked
from shelf.calendar_handoff import digest
from shelf.discovery_state import Conflict
from shelf.models import DiscoveryState, Grant, Profile, SavedItem, SocialDiscoveryState, SynthesisRun


class SocialIntentFunnelTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-social-intent-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Synthetic owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('No network')))

    def state(self, data, **scope):
        return SocialDiscoveryState.objects.create(actor=scope.get('actor', self.actor),
            profile=scope.get('profile', self.profile), mode=scope.get('mode', 'household'),
            nonce='synthetic-social-nonce', data=data)

    def snapshot(self):
        return taste_funnel.synthesis_snapshot(self.actor, self.profile.pk)

    def observe(self, data=None, identity='123', **changes):
        fields = dict(action='observe_event', platform='facebook', platform_event_id=identity,
            canonical_url=f'https://www.facebook.com/events/{identity}', title='Synthetic event',
            organizer='', venue='', raw_date_time='Nov 21 7pm', timezone_name='unknown',
            observed_at='2026-10-30T01:00:00+00:00', evidence_ref='synthetic-browser-1',
            actor_origin='owner_confirmed', signal_type='interested', confidence='observed',
            lineage_ref='synthetic-action-' + identity, public_confirmed=True, supersedes='', conflict_note='')
        fields.update(changes)
        return events.apply_event_action(data or {}, fields,
            captured_at='2026-10-30T01:01:00+00:00', capture_actor='owner:1')

    def change(self, state, data):
        state.data = data
        state.revision += 1
        state.save(update_fields=['data', 'revision'])

    def request(self):
        request = RequestFactory().post('/profiles/1/taste/synergize/')
        request.user = self.actor
        request.session = SessionStore()
        request.session['selected_profile_id'] = self.profile.pk
        request.session.save()
        return request

    def test_only_observed_owner_intent_enters_synthesis_without_metadata_or_false_ratings(self):
        data = self.observe(venue='Synthetic public venue', raw_date_time='Unresolved raw time')
        for identity, origin in (('124', 'agent_marked'), ('125', 'unknown'),
                                 ('26754161534246333', 'owner_confirmed'),
                                 ('1092959976817399', 'owner_confirmed')):
            data = self.observe(data, identity, actor_origin=origin)
        data = self.observe(data, '126', confidence='reported')
        data = self.observe(data, '127', confidence='uncertain')
        data['sources'] = [{'platform': 'instagram', 'scope': 'followed', 'label': 'Synthetic followed page'}]
        self.state(data)
        value = synthesis.snapshot(self.actor, self.profile.pk)
        self.assertEqual(value['signals'], [{'key': events.event_identity('facebook', '123'),
            'title': 'Synthetic event', 'medium': 'event', 'source': 'Public social event observation',
            'basis': 'social_intent', 'value': 'interested'}])
        self.assertFalse(DiscoveryState.objects.exists())
        self.assertFalse(SavedItem.objects.exists())

    def test_duplicate_contradictory_and_corrected_intent_preserve_one_typed_identity(self):
        data = self.observe()
        self.assertEqual(self.observe(data), data)
        original = data['event_intake']['observations'][0]['id']
        data = self.observe(data, signal_type='attending', evidence_ref='synthetic-browser-2',
            raw_date_time='Conflicting raw time')
        state = self.state(data)
        before_correction = self.snapshot()
        self.assertEqual([(s['basis'], s['value']) for s in self.snapshot()['signals']],
            [('social_intent', 'attending')])
        attending = data['event_intake']['observations'][-1]['id']
        data = self.observe(data, signal_type='none', supersedes=attending,
            evidence_ref='synthetic-correction', conflict_note='Owner withdrew Attending observation')
        self.change(state, data)
        # The explicit correction removes only its superseded evidence; the
        # independent original Interested statement remains qualitative intent.
        self.assertEqual(self.snapshot()['signals'][0]['value'], 'interested')
        self.assertNotEqual(self.snapshot()['input_digest'], before_correction['input_digest'])
        self.assertTrue(any(row['id'] == original for row in events.event_projection(data)))

    def test_unknown_lineage_copy_needs_auditable_direct_owner_correction(self):
        data = self.observe(actor_origin='unknown', lineage_ref='unknown-action-lineage')
        unknown = data['event_intake']['observations'][0]['id']
        state = self.state(data)
        self.change(state, self.observe(state.data, actor_origin='owner_confirmed',
            lineage_ref='unknown-action-lineage'))
        self.assertEqual(self.snapshot()['signals'], [])
        before_correction = self.snapshot()
        self.change(state, self.observe(state.data, actor_origin='owner_confirmed',
            lineage_ref='unknown-action-lineage', supersedes=unknown,
            evidence_ref='synthetic-direct-owner-confirmation'))
        self.assertEqual(self.snapshot()['signals'][0]['value'], 'interested')
        self.assertNotEqual(self.snapshot()['input_digest'], before_correction['input_digest'])

    def test_agent_lineage_and_withdrawal_invalidate_queued_and_completed_receipts(self):
        state = self.state(self.observe(lineage_ref='shared-action-lineage'))
        before = self.snapshot()
        request = self.request()
        run = synthesis.request(request, self.profile.pk, before['input_digest'])
        data = self.observe(state.data, '124', actor_origin='agent_marked', lineage_ref='shared-action-lineage')
        self.change(state, data)
        current = self.snapshot()
        self.assertEqual(current['signals'], [])
        self.assertNotEqual(current['input_digest'], before['input_digest'])
        # A remaining explicit signal permits the stale-input check to run.
        self.change(state, self.observe(state.data, '128'))
        with self.assertRaisesMessage(Conflict, 'Taste signals changed'):
            synthesis.request(request, self.profile.pk, before['input_digest'])
        with self.assertRaisesMessage(Conflict, 'Queued input is stale'):
            synthesis.reserve_operator(actor=self.actor, profile_id=self.profile.pk,
                run_id=run.pk, session_guard=lambda: None)
        run.state = 'completed'
        run.output = {'synthetic': 'historical'}
        self.assertEqual(synthesis.status(run, self.snapshot()['input_digest'])['state'], 'stale')
        self.assertIsNone(synthesis.status(run, self.snapshot()['input_digest'])['output'])
        before_withdrawal = self.snapshot()
        identity = next(row['id'] for row in events.event_projection(state.data)
            if row['platform_event_id'] == '128')
        self.change(state, events.apply_event_action(state.data,
            {'action': 'event_withdraw', 'identity': identity},
            captured_at='2026-10-30T01:02:00+00:00', capture_actor='owner:1'))
        self.assertEqual(self.snapshot()['signals'], [])
        self.assertNotEqual(self.snapshot()['input_digest'], before_withdrawal['input_digest'])

    def test_household_actor_profile_isolation_and_read_only_projection(self):
        other = get_user_model().objects.create_user(username='SYNTHETIC-other-actor')
        profile = Profile.objects.create(kind='partner', active=True, label='Synthetic other profile')
        self.state({'isolated': 'sample'}, mode='sample')
        self.state({'isolated': 'actor'}, actor=other)
        self.state({'isolated': 'profile'}, profile=profile)
        before = self.snapshot()
        with patch.object(events, 'event_signals', wraps=events.event_signals) as project:
            self.assertEqual(self.snapshot(), before)
            project.assert_called_once_with({})
        self.assertEqual(before['signals'], [])
        self.assertFalse(DiscoveryState.objects.exists())
        self.assertFalse(SynthesisRun.objects.exists())
        self.assertEqual(SocialDiscoveryState.objects.count(), 3)

    def test_full_social_provenance_binds_digest_even_without_emitted_signals(self):
        state = self.state({})
        before = self.snapshot()
        state.revision += 1
        state.save(update_fields=['revision'])
        after = self.snapshot()
        self.assertEqual(before['signals'], after['signals'])
        self.assertNotEqual(before['input_digest'], after['input_digest'])
        self.assertEqual(after['revision_map']['social_intent'],
            {'revision': 1, 'fingerprint': events.event_revision(state.data)})
        self.assertEqual(after['input_digest'], digest({k: v for k, v in after.items() if k != 'input_digest'}))

    def test_social_values_remain_distinct_from_ratings_and_proof_claims(self):
        for value in ('interested', 'attending'):
            self.assertTrue(synthesis.signal_value('social_intent', value))
        for value in (True, 1, 5, 'enjoyed', 'attended', 'ticket_owned', 'paid', 'unknown'):
            self.assertFalse(synthesis.signal_value('social_intent', value))
        data = self.snapshot()
        data['signals'] = [{'key': 'facebook:event:123', 'title': 'Synthetic event',
            'medium': 'movie', 'source': 'Facebook', 'basis': 'social_intent', 'value': 'attending'}]
        data['input_digest'] = digest({k: v for k, v in data.items() if k != 'input_digest'})
        with patch.object(taste_funnel, 'synthesis_snapshot', return_value=data):
            with self.assertRaisesMessage(Blocked, 'Minimal typed taste signals'):
                synthesis.snapshot(self.actor, self.profile.pk)

    def test_existing_explicit_rating_and_favourite_keep_their_meaning(self):
        card = {'key': 'tmdb:movie:123', 'title': 'Synthetic film', 'medium': 'movie', 'source': 'TMDB',
            'url': 'https://www.themoviedb.org/movie/123', 'provenance': 'live-automated',
            'lane': 'synthetic', 'roots': [], 'fetched_at': timezone.now().isoformat()}
        history = [{'operation': 'rate', 'key': card['key'], 'card': card, 'basis': 'enjoyment',
            'value': 4, 'experienced': True, 'mode': 'household', 'origin': 'authenticated-human-post',
            'at': timezone.now().isoformat()}]
        DiscoveryState.objects.create(actor=self.actor, profile=self.profile, mode='household',
            nonce='synthetic-discovery', history=history)
        SavedItem.objects.create(actor=self.actor, profile=self.profile, provider='tmdb', medium='movie',
            source_id=123, list_kind='favorites', title=card['title'], url=card['url'], fetched_at=timezone.now())
        self.state(self.observe(signal_type='attending'))
        signals = self.snapshot()['signals']
        self.assertEqual([(s['basis'], s['value']) for s in signals if s['key'] == card['key']],
            [('enjoyment', 4), ('favourite', True)])
        self.assertEqual([(s['basis'], s['value']) for s in signals if s['medium'] == 'event'],
            [('social_intent', 'attending')])
        self.grant.can_select = False
        self.grant.save()
        with self.assertRaises(Http404):
            self.snapshot()

    def test_signal_cap_applies_after_exact_duplicate_collapse(self):
        signal = {'key': 'facebook:event:123', 'title': 'Synthetic event', 'medium': 'event',
            'source': 'Facebook', 'basis': 'social_intent', 'value': 'interested'}
        with patch.object(events, 'event_signals', return_value=[signal, deepcopy(signal)]):
            self.assertEqual(self.snapshot()['signals'], [signal])
        with patch.object(events, 'event_signals', return_value=[dict(signal, key=f'facebook:event:{n}') for n in range(501)]):
            with self.assertRaisesMessage(Conflict, 'smaller explicit history'):
                self.snapshot()
