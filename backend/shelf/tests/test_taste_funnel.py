"""Actual unified-funnel HTTP flows with synthetic cards and denied transports."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from shelf import discovery_sources as sources, discovery_views, intake
from shelf import provider_catalogue as catalogue, taste_funnel, taste_selection
from shelf.models import DiscoveryState, Grant, IntakeRun, Profile, SavedItem, SynthesisRun


class BrowserFormClient(Client):
    """Use the encoding emitted by these plain HTML forms, with real CSRF checks."""
    def post(self, path, data=None, **extra):
        return super().post(path, urlencode(data or {}, doseq=True),
                            content_type='application/x-www-form-urlencoded', **extra)


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class TasteFunnelHTTPTests(TestCase):
    url = '/profiles/1/taste/'
    action_url = '/profiles/1/taste/action/'
    fetch_url = '/profiles/1/taste/fetch/'
    synthesis_url = '/profiles/1/taste/synergize/'

    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-funnel-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Synthetic owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client = self.new_client()
        self.now = timezone.now()
        discovery_views.PUBLIC_CACHE.clear()
        self.addCleanup(discovery_views.PUBLIC_CACHE.clear)
        for target in ('shelf.provider_catalogue._request', 'shelf.provider_catalogue.initial_deck',
                       'shelf.discovery_sources.fetch_public', 'shelf.discovery_transport._send',
                       'socket.create_connection'):
            self.enterContext(patch(target, side_effect=AssertionError('Unmocked source access forbidden')))
        self.artists = [sources.Candidate(
            f'musicbrainz:artist:00000000-0000-0000-0000-{n:012d}', f'Synthetic artist {n}',
            'artist', 'Synthetic artist seeds', 'manual-attributed', n, None,
            url=f'https://musicbrainz.org/artist/00000000-0000-0000-0000-{n:012d}') for n in (1, 2)]
        self.enterContext(patch.object(sources, 'household_candidates', return_value=self.artists))
        self.provider_cards = []
        for medium, base in (('movie', 100), ('tv', 200), ('book', 300)):
            for offset in range(8):
                number = base + offset
                provider = 'hardcover' if medium == 'book' else 'tmdb'
                self.provider_cards += catalogue.parse_candidates(provider, medium,
                    [{'id': number, 'title': f'Synthetic {medium} {number}', 'name': f'Synthetic {medium} {number}',
                      'slug': f'synthetic-{number}'}], lane=f'editorial:{medium}', lane_label='Synthetic exploration')
        self.write_catalogue(self.provider_cards)
        self.public_cards = tuple(sources.Candidate(
            f'moshtix:event:{900+n}', f'Synthetic event {n}', 'event', 'Synthetic event source',
            'live-automated', n, self.now.isoformat(), url=f'https://www.moshtix.com.au/v2/event/synthetic/{900+n}',
            starts_at=(self.now+timedelta(days=5)).isoformat()) for n in range(6))
        self.write_public(self.public_cards)

    def new_client(self):
        client = BrowserFormClient(enforce_csrf_checks=True)
        client.force_login(self.actor)
        session = client.session
        session['selected_profile_id'] = 1
        session.save()
        return client

    def write_catalogue(self, cards):
        state = intake.new_state(self.actor.pk, self.profile.pk)
        intake.merge(state, cards)
        state['started'] = True
        session = self.client.session
        session[intake.SESSION_KEY] = state
        session.save()

    def write_public(self, cards):
        discovery_views.PUBLIC_CACHE.set(f'1:1:{self.client.session.session_key}',
            {('synthetic', '', ''): {'at': self.now, 'source': 'Synthetic events',
                                   'outcome': sources.Outcome('ready', tuple(cards), 'Synthetic bounded response')}}, timeout=900)

    def page(self, client=None):
        response = (client or self.client).get(self.url)
        self.assertEqual(response.status_code, 200)
        return response

    def fields(self, operation, key='', *, page=None, client=None, **extra):
        client = client or self.client
        page = page or self.page(client)
        return dict(operation=operation, key=key, revision=page.context['revision'], nonce=page.context['nonce'],
                    preview=page.context['preview'], csrfmiddlewaretoken=client.cookies[settings.CSRF_COOKIE_NAME].value, **extra)

    def post(self, operation, key='', **extra):
        return self.client.post(self.action_url, self.fields(operation, key, **extra))

    def projected(self):
        row = DiscoveryState.objects.filter(actor=self.actor, profile=self.profile, mode='household').first()
        return taste_selection.project(row.history if row else [])

    def fetch_fields(self, page=None):
        page = page or self.page()
        return {'revision': page.context['revision'], 'nonce': page.context['nonce'], 'confirmed': 'on',
                'csrfmiddlewaretoken': self.client.cookies[settings.CSRF_COOKIE_NAME].value}

    def mocked_fetch(self, fields=None):
        with patch.object(catalogue, 'initial_deck', return_value=(self.provider_cards, [])) as initial, \
             patch.object(catalogue, 'browse_books', return_value=([], False)) as graphic, \
             patch.object(sources, 'fetch_public', return_value=sources.Outcome('empty', (), 'Synthetic empty source')) as public:
            response = self.client.post(self.fetch_url, fields or self.fetch_fields())
        if initial.call_count:
            graphic.assert_called_once_with('graphic_novels', actor_id=self.actor.pk, profile_id=self.profile.pk)
        else:
            graphic.assert_not_called()
        return response, initial.call_count, public.call_count

    def test_get_is_read_only_and_current_round_balances_all_media(self):
        page = self.page()
        self.assertEqual({r['card']['medium'] for r in page.context['cards']}, {'movie', 'tv', 'book', 'music', 'event'})
        self.assertEqual(len(page.context['cards']), 12)
        self.assertIn('no-store', page['Cache-Control'])
        for model in (DiscoveryState, SavedItem, IntakeRun, SynthesisRun):
            self.assertFalse(model.objects.exists())
        self.assertEqual(self.page().context['snapshot'], page.context['snapshot'])
        self.assertEqual(self.client.get(self.action_url).status_code, 405)

    def test_numeric_one_to_five_edit_clear_and_experience_are_real_posts(self):
        key = 'tmdb:movie:100'
        for basis in ('interest', 'enjoyment'):
            for value in range(1, 6):
                fields = dict(basis=basis, value=value)
                if basis == 'enjoyment':
                    fields['experienced'] = 'on'
                self.assertEqual(self.post('rate', key, **fields).status_code, 302)
                self.assertEqual(self.projected()['ratings'][key][basis], value)
        self.assertEqual(self.post('rate', key, basis='interest', value='').status_code, 302)
        self.assertEqual(self.projected()['ratings'][key], {'enjoyment': 5})
        self.assertEqual(self.post('rate', key, basis='enjoyment', value='', experienced='on').status_code, 302)
        self.assertNotIn(key, self.projected()['ratings'])
        self.assertEqual(self.post('rate', key, basis='enjoyment', value=5).status_code, 400)
        event = self.public_cards[0].key
        self.assertEqual(self.post('rate', event, basis='enjoyment', value=5).status_code, 400)
        self.assertEqual(self.post('rate', event, basis='enjoyment', value=5, experienced='on').status_code, 302)
        self.assertTrue(DiscoveryState.objects.get().history[-1]['experienced'])

    def test_canonical_favourite_is_independent_and_has_one_existing_saved_item(self):
        key = 'tmdb:movie:100'
        fields = self.fields('save', key)
        self.assertEqual(self.client.post(self.action_url, fields).status_code, 302)
        self.assertEqual(self.client.post(self.action_url, fields).status_code, 302)
        saved = SavedItem.objects.get()
        self.assertEqual((saved.provider, saved.medium, saved.source_id, saved.list_kind), ('tmdb', 'movie', 100, 'favorites'))
        self.assertEqual(saved.url, 'https://www.themoviedb.org/movie/100')
        self.assertEqual(self.projected()['ratings'], {})
        self.assertEqual(DiscoveryState.objects.get().history, [])
        self.assertIn(key, self.page().context['projection']['saved'])
        self.assertEqual(self.post('rate', key, basis='interest', value=4).status_code, 302)
        self.assertEqual(self.post('unsave', key).status_code, 302)
        self.assertFalse(SavedItem.objects.exists())
        self.assertEqual(self.projected()['ratings'][key], {'interest': 4})

    def test_music_favourite_and_skip_never_become_numeric_ratings(self):
        key = self.artists[0].key
        self.assertEqual(self.post('save', key).status_code, 302)
        self.assertEqual(self.post('funnel_skip', key).status_code, 302)
        projection = self.projected()
        self.assertEqual(projection['saved'], [key])
        self.assertEqual(projection['skipped'], [key])
        self.assertEqual(projection['ratings'], {})
        self.assertFalse(SavedItem.objects.exists())
        self.assertNotIn(key, [r['card']['key'] for r in self.page().context['cards']])

    def test_positive_ratings_leave_other_media_and_exploration_in_http_round(self):
        self.assertEqual(self.post('rate', 'tmdb:movie:100', basis='interest', value=5).status_code, 302)
        self.assertEqual(self.post('rate', self.artists[0].key, basis='enjoyment', value=4, experienced='on').status_code, 302)
        page = self.page()
        rows = page.context['cards']
        self.assertEqual({r['card']['medium'] for r in rows}, {'movie', 'tv', 'book', 'music', 'event'})
        self.assertGreaterEqual(sum(r['card']['medium'] in ('tv', 'book', 'event') for r in rows), 4)
        self.assertEqual(len({r['card']['key'] for r in rows}), len(rows))
        self.assertEqual(len(page.context['projection']['ratings']), 2)

    def test_next_round_no_duplicates_undo_and_resumed_session_keep_durable_ratings(self):
        self.assertEqual(self.post('rate', 'tmdb:movie:100', basis='interest', value=5).status_code, 302)
        first = self.page()
        shown = {r['card']['key'] for r in first.context['cards']}
        self.assertEqual(self.client.post(self.action_url, self.fields('funnel_next', page=first)).status_code, 302)
        next_page = self.page()
        self.assertTrue(shown.isdisjoint(r['card']['key'] for r in next_page.context['cards']))
        self.assertEqual(next_page.context['projection']['round'], 1)
        self.assertEqual(self.post('undo').status_code, 302)
        self.assertEqual({r['card']['key'] for r in self.page().context['cards']}, shown)
        resumed = self.page(self.new_client())
        self.assertEqual(resumed.context['projection']['ratings']['tmdb:movie:100'], {'interest': 5})
        self.assertEqual(resumed.context['projection']['round'], 0)

    def test_signed_stale_tab_replay_and_modified_receipt_are_distinct(self):
        page = self.page()
        first = self.fields('rate', 'tmdb:movie:100', page=page, basis='interest', value=4)
        stale = self.fields('rate', 'tmdb:movie:101', page=page, basis='interest', value=5)
        self.assertEqual(self.client.post(self.action_url, first).status_code, 302)
        self.assertEqual(self.client.post(self.action_url, first).status_code, 302)
        self.assertEqual(len(DiscoveryState.objects.get().history), 1)
        self.assertEqual(self.client.post(self.action_url, stale).status_code, 409)
        self.assertEqual(self.client.post(self.action_url, dict(first, value=1)).status_code, 409)
        self.assertEqual(self.projected()['ratings']['tmdb:movie:100'], {'interest': 4})

    def test_bad_signature_csrf_unknown_fields_and_cross_profile_reject_without_writes(self):
        fields = self.fields('rate', 'tmdb:movie:100', basis='interest', value=4)
        for change, expected in (({'preview': fields['preview']+'x'}, 409), ({'value': 6}, 400),
                                 ({'extra': 'unexpected'}, 400), ({'csrfmiddlewaretoken': ''}, 403)):
            self.assertEqual(self.client.post(self.action_url, dict(fields, **change)).status_code, expected)
        self.assertEqual(self.client.post('/profiles/2/taste/action/', fields).status_code, 404)
        self.grant.can_select = False
        self.grant.save()
        self.assertEqual(self.client.post(self.action_url, fields).status_code, 404)
        self.assertFalse(DiscoveryState.objects.exists())

    def test_valid_multipart_rating_form_does_not_reread_consumed_body(self):
        fields = self.fields('rate', 'tmdb:movie:100', basis='interest', value=4)
        # Django's default multipart encoding already consumed by CSRF middleware
        # must not cause RawPostDataException in the view's size guard.
        self.assertEqual(Client.post(self.client, self.action_url, fields).status_code, 302)
        self.assertEqual(self.projected()['ratings']['tmdb:movie:100'], {'interest': 4})

    def test_changed_selected_profile_rejects_prior_signed_form(self):
        fields = self.fields('rate', 'tmdb:movie:100', basis='interest', value=4)
        session = self.client.session
        session['selected_profile_id'] = 2
        session.save()
        self.assertEqual(self.client.post(self.action_url, fields).status_code, 404)
        self.assertFalse(DiscoveryState.objects.exists())

    def test_expired_unjudged_cache_and_expired_signature_cannot_record_ratings(self):
        fields = self.fields('rate', 'tmdb:movie:100', basis='interest', value=4)
        session = self.client.session
        session[intake.SESSION_KEY]['expires_at'] = 0
        session.save()
        self.assertEqual(self.client.post(self.action_url, fields).status_code, 409)
        self.write_catalogue(self.provider_cards)
        fields = self.fields('rate', 'tmdb:movie:100', basis='interest', value=4)
        with patch('django.core.signing.time.time', return_value=self.now.timestamp()+901):
            self.assertEqual(self.client.post(self.action_url, fields).status_code, 409)
        self.assertFalse(DiscoveryState.objects.exists())

    def test_current_source_identity_supersedes_history_for_edit_and_new_signature(self):
        key = 'tmdb:movie:100'
        self.assertEqual(self.post('rate', key, basis='interest', value=4).status_code, 302)
        stale = self.fields('rate', key, basis='interest', value=5)
        cards = [replace(c, title='Synthetic revised source title') if c.key == key else c for c in self.provider_cards]
        self.write_catalogue(cards)
        current = self.page()
        row = next(r for r in current.context['responses'] if r['card']['key'] == key)
        self.assertEqual(row['card']['title'], 'Synthetic revised source title')
        self.assertEqual(self.client.post(self.action_url, stale).status_code, 409)
        fields = self.fields('rate', key, page=current, basis='interest', value=5)
        self.assertEqual(self.client.post(self.action_url, fields).status_code, 302)
        self.assertEqual(self.projected()['cards'][key]['title'], 'Synthetic revised source title')

    def test_next_round_form_refuses_changed_source_pool(self):
        page = self.page()
        self.write_catalogue([])
        discovery_views.PUBLIC_CACHE.clear()
        self.assertEqual(self.client.post(self.action_url, self.fields('funnel_next', page=page)).status_code, 409)
        self.assertFalse(DiscoveryState.objects.exists())

    def test_fetch_is_explicit_idempotent_and_does_not_create_ratings_or_signals(self):
        page = self.page()
        original = page.context['snapshot']['input_digest']
        fields = self.fetch_fields(page)
        response, initial_count, public_count = self.mocked_fetch(fields)
        self.assertEqual((response.status_code, initial_count, public_count), (302, 1, 7))
        response, initial_count, public_count = self.mocked_fetch(fields)
        self.assertEqual((response.status_code, initial_count, public_count), (302, 0, 0))
        self.assertEqual(DiscoveryState.objects.get().history, [])
        self.assertEqual(self.page().context['snapshot']['input_digest'], original)
        self.assertFalse(SavedItem.objects.exists())

    def test_synthesis_digest_stable_after_fetch_and_next_but_changes_on_real_rating_edit(self):
        key = 'tmdb:movie:100'
        self.assertEqual(self.post('rate', key, basis='interest', value=4).status_code, 302)
        before = self.page().context['snapshot']
        self.assertEqual(self.mocked_fetch()[0].status_code, 302)
        self.assertEqual(self.page().context['snapshot'], before)
        self.assertEqual(self.post('funnel_next').status_code, 302)
        self.assertEqual(self.page().context['snapshot'], before)
        fields = {'expected_digest': before['input_digest'], 'confirmed': 'on',
                  'csrfmiddlewaretoken': self.client.cookies[settings.CSRF_COOKIE_NAME].value}
        self.assertEqual(self.client.post(self.synthesis_url, fields).status_code, 302)
        self.assertEqual(self.client.post(self.synthesis_url, fields).status_code, 302)
        queued = SynthesisRun.objects.get()
        self.assertEqual(queued.input_digest, before['input_digest'])
        self.assertEqual(queued.state, 'queued')
        self.assertEqual(self.post('rate', key, basis='interest', value=2).status_code, 302)
        self.assertNotEqual(self.page().context['snapshot']['input_digest'], before['input_digest'])
        self.assertEqual(self.client.post(self.synthesis_url, fields).status_code, 409)
        queued.refresh_from_db()
        self.assertEqual(queued.input_digest, before['input_digest'])
        self.assertEqual(SynthesisRun.objects.count(), 1)

    def test_favourite_changed_elsewhere_invalidates_signed_form(self):
        fields = self.fields('rate', 'tmdb:movie:101', basis='interest', value=4)
        SavedItem.objects.create(actor=self.actor, profile=self.profile, provider='tmdb', medium='movie',
            source_id=100, list_kind='favorites', title='Synthetic other-view favourite',
            url='https://www.themoviedb.org/movie/100', fetched_at=self.now)
        self.assertEqual(self.client.post(self.action_url, fields).status_code, 409)
        self.assertFalse(DiscoveryState.objects.exists())

    def test_response_pagination_bounds_signed_cards_and_allows_older_rating_edit(self):
        first_key = None
        for _ in range(26):
            page = self.page()
            key = page.context['cards'][0]['card']['key']
            first_key = first_key or key
            fields = self.fields('rate', key, page=page, basis='interest', value=4)
            self.assertEqual(self.client.post(self.action_url, fields).status_code, 302)
        recent = self.page()
        self.assertEqual(len(recent.context['responses']), 24)
        self.assertEqual(recent.context['next_responses'], 2)
        self.assertNotIn(first_key, [r['card']['key'] for r in recent.context['responses']])
        bound = signing.loads(recent.context['preview'], salt='taste-funnel-v1')
        rendered = recent.context['cards'] + recent.context['responses']
        self.assertEqual(set(bound['cards']), {r['card']['key'] for r in rendered})
        self.assertLessEqual(len(bound['cards']), 36)
        hidden = self.fields('rate', first_key, page=recent, basis='interest', value=2)
        self.assertEqual(self.client.post(self.action_url, hidden).status_code, 409)
        older = self.client.get(self.url, {'responses': 2})
        self.assertEqual(older.status_code, 200)
        self.assertEqual(len(older.context['responses']), 2)
        self.assertEqual(older.context['previous_responses'], 1)
        self.assertIn(first_key, [r['card']['key'] for r in older.context['responses']])
        edit = self.fields('rate', first_key, page=older, basis='interest', value=2)
        self.assertEqual(self.client.post(self.action_url, edit).status_code, 302)
        self.assertEqual(self.projected()['ratings'][first_key]['interest'], 2)
        for value in (0, 22, 'invalid', ['1', '2']):
            self.assertEqual(self.client.get(self.url, {'responses': value}).status_code, 400)

    def test_fetch_revocation_discards_results_and_never_writes_preferences(self):
        fields = self.fetch_fields()
        def revoke(*args, **kwargs):
            self.grant.can_select = False
            self.grant.save()
            return self.provider_cards, []
        with patch.object(catalogue, 'initial_deck', side_effect=revoke), \
             patch.object(sources, 'fetch_public') as public:
            self.assertEqual(self.client.post(self.fetch_url, fields).status_code, 404)
            public.assert_not_called()
        self.assertEqual(DiscoveryState.objects.get().history, [])
        self.assertFalse(SavedItem.objects.exists())
