"""Actual native owner/create/form/event callers; all transport is synthetic."""
import json
from types import SimpleNamespace
from urllib.parse import urlencode
import uuid

from django.test import Client

from shelf import disclosure, grant_authority as ga, profile_snapshot
from shelf.category_registry import KEYS, registry_view
from shelf.current_profile import CurrentProfile
from shelf.models import FeedbackEvent, ProfileCabinetState
from shelf.portable_authority import AuthorityFault
from shelf.portable_feedback_views import feedback_context, card_feedback_context
from shelf.portable_reactions import target_ref
from shelf.portable_taste_inputs import PortableTasteInputs
from .test_grant_authority import RecoveryCase
from .test_portable_authority import PASSWORD


class NativeFeedbackCase(RecoveryCase):
    def claim_owner(self, password=PASSWORD):
        self.enterContext(self.settings(ROOT_URLCONF='cabinet.portable_urls',
            PORTABLE_AUTHORITY=self.authority, SESSION_COOKIE_NAME=self.boundary.session_cookie,
            CSRF_COOKIE_NAME=self.boundary.csrf_cookie, MIDDLEWARE=[
                'cabinet.portable_boundary.PortableSecurityMiddleware',
                'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.csrf.CsrfViewMiddleware']))
        self.http = Client(enforce_csrf_checks=True)
        self.headers = {'HTTP_HOST': self.boundary.origin.removeprefix('http://'),
                        'HTTP_ORIGIN': self.boundary.origin}
        response = self.http.post('/setup/claim',
            json.dumps({'code': self.challenge(), 'passphrase': password}), content_type='application/json',
            HTTP_X_CSRFTOKEN=self.csrf(), **self.headers)
        self.assertEqual(response.status_code, 200)
        return self.http.session['portable_session']

    def csrf(self):
        return self.http.get('/setup/status', **self.headers).json()['csrf_token']

    def post(self, path, fields, *, csrf=True):
        return self.http.post(path, urlencode(fields), content_type='application/x-www-form-urlencoded',
            **({'HTTP_X_CSRFTOKEN': self.csrf()} if csrf else {}), **self.headers)

    def setUp(self):
        super().setUp()
        self.coordinator = self.enterContext(self.controlled_recovery_state('clear'))
        self.enhance = self.compose_enhance()
        created = self.post('/setup/cabinet/create',
            {'expected_owner_revision': self.authority.authorize(self.ref).owner_revision})
        self.assertEqual(created.status_code, 303)
        self.assertEqual(self.http.get(created['Location'], **self.headers).status_code, 200)
        self.state = ProfileCabinetState.objects.get()
        self.profile_ref = str(self.state.profile_ref)
        self.assertEqual(self.state.data_schema_version, 2)
        self.assertEqual(self.http.get('/cabinet', {'profile_ref': self.profile_ref}, **self.headers).status_code, 200)

    def compose_enhance(self):
        from shelf.portable_enhance_service import EnhanceService
        return EnhanceService.compose(self.authority, self.service)

    def fields(self, **changes):
        values = dict(profile_ref=self.profile_ref, entry_ref=str(uuid.uuid4()), mutation_id=str(uuid.uuid4()),
            base_revision='0', operation='set', signal='interest', entry_kind='item', category='book',
            title='Fictional deliberate title', value='yes')
        values.update(changes)
        return values

    def note_fields(self, **changes):
        values = self.fields(signal='note', entry_kind='category_note', text='  A fictional preference\n雪  ')
        values.pop('title'); values.pop('value')
        values.update(changes)
        return values


class NativeFeedbackTests(NativeFeedbackCase):
    def test_real_create_and_all_registry_inputs_readback_and_authored_undo(self):
        self.assertEqual(self.http.get('/cabinet/taste', {'profile_ref': self.profile_ref}, **self.headers).status_code, 200)
        for category in KEYS:
            self.assertEqual(self.post('/cabinet/taste', self.fields(category=category)).status_code, 303)
            self.assertEqual(self.post('/cabinet/taste', self.note_fields(category=category)).status_code, 303)
        context = feedback_context(self.authority, self.ref, self.profile_ref)
        self.assertEqual([row['key'] for row in context['feedback_categories']], list(KEYS))
        entries = context['feedback_entries']
        self.assertEqual(len(entries), 2 * len(KEYS))
        self.assertEqual({row['category'] for row in entries}, set(KEYS))
        note = next(row for row in entries if row['entry_kind'] == 'category_note')
        binding = note['signals'][0]
        self.assertEqual(binding['text'], '  A fictional preference\n雪  ')
        undo = dict(profile_ref=self.profile_ref, entry_ref=note['entry_ref'], mutation_id=binding['undo_mutation_id'],
            base_revision=str(binding['revision']), operation='reverse', signal='note', reverses_id=binding['event_ref'])
        self.assertEqual(self.post('/cabinet/taste', undo).status_code, 303)
        read = PortableTasteInputs(self.authority).read_entries(self.ref, self.profile_ref)
        restored = next(row for row in read if row['entry_ref'] == note['entry_ref'])['signals'][0]
        self.assertEqual((restored['text'], restored['revision'], restored['can_undo']), ('', 2, False))

    def test_closed_form_csrf_rating_ack_replay_changed_intent_and_stale_bindings(self):
        fields = self.fields()
        self.assertEqual(self.post('/cabinet/taste', fields, csrf=False).status_code, 403)
        self.assertEqual(self.post('/cabinet/taste', {**fields, 'actor_id': '1'}).status_code, 422)
        self.assertEqual(self.post('/cabinet/taste', [*fields.items(), ('value', 'no')]).status_code, 422)
        self.assertEqual(self.post('/cabinet/taste', {**fields, 'signal': 'enjoyment', 'value': '4'}).status_code, 422)
        self.assertEqual(self.post('/cabinet/taste', fields).status_code, 303)
        newer = {**fields, 'mutation_id': str(uuid.uuid4()), 'base_revision': '1', 'value': 'maybe'}
        self.assertEqual(self.post('/cabinet/taste', newer).status_code, 303)
        self.assertEqual(self.post('/cabinet/taste', fields).status_code, 303)
        self.assertEqual(self.post('/cabinet/taste', {**fields, 'value': 'no'}).status_code, 409)
        self.assertEqual(FeedbackEvent.objects.count(), 2)
        rating = {**fields, 'signal': 'enjoyment', 'value': '4', 'experienced_ack': 'yes',
                  'mutation_id': str(uuid.uuid4())}
        self.assertEqual(self.post('/cabinet/taste', rating).status_code, 303)
        self.assertEqual(FeedbackEvent.objects.get(signal='enjoyment').body,
                         {'value': 4, 'basis': 'experienced', 'scale': 'integer-1-5'})
        self.assertEqual(self.post('/cabinet/taste', self.note_fields(text='bad\0note')).status_code, 422)
        self.assertEqual(self.post('/cabinet/taste', self.note_fields(text='x' * 501)).status_code, 422)

    def test_note_exact_disclosure_noninterference_and_no_hard_constraint_or_query_term(self):
        text = '  e\u0301 / é\t\r\n雪  '
        fields = self.note_fields(text=text)
        self.assertEqual(self.post('/cabinet/taste', fields).status_code, 303)
        self.consent(self.profile_ref, classes=['canonical_seed'])
        def projected():
            view = self.view(self.profile_ref)
            snapshot = profile_snapshot.capture(view)
            prepared = disclosure.prepare(snapshot, view.grants, view.context, [], {}, {}, now=self.clock.wall)
            self.assertTrue(prepared.ok)
            return snapshot, disclosure.read_json(prepared.profile_bytes)['profile']
        snapshot, denied = projected()
        self.assertEqual(snapshot['hard_constraints'], [])
        self.assertEqual(denied['signals'], [])
        self.assertNotIn('raw_notes', denied)
        changed = {**fields, 'base_revision': '1', 'mutation_id': str(uuid.uuid4()), 'text': 'DIFFERENT DENIED NOTE'}
        self.assertEqual(self.post('/cabinet/taste', changed).status_code, 303)
        later, still_denied = projected()
        self.assertNotEqual(snapshot['profile_revision_digest'], later['profile_revision_digest'])
        self.assertEqual(denied, still_denied)
        self.consent(self.profile_ref, action='revoke', revision=1, classes=['canonical_seed'])
        self.consent(self.profile_ref, action='regrant', revision=2, classes=['raw_notes'])
        self.assertNotIn('raw_notes', projected()[1])
        self.consent(self.profile_ref, action='revoke', revision=3, classes=['raw_notes'])
        self.consent(self.profile_ref, action='regrant', revision=4, classes=['raw_notes', 'free_text_entry'])
        self.assertEqual(self.post('/cabinet/taste', {**fields, 'base_revision': '2',
            'mutation_id': str(uuid.uuid4())}).status_code, 303)
        _, allowed = projected()
        self.assertEqual(allowed['raw_notes'][0]['text'], text)
        self.assertEqual(allowed['signals'], [])
        from shelf import inference_body, portable_disclosure as pd
        from .test_inference_body import InferenceBodyTests
        record = InferenceBodyTests.record(self)
        collection = self.disclosure.fixture_collection([record],
            {'observation:1': {'revision': 7, 'digest': 'c' * 64}},
            {'policy:1': {'revision': 3}}, expires_at=self.expires)
        view = self.view(self.profile_ref)
        encoded = inference_body.serialize(profile_snapshot.capture(view), view.grants,
            view.context, collection, now=self.clock.wall)
        self.assertEqual(json.loads(json.loads(encoded.body)['input'])['profile']['raw_notes'][0]['text'], text)
        preview = pd.preview(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), collection_ref=collection.ref)
        self.assertEqual(preview['body'], encoded.body)
        self.assertEqual(pd.isolated_capture(preview['preview_ref'], self.authority, self.ref), encoded.body)
        # A separately scoped synthetic catalogue grant tests only the codec,
        # not a source permission or a real network operation.
        self.consent(self.profile_ref, purpose='catalogue_lookup', classes=['raw_notes', 'free_text_entry'])
        view = ga.current_view(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), purpose='catalogue_lookup')
        body, _ = disclosure.catalogue_input(profile_snapshot.capture(view), view.grants,
            view.context, registry_view().categories, now=self.clock.wall)
        self.assertTrue(all(row['terms'] == [] for row in disclosure.read_json(body)['queries']))
        self.assertEqual(CurrentProfile(self.authority).current(self.authority, self.ref,
            profile_ref=self.profile_ref, connection_ref=str(self.config.pk)).view.profile_ref, self.profile_ref)

    def test_card_current_set_note_exact_replay_stale_new_set_and_authored_undo(self):
        card = {'key': 'synthetic:book:workflow', 'title': 'Synthetic current book', 'category': 'book'}
        opaque = target_ref(self.profile_ref, card['key'])
        stale = False
        def current(*args, **kwargs):
            if stale:
                raise AuthorityFault('PREVIEW_STALE', 409)
            return {'manifest': {'categories': [{'cards': [card]}]}}
        self.authority._enhance_service = SimpleNamespace(authority=self.authority, current=current)
        fields = dict(profile_ref=self.profile_ref, target_ref=opaque, mutation_id=str(uuid.uuid4()),
            base_revision='0', operation='set', signal='note', text='  Exact card note\n雪  ')
        self.assertEqual(self.post('/cabinet/feedback', fields).status_code, 303)
        context = card_feedback_context(self.authority, self.ref, self.profile_ref, target_ref=opaque)
        binding = next(row for row in context['signals'] if row['signal'] == 'note')
        self.assertEqual(binding['text'], fields['text'])
        stale = True
        self.assertEqual(self.post('/cabinet/feedback', fields).status_code, 303)
        self.assertEqual(self.post('/cabinet/feedback', {**fields, 'mutation_id': str(uuid.uuid4()),
            'base_revision': '1', 'text': 'New stale assertion'}).status_code, 409)
        undo = dict(profile_ref=self.profile_ref, target_ref=opaque, mutation_id=str(uuid.uuid4()),
            base_revision='1', operation='reverse', signal='note', reverses_id=binding['event_ref'])
        self.assertEqual(self.post('/cabinet/feedback', undo).status_code, 303)
        self.assertEqual(FeedbackEvent.objects.filter(signal='note').count(), 2)
        self.assertEqual(self.post('/cabinet/feedback', {**fields, 'target_ref': str(uuid.uuid4()),
            'mutation_id': str(uuid.uuid4())}).status_code, 409)

    def test_foreign_profile_recent_auth_and_recovery_refuse_before_event_creation(self):
        fields = self.fields(profile_ref=str(uuid.uuid4()))
        self.assertEqual(self.post('/cabinet/taste', fields).status_code, 403)
        self.clock.advance(300)
        self.assertEqual(self.post('/cabinet/taste', self.fields()).status_code, 401)
        self.authority.reauthenticate(self.ref, PASSWORD)
        self.prepare_restriction(self.coordinator)
        self.assertEqual(self.post('/cabinet/taste', self.fields()).status_code, 503)
        self.assertFalse(FeedbackEvent.objects.exists())

    def test_combined_bound_includes_legacy_artist_entries_without_overwrite(self):
        from unittest.mock import patch
        from shelf.models import FeedbackTarget
        with patch('shelf.portable_taste_inputs.MAX_ENTRIES', 2):
            artist = dict(profile_ref=self.profile_ref, entry_ref=str(uuid.uuid4()),
                mutation_id=str(uuid.uuid4()), base_revision='0', operation='set',
                signal='interest', title='Fictional Artist', value='yes')
            self.assertEqual(self.post('/cabinet/taste/artist', artist).status_code, 303)
            self.assertEqual(self.post('/cabinet/taste', self.fields()).status_code, 303)
            self.assertEqual(self.post('/cabinet/taste', self.note_fields()).status_code, 503)
            self.assertEqual(FeedbackTarget.objects.count(), 2)
        read = PortableTasteInputs(self.authority).read(self.ref, self.profile_ref)
        self.assertEqual((read[0]['title'], read[0]['signals'][0]['value']), ('Fictional Artist', 'yes'))

    def test_shared_note_null_clear_keeps_revision_undo_and_exact_empty_codec(self):
        from shelf import inference_body
        from shelf.portable_reactions import PortableReactions
        from .test_inference_body import InferenceBodyTests
        card = {'key': 'synthetic:book:null-note', 'title': 'Synthetic book', 'category': 'book'}
        opaque = target_ref(self.profile_ref, card['key'])
        service = SimpleNamespace(authority=self.authority,
            current=lambda *a, **k: {'manifest': {'categories': [{'cards': [card]}]}})
        self.authority._enhance_service = service
        reactions = PortableReactions(self.authority, service=service)
        self.consent(self.profile_ref, classes=['raw_notes', 'free_text_entry'])
        text = '  e\u0301 / é\t\r\n雪  '
        def set_note(value, revision):
            return reactions.mutate(self.ref, self.profile_ref, opaque, str(uuid.uuid4()), revision,
                'set', {'signal': 'note', 'value': value})
        def project():
            view = self.view(self.profile_ref)
            snapshot = profile_snapshot.capture(view)
            prepared = disclosure.prepare(snapshot, view.grants, view.context, [], {}, {}, now=self.clock.wall)
            self.assertTrue(prepared.ok)
            collection = self.disclosure.fixture_collection([InferenceBodyTests.record(self)],
                {'observation:1': {'revision': 7, 'digest': 'c' * 64}},
                {'policy:1': {'revision': 3}}, expires_at=self.expires)
            encoded = inference_body.serialize(snapshot, view.grants, view.context, collection, now=self.clock.wall)
            self.assertEqual(json.loads(json.loads(encoded.body)['input'])['profile'],
                             disclosure.read_json(prepared.profile_bytes)['profile'])
            return snapshot, disclosure.read_json(prepared.profile_bytes)['profile']
        first = set_note({'text': text}, 0)
        self.assertEqual(project()[1]['raw_notes'][0]['text'], text)
        cleared = set_note(None, 1)
        snapshot, projected = project()
        node = next(row for row in snapshot['evidence'] if row['value']['signal'] == 'note')
        self.assertIsNone(node['value']['value'])
        self.assertEqual([row['revision'] for row in node['event_lineage']], [1, 2])
        self.assertEqual(snapshot['hard_constraints'], [])
        self.assertEqual(projected['signals'], [])
        self.assertNotIn('raw_notes', projected)
        binding = next(row for row in card_feedback_context(self.authority, self.ref, self.profile_ref,
            target_ref=opaque)['signals'] if row['signal'] == 'note')
        self.assertEqual((binding['revision'], binding['event_ref'], binding['can_undo'], binding['cleared']),
                         (2, cleared['event_ref'], True, True))
        self.consent(self.profile_ref, purpose='catalogue_lookup', classes=['raw_notes', 'free_text_entry'])
        source_view = ga.current_view(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), purpose='catalogue_lookup')
        query, _ = disclosure.catalogue_input(profile_snapshot.capture(source_view), source_view.grants,
            source_view.context, registry_view().categories, now=self.clock.wall)
        self.assertTrue(all(row['terms'] == [] for row in disclosure.read_json(query)['queries']))
        self.assertNotIn('raw_notes', disclosure.read_json(query)['profile'])
        reactions.mutate(self.ref, self.profile_ref, opaque, str(uuid.uuid4()), 2, 'reverse',
            {'signal': 'note', 'value': None}, cleared['event_ref'])
        self.assertEqual(project()[1]['raw_notes'][0]['text'], text)
        set_note({'text': ''}, 3)
        self.assertEqual(project()[1]['raw_notes'][0]['text'], '')
        binding = next(row for row in card_feedback_context(self.authority, self.ref, self.profile_ref,
            target_ref=opaque)['signals'] if row['signal'] == 'note')
        self.assertEqual((binding['revision'], binding['can_undo'], binding['cleared']), (4, True, False))
