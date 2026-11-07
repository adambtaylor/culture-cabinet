"""Synthetic explicit seed storage, ownership, replay and bounded refresh tests."""
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import RequestFactory, TestCase

from shelf import authored_discovery as storage
from shelf.authority import SELECTION_KEY
from shelf.discovery_state import Conflict
from shelf.models import AuthoredDiscoveryState, DiscoveryState, Grant, Profile


class AuthoredDiscoveryTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-authored-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='SYNTHETIC owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = self.profile.pk
        session.save()
        self.request = RequestFactory().post('/')
        self.request.user = self.actor
        self.request.session = session
        self.mode = 'household'

    def state(self):
        return AuthoredDiscoveryState.objects.filter(actor=self.actor, profile=self.profile, mode=self.mode).first()

    def fields(self, action, **changes):
        state = self.state()
        return dict(action=action, mode=self.mode, revision=state.revision if state else 0,
            nonce=state.nonce if state else storage.initial_nonce(self.actor.pk, self.profile.pk, self.mode), **changes)

    def apply(self, action, **changes):
        return storage.apply(self.request, self.profile.pk, self.fields(action, **changes))

    def defaults(self):
        return self.apply('seed_authorized_defaults', authorization=storage.DEFAULT_AUTHORIZATION)

    def test_defaults_are_explicit_idempotent_provenance_and_never_ratings(self):
        with self.assertRaises(ValueError):
            self.apply('seed_authorized_defaults')
        self.assertIsNone(self.state())
        state = self.defaults()
        original = deepcopy(state.data)
        self.assertEqual(len(original['seeds']), 5)
        self.assertEqual({row['id'] for row in original['seeds']},
            {'deep_dives', 'macroeconomics', 'sleep_health', 'living_soil', 'unusual_history'})
        for row in original['seeds']:
            self.assertTrue(row['captured_at'])
            self.assertEqual(row['captured_by'], 'user:' + str(self.actor.pk))
            self.assertTrue(row['source_text'])
            self.assertEqual(row['authorization'], storage.DEFAULT_AUTHORIZATION)
        self.defaults()
        self.assertEqual(self.state().data, original)
        self.assertFalse(DiscoveryState.objects.exists())
        signal = storage.signals(original)[0]
        self.assertEqual(set(signal), {'key', 'title', 'medium', 'source', 'basis', 'value'})
        self.assertEqual((signal['medium'], signal['basis'], signal['value']), ('topic', 'discovery_seed', 'explore'))
        changed = deepcopy(original)
        changed['seeds'][0]['source_text'] += ' amended'
        self.assertNotEqual(storage.revision(original), storage.revision(changed))

    def test_replay_changed_nonce_and_stale_forms(self):
        fields = self.fields('seed_authorized_defaults', authorization=storage.DEFAULT_AUTHORIZATION)
        first = storage.apply(self.request, self.profile.pk, fields)
        self.assertEqual(storage.apply(self.request, self.profile.pk, fields).revision, first.revision)
        with self.assertRaises(Conflict):
            storage.apply(self.request, self.profile.pk, dict(fields, action='seed_remove', identity='deep_dives'))
        with self.assertRaises(Conflict):
            storage.apply(self.request, self.profile.pk, dict(fields, nonce='stale'))

    def test_persisted_selection_revoked_grant_and_actor_isolation(self):
        self.defaults()
        self.grant.can_select = False
        self.grant.save()
        with self.assertRaises(Http404):
            self.apply('seed_remove', identity='deep_dives')
        self.grant.can_select = True
        self.grant.save()
        persisted = self.client.session
        persisted[SELECTION_KEY] = self.profile.pk + 100
        persisted.save()
        with self.assertRaises(Http404):
            storage.owned(self.request, self.profile.pk, self.mode)
        persisted[SELECTION_KEY] = self.profile.pk
        persisted.save()
        other = get_user_model().objects.create_user(username='SYNTHETIC-other')
        Grant.objects.create(user=other, profile=self.profile, can_view=True, can_select=True)
        self.request.user = other
        with self.assertRaises(Http404):
            storage.owned(self.request, self.profile.pk, self.mode)

    def test_sample_separate_and_invalid_mode(self):
        self.defaults()
        self.mode = 'sample'
        self.assertIsNone(self.state())
        self.defaults()
        self.assertEqual(AuthoredDiscoveryState.objects.count(), 2)
        with self.assertRaises(ValueError):
            storage.owned(self.request, self.profile.pk, 'other')

    def test_seed_validation_cap_and_cleanup_at_receipt_limit(self):
        seed = dict(storage.DEFAULT_SEEDS[0], id='custom', source_text='SYNTHETIC direct instruction')
        with self.assertRaises(ValueError):
            self.apply('seed_add', seed=dict(seed, rating='loved'))
        with self.assertRaises(ValueError):
            self.apply('seed_add', seed=dict(seed, terms=['a'] * 13))
        seed['source_text'] = '  SYNTHETIC source text\nsecond line  '
        state = self.apply('seed_add', seed=seed)
        self.assertEqual(state.data['seeds'][0]['source_text'], seed['source_text'])
        with patch.object(storage, 'MAX_SEEDS', 1), self.assertRaises(Conflict):
            self.defaults()
        AuthoredDiscoveryState.objects.filter(pk=state.pk).update(receipts={str(n): 'digest' for n in range(storage.MAX_RECEIPTS)})
        with self.assertRaises(Conflict):
            self.defaults()
        self.apply('seed_remove', identity='custom')
        self.assertEqual(self.state().data['seeds'], [])
        self.assertEqual(len(self.state().receipts), storage.MAX_RECEIPTS)

    def test_shortlist_trusted_snapshot_and_bounded_catalog(self):
        episode = dict(key='apple:episode:123', title='SYNTHETIC episode', show='SYNTHETIC show',
            apple_episode_id='123', apple_url='https://podcasts.apple.com/us/podcast/id456?i=123',
            books=[], private_payload='must never persist')
        fields = self.fields('shortlist_add', identity=episode['key'], title='UNTRUSTED POST title')
        with self.assertRaises(ValueError):
            storage.apply(self.request, self.profile.pk, fields)
        state = storage.apply(self.request, self.profile.pk, fields, catalog={episode['key']: episode})
        self.assertEqual(state.data['shortlist'][0]['title'], episode['title'])
        self.assertNotIn('private_payload', state.data['shortlist'][0])
        self.apply('shortlist_remove', identity=episode['key'])
        self.assertEqual(self.state().data['shortlist'], [])
        with self.assertRaises(ValueError):
            storage.episode_snapshot(episode['key'], {episode['key']: dict(episode, title='x' * 501)})

    def test_refresh_reserved_once_and_superseded_results_rejected(self):
        fields = self.fields('refresh', identity='deep_dives')
        reserved = storage.apply(self.request, self.profile.pk, fields)
        self.assertIsNone(storage.apply(self.request, self.profile.pk, fields))
        storage.check_result(self.request, self.profile.pk, reserved)
        self.defaults()
        with self.assertRaises(Conflict):
            storage.check_result(self.request, self.profile.pk, reserved)
        with self.assertRaises(ValueError):
            self.apply('refresh', identity='unknown')
        self.assertTrue(storage.available())

    def operator(self, guard=lambda: None):
        return storage.authorize_defaults_operator(self.actor, self.profile.pk, guard)

    def test_operator_requires_current_fixed_owner_and_session_guard(self):
        with self.assertRaises(ValueError):
            self.operator()
        self.actor.username = 'demo-curator'
        self.actor.save()
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.operator(lambda: (_ for _ in ()).throw(ValueError('expired')))
        self.assertIsNone(self.state())
        self.grant.can_select = False
        self.grant.save()
        with self.assertRaises(ValueError):
            self.operator()
        self.assertIsNone(self.state())

    def test_operator_idempotence_preserves_custom_seed_and_owner_removal(self):
        custom = dict(storage.DEFAULT_SEEDS[0], id='custom', topic='SYNTHETIC custom topic')
        self.apply('seed_add', seed=custom)
        self.actor.username = 'demo-curator'
        self.actor.save()
        calls = []
        state = self.operator(lambda: calls.append('checked'))
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(len(state.data['seeds']), 6)
        self.assertEqual(state.data['seeds'][0]['topic'], 'SYNTHETIC custom topic')
        self.assertEqual(state.data['seeds'][1]['captured_actor'], 'operator_for_owner:1')
        original = deepcopy(state.data)
        self.assertEqual(self.operator().revision, state.revision)
        self.assertEqual(self.state().data, original)
        # Receipt compaction must retain the operator packet replay guard.
        receipt_key, digest = next((key, value) for key, value in self.state().receipts.items()
                                   if key.startswith('operator-defaults:'))
        receipts = {receipt_key: digest}
        receipts.update({str(n): 'digest' for n in range(storage.MAX_RECEIPTS - 1)})
        AuthoredDiscoveryState.objects.filter(pk=state.pk).update(receipts=receipts)
        self.apply('seed_remove', identity='deep_dives')
        self.operator()
        self.assertNotIn('deep_dives', {row['id'] for row in self.state().data['seeds']})
        self.assertFalse(DiscoveryState.objects.exists())

    def test_operator_conflicting_seed_and_guard_revocation_roll_back(self):
        self.actor.username = 'demo-curator'
        self.actor.save()
        calls = []

        def expiring_guard():
            calls.append('checked')
            if len(calls) == 2:
                raise ValueError('expired during operation')

        with self.assertRaises(ValueError):
            self.operator(expiring_guard)
        self.assertIsNone(self.state())
        edited = dict(storage.DEFAULT_SEEDS[0], topic='SYNTHETIC owner edit')
        self.apply('seed_add', seed=edited)
        with self.assertRaises(Conflict):
            self.operator()
        self.assertEqual(len(self.state().data['seeds']), 1)
        self.assertEqual(self.state().data['seeds'][0]['topic'], 'SYNTHETIC owner edit')
