"""Fictional multi-round, durable/session upgrade, and bounded provider contracts."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model, HASH_SESSION_KEY
from django.contrib.sessions.models import Session
from django.db import connection
from django.http import Http404
from django.test import TestCase, SimpleTestCase, Client, override_settings
from django.utils import timezone

from cabinet import boundary
from shelf import intake, durable_intake as d, provider_catalogue as p
from shelf.authority import SELECTION_KEY
from shelf.models import IntakeRun, Profile, Grant, FeedbackEvent, SavedItem


def fiction_cards():
    """Distinct source IDs, media, lanes, and literal fictional titles."""
    output = []
    for medium, lanes in [('movie', ['film-space', 'film-drama']),
                          ('tv', ['series-comedy', 'series-fantasy']),
                          ('book', ['books-popular', 'books-search'])]:
        provider = 'hardcover' if medium == 'book' else 'tmdb'
        for lane_number, lane in enumerate(lanes):
            for i in range(8):
                source_id = 100 + lane_number * 10 + i
                row = {'id': source_id, 'title': f'Fictional {lane} {i}',
                       'name': f'Fictional {lane} {i}', 'slug': f'fiction-{source_id}'}
                output.extend(p.parse_candidates(provider, medium, [row], lane=lane, lane_label='Fiction fixture lane'))
    return output


class V2SelectionTests(SimpleTestCase):
    def state(self):
        state = intake.new_state(1, 1)
        intake.merge(state, fiction_cards())
        intake.select_round_v2(state)
        return state

    def test_initial_nine_balanced_and_deterministic_with_lane_caps(self):
        state = self.state()
        self.assertEqual(state['current'], self.state()['current'])
        self.assertEqual(len(state['current']), 9)
        for medium in ('movie', 'tv', 'book'):
            self.assertEqual(sum(state['candidates'][k]['medium'] == medium for k in state['current']), 3)
        for lane in {c['lane'] for c in state['candidates'].values()}:
            self.assertLessEqual(sum(state['candidates'][k]['lane'] == lane for k in state['current']), 4)
        self.assertTrue(any('Exploration slot' in r for r in state['reasons'].values()))

    def test_same_exposure_positive_negative_unknown_changes_distribution(self):
        initial = self.state()
        roots = [k for k in initial['current'] if initial['candidates'][k]['lane'] == 'film-space']
        self.assertTrue(roots)
        # Identical judged identity and remaining pool; only the human response differs.
        branches = {}
        for choice in intake.CHOICES:
            state = deepcopy(initial)
            state['choices'][roots[0]] = choice
            intake.select_round_v2(state)
            branches[choice] = state
        positive = branches['enjoyed']; negative = branches['not_for_me']; unknown = branches['unknown']
        self.assertNotEqual(positive['current'], negative['current'])
        self.assertNotEqual(positive['current'], unknown['current'])
        self.assertEqual(intake.positive_roots(unknown), [])
        self.assertEqual(intake.lane_weights(unknown)['film-space'], 0)
        self.assertEqual(intake.lane_weights(branches['interested'])['film-space'], 1)
        self.assertEqual(intake.lane_weights(positive)['film-space'], 3)
        self.assertGreater(sum(positive['candidates'][k]['lane'] == 'film-space' for k in positive['current']),
                           sum(negative['candidates'][k]['lane'] == 'film-space' for k in negative['current']))

    def test_two_original_positives_survive_competing_strong_lane(self):
        state = self.state()
        roots = [c for c in state['candidates'].values() if c['medium'] == 'movie' and c['lane'] == 'film-space'][:2]
        for number, root in enumerate(roots):
            state['choices'][root['key']] = 'enjoyed' if number == 0 else 'interested'
            card = p.Candidate(f'tmdb:movie:{900+number}', 'tmdb', 900+number, 'movie',
                'Fictional provider neighbor', '', f'https://www.themoviedb.org/movie/{900+number}',
                '2026-01-01T00:00:00+00:00', 'root:' + root['key'], 'Fiction neighborhood', 'neighbor', root['key'])
            intake.merge(state, [card])
        intake.select_round_v2(state)
        self.assertIn('tmdb:movie:900', state['current'])
        self.assertIn('tmdb:movie:901', state['current'])
        state['choices'][roots[0]['key']] = 'not_for_me'
        intake.select_round_v2(state)
        self.assertNotIn('tmdb:movie:900', state['current'])
        self.assertIn('tmdb:movie:901', state['current'])
        state['choices']['tmdb:movie:901'] = 'enjoyed'
        self.assertNotIn('tmdb:movie:901', [c['key'] for c in intake.positive_roots(state)])

    def test_finite_pool_no_duplicates_and_exhaustion_no_implicit_response(self):
        state = intake.new_state(1, 1)
        intake.merge(state, fiction_cards()[:2])
        self.assertEqual(len(intake.select_round_v2(state)), 2)
        self.assertEqual(state['choices'], {})
        for key in state['current']:
            state['choices'][key] = 'unknown'
        self.assertEqual(intake.select_round_v2(state), [])


class DurableFixture:
    def setUp(self):
        super().setUp()
        self.actor = get_user_model().objects.create_user(pk=1, username='durable-fiction-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Durable fiction')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.run = d.new_run(self.actor, 1)
        self.state = d.hydrate(self.run)
        intake.merge(self.state, fiction_cards())
        self.state['started'] = True
        intake.select_round_v2(self.state)

    def commit(self, action='choose', *, state=None, **extra):
        state = self.state if state is None else state
        payload = {'action': action, **extra}
        run, result = d.commit(actor=self.actor, profile_id=1, run_id=state['run_id'],
            revision=state['revision'], nonce=state['nonce'], payload=payload, state=state)
        return run, result


class DurableServiceTests(DurableFixture, TestCase):
    def test_receipt_exact_replay_and_two_tabs_no_lost_update(self):
        tab_a, tab_b = deepcopy(self.state), deepcopy(self.state)
        first, second = tab_a['current'][:2]
        run, receipt = self.commit(state=tab_a, candidate_id=first, choice='enjoyed')
        replay, repeated = self.commit(state=tab_a, candidate_id=first, choice='enjoyed')
        self.assertEqual(receipt, repeated)
        self.assertEqual(len(replay.history), 1)
        with self.assertRaises(intake.IntakeError) as stale:
            self.commit(state=tab_b, candidate_id=second, choice='not_for_me')
        self.assertEqual(stale.exception.status, 409)
        refreshed = d.hydrate(run, tab_b)
        updated, _ = self.commit(state=refreshed, candidate_id=second, choice='not_for_me')
        self.assertEqual(d.effective(updated)[0], {first: 'enjoyed', second: 'not_for_me'})
        self.assertEqual(updated.revision, 2)

    def test_minimal_history_distinct_signal_provenance_no_machine_pool(self):
        key = self.state['current'][0]
        self.state['candidates'][key]['description'] = 'PROVIDER DESCRIPTION MUST STAY TEMPORARY'
        run, _ = self.commit(candidate_id=key, choice='unknown')
        event = run.history[0]
        self.assertEqual(event['choice'], 'unknown')
        self.assertEqual(event['card']['key'], key)
        self.assertTrue(event['responded_at'])
        body = json.dumps(run.history)
        self.assertNotIn('PROVIDER DESCRIPTION', body)
        self.assertNotIn('rating', body)
        self.assertNotIn(self.state['current'][1], body)
        self.assertEqual(FeedbackEvent.objects.count(), 0)
        self.assertEqual(SavedItem.objects.count(), 0)
        self.grant.refresh_from_db()
        self.assertFalse(self.grant.can_mutate)
        self.assertFalse(self.grant.can_export)

    def test_undo_and_correction_keep_original_human_history(self):
        key = self.state['current'][0]
        run, _ = self.commit(candidate_id=key, choice='enjoyed')
        state = d.hydrate(run, self.state)
        run, _ = self.commit('correct', state=state, candidate_id=key, choice='not_for_me')
        self.assertEqual(d.effective(run)[0][key], 'not_for_me')
        state = d.hydrate(run, state)
        run, _ = self.commit('undo', state=state, candidate_id=key)
        self.assertEqual(d.effective(run)[0], {})
        self.assertEqual([e['choice'] for e in run.history], ['enjoyed', 'not_for_me', None])
        self.assertIn(key, d.hydrate(run, state)['current'])

    def test_reset_archives_and_creates_separate_empty_run_atomically(self):
        key = self.state['current'][0]
        run, _ = self.commit(candidate_id=key, choice='interested')
        state = d.hydrate(run, self.state)
        archived, result = self.commit('reset', state=state)
        self.assertIsNotNone(archived.archived_at)
        replacement = d.owned(self.actor, 1, result['next_run_id'])
        self.assertEqual(replacement.history, [])
        self.assertEqual(len(archived.history), 1)
        replay, receipt = self.commit('reset', state=state)
        self.assertEqual(receipt, result)
        self.assertEqual(IntakeRun.objects.count(), 2)

    def test_authority_and_source_identity_fail_closed(self):
        key = self.state['current'][0]
        self.state['candidates'][key]['url'] = 'https://evil.invalid/book'
        with self.assertRaises(intake.IntakeError):
            self.commit(candidate_id=key, choice='enjoyed')
        self.run.refresh_from_db()
        self.assertEqual(self.run.history, [])
        self.grant.can_select = False; self.grant.save()
        with self.assertRaises(Http404):
            self.commit(candidate_id=key, choice='unknown')

    def test_noncurrent_candidate_cannot_be_submitted_and_search_add_no_like(self):
        self.state['search_results'] = [fiction_cards()[-1].key]
        with self.assertRaises(intake.IntakeError):
            self.commit(candidate_id=self.state['search_results'][0], choice='enjoyed')
        for action in ('search', 'add', 'neighbors'):
            run, _ = self.commit(action)
            self.state = d.hydrate(run, self.state)
        self.assertEqual(self.state['choices'], {})
        self.assertEqual(run.history, [])

    def test_import_owned_unexpired_sessions_separate_idempotent_unknown_time(self):
        self.state.pop('run_id')
        self.state['choices'] = {self.state['current'][0]: 'unknown', self.state['current'][1]: 'enjoyed'}
        first = d.import_state(actor=self.actor, profile_id=1, state=self.state, session_key='fiction-session-A')
        same = d.import_state(actor=self.actor, profile_id=1, state=self.state, session_key='fiction-session-A')
        second = d.import_state(actor=self.actor, profile_id=1, state=self.state, session_key='fiction-session-B')
        self.assertEqual(first.pk, same.pk)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(len(first.history), 2)
        self.assertTrue(all(e['responded_at'] is None for e in first.history))
        self.assertTrue(all('unknown' in e['provenance'] for e in first.history))
        self.assertNotIn('candidates', json.dumps(first.history))

    def test_invalid_expired_mismatched_import_does_not_invent_history(self):
        state = deepcopy(self.state)
        state['choices'] = {state['current'][0]: 'enjoyed'}
        for field, value in [('actor_id', 2), ('profile_id', 2), ('expires_at', time.time()-1),
                             ('choices', {'invented': 'enjoyed'})]:
            bad = deepcopy(state); bad[field] = value
            self.assertIsNone(d.import_state(actor=self.actor, profile_id=1, state=bad, session_key='fiction'))
        self.assertEqual(IntakeRun.objects.count(), 1)

    def test_prepare_uses_framework_auth_expiry_and_never_changes_sessions(self):
        client = Client(); client.force_login(self.actor)
        session = client.session
        session[SELECTION_KEY] = 1
        session[intake.SESSION_KEY] = deepcopy(self.state)
        session[intake.SESSION_KEY].pop('run_id')
        key = self.state['current'][0]
        session[intake.SESSION_KEY]['choices'] = {key: 'interested'}
        session.save()
        before = list(Session.objects.values('session_key', 'session_data', 'expire_date'))
        self.assertEqual(d.preserve_sessions(), 1)
        self.assertEqual(d.preserve_sessions(), 1)
        self.assertEqual(list(Session.objects.values('session_key', 'session_data', 'expire_date')), before)
        imported = IntakeRun.objects.get(imported_session__isnull=False)
        self.assertEqual(d.effective(imported)[0], {key: 'interested'})
        Session.objects.filter(session_key=session.session_key).update(expire_date=timezone.now()-timedelta(seconds=1))
        self.assertEqual(d.preserve_sessions(), 0)
        Session.objects.filter(session_key=session.session_key).update(expire_date=timezone.now()+timedelta(hours=1))
        session[HASH_SESSION_KEY] = 'invalid-fiction-hash'; session.save()
        before = list(Session.objects.values('session_key', 'session_data', 'expire_date'))
        self.assertEqual(d.preserve_sessions(), 0)
        self.assertEqual(list(Session.objects.values('session_key', 'session_data', 'expire_date')), before)


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class DurableViewsTests(DurableFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.actor)
        self.client.post('/profiles/select/', {'profile_id': 1})
        session = self.client.session
        session[intake.SESSION_KEY] = self.state
        session.save()
        self.url = '/profiles/1/intake/action/'

    def post(self, action, **extra):
        response = self.client.get('/profiles/1/')
        return self.client.post(self.url, {'action': action, 'revision': response.context['revision'],
            'nonce': response.context['nonce'], **extra})

    def test_logout_and_session_expiry_resume_human_history_without_fetch(self):
        key = self.state['current'][0]
        self.assertEqual(self.post('choose', candidate_id=key, choice='enjoyed').status_code, 302)
        self.client.logout(); self.client.force_login(self.actor)
        self.client.post('/profiles/select/', {'profile_id': 1})
        with patch.object(p, '_request', side_effect=AssertionError('GET cannot fetch')):
            response = self.client.get('/profiles/1/')
        self.assertEqual(response.context['choice_counts']['enjoyed'], 1)
        self.assertEqual(response.context['cards'], [])
        self.assertTrue(response.context['durable_active'])
        self.assertTrue(response.context['positive_roots'][0]['can_expand'])
        self.assertEqual(response.context['run_id'], str(self.run.pk))

    def test_http_exact_replay_does_not_duplicate_and_stale_tab_conflicts(self):
        response = self.client.get('/profiles/1/')
        data = {'action': 'choose', 'revision': response.context['revision'], 'nonce': response.context['nonce'],
                'candidate_id': self.state['current'][0], 'choice': 'unknown'}
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(self.client.post(self.url, dict(data, choice='enjoyed')).status_code, 409)
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.history), 1)

    def test_search_add_is_not_response_and_new_run_archives(self):
        card = replace(fiction_cards()[-1], source_id=999, key='hardcover:book:999')
        with patch.object(p, 'search', return_value=[card]):
            self.assertEqual(self.post('search', medium='book', query='Fiction').status_code, 302)
        self.assertEqual(self.post('add', candidate_id=card.key).status_code, 302)
        response = self.client.get('/profiles/1/')
        self.assertIn(card.key, [c['key'] for c in response.context['cards']])
        self.assertEqual(sum(response.context['choice_counts'].values()), 0)
        self.assertEqual(self.post('choose', candidate_id=card.key, choice='interested').status_code, 302)
        self.assertEqual(self.post('new_run').status_code, 302)
        self.run.refresh_from_db(); self.assertIsNotNone(self.run.archived_at)
        self.assertEqual(len(self.run.history), 1)
        self.assertEqual(self.client.get('/profiles/1/').context['choice_counts']['interested'], 0)

    def test_book_neighbors_one_explicit_fetch_failure_budget_and_human_only_roots(self):
        book = next(k for k in self.state['current'] if self.state['candidates'][k]['medium'] == 'book')
        with patch.object(p, 'neighbors', return_value=[]) as fetch:
            self.assertEqual(self.post('neighbors', candidate_id=book).status_code, 400)
            fetch.assert_not_called()
            self.assertEqual(self.post('choose', candidate_id=book, choice='interested').status_code, 302)
            self.client.get('/profiles/1/')
            fetch.assert_not_called()
            self.assertEqual(self.post('neighbors', candidate_id=book).status_code, 302)
            self.assertEqual(self.post('neighbors', candidate_id=book).status_code, 400)
            fetch.assert_called_once()

    def test_revoke_during_io_rolls_back_choices_and_does_not_save_pool(self):
        def search(*args, **kwargs):
            Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
            return [fiction_cards()[0]]
        with patch.object(p, 'search', side_effect=search):
            self.assertEqual(self.post('search', medium='movie', query='Fiction').status_code, 404)
        self.run.refresh_from_db()
        self.assertEqual(self.run.revision, 0)
        self.assertNotIn(intake.SESSION_KEY, self.client.session)

    def test_export_csrf_and_post_only_with_no_provider_data(self):
        key = self.state['current'][0]
        self.post('choose', candidate_id=key, choice='unknown')
        response = self.post('export')
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body['schema'], 'human-intake-v1')
        self.assertNotIn('description', json.dumps(body))
        self.assertEqual(self.client.get(self.url).status_code, 405)
        client = Client(enforce_csrf_checks=True); client.force_login(self.actor)
        session = client.session; session[SELECTION_KEY] = 1; session.save()
        self.assertEqual(client.post(self.url, {'action': 'continue', 'revision': 0, 'nonce': 'x'}).status_code, 403)


class HardcoverNeighborTests(SimpleTestCase):
    def root(self):
        return next(c.data() for c in fiction_cards() if c.medium == 'book')

    def test_fixed_query_bounded_ids_lookup_and_no_recursive_harvest(self):
        provider, command = p._command('book_neighbors', 100)
        document = json.loads(command[command.index('--data-binary')+1])
        self.assertEqual(provider, 'hardcover')
        self.assertEqual(document, {'query': 'query Neighbors($id:Int!){books_by_pk(id:$id){cached_similar_book_ids}}', 'variables': {'id': 100}})
        ids = list(range(200, 220))
        with patch.object(p, '_request', side_effect=[{'books_by_pk': {'cached_similar_book_ids': ids}},
                {'books': [{'id': 200, 'title': 'Fictional neighbor', 'slug': 'fiction-neighbor'}]}]) as fetch:
            cards = p.neighbors(self.root(), actor_id=1, profile_id=1)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[1].args, ('book_details', ids[:9]))
        self.assertEqual(cards[0].origin, 'neighbor')
        self.assertEqual(cards[0].root, self.root()['key'])
        with patch.object(p, '_request') as fetch:
            with self.assertRaises(p.CatalogueError):
                p.neighbors(cards[0].data(), actor_id=1, profile_id=1)
            fetch.assert_not_called()

    def test_unknown_jsonb_shapes_explicit_unavailable_and_empty_is_empty(self):
        for shape in (None, {}, {'ids': [1]}, '1,2', ['1'], [True], [0], [-1], [2147483648]):
            with self.subTest(shape=shape), patch.object(p, '_request', return_value={'books_by_pk': {'cached_similar_book_ids': shape}}) as fetch:
                with self.assertRaises(p.CatalogueError) as error:
                    p.neighbors(self.root(), actor_id=1, profile_id=1)
                self.assertEqual(error.exception.status, p.Status.UNAVAILABLE)
                self.assertEqual(fetch.call_count, 1)
        with patch.object(p, '_request', return_value={'books_by_pk': {'cached_similar_book_ids': []}}) as fetch:
            self.assertEqual(p.neighbors(self.root(), actor_id=1, profile_id=1), [])
            self.assertEqual(fetch.call_count, 1)

    def test_prepare_flag_is_enumerated_without_command_execution(self):
        self.assertEqual(boundary.validate_command(['catalogue_prepare', '--provider-demo', '--preserve-intake']), 'catalogue_prepare')
        with self.assertRaises(boundary.BoundaryError):
            boundary.validate_command(['catalogue_runserver', '--provider-demo', '--preserve-intake'])


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class DurableUpgradeViewsTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='upgrade-fiction-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Upgrade fiction')
        Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client.force_login(self.actor)
        self.client.post('/profiles/select/', {'profile_id': 1})

    def fields(self):
        response = self.client.get('/profiles/1/')
        return {'revision': response.context['revision'], 'nonce': response.context['nonce']}

    def test_actual_validated_start_automatically_uses_durable_balanced_path(self):
        with patch.object(p, 'initial_deck', return_value=(fiction_cards(), [])) as fetch:
            self.assertEqual(self.client.post('/profiles/1/catalogue/action/', dict(self.fields(), action='start')).status_code, 302)
            fetch.assert_called_once()
        response = self.client.get('/profiles/1/')
        self.assertTrue(response.context['durable_active'])
        self.assertEqual(response.context['selector_version'], intake.SELECTOR_V2)
        for medium in ('movie', 'tv', 'book'):
            self.assertEqual(sum(c['medium'] == medium for c in response.context['cards']), 3)
        key = response.context['cards'][0]['key']
        self.assertEqual(self.client.post('/profiles/1/catalogue/action/', dict(self.fields(), action='choose', candidate_id=key, choice='interested')).status_code, 302)
        run = IntakeRun.objects.get()
        self.assertEqual(run.history[0]['choice'], 'interested')
        self.assertIsNotNone(run.history[0]['responded_at'])
        self.assertEqual(run.history[0]['provenance'], 'authenticated-human-post')

    def test_first_use_preserves_canonical_prototype_choices_and_selected_profile(self):
        state = intake.new_state(1, 1)
        intake.merge(state, fiction_cards()); intake.select_round(state)
        key = state['current'][0]
        state['choices'] = {key: 'not_for_me'}
        session = self.client.session; session[intake.SESSION_KEY] = state; session.save()
        with patch.object(p, '_request', side_effect=AssertionError('no fetch on upgrade')):
            response = self.client.get('/profiles/1/')
        self.assertEqual(response.context['choice_counts']['not_for_me'], 1)
        run = IntakeRun.objects.get()
        self.assertIsNone(run.history[0]['responded_at'])
        self.assertEqual(self.client.session[SELECTION_KEY], 1)
        self.assertEqual(self.client.session[intake.SESSION_KEY]['choices'], {key: 'not_for_me'})
        self.assertEqual(self.client.get('/profiles/1/').context['run_id'], str(run.pk))
        self.assertEqual(IntakeRun.objects.count(), 1)

    def test_unprepared_explicit_durable_route_unavailable_without_provider_io(self):
        with patch.object(d, 'available', return_value=False), patch.object(p, '_request') as fetch:
            self.assertEqual(self.client.post('/profiles/1/intake/action/', dict(self.fields(), action='start')).status_code, 503)
            response = self.client.get('/profiles/1/')
            self.assertFalse(response.context['durable_available'])
            fetch.assert_not_called()

    def test_explicit_continue_empty_is_not_like_and_refresh_only_post(self):
        self.assertEqual(self.client.post('/profiles/1/intake/action/', dict(self.fields(), action='continue')).status_code, 302)
        run = IntakeRun.objects.get(); self.assertEqual(run.history, [])
        with patch.object(p, 'initial_deck', return_value=(fiction_cards(), [])) as fetch:
            self.assertEqual(self.client.get('/profiles/1/intake/').status_code, 200)
            fetch.assert_not_called()
            self.assertEqual(self.client.post('/profiles/1/intake/action/', dict(self.fields(), action='refresh')).status_code, 302)
            fetch.assert_called_once()
        run.refresh_from_db(); self.assertEqual(run.history, [])


class DurableBoundsTests(DurableFixture, TestCase):
    def test_full_run_can_still_archive_without_erasing_or_adding_choices(self):
        key, other = self.state['current'][:2]
        with patch.object(d, 'MAX_HISTORY', 1), patch.object(d, 'MAX_RECEIPTS', 1):
            run, _ = self.commit(candidate_id=key, choice='unknown')
            state = d.hydrate(run, self.state)
            with self.assertRaises(intake.IntakeError):
                self.commit(state=state, candidate_id=other, choice='enjoyed')
            archived, result = self.commit('new_run', state=state)
        self.assertEqual(len(archived.history), 1)
        self.assertEqual(d.owned(self.actor, 1, result['next_run_id']).history, [])

    def test_malformed_server_session_shapes_are_not_imported(self):
        for state in ([], ['not-a-state'], 'bad', {'actor_id': True, 'profile_id': 1, 'expires_at': time.time()+100},
                      {'actor_id': 1, 'profile_id': 1, 'expires_at': 'tomorrow'},
                      {'actor_id': 1, 'profile_id': 1, 'expires_at': float('nan')}):
            self.assertIsNone(d.import_state(actor=self.actor, profile_id=1, state=state, session_key='fiction'))
        self.assertEqual(IntakeRun.objects.count(), 1)

    def test_corrupt_timestamp_not_persisted_and_expired_state_rejects_post(self):
        key = self.state['current'][0]
        self.state['candidates'][key]['fetched_at'] = '2026-99-99T99:00:00+00:00'
        with self.assertRaises(intake.IntakeError):
            self.commit(candidate_id=key, choice='enjoyed')
        self.state['expires_at'] = time.time()-1
        with self.assertRaises(intake.IntakeError) as error:
            self.commit('continue')
        self.assertEqual(error.exception.status, 409)
        self.run.refresh_from_db(); self.assertEqual(self.run.revision, 0)

    def test_latest_correction_moves_to_latest_removable_choice(self):
        first, second = self.state['current'][:2]
        run, _ = self.commit(candidate_id=first, choice='enjoyed')
        state = d.hydrate(run, self.state)
        run, _ = self.commit(state=state, candidate_id=second, choice='unknown')
        state = d.hydrate(run, state)
        run, _ = self.commit('correct', state=state, candidate_id=first, choice='interested')
        state = d.hydrate(run, state)
        run, _ = self.commit('undo', state=state)
        self.assertEqual(d.effective(run)[0], {second: 'unknown'})
        self.assertEqual(len(run.history), 4)


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class DurableResumeTests(DurableFixture, TestCase):
    def test_explicit_resume_keeps_conflicting_histories_separate_and_is_idempotent(self):
        key = self.state['current'][0]
        first, _ = self.commit(candidate_id=key, choice='enjoyed')
        second = d.new_run(self.actor, 1)
        other_state = d.hydrate(second)
        intake.merge(other_state, fiction_cards()); intake.select_round_v2(other_state)
        second, _ = self.commit(state=other_state, candidate_id=key, choice='not_for_me')
        self.client.force_login(self.actor)
        session = self.client.session; session[SELECTION_KEY] = 1
        session[intake.SESSION_KEY] = d.hydrate(first, self.state); session.save()
        response = self.client.get('/profiles/1/')
        payload = {'action': 'resume', 'run_id': str(second.pk),
                   'nonce': response.context['nonce'], 'revision': response.context['revision']}
        for _ in range(2):
            self.assertEqual(self.client.post('/profiles/1/intake/action/', payload).status_code, 302)
        response = self.client.get('/profiles/1/')
        self.assertEqual(response.context['run_id'], str(second.pk))
        self.assertEqual(response.context['choice_counts']['not_for_me'], 1)
        self.assertEqual(response.context['choice_counts']['enjoyed'], 0)
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual(d.effective(first)[0], {key: 'enjoyed'})
        self.assertEqual(d.effective(second)[0], {key: 'not_for_me'})
        self.assertEqual(len(first.history), 1)
        self.assertEqual(len(second.history), 1)
