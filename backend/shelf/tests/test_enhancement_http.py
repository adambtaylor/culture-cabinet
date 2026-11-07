"""Actual lifecycle routes with disposable owner data and synthetic source/model results.

No preview, authorization, request, consumer or materializer is replaced. The
operator service records a clearly synthetic response without launching a model.
"""
from copy import deepcopy
from dataclasses import replace
import time
from unittest.mock import patch
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from shelf import discovery_sources, discovery_views, durable_intake, intake, provider_catalogue, synthesis
from shelf.authority import SELECTION_KEY
from shelf.discovery_state import Conflict
from shelf.models import DiscoveryState, Grant, Profile, SynthesisRun


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class EnhancementHTTPTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='synthetic-enhancement-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Synthetic enhancement owner')
        Profile.objects.create(pk=2, kind='partner', active=False, label='Synthetic separate profile')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.actor)
        self.intake_run = durable_intake.new_run(self.actor, 1)
        state = durable_intake.hydrate(self.intake_run)
        cards = provider_catalogue.parse_candidates('hardcover', 'book', [
            {'id': number, 'title': f'Synthetic cached book {number}', 'slug': f'synthetic-{number}'}
            for number in range(100, 116)], lane='synthetic-books', lane_label='Synthetic source', limit=16)
        intake.merge(state, cards)
        state['started'] = True
        intake.select_round_v2(state, category='book')
        state['queue'] = 'book'
        self.store(state)
        self.enterContext(patch('shelf.provider_catalogue._request', side_effect=AssertionError('No provider call')))
        self.artists = [discovery_sources.Candidate(
            'musicbrainz:artist:' + str(UUID(int=number)), f'Synthetic artist {number}', 'artist',
            'Synthetic cached registry', 'manual-attributed', number, timezone.now().isoformat(),
            url='https://musicbrainz.org/artist/' + str(UUID(int=number))) for number in range(1, 19)]
        self.artist_root = self.artists[0].key
        self.artists = [replace(card, roots=(self.artist_root,)) if index >= 14 else card
                        for index, card in enumerate(self.artists)]
        self.source_unavailable_after_commit = False
        self.post_commit_source_reads = 0
        self.enterContext(patch.object(discovery_sources, 'household_candidates', side_effect=self.artist_source))
        self.enterContext(patch.object(discovery_sources, 'fetch_public', side_effect=AssertionError('No source fetch')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('No network')))
        page = self.page()
        self.root = page.context['cards'][0]['key']
        self.assertEqual(self.feedback(self.root, 'choose', note='  Synthetic root note\r\n  ').status_code, 200)
        state = self.state()
        self.supported = 'hardcover:book:990'
        neighbor = provider_catalogue.Candidate(self.supported, 'hardcover', 990, 'book',
            'Synthetic supported cached book', '', 'https://hardcover.app/books/synthetic-990',
            timezone.now().isoformat(), 'root:' + self.root, 'Synthetic source root', 'neighbor', self.root)
        intake.merge(state, [neighbor])
        self.store(state)

    def artist_source(self):
        if self.source_unavailable_after_commit:
            for run in SynthesisRun.objects.all():
                if any(c.get('state') == 'applied' for c in run.evidence.get('enhancements', {}).values()):
                    self.post_commit_source_reads += 1
                    if self.post_commit_source_reads > 1:
                        raise Conflict('Synthetic source unavailable during materialization after receipt commit.')
        return list(self.artists)

    def store(self, state):
        session = self.client.session
        session[SELECTION_KEY] = 1
        session[intake.SESSION_KEY] = deepcopy(state)
        session.save()

    def state(self):
        return deepcopy(self.client.session[intake.SESSION_KEY])

    def post(self, url, data=None, *, csrf=True, json=True):
        headers = {'HTTP_ACCEPT': 'application/json'} if json else {}
        if csrf:
            headers['HTTP_X_CSRFTOKEN'] = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        return self.client.post(url, data or {}, **headers)

    def page(self, category='book'):
        if category == 'artist':
            response = self.client.get('/profiles/1/discovery/', {'mode': 'household', 'queue': 'artist'})
        else:
            response = self.client.get('/profiles/1/', {'queue': category})
        self.assertEqual(response.status_code, 200)
        return response

    def feedback(self, key, action='correct', **extra):
        state = self.state()
        return self.post('/profiles/1/intake/action/', {
            'action': action, 'revision': state['revision'], 'nonce': state['nonce'],
            'run_id': state['run_id'], 'queue': state.get('queue', 'all'),
            'candidate_id': key, 'choice': 'love_it', **extra})

    def prepare_artist(self):
        page = self.page('artist')
        response = self.post('/profiles/1/discovery/action/', {
            'mode': 'household', 'queue': 'artist', 'revision': page.context['revision'],
            'nonce': page.context['nonce'], 'operation': 'respond', 'key': self.artist_root,
            'choice': 'love_it', 'note': 'Synthetic artist root note'})
        self.assertEqual(response.status_code, 200)

    def start(self, category='book'):
        page = self.page(category)
        response = self.post(page.context['enhance_url'], {
            'category': category, 'expected_digest': page.context['enhance_digest'],
            'preview_token': page.context['enhance_token']})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['state'], 'awaiting_authorized_worker')
        self.assertEqual(body['attempts'], 0)
        return body

    def complete(self, body, outcome='completed'):
        run = SynthesisRun.objects.get(pk=body['run_id'])
        run, packet = synthesis.reserve_operator(actor=self.actor, profile_id=1, run_id=run.pk,
                                                session_guard=lambda: None)
        self.assertFalse(packet['reconciliation_only'])
        keys = {s['key'] for s in run.input_snapshot['signals']}
        members = [key for key in (self.root, self.artist_root) if key in keys]
        output = {'basis': synthesis.ASSOCIATION, 'clusters': [
            {'label': 'Synthetic association', 'members': members,
             'explanation': 'Synthetic fixture association, not actual model inference.'}], 'links': []}
        evidence = {**synthesis.binding(run), 'evidence_id': 'synthetic:enhancement-http', 'outcome': outcome,
                    'reason': '' if outcome == 'completed' else 'response_unavailable',
                    'output': output if outcome == 'completed' else None}
        return synthesis.record_operator(actor=self.actor, profile_id=1, run_id=run.pk,
                                         evidence=evidence, session_guard=lambda: None)

    def consumer(self, body, category='book'):
        run = SynthesisRun.objects.get(pk=body['run_id'])
        return run, run.evidence['enhancements'][category]

    def deck(self):
        return deepcopy(self.client.session.get(discovery_views.DECK_KEY, {}))

    def assert_stale_without_writes(self, body, *, expected_receipt='completed'):
        before = deepcopy(dict(self.client.session))
        run = SynthesisRun.objects.get(pk=body['run_id'])
        evidence, revision = deepcopy(run.evidence), run.revision
        status = self.client.get(body['status_url'], HTTP_ACCEPT='application/json')
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()['state'], 'stale')
        self.assertEqual(status.json()['receipt_state'], expected_receipt)
        self.assertNotIn('deck_html', status.json())
        response = self.post(body['apply_url'])
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('deck_html', response.json())
        self.assertEqual(dict(self.client.session), before)
        run.refresh_from_db()
        self.assertEqual(run.evidence, evidence)
        self.assertEqual(run.revision, revision)
        self.assertEqual(run.attempts, 1 if expected_receipt != 'queued' else 0)

    def test_request_status_apply_and_valid_replay_keep_one_actual_bound_receipt(self):
        body = self.start()
        before = self.state()
        self.assertNotIn(self.supported, before['current'])
        run = self.complete(body)
        status = self.client.get(body['status_url'], HTTP_ACCEPT='application/json').json()
        self.assertEqual((status['state'], status['receipt_state']), ('completed', 'completed'))
        response = self.post(body['apply_url'])
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['application_state'], 'applied')
        self.assertEqual(result['outcome'], 'supported')
        self.assertIn(self.supported, self.state()['current'])
        self.assertIn(self.supported, result['deck_html'])
        self.assertEqual(set(result['enhance_context']),
            {'enhance_digest', 'enhance_token', 'enhance_url', 'active_queue', 'enhancement'})
        run, consumer = self.consumer(body)
        self.assertEqual(consumer['after_ids'], self.state()['current'])
        self.assertEqual(consumer['result_record_digest'], run.evidence['record_digest'])
        committed = (run.revision, deepcopy(run.evidence), run.updated_at)
        after = deepcopy(dict(self.client.session))
        replay = self.post(body['apply_url'])
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(all(key in replay.json()['deck_html'] for key in self.state()['current']))
        self.assertEqual(dict(self.client.session), after)
        run.refresh_from_db()
        self.assertEqual((run.revision, run.evidence, run.updated_at), committed)
        self.assertEqual((SynthesisRun.objects.count(), run.attempts), (1, 1))
        _, packet = synthesis.reserve_operator(actor=self.actor, profile_id=1, run_id=run.pk,
                                              session_guard=lambda: None)
        self.assertTrue(packet['reconciliation_only'])
        self.assertNotIn('signals', packet)

    def test_exact_lost_session_response_recovers_approved_before_order(self):
        body = self.start()
        before = self.state()
        self.complete(body)
        first = self.post(body['apply_url'])
        self.assertEqual(first.status_code, 200)
        after = self.state()
        run, _ = self.consumer(body)
        revision = run.revision
        self.store(before)
        recovered = self.post(body['apply_url'])
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(self.state(), after)
        self.assertTrue(all(key in recovered.json()['deck_html'] for key in after['current']))
        run.refresh_from_db()
        self.assertEqual((run.revision, run.attempts), (revision, 1))

    def test_repeated_explicit_request_reuses_the_same_unreserved_consumer(self):
        first, second = self.start(), self.start()
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertEqual(first['request_id'], second['request_id'])
        self.assertEqual(SynthesisRun.objects.count(), 1)
        self.assertEqual(SynthesisRun.objects.get().attempts, 0)

    def test_actual_associations_without_supported_roots_leave_cached_order_unchanged(self):
        state = self.state()
        state['candidates'][self.supported]['root'] = ''
        self.store(state)
        body = self.start()
        self.complete(body)
        before = deepcopy(dict(self.client.session))
        response = self.post(body['apply_url'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['outcome'], 'unchanged')
        self.assertEqual(dict(self.client.session), before)
        _, consumer = self.consumer(body)
        self.assertEqual(consumer['why'], {})
        self.assertEqual(consumer['after_ids'], before[intake.SESSION_KEY]['current'])

    def test_empty_verified_pool_records_no_candidates_without_manufacturing_inventory(self):
        state = self.state()
        state['candidates'] = {self.root: state['candidates'][self.root]}
        state['current'], state['queued'], state['search_results'] = [], [], []
        state.pop('queue_decks', None)
        self.store(state)
        body = self.start()
        self.complete(body)
        before = deepcopy(dict(self.client.session))
        response = self.post(body['apply_url'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['outcome'], 'no_candidates')
        self.assertEqual(response.json()['deck_html'], '')
        self.assertEqual(dict(self.client.session), before)
        _, consumer = self.consumer(body)
        self.assertEqual(consumer['after_ids'], [])

    def test_applied_replay_after_root_note_edit_has_no_deck_html_or_session_write(self):
        body = self.start()
        run = self.complete(body)
        self.assertEqual(self.post(body['apply_url']).status_code, 200)
        original_digest = run.input_digest
        self.assertEqual(self.feedback(self.root, note='  Different exact owner note\r\n  ').status_code, 200)
        self.assertNotEqual(synthesis.snapshot(self.actor, 1)['input_digest'], original_digest)
        self.assert_stale_without_writes(body)

    def test_pending_result_after_taste_reaction_edit_is_stale(self):
        body = self.start()
        self.complete(body)
        self.assertEqual(self.feedback(self.root, choice='hate_it', note='Synthetic negative correction').status_code, 200)
        self.assert_stale_without_writes(body)

    def test_changed_source_pool_retaining_old_ids_invalidates_applied_replay(self):
        body = self.start()
        self.complete(body)
        self.assertEqual(self.post(body['apply_url']).status_code, 200)
        state = self.state()
        state['candidates'][self.supported]['root'] = 'hardcover:book:other-source-root'
        self.store(state)
        self.assert_stale_without_writes(body)
        self.assertEqual(self.page().context['enhancement']['state'], 'stale')

    def test_expired_metadata_cannot_be_restored_from_durable_history(self):
        body = self.start()
        self.complete(body)
        self.assertEqual(self.post(body['apply_url']).status_code, 200)
        state = self.state()
        state['expires_at'] = time.time() - 1
        self.store(state)
        self.assert_stale_without_writes(body)

    def test_pending_pool_change_is_stale_in_json_and_standalone_html(self):
        body = self.start()
        self.complete(body)
        state = self.state()
        del state['candidates'][self.supported]
        state['queued'].remove(self.supported)
        self.store(state)
        self.assert_stale_without_writes(body)
        page = self.client.get(body['status_url'])
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['enhancement']['state'], 'stale')
        self.assertNotContains(page, 'Taste snapshot ready')
        self.assertEqual(self.page().context['enhancement']['state'], 'stale')

    def test_newer_visible_order_cannot_be_overwritten_by_applied_replay(self):
        body = self.start()
        self.complete(body)
        self.assertEqual(self.post(body['apply_url']).status_code, 200)
        state = self.state()
        state['current'] = list(reversed(state['current']))
        self.store(state)
        self.assert_stale_without_writes(body)

    def test_changed_pending_before_order_is_stale_even_when_pool_is_unchanged(self):
        body = self.start()
        self.complete(body)
        state = self.state()
        state['current'] = list(reversed(state['current']))
        self.store(state)
        self.assert_stale_without_writes(body)

    def test_cancel_before_reservation_has_no_model_attempt_or_deck_effect(self):
        body = self.start()
        before = deepcopy(dict(self.client.session))
        response = self.post(body['cancel_url'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['state'], 'cancelled')
        self.assertFalse(response.json()['external_work_cancelled'])
        self.assertNotIn('deck_html', response.json())
        self.assertEqual(dict(self.client.session), before)
        run, consumer = self.consumer(body)
        self.assertEqual((run.state, run.attempts, consumer['state']), ('queued', 0, 'cancelled'))
        from shelf.acquisition_contract import Blocked
        with self.assertRaises(Blocked):
            synthesis.reserve_operator(actor=self.actor, profile_id=1, run_id=run.pk, session_guard=lambda: None)
        self.assertEqual(self.post(body['apply_url']).json()['state'], 'cancelled')
        self.assertEqual(dict(self.client.session), before)

    def test_cancel_after_completion_stops_application_and_retains_actual_receipt(self):
        body = self.start()
        run = self.complete(body)
        fingerprint = run.evidence['record_digest']
        before = self.state()
        self.assertEqual(self.post(body['cancel_url']).json()['state'], 'cancelled')
        self.assertEqual(self.post(body['apply_url']).json()['state'], 'cancelled')
        self.assertEqual(self.state(), before)
        run.refresh_from_db()
        self.assertEqual((run.state, run.attempts, run.evidence['record_digest']), ('completed', 1, fingerprint))

    def test_unknown_result_retains_reconciliation_state_after_pool_change(self):
        body = self.start()
        self.complete(body, outcome='unknown')
        status = self.client.get(body['status_url'], HTTP_ACCEPT='application/json').json()
        self.assertEqual((status['state'], status['receipt_state']), ('unknown', 'unknown'))
        self.assertEqual(self.post(body['apply_url']).status_code, 409)
        state = self.state()
        state['candidates'][self.supported]['root'] = ''
        self.store(state)
        self.assert_stale_without_writes(body, expected_receipt='unknown')

    def test_standalone_status_explicitly_loads_scripts_with_existing_csp(self):
        body = self.start()
        page = self.client.get(body['status_url'])
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context['feedback_enabled'])
        self.assertContains(page, 'src="/static/card-feedback.js"')
        self.assertContains(page, 'src="/static/enhancement.js"')
        self.assertIn("script-src 'self'", page['Content-Security-Policy'])
        self.assertIn("connect-src 'self'", page['Content-Security-Policy'])
        with override_settings(ROOT_URLCONF='cabinet.urls'):
            login = self.client.get('/login/')
            self.assertIn("script-src 'none'", login['Content-Security-Policy'])

    def test_all_mutation_routes_require_csrf_and_status_requires_authentication(self):
        body = self.start()
        page = self.page()
        fields = {'category': 'book', 'expected_digest': page.context['enhance_digest'],
                  'preview_token': page.context['enhance_token']}
        self.assertEqual(self.post(page.context['enhance_url'], fields, csrf=False).status_code, 403)
        for url in (body['apply_url'], body['cancel_url']):
            self.assertEqual(self.post(url, csrf=False).status_code, 403)
            self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(Client().get(body['status_url']).status_code, 302)
        run, consumer = self.consumer(body)
        self.assertEqual((run.state, run.attempts, consumer['state']), ('queued', 0, 'pending'))

    def test_current_profile_and_select_grant_are_required_for_every_route(self):
        body = self.start()
        self.complete(body)
        page = self.page()
        fields = {'category': 'book', 'expected_digest': page.context['enhance_digest'],
                  'preview_token': page.context['enhance_token']}
        for denied in ('profile', 'grant'):
            with self.subTest(denied=denied):
                session = self.client.session
                session[SELECTION_KEY] = 2 if denied == 'profile' else 1
                session.save()
                self.grant.can_select = denied != 'grant'
                self.grant.save()
                self.assertEqual(self.client.get(body['status_url'], HTTP_ACCEPT='application/json').status_code, 404)
                self.assertEqual(self.post(body['apply_url']).status_code, 404)
                self.assertEqual(self.post(body['cancel_url']).status_code, 404)
                self.assertEqual(self.post(page.context['enhance_url'], fields).status_code, 404)
        run, consumer = self.consumer(body)
        self.assertEqual((run.attempts, consumer['state']), (1, 'pending'))

    def test_category_and_signed_preview_cannot_cross_profile_or_sample_scope(self):
        page = self.page()
        fields = {'category': 'artist', 'expected_digest': page.context['enhance_digest'],
                  'preview_token': page.context['enhance_token']}
        artist_url = reverse('enhancement_request', kwargs={'profile_id': 1, 'category': 'artist'})
        self.assertEqual(self.post(artist_url, fields).status_code, 409)
        sample_url = reverse('enhancement_request', kwargs={'profile_id': 1, 'category': 'sample'})
        self.assertEqual(self.post(sample_url, {**fields, 'category': 'sample'}).status_code, 400)
        other_url = reverse('enhancement_request', kwargs={'profile_id': 2, 'category': 'book'})
        self.assertEqual(self.post(other_url, {**fields, 'category': 'book'}).status_code, 400)
        self.assertFalse(SynthesisRun.objects.exists())

    def test_artist_application_failure_after_receipt_commit_recovers_without_new_attempt(self):
        self.prepare_artist()
        body = self.start('artist')
        self.complete(body)
        before = deepcopy(dict(self.client.session))
        self.source_unavailable_after_commit = True
        failed = self.post(body['apply_url'])
        self.assertEqual(failed.status_code, 409)
        self.assertNotIn('deck_html', failed.json())
        self.assertGreater(self.post_commit_source_reads, 1)
        self.assertEqual(dict(self.client.session), before)
        run, consumer = self.consumer(body, 'artist')
        self.assertEqual((run.state, run.attempts, consumer['state']), ('completed', 1, 'applied'))
        committed = (run.revision, deepcopy(run.evidence))
        self.source_unavailable_after_commit = False
        recovered = self.post(body['apply_url'])
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()['deck_kind'], 'artist')
        keys = self.deck()['1:1:household:artist']['keys']
        self.assertEqual(keys, consumer['after_ids'])
        self.assertTrue(all(key in recovered.json()['deck_html'] for key in keys))
        run.refresh_from_db()
        self.assertEqual((run.revision, run.evidence), committed)
        self.assertEqual(len(DiscoveryState.objects.get().history), 1)

    def test_artist_applied_replay_after_source_pool_expiry_cannot_replace_deck(self):
        self.prepare_artist()
        body = self.start('artist')
        self.complete(body)
        self.assertEqual(self.post(body['apply_url']).status_code, 200)
        self.artists = [self.artists[0]]
        self.assert_stale_without_writes(body)

    def test_artist_applied_replay_after_root_note_edit_is_stale(self):
        self.prepare_artist()
        body = self.start('artist')
        self.complete(body)
        self.assertEqual(self.post(body['apply_url']).status_code, 200)
        page = self.page('artist')
        corrected = self.post('/profiles/1/discovery/action/', {
            'mode': 'household', 'queue': 'artist', 'revision': page.context['revision'],
            'nonce': page.context['nonce'], 'operation': 'respond', 'key': self.artist_root,
            'choice': 'love_it', 'note': 'Synthetic edited artist note'})
        self.assertEqual(corrected.status_code, 200)
        self.assert_stale_without_writes(body)
