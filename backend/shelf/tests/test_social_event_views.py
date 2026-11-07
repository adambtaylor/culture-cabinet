"""Bounded public event handoff through the existing authenticated CAS form."""
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from shelf import social_discovery as storage, social_event_intake as events
from shelf.authority import SELECTION_KEY
from shelf.models import Grant, Profile, SocialDiscoveryState
from shelf.social_views import ActionForm


def observation(**changes):
    return dict(platform='facebook', platform_event_id='123456789',
        canonical_url='https://www.facebook.com/events/123456789', title='SYNTHETIC public concert',
        organizer='SYNTHETIC organizer', venue='SYNTHETIC public hall', raw_date_time='Friday 7pm',
        timezone_name='unknown', observed_at=timezone.now().isoformat(), evidence_ref='browser:synthetic',
        actor_origin='owner_confirmed', signal_type='interested', confidence='observed',
        lineage_ref='action:synthetic', public_confirmed='on', **changes)


class EventActionFormTests(SimpleTestCase):
    def payload(self, **changes):
        data = dict(mode='household', action='observe_event', revision=0, nonce='synthetic', confirmed='on')
        data.update(observation())
        data.update(changes)
        return data

    def test_requires_public_scope_and_rejects_cross_action_fields(self):
        self.assertTrue(ActionForm(self.payload()).is_valid())
        for changes in ({'public_confirmed': ''}, {'canonical_url': ''}, {'evidence_ref': ''},
                        {'lineage_ref': ''}, {'identity': 'a' * 64}, {'venue': 'x' * 161},
                        {'actor_origin': 'human'}, {'supersedes': 'a' * 32}):
            with self.subTest(changes=changes):
                self.assertFalse(ActionForm(self.payload(**changes)).is_valid())
        self.assertFalse(ActionForm(dict(mode='household', action='event_withdraw', revision=0,
            nonce='synthetic', identity='a' * 32, confirmed='on')).is_valid())


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class EventIntakeViewTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-event-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='SYNTHETIC owner')
        Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = 1
        session.save()
        self.base = '/profiles/1/social-discovery/'

    def state(self):
        return SocialDiscoveryState.objects.filter(actor=self.actor, profile=self.profile, mode='household').first()

    def payload(self, action='observe_event', **changes):
        state = self.state()
        data = dict(mode='household', action=action, revision=state.revision if state else 0,
                    nonce=state.nonce if state else storage.initial_nonce(1, 1, 'household'), confirmed='on')
        if action == 'observe_event':
            data.update(observation())
        data.update(changes)
        return data

    def test_public_observation_escaped_history_idempotency_stale_and_withdrawal(self):
        payload = self.payload(title='<script>alert(1)</script> concert')
        self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 302)
        rows = events.event_intake_view(self.state().data)
        self.assertEqual(len(rows), 1)
        page = self.client.get(self.base)
        self.assertContains(page, '&lt;script&gt;alert(1)&lt;/script&gt; concert')
        self.assertNotContains(page, '<script>alert(1)</script>')
        self.assertContains(page, 'A browser observation is not proof')
        self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 302)
        self.assertEqual(self.client.post(self.base + 'action/', dict(payload, title='Changed stale capture')).status_code, 409)
        repeated = self.payload()
        repeated.update({key: value for key, value in payload.items()
                         if key not in ('revision', 'nonce')})
        self.assertEqual(self.client.post(self.base + 'action/', repeated).status_code, 302)
        self.assertEqual(len(events.event_intake_view(self.state().data)), 1)
        withdraw = self.payload('event_withdraw', identity=rows[0]['id'])
        self.assertEqual(self.client.post(self.base + 'action/', withdraw).status_code, 302)
        self.assertEqual(events.event_intake_view(self.state().data), [])
        self.assertNotContains(self.client.get(self.base), '&lt;script&gt;alert(1)&lt;/script&gt; concert')

    def test_private_scope_tampering_duplicates_and_limits_do_not_store(self):
        for changes in ({'public_confirmed': ''}, {'private_party': 'on'}, {'venue': 'x' * 161},
                        {'canonical_url': 'javascript:alert(1)'}, {'observed_at': '2026-10-30T12:00:00'},
                        {'nonce': 'tampered'}):
            with self.subTest(changes=changes):
                self.assertIn(self.client.post(self.base + 'action/', self.payload(**changes)).status_code,
                              (400, 409))
                self.assertIsNone(self.state())
        payload = self.payload()
        payload['platform'] = ['facebook', 'instagram']
        self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 400)
        self.assertIsNone(self.state())

    def test_authentication_grants_and_csrf_are_required(self):
        anonymous = Client()
        self.assertEqual(anonymous.post(self.base + 'action/', self.payload()).status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.actor)
        session = csrf_client.session
        session[SELECTION_KEY] = 1
        session.save()
        self.assertEqual(csrf_client.post(self.base + 'action/', self.payload()).status_code, 403)
        Grant.objects.filter(user=self.actor).update(can_select=False)
        self.assertEqual(self.client.post(self.base + 'action/', self.payload()).status_code, 404)
        self.assertIsNone(self.state())
