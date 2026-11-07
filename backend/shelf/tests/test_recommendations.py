"""Real view/selector integration with synthetic source rows and human histories."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import TestCase, override_settings
from django.utils import timezone

from shelf import durable_intake as durable, intake, provider_catalogue as catalogue
from shelf import discovery_sources as sources, discovery_views
from shelf.authority import SELECTION_KEY
from shelf.models import DiscoveryState, FeedbackEvent, Grant, IntakeRun, Profile, SavedItem


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class RecommendationTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-overview-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='SYNTHETIC owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client.force_login(self.actor)
        self.url = '/profiles/1/recommendations/'
        session = self.client.session
        session[SELECTION_KEY] = 1
        session.save()
        discovery_views.PUBLIC_CACHE.clear()
        self.addCleanup(discovery_views.PUBLIC_CACHE.clear)
        self.now = timezone.now()

    def cache_catalogue(self, *, durable_run=True):
        cards = []
        for medium, number in (('movie', 100), ('movie', 101), ('tv', 200), ('book', 300), ('book', 301)):
            provider = 'hardcover' if medium == 'book' else 'tmdb'
            title = f'SYNTHETIC {medium} {number}'
            row = {'id': number, 'title': title, 'name': title, 'slug': f'synthetic-{number}',
                   'book_category_id': 4 if number == 301 else 1}
            cards += catalogue.parse_candidates(provider, medium, [row], lane=f'editorial:{medium}', lane_label='Synthetic exploration')
        cards.append(replace(cards[0], key='tmdb:movie:900', source_id=900,
            title='SYNTHETIC provider neighbor', url='https://www.themoviedb.org/movie/900',
            origin='neighbor', root='tmdb:movie:100', lane='root:tmdb:movie:100'))
        self.run = durable.new_run(self.actor, 1) if durable_run else None
        self.state = durable.hydrate(self.run) if self.run else intake.new_state(1, 1)
        intake.merge(self.state, cards)
        self.state['started'] = True
        intake.select_round_v2(self.state)
        self.save_session_state()
        return cards

    def save_session_state(self):
        session = self.client.session
        session[intake.SESSION_KEY] = deepcopy(self.state)
        session.save()

    def respond(self, key, choice, operation='choose', *, update_session=True):
        run, _ = durable.commit(actor=self.actor, profile_id=1, run_id=self.state['run_id'],
            revision=self.state['revision'], nonce=self.state['nonce'],
            payload={'action': operation, 'candidate_id': key, 'choice': choice}, state=self.state)
        self.run = run
        self.state = durable.hydrate(run, self.state)
        if update_session:
            self.save_session_state()

    def public_result(self, *, age=0, status='ready', event_start=None):
        at = self.now - timedelta(seconds=age)
        recording = sources.Candidate('musicbrainz:recording:00000000-0000-0000-0000-000000000003',
            'SYNTHETIC public recording', 'recording', 'ListenBrainz artist radio', 'live-automated',
            1, at.isoformat(), url='https://musicbrainz.org/recording/00000000-0000-0000-0000-000000000003')
        event = sources.Candidate('moshtix:event:90001', 'SYNTHETIC public event', 'event', 'Moshtix',
            'live-automated', 1, at.isoformat(), url='https://moshtix.com.au/v2/event/synthetic/90001',
            starts_at=(event_start or self.now + timedelta(days=2)).isoformat())
        outcome = sources.Outcome(status, (recording, event) if status == 'ready' else (), 'Synthetic bounded response')
        discovery_views.PUBLIC_CACHE.set(f'1:1:{self.client.session.session_key}',
            {'test-result': {'source': 'SYNTHETIC public source', 'at': at, 'outcome': outcome}}, timeout=900)
        return recording, event

    def discovery_response(self, key, choice, *, mode='household', operation='respond'):
        row, _ = DiscoveryState.objects.get_or_create(actor=self.actor, profile=self.profile, mode=mode,
                                                      defaults={'nonce': 'synthetic-nonce'})
        row.history.append({'operation': operation, 'key': key, 'choice': choice,
            'mode': mode, 'origin': 'authenticated-human-post', 'at': self.now.isoformat()})
        row.revision += 1
        row.save()
        return row

    def test_empty_get_is_read_only_no_fetch_or_preference_initialization(self):
        before = Session.objects.get(session_key=self.client.session.session_key).session_data
        with patch.object(catalogue, '_request', side_effect=AssertionError('no provider on GET')), \
                patch.object(sources, 'fetch_public', side_effect=AssertionError('no provider on GET')):
            response = self.client.get('/recommendations/')
        self.assertContains(response, 'Across your Cabinet')
        self.assertContains(response, 'No positive responses yet')
        self.assertContains(response, 'Not fetched or expired', count=2)
        self.assertContains(response, 'verified adaptation and shared-theme links are not connected')
        self.assertEqual(Session.objects.get(session_key=self.client.session.session_key).session_data, before)
        for model in (IntakeRun, DiscoveryState, FeedbackEvent, SavedItem):
            self.assertEqual(model.objects.count(), 0)

    def test_current_source_rounds_cover_every_medium_and_keep_graphic_classification(self):
        self.cache_catalogue()
        self.public_result()
        response = self.client.get(self.url)
        sections = response.context['sections']
        self.assertEqual({s['id'] for s in sections if s['cards']}, {'movie', 'tv', 'book', 'music', 'event'})
        self.assertContains(response, 'Graphic novel')
        self.assertContains(response, 'href="https://hardcover.app/books/synthetic-301"')
        self.assertContains(response, 'referrerpolicy="no-referrer"')
        self.assertContains(response, 'SYNTHETIC public recording')
        self.assertContains(response, 'Ticket availability, price and cancellation status unverified')
        self.assertEqual(response.context['lanes'], [])

    def test_independent_catalogue_and_music_seeds_keep_distinct_signal_labels(self):
        self.cache_catalogue()
        self.respond('tmdb:movie:100', 'enjoyed')
        self.respond('tmdb:tv:200', 'interested')
        seed = sources.household_candidates()[0]
        self.discovery_response(seed.key, 'interested')
        response = self.client.get(self.url)
        lanes = response.context['lanes']
        self.assertEqual(len(lanes), 3)
        self.assertEqual({r['choice'] for r in lanes}, {'Experienced enjoyment', 'Anticipated interest'})
        self.assertContains(response, 'Provider suggestion from SYNTHETIC movie 100')
        self.assertEqual(len({r['title'] for r in lanes}), 3)

    def test_newer_correction_removes_stale_tab_seed_and_its_neighbor(self):
        self.cache_catalogue()
        self.respond('tmdb:movie:100', 'enjoyed')
        self.respond('tmdb:movie:100', 'not_for_me', 'correct', update_session=False)
        before = deepcopy(self.run.history)
        response = self.client.get(self.url)
        self.assertEqual(response.context['lanes'], [])
        self.assertNotContains(response, 'SYNTHETIC provider neighbor')
        self.assertEqual(response.context['intake_revision'], self.run.revision)
        self.run.refresh_from_db()
        self.assertEqual(self.run.history, before)
        self.assertEqual(self.client.session[intake.SESSION_KEY]['choices']['tmdb:movie:100'], 'enjoyed')

    def test_undo_clears_lane_without_inventing_human_feedback(self):
        self.cache_catalogue()
        self.respond('tmdb:movie:100', 'enjoyed')
        self.respond('tmdb:movie:100', None, 'undo')
        response = self.client.get(self.url)
        self.assertEqual(response.context['lanes'], [])
        self.assertNotContains(response, 'Provider suggestion from SYNTHETIC movie 100')

    def test_expired_cache_preserves_owned_seed_but_drops_source_content(self):
        self.cache_catalogue()
        self.respond('tmdb:movie:100', 'enjoyed')
        self.state['expires_at'] = self.now.timestamp() - 1
        self.save_session_state()
        response = self.client.get(self.url)
        self.assertEqual(len(response.context['lanes']), 1)
        self.assertEqual(response.context['lanes'][0]['count'], 0)
        self.assertContains(response, 'Not fetched or expired', count=2)
        self.assertNotContains(response, 'SYNTHETIC provider neighbor')
        self.assertFalse(any(s['cards'] for s in response.context['sections'] if s['id'] in ('movie', 'tv', 'book')))

    def test_archived_run_and_its_cached_positives_cannot_reappear(self):
        self.cache_catalogue()
        self.respond('tmdb:movie:100', 'enjoyed')
        self.run.archived_at = self.now
        self.run.save()
        response = self.client.get(self.url)
        self.assertEqual(response.context['lanes'], [])
        self.assertNotContains(response, 'SYNTHETIC provider neighbor')

    def test_expired_and_other_session_public_results_are_absent(self):
        self.public_result(age=901)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'SYNTHETIC public recording')
        self.assertEqual(response.context['public_results'], [])
        self.public_result()
        self.client.logout()
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = 1
        session.save()
        self.assertNotContains(self.client.get(self.url), 'SYNTHETIC public recording')

    def test_sample_history_cannot_seed_household_overview(self):
        seed = sources.household_candidates()[0]
        self.discovery_response(seed.key, 'enjoyed', mode='sample')
        response = self.client.get(self.url)
        self.assertEqual(response.context['lanes'], [])
        self.assertEqual(self.client.get(self.url, {'mode': 'sample'}).status_code, 404)

    def test_save_and_unknown_never_create_positive_lanes(self):
        seeds = sources.household_candidates()
        self.discovery_response(seeds[0].key, '', operation='save')
        self.discovery_response(seeds[1].key, 'unknown')
        self.assertEqual(self.client.get(self.url).context['lanes'], [])

    def test_past_events_are_not_recommended_and_negative_recording_is_hidden(self):
        recording, _ = self.public_result(event_start=self.now - timedelta(days=1))
        self.discovery_response(recording.key, 'not_for_me')
        response = self.client.get(self.url)
        self.assertNotContains(response, 'SYNTHETIC public event')
        self.assertNotContains(response, 'SYNTHETIC public recording')

    def test_public_empty_timeout_and_rate_limit_are_distinct(self):
        for status in ('empty', 'timeout', 'rate-limited'):
            with self.subTest(status=status):
                self.public_result(status=status)
                response = self.client.get(self.url)
                self.assertContains(response, 'SYNTHETIC public source · ' + status)
                self.assertNotContains(response, 'SYNTHETIC public event')

    def test_provider_text_is_escaped_and_future_timestamp_is_not_fresh(self):
        self.cache_catalogue()
        self.state['candidates']['tmdb:movie:101']['title'] = '<script>alert(1)</script>'
        self.state['candidates']['tmdb:movie:101']['fetched_at'] = (self.now + timedelta(days=1)).isoformat()
        self.save_session_state()
        response = self.client.get(self.url)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, 'Retrieval time is in the future; freshness unknown')

    def test_profile_grants_ownership_and_get_only_are_enforced(self):
        self.assertEqual(self.client.post(self.url, {}).status_code, 405)
        self.assertEqual(self.client.get('/profiles/2/recommendations/').status_code, 404)
        self.grant.can_select = False
        self.grant.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.grant.can_select = True
        self.grant.can_view = False
        self.grant.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_foreign_cached_state_cannot_expose_titles_or_lanes(self):
        self.cache_catalogue()
        self.state['actor_id'] = 99
        self.state['profile_id'] = 99
        self.save_session_state()
        response = self.client.get(self.url)
        self.assertNotContains(response, 'SYNTHETIC provider neighbor')
        self.assertEqual(response.context['lanes'], [])

    def test_missing_optional_tables_keep_overview_read_only(self):
        with patch('shelf.recommendation_views.connection.introspection.table_names', return_value=[]):
            response = self.client.get(self.url)
        self.assertContains(response, 'No positive responses yet')
        self.assertEqual(response.context['intake_revision'], None)
        self.assertEqual(response.context['discovery_revision'], None)

    def test_expired_saved_identities_do_not_count_or_crowd_out_fresh_results(self):
        seed = sources.household_candidates()[0]
        self.discovery_response(seed.key, 'enjoyed')
        for number in range(12):
            key = f'musicbrainz:recording:00000000-0000-0000-0000-{number + 100:012d}'
            row = self.discovery_response(key, '', operation='save')
            row.history[-1]['source'] = {'name': 'ListenBrainz artist radio', 'provenance': 'live-automated',
                'rank': 0, 'retrieved_at': (self.now - timedelta(hours=1)).isoformat(), 'roots': [seed.key]}
            row.save()
        response = self.client.get(self.url)
        self.assertEqual(response.context['lanes'][0]['count'], 0)
        self.public_result()
        self.assertContains(self.client.get(self.url), 'SYNTHETIC public recording')

    def test_judged_current_cards_do_not_consume_unjudged_recommendation_slots(self):
        seeds = [sources.Candidate(f'official:artist:synthetic-{n}', f'SYNTHETIC judged artist {n}',
            'artist', 'Synthetic test seed', 'manual-attributed', 0, None,
            url=f'https://example.org/artist-{n}') for n in range(14)]
        for seed in seeds:
            self.discovery_response(seed.key, 'unknown')
        self.public_result()
        with patch.object(sources, 'household_candidates', return_value=seeds):
            response = self.client.get(self.url)
        self.assertContains(response, 'SYNTHETIC public recording')
        self.assertNotContains(response, 'SYNTHETIC judged artist')

    def test_expired_positive_identity_can_anchor_current_lineage_without_being_a_suggestion(self):
        key = 'musicbrainz:artist:00000000-0000-0000-0000-000000000001'
        row = self.discovery_response(key, 'interested')
        row.history[-1]['source'] = {'name': 'Synthetic live source', 'provenance': 'live-automated',
            'rank': 0, 'retrieved_at': (self.now - timedelta(hours=1)).isoformat(), 'roots': []}
        row.save()
        recording, _ = self.public_result()
        cache_key = f'1:1:{self.client.session.session_key}'
        entries = discovery_views.PUBLIC_CACHE.get(cache_key)
        entries['test-result']['outcome'] = sources.Outcome('ready', (replace(recording, roots=(key,)),))
        discovery_views.PUBLIC_CACHE.set(cache_key, entries)
        response = self.client.get(self.url)
        lane, = response.context['lanes']
        self.assertEqual(lane['count'], 1)
        self.assertIsNone(lane['url'])
        music, = [s for s in response.context['sections'] if s['id'] == 'music']
        self.assertIn(recording.key, [c['key'] for c in music['cards']])

