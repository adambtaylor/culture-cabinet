"""Synthetic qualitative evidence, origin isolation and transactional imports."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import RequestFactory, TestCase

from shelf import authored_discovery, authored_observations as storage
from shelf.authority import SELECTION_KEY
from shelf.discovery_state import Conflict
from shelf.models import AuthoredDiscoveryState, Grant, Profile


def observation(**changes):
    row = dict(id='synthetic-book', title='SYNTHETIC work', raw_title='SYNTHETIC raw work',
        medium='book', reaction='mixed', experience='reported_experience',
        identity_status='confirmed', reasons=['SYNTHETIC pacing observation'],
        source_text='SYNTHETIC owner wording', uncertainty='',
        provenance=dict(source_id='synthetic-message-1', source_url='https://chatgpt.com/c/synthetic',
            observed_date=None, actor_origin='owner', dossier_sha256='a' * 64))
    row.update(changes)
    return row


class ObservationSchemaTests(TestCase):
    def packet(self, *rows):
        return {'observations': list(rows or [observation()])}

    def test_strict_schema_bounded_text_and_no_ratings_or_provider_ids(self):
        valid = self.packet()
        normalized = storage.validate_packet(json.dumps(valid), 1)
        self.assertEqual(normalized[0]['captured_by'], 'user:1')
        self.assertEqual(valid['observations'][0]['source_text'], normalized[0]['source_text'])
        for changes in ({'rating': 5}, {'provider_id': 'guessed'}, {'reaction': 5},
                        {'medium': 'comic'}, {'reasons': ['x'] * 5}, {'uncertainty': 'x' * 201}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                storage.validate_packet(self.packet(observation(**changes)), 1)
        for packet in ({'observations': []}, {'observations': [observation()] * 101},
                       dict(valid, provider='guessed'), self.packet(observation(), observation()),
                       '{"observations":[],"observations":[]}',' ' * (storage.MAX_PACKET_BYTES + 1)):
            with self.subTest(packet=str(packet)[:60]), self.assertRaises(ValueError):
                storage.validate_packet(packet, 1)

    def test_provenance_dates_urls_and_origin_are_strict(self):
        for changes in ({'observed_date': '2026-02-30'}, {'observed_date': '20260101'},
                        {'source_url': 'https://chatgpt.com.evil.test/c/x'},
                        {'source_url': 'http://chatgpt.com/c/x'},
                        {'source_url': 'https://user@chatgpt.com/c/x'},
                        {'dossier_sha256': 'guessed'}, {'actor_origin': 'owner_confirmed'},
                        {'source_refs': ['additional']}):
            row = observation()
            row['provenance'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                storage.validate_packet(self.packet(row), 1)

    def test_projection_preserves_medium_and_uncertain_identity_without_origin_laundering(self):
        rows = [observation(), observation(id='graphic', medium='graphic_novel'),
                observation(id='movie', medium='movie', identity_status='provisional',
                            uncertainty='Exact screen adaptation not established'),
                observation(id='suggestion', reaction='assistant_suggestion'),
                observation(id='assistant'), observation(id='unknown')]
        rows[4]['provenance']['actor_origin'] = 'assistant'
        rows[5]['provenance']['actor_origin'] = 'unknown'
        data = {'observations': storage.validate_packet(self.packet(*rows), 1)}
        signals = storage.signals(data)
        self.assertEqual([row['medium'] for row in signals], ['book', 'graphic_novel', 'movie'])
        self.assertEqual(signals[2]['title'], rows[2]['raw_title'])
        self.assertIn('identity: provisional', signals[2]['value'])
        self.assertIn(rows[2]['uncertainty'], signals[2]['value'])
        for signal in signals:
            self.assertEqual(set(signal), {'key', 'title', 'medium', 'source', 'basis', 'value'})
            self.assertEqual(signal['basis'], 'authored_observation')
            self.assertLessEqual(len(signal['value']), storage.MAX_SIGNAL_VALUE)
        projection = storage.project(data)
        self.assertEqual(sum(row['eligible'] for row in projection), 3)
        projection[0]['provenance']['source_id'] = 'detached'
        self.assertNotEqual(projection[0]['provenance'], data['observations'][0]['provenance'])
        for field, value in [('source_text', 'changed'), ('uncertainty', 'changed')]:
            changed = deepcopy(data)
            changed['observations'][0][field] = value
            self.assertNotEqual(storage.revision(data), storage.revision(changed))
        changed = deepcopy(data)
        changed['observations'][0]['provenance']['dossier_sha256'] = 'b' * 64
        self.assertNotEqual(storage.revision(data), storage.revision(changed))


class ObservationImportTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-observation-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='SYNTHETIC owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = self.profile.pk
        session.save()
        self.request = RequestFactory().post('/')
        self.request.user, self.request.session = self.actor, session

    def state(self):
        return AuthoredDiscoveryState.objects.filter(actor=self.actor, profile=self.profile, mode='household').first()

    def fields(self, *rows):
        state = self.state()
        return dict(operation='observations_import', mode='household',
            packet=json.dumps({'observations': list(rows or [observation()])}),
            revision=state.revision if state else 0,
            nonce=state.nonce if state else storage.initial_nonce(self.actor.pk, self.profile.pk))

    def apply(self, fields=None):
        return storage.apply(self.request, self.profile.pk, fields or self.fields())

    def test_replay_tamper_stale_and_changed_same_id_conflict(self):
        fields = self.fields()
        first = self.apply(fields)
        self.assertEqual(self.apply(fields).revision, first.revision)
        changed = dict(fields, packet=json.dumps({'observations': [observation(reaction='like')]}))
        with self.assertRaises(Conflict):
            self.apply(changed)
        with self.assertRaises(Conflict):
            self.apply(dict(fields, nonce='stale'))
        original = deepcopy(self.state().data)
        with self.assertRaises(Conflict):
            self.apply(self.fields(observation(reaction='like')))
        self.assertEqual(self.state().data, original)
        self.apply(self.fields())
        self.assertEqual(self.state().data, original)

    def test_preserves_existing_seed_shortlist_and_unknown_state(self):
        state = authored_discovery.apply(self.request, self.profile.pk,
            dict(action='seed_authorized_defaults', mode='household', revision=0,
                 nonce=storage.initial_nonce(self.actor.pk, self.profile.pk),
                 authorization=authored_discovery.DEFAULT_AUTHORIZATION))
        data = dict(state.data, shortlist=[{'key': 'synthetic:episode'}], other={'retain': True})
        AuthoredDiscoveryState.objects.filter(pk=state.pk).update(data=data)
        imported = self.apply()
        for key in data:
            self.assertEqual(imported.data[key], data[key])
        self.assertEqual(len(imported.data['observations']), 1)

    def test_profile_selection_revocation_and_actor_isolation(self):
        self.apply()
        self.grant.can_select = False
        self.grant.save()
        with self.assertRaises(Http404):
            self.apply()
        self.grant.can_select = True
        self.grant.save()
        session = self.client.session
        session[SELECTION_KEY] = self.profile.pk + 1
        session.save()
        with self.assertRaises(Http404):
            self.apply()
        session[SELECTION_KEY] = self.profile.pk
        session.save()
        self.request.user = get_user_model().objects.create_user(username='SYNTHETIC-other')
        with self.assertRaises(Http404):
            self.apply()

    def test_caps_and_packet_conflict_are_atomic(self):
        state = self.apply()
        before = deepcopy(state.data)
        with patch.object(storage, 'MAX_OBSERVATIONS', 1), self.assertRaises(Conflict):
            self.apply(self.fields(observation(id='new')))
        with self.assertRaises(Conflict):
            self.apply(self.fields(observation(id='new'), observation(reaction='like')))
        self.assertEqual(self.state().data, before)
        AuthoredDiscoveryState.objects.filter(pk=state.pk).update(
            receipts={str(n): 'digest' for n in range(authored_discovery.MAX_RECEIPTS)})
        with self.assertRaises(Conflict):
            self.apply()

    def test_invalid_forms_leave_no_state(self):
        for change in ({'mode': 'sample'}, {'revision': True}, {'operation': 'seed_add'},
                       {'rating': 5}, {'packet': {'observations': [observation()]}}, {'nonce': 'wrong'}):
            with self.subTest(change=change), self.assertRaises((ValueError, Conflict)):
                self.apply(dict(self.fields(), **change))
        self.assertIsNone(self.state())
