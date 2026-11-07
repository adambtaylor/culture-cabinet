"""Grassroots sources enter the existing authenticated funnel; all I/O is synthetic."""
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from shelf import discovery_sources as sources, discovery_transport as transport, discovery_views
from shelf import provider_catalogue as catalogue, taste_selection
from shelf.models import DiscoveryState, Grant, Profile, SavedItem, SynthesisRun
from shelf.tests.test_taste_funnel import BrowserFormClient
from shelf.tests import test_tribe_events as tribe_fixture, test_soundsystems_events as sound_fixture


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class GrassrootsHTTPTests(TestCase):
    url = '/profiles/1/taste/'
    fetch_url = '/profiles/1/taste/fetch/'
    action_url = '/profiles/1/taste/action/'
    discovery_url = '/profiles/1/discovery/'
    discovery_action = '/profiles/1/discovery/action/'

    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-grassroots-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Synthetic owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client = BrowserFormClient(enforce_csrf_checks=True)
        self.client.force_login(self.actor)
        session = self.client.session
        session['selected_profile_id'] = 1
        session.save()
        self.now = tribe_fixture.NOW
        self.enterContext(patch('django.utils.timezone.now', return_value=self.now))
        discovery_views.PUBLIC_CACHE.clear()
        self.addCleanup(discovery_views.PUBLIC_CACHE.clear)
        for target in ('shelf.provider_catalogue._request', 'shelf.discovery_transport._send', 'socket.create_connection'):
            self.enterContext(patch(target, side_effect=AssertionError('Unmocked network forbidden')))
        self.artists = [sources.Candidate(
            f'musicbrainz:artist:00000000-0000-0000-0000-{n:012d}', f'Synthetic artist {n}',
            'artist', 'Synthetic seeds', 'manual-attributed', n, None,
            url=f'https://musicbrainz.org/artist/00000000-0000-0000-0000-{n:012d}') for n in (1, 2)]
        self.enterContext(patch.object(sources, 'household_candidates', return_value=self.artists))
        self.provider_cards = []
        for medium, number in (('movie', 101), ('tv', 201), ('book', 301)):
            self.provider_cards += catalogue.parse_candidates('hardcover' if medium == 'book' else 'tmdb', medium,
                [{'id': number, 'title': f'Synthetic {medium}', 'name': f'Synthetic {medium}', 'slug': f'synthetic-{number}'}],
                lane=f'editorial:{medium}', lane_label='Synthetic exploration')
        self.initial = self.enterContext(patch.object(catalogue, 'initial_deck', side_effect=lambda **kw: (list(self.provider_cards), [])))
        self.graphic = self.enterContext(patch.object(catalogue, 'browse_books', return_value=([], False)))
        self.tribe = self.enterContext(patch.object(transport, 'tribe', return_value=(200, tribe_fixture.listing())))
        self.sound = self.enterContext(patch.object(transport, 'soundsystems', return_value=(200, sound_fixture.listing([
            sound_fixture.card(title=f'SYNTHETIC dance {n}', post=f'https://www.instagram.com/p/Synthetic{n}/',
                location='Secret location' if n == 1 else 'SYNTHETIC Town Hall') for n in range(1, 5)]))))
        original = sources.fetch_public
        def fetch(operation, seed, mode, now):
            if operation in ('tribe', 'soundsystems'):
                return original(operation, seed, mode, now)
            return sources.Outcome('empty', (), 'Synthetic empty existing source')
        self.public = self.enterContext(patch.object(sources, 'fetch_public', side_effect=fetch))

    def page(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        return page

    def csrf(self):
        return self.client.cookies[settings.CSRF_COOKIE_NAME].value

    def fetch_fields(self):
        page = self.page()
        return {'revision': page.context['revision'], 'nonce': page.context['nonce'],
                'confirmed': 'on', 'csrfmiddlewaretoken': self.csrf()}

    def gather(self):
        self.assertEqual(self.client.post(self.fetch_url, self.fetch_fields()).status_code, 302)
        return self.page()

    def rating(self, page, key, value=4):
        return {'revision': page.context['revision'], 'nonce': page.context['nonce'],
                'preview': page.context['preview'], 'csrfmiddlewaretoken': self.csrf(),
                'operation': 'rate', 'key': key, 'basis': 'interest', 'value': value}

    def source_fields(self, operation):
        page = self.client.get(self.discovery_url)
        self.assertEqual(page.status_code, 200)
        return {'revision': page.context['revision'], 'nonce': page.context['nonce'],
                'mode': 'household', 'operation': operation, 'csrfmiddlewaretoken': self.csrf()}

    def assert_no_taste(self):
        for state in DiscoveryState.objects.all():
            self.assertEqual(state.history, [])
        self.assertFalse(SavedItem.objects.exists())
        self.assertFalse(SynthesisRun.objects.exists())

    def test_get_and_unconfirmed_post_do_not_read_sources_or_create_taste(self):
        self.page()
        self.client.get(self.discovery_url)
        fields = self.fetch_fields()
        fields.pop('confirmed')
        self.assertEqual(self.client.post(self.fetch_url, fields).status_code, 400)
        self.assertEqual(self.client.get(self.fetch_url).status_code, 405)
        self.initial.assert_not_called()
        self.public.assert_not_called()
        self.tribe.assert_not_called()
        self.sound.assert_not_called()
        self.assertFalse(DiscoveryState.objects.exists())
        self.assert_no_taste()

    def test_one_gather_uses_shared_sources_and_replay_does_no_io(self):
        fields = self.fetch_fields()
        original_digest = self.page().context['snapshot']['input_digest']
        self.assertEqual(self.client.post(self.fetch_url, fields).status_code, 302)
        self.assertEqual([c.args[0] for c in self.public.call_args_list],
                         ['rrr', 'beat', 'pbs', 'tribe', 'soundsystems', 'radio', 'radio'])
        self.tribe.assert_called_once_with()
        self.sound.assert_called_once_with()
        self.assertEqual(self.client.post(self.fetch_url, fields).status_code, 302)
        self.assertEqual(self.public.call_count, 7)
        self.initial.assert_called_once()
        self.assertEqual(self.page().context['snapshot']['input_digest'], original_digest)
        self.assert_no_taste()

    def test_four_shared_url_dances_and_tribe_are_individually_ratable_in_same_round(self):
        page = self.gather()
        cards = [row['card'] for row in page.context['cards']]
        self.assertEqual({card['medium'] for card in cards}, {'movie', 'tv', 'book', 'music', 'event'})
        dances = [card for card in cards if card['title'].startswith('SYNTHETIC dance')]
        self.assertEqual(len(dances), 4)
        self.assertEqual(len({card['key'] for card in dances}), 4)
        self.assertEqual({card['url'] for card in dances}, {sound_fixture.source.LISTING_URL})
        tribe = next(card for card in cards if card['title'] == 'SYNTHETIC Tribe event')
        self.assertEqual(tribe['url'], tribe_fixture.tribe.LISTING_URL + '/synthetic-event')
        for card in [*dances, tribe]:
            self.assertEqual(self.client.post(self.action_url, self.rating(self.page(), card['key'])).status_code, 302)
        projected = taste_selection.project(DiscoveryState.objects.get().history)
        self.assertEqual(set(projected['ratings']), {card['key'] for card in [*dances, tribe]})
        self.assertTrue(all(value == {'interest': 4} for value in projected['ratings'].values()))
        self.assertTrue(all(not event['experienced'] for event in DiscoveryState.objects.get().history))
        self.assertFalse(SavedItem.objects.exists())

    def test_publication_build_and_private_location_remain_honestly_labelled(self):
        page = self.gather()
        self.assertContains(page, 'Announcement published: ' + tribe_fixture.PUBLISHED)
        self.assertContains(page, 'This is not the event date.')
        self.assertContains(page, 'Feed updated: ' + tribe_fixture.BUILT)
        self.assertContains(page, 'Location undisclosed; check the organiser')
        self.assertNotContains(page, 'CANARY')
        self.assertNotContains(page, 'unrelated.invalid')
        self.assertNotContains(page, 'Secret location')
        cards = [row['card'] for row in page.context['cards']]
        tribe = next(card for card in cards if card['title'] == 'SYNTHETIC Tribe event')
        self.assertIsNone(tribe['source_date'])
        private = next(card for card in cards if card['title'] == 'SYNTHETIC dance 1')
        self.assertEqual((private['venue'], private['location_status']), ('', 'undisclosed'))

    def test_source_cache_expiry_removes_unjudged_and_rejects_old_form(self):
        page = self.gather()
        key = next(row['card']['key'] for row in page.context['cards'] if row['card']['medium'] == 'event')
        fields = self.rating(page, key)
        cache_key = f'1:1:{self.client.session.session_key}'
        entries = discovery_views.PUBLIC_CACHE.get(cache_key)
        for entry in entries.values():
            entry['at'] -= timedelta(minutes=16)
        discovery_views.PUBLIC_CACHE.set(cache_key, entries, timeout=900)
        self.assertFalse(any(row['card']['medium'] == 'event' for row in self.page().context['cards']))
        self.assertEqual(self.client.post(self.action_url, fields).status_code, 409)
        self.assert_no_taste()

    def test_revocation_during_new_source_fetch_discards_results(self):
        fields = self.fetch_fields()
        def revoke():
            self.grant.can_select = False
            self.grant.save()
            return 200, tribe_fixture.listing()
        self.tribe.side_effect = revoke
        self.assertEqual(self.client.post(self.fetch_url, fields).status_code, 404)
        self.sound.assert_not_called()
        self.assertIsNone(discovery_views.PUBLIC_CACHE.get(f'1:1:{self.client.session.session_key}'))
        self.assert_no_taste()

    def test_source_failure_has_safe_human_report_without_invented_taste(self):
        self.tribe.return_value = (429, b'CANARY credential detail')
        self.sound.return_value = (200, b'<html>CANARY challenge</html>')
        self.initial.side_effect = None
        self.initial.return_value = ([], [{'lane': 'books_graphic_novels', 'status': 'timeout', 'detail': 'CANARY secret'}])
        page = self.gather()
        self.assertContains(page, 'Graphic novels: provider timed out. No automatic retry.')
        self.assertContains(page, 'Tribe Soundsystem: rate-limited')
        self.assertContains(page, 'Sound Systems of Melbourne: invalid')
        self.assertNotContains(page, 'CANARY')
        self.assertFalse(any(row['card']['medium'] == 'event' for row in page.context['cards']))
        self.assertEqual(page.context['snapshot']['signals'], [])
        self.assert_no_taste()

    def test_halftix_is_visible_official_link_and_never_an_ingestion_operation(self):
        page = self.gather()
        self.assertContains(page, 'href="https://halftixmelbourne.com/all-events/"')
        self.assertContains(page, 'official link only; publisher permission required')
        self.assertContains(page, 'not every offer is half price')
        self.assertNotIn('halftix', [call.args[0] for call in self.public.call_args_list])
        fields = self.source_fields('halftix')
        count = self.public.call_count
        self.assertEqual(self.client.post(self.discovery_action, fields).status_code, 400)
        self.assertEqual(self.public.call_count, count)
        self.assert_no_taste()

    def test_existing_source_actions_share_funnel_cache_and_reserve_replays(self):
        for operation, transport_mock, count in (('tribe', self.tribe, 1), ('soundsystems', self.sound, 5)):
            fields = self.source_fields(operation)
            self.assertEqual(self.client.post(self.discovery_action, fields).status_code, 302)
            self.assertEqual(self.client.post(self.discovery_action, fields).status_code, 302)
            transport_mock.assert_called_once_with()
            self.assertEqual(sum(row['card']['medium'] == 'event' for row in self.page().context['cards']), count)
        self.initial.assert_not_called()
        self.assert_no_taste()

    def test_existing_source_action_rechecks_grant_and_rejects_sample_mode(self):
        fields = self.source_fields('soundsystems')
        self.assertEqual(self.client.post(self.discovery_action, dict(fields, mode='sample')).status_code, 400)
        self.sound.assert_not_called()
        def revoke():
            self.grant.can_select = False
            self.grant.save()
            return 200, sound_fixture.listing()
        self.sound.side_effect = revoke
        self.assertEqual(self.client.post(self.discovery_action, fields).status_code, 404)
        self.assertIsNone(discovery_views.PUBLIC_CACHE.get(f'1:1:{self.client.session.session_key}'))
        self.assert_no_taste()
