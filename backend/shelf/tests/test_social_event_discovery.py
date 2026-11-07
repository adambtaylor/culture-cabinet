"""Observed event cards share exact connector identities without taste writes."""
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase
from django.utils import timezone

from shelf import social_discovery, social_event_intake
from shelf.authority import SELECTION_KEY
from shelf.models import Grant, Profile, SocialDiscoveryState
from shelf.social_sources import SocialCandidate


class ObservedEventDiscoveryTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-event-discovery')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='SYNTHETIC owner')
        Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.request = RequestFactory().get('/profiles/1/social-discovery/')
        self.request.user = self.actor
        self.request.session = SessionStore()
        self.request.session[SELECTION_KEY] = 1
        self.request.session.save()
        now = timezone.now().isoformat()
        self.url = 'https://www.facebook.com/events/12345'
        self.fields = dict(action='observe_event', platform='facebook', platform_event_id='12345',
            canonical_url=self.url, title='SYNTHETIC observed concert', organizer='', venue='',
            raw_date_time='Friday 7pm', timezone_name='unknown', observed_at=now,
            evidence_ref='browser:synthetic-capture', actor_origin='agent_marked',
            signal_type='interested', confidence='observed', lineage_ref='action:synthetic',
            public_confirmed=True)
        data = social_event_intake.apply_event_action(social_discovery.empty_data(), self.fields,
            captured_at=now, capture_actor='user:1')
        self.state = SocialDiscoveryState.objects.create(actor=self.actor, profile=self.profile,
            mode='household', nonce='synthetic', data=data)

    def test_agent_event_discovery_retains_origin_without_preference(self):
        cards = social_discovery.reviewed_candidates(self.request, 1, [])
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card.url, self.url)
        self.assertEqual(card.social_evidence[0]['actor_origin'], 'agent_marked')
        self.assertFalse(card.social_evidence[0]['eligible'])
        self.assertEqual(social_event_intake.event_signals(self.state.data), [])
        self.assertIn('need confirmation', card.freshness(timezone.now()))

    def test_exact_connector_url_deduplicates_but_similar_title_does_not(self):
        def existing(url):
            return SocialCandidate(key='official:event:fixture', title=self.fields['title'],
                kind='event', source='SYNTHETIC connector', provenance='fixture', rank=1,
                retrieved_at=timezone.now().isoformat(), url=url, category='all')
        self.assertEqual(social_discovery.reviewed_candidates(self.request, 1, [existing(self.url + '/')]), [])
        self.assertEqual(len(social_discovery.reviewed_candidates(self.request, 1,
            [existing('https://www.facebook.com/events/99999')])), 1)
        self.state.refresh_from_db()
        self.assertEqual(len(social_event_intake.event_intake_view(self.state.data)), 1)

    def test_sample_observations_and_withdrawn_event_do_not_enter_discovery(self):
        self.state.mode = 'sample'
        self.state.save(update_fields=['mode'])
        self.assertEqual(social_discovery.reviewed_candidates(self.request, 1, []), [])
        self.state.mode = 'household'
        observation = social_event_intake.event_intake_view(self.state.data)[0]
        self.state.data = social_event_intake.apply_event_action(self.state.data,
            {'action': 'event_withdraw', 'identity': observation['id']},
            captured_at=timezone.now().isoformat(), capture_actor='user:1')
        self.state.save(update_fields=['mode', 'data'])
        self.assertEqual(social_discovery.reviewed_candidates(self.request, 1, []), [])
