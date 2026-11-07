"""Card-local discovery feedback with fictional registered artists, no source I/O."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings

from shelf import discovery_sources as sources, discovery_views as views
from shelf.discovery_reducer import replay
from shelf.models import DiscoveryState, Grant, Profile


def cards():
    output = [sources.Candidate(f'official:artist:fictional-{n}', f'Fictional artist {n}', 'artist',
        'Fictional verified registry', 'manual-attributed', n, '2026-11-01T00:00:00+00:00',
        url=f'https://example.org/fictional-artist-{n}') for n in range(18)]
    output += [sources.Candidate(f'official:{kind}:fictional-{n}', f'Fictional {kind} {n}', kind,
        'Fictional verified registry', 'manual-attributed', n, '2026-11-01T00:00:00+00:00',
        url=f'https://example.org/fictional-{kind}-{n}') for kind in ('recording', 'event') for n in range(2)]
    return output


class DiscoveryFeedbackFormTests(SimpleTestCase):
    def test_legacy_receipt_fields_unchanged_when_note_and_queue_absent(self):
        data = dict(mode='household', revision=0, nonce='old-nonce', operation='respond',
                    key='official:artist:fictional-0', choice='interested')
        form = views.ActionForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data, {**data, 'radio_mode': ''})
        note = '  Literal note\nwith tab\t  '
        form = views.ActionForm({**data, 'note': note, 'queue': 'artist'})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['note'], note)
        for value in ('x' * 501, 'text\x00', '\u200b'):
            self.assertFalse(views.ActionForm({**data, 'note': value}).is_valid())


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class DiscoveryCardFeedbackTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='fictional-artist-feedback')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Fictional artists')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client.force_login(self.actor)
        session = self.client.session
        session['selected_profile_id'] = 1
        session.save()
        self.url = '/profiles/1/discovery/'
        self.action = self.url + 'action/'
        self.addCleanup(patch.stopall)
        patch.object(sources, 'household_candidates', side_effect=cards).start()
        self.network = patch.object(sources, 'fetch_public', side_effect=AssertionError('no implicit provider I/O')).start()
        patch('shelf.pick_enhancement.context', return_value={}).start()

    def page(self, queue='artist'):
        response = self.client.get(self.url, {'mode': 'household', 'queue': queue})
        self.assertEqual(response.status_code, 200)
        return response

    def data(self, response=None):
        response = response or self.page()
        return dict(mode='household', queue=response.context['queue'], revision=response.context['revision'],
                    nonce=response.context['nonce'], operation='respond',
                    key=response.context['music'][0]['card'].key, choice='intrigued')

    def deck(self, queue='artist'):
        session = self.client.session
        return session[views.DECK_KEY][f'1:1:household:{queue}']

    def submit(self, data):
        return self.client.post(self.action, data, HTTP_ACCEPT='application/json')

    def test_artist_queue_has_no_recordings_or_events_and_five_reactions(self):
        response = self.page()
        self.assertEqual(len(response.context['music']), 12)
        self.assertTrue(all(row['card'].kind == 'artist' for row in response.context['music']))
        self.assertEqual(response.context['events'], [])
        self.assertEqual(response.context['more_source_events'], [])
        self.assertEqual([label for _, label in response.context['reaction_options']],
            ['Love it', 'Intrigued', 'Mildly interested', 'Sounds boring', 'Hate it'])
        self.network.assert_not_called()
        self.assertFalse(DiscoveryState.objects.exists())
        all_response = self.page('all')
        self.assertTrue(any(row['card'].kind == 'recording' for row in all_response.context['music']))
        self.assertTrue(all_response.context['events'])

    def test_only_submitted_slot_changes_and_remaining_next_click_stays_valid(self):
        response = self.page()
        before = list(self.deck()['keys'])
        data = self.data(response)
        data['key'] = before[5]
        result = self.submit(data)
        self.assertEqual(result.status_code, 200)
        after = self.deck()['keys']
        self.assertEqual(after[:5], before[:5])
        self.assertEqual(after[6:], before[6:])
        self.assertNotIn(after[5], before)
        self.assertIn(after[5], result.json()['replacement_html'])
        self.assertEqual(result.json()['replacement_html'].count('data-feedback-card'), 1)
        self.assertTrue(result.json()['explanation_updates'])
        refreshed = self.page()
        self.assertEqual(self.deck()['keys'], after)
        next_data = self.data(refreshed)
        next_data['key'] = before[8]
        self.assertEqual(self.submit(next_data).status_code, 200)
        self.assertEqual(len(DiscoveryState.objects.get().history), 2)
        self.network.assert_not_called()

    def test_replay_and_lost_response_restore_same_slot_without_duplicate(self):
        response = self.page()
        original = deepcopy(self.client.session[views.DECK_KEY])
        data = {**self.data(response), 'note': '  exact owner text\n  '}
        first = self.submit(data)
        self.assertEqual(first.status_code, 200)
        after = deepcopy(self.deck())
        repeated = self.submit(data)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()['nonce'], first.json()['nonce'])
        self.assertEqual(self.deck(), after)
        session = self.client.session
        session[views.DECK_KEY] = original
        session.save()
        recovered = self.submit(data)
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(self.deck(), after)
        self.assertIn(after['keys'][0], recovered.json()['replacement_html'])
        state = DiscoveryState.objects.get()
        self.assertEqual(len(state.history), 1)
        self.assertEqual(state.history[0]['note'], data['note'])
        self.assertEqual(self.submit({**data, 'note': 'changed retry'}).status_code, 409)

    def test_registered_artist_pool_change_refreshes_without_human_feedback(self):
        self.page()
        before = deepcopy(self.deck())
        added = replace(cards()[0], key='official:artist:new-import', title='New registered fictional artist', rank=-1)
        with patch.object(sources, 'household_candidates', return_value=cards() + [added]):
            response = self.page()
            self.assertIn(added.key, [row['card'].key for row in response.context['music']])
            self.assertNotEqual(self.deck()['pool_digest'], before['pool_digest'])
            self.assertEqual(self.deck()['revision'], before['revision'])
        self.assertFalse(DiscoveryState.objects.exists())
        self.network.assert_not_called()

    def test_new_source_pool_event_is_inspectable_after_an_earlier_get(self):
        self.page('all')
        before = deepcopy(self.deck('all'))
        added = sources.Candidate('official:event:new-source-read', 'New fictional source event', 'event',
            'Fictional bounded source read', 'live-automated', 100, '2026-11-01T00:00:00+00:00',
            url='https://example.org/new-source-event')
        entries = {('fictional', '', ''): {'outcome': SimpleNamespace(candidates=(added,)),
            'source': 'Fictional source'}}
        with patch.object(views, 'public_entries', return_value=entries):
            response = self.page('all')
            visible = response.context['events'] + response.context['more_source_events']
            self.assertIn(added.key, [row['card'].key for row in visible])
            self.assertIn(added.key, self.deck('all')['source_keys'])
            self.assertNotEqual(self.deck('all')['pool_digest'], before['pool_digest'])
        self.assertFalse(DiscoveryState.objects.exists())
        self.network.assert_not_called()

    def test_partial_response_conflicts_when_registered_pool_changed_since_get(self):
        data = self.data()
        before = deepcopy(self.deck())
        added = replace(cards()[0], key='official:artist:new-import', rank=-1)
        with patch.object(sources, 'household_candidates', return_value=cards() + [added]):
            self.assertEqual(self.submit(data).status_code, 409)
        self.assertEqual(self.deck(), before)
        self.assertFalse(DiscoveryState.objects.exists())

    def test_note_correction_retains_history_choice_and_visible_deck(self):
        data = {**self.data(), 'choice': 'interested', 'note': 'old note'}
        self.assertEqual(self.submit(data).status_code, 200)
        response = self.page()
        before = deepcopy(self.deck())
        correction = {**self.data(response), 'key': data['key'], 'choice': 'interested', 'note': '  revised note  '}
        result = self.submit(correction)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['operation'], 'correct')
        self.assertEqual(result.json()['replacement_html'], '')
        self.assertEqual(self.deck()['keys'], before['keys'])
        stored = DiscoveryState.objects.get()
        self.assertEqual(len(stored.history), 2)
        self.assertEqual(stored.history[0]['note'], 'old note')
        self.assertEqual(replay(stored.history)['notes'][data['key']], '  revised note  ')
        self.assertEqual(replay(stored.history)['choices'][data['key']], 'interested')

    def test_stale_and_category_forgery_never_record_or_replace(self):
        data = self.data()
        before = deepcopy(self.deck())
        for key in ('official:recording:fictional-0', 'official:event:fictional-0', 'official:artist:invented'):
            self.assertEqual(self.submit({**data, 'key': key}).status_code, 400)
        self.assertEqual(self.deck(), before)
        self.assertFalse(DiscoveryState.objects.exists())
        self.assertEqual(self.submit(data).status_code, 200)
        changed = {**data, 'key': before['keys'][1]}
        self.assertEqual(self.submit(changed).status_code, 409)
        self.assertEqual(len(DiscoveryState.objects.get().history), 1)
        for query in ('queue=recording', 'queue=artist&queue=all', 'queue='):
            self.assertEqual(self.client.get(self.url + '?' + query).status_code, 404)

    def test_normal_post_keeps_category_redirect_and_source_reads_reject_note(self):
        data = self.data()
        response = self.client.post(self.action, data)
        self.assertEqual(response.status_code, 302)
        self.assertIn('mode=household&queue=artist#catalogue-card-', response['Location'])
        current = self.data()
        source = {**current, 'operation': 'events', 'key': '', 'choice': ''}
        self.assertEqual(self.client.post(self.action, source).status_code, 400)
        self.assertEqual(self.client.post(self.action, {**source, 'queue': 'all', 'note': 'forged source note'}).status_code, 400)
        self.network.assert_not_called()

    def test_csrf_current_selection_and_grant_remain_required(self):
        data = self.data()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        session = client.session
        session['selected_profile_id'] = 1
        session.save()
        self.assertEqual(client.post(self.action, data, HTTP_ACCEPT='application/json').status_code, 403)
        self.grant.can_select = False
        self.grant.save()
        self.assertEqual(self.submit(data).status_code, 404)
        self.assertFalse(DiscoveryState.objects.exists())

    def test_empty_artist_queue_is_honest_and_sample_feedback_stays_separate(self):
        with patch.object(sources, 'household_candidates', return_value=[card for card in cards() if card.kind != 'artist']):
            empty = self.page()
            self.assertEqual(empty.context['music'], [])
            self.assertEqual(empty.context['events'], [])
        response = self.client.get(self.url, {'mode': 'sample', 'queue': 'artist'})
        self.assertEqual(response.status_code, 200)
        row = response.context['music'][0]
        data = dict(mode='sample', queue='artist', revision=response.context['revision'],
                    nonce=response.context['nonce'], key=row['card'].key, operation='respond', choice='love_it')
        self.assertEqual(self.submit(data).status_code, 200)
        self.assertEqual(DiscoveryState.objects.get().mode, 'sample')
        self.assertFalse(DiscoveryState.objects.filter(mode='household').exists())

    def test_artist_enhancement_order_accepts_only_registered_unjudged_artists(self):
        self.page()
        from django.test import RequestFactory
        request = RequestFactory().post('/fictional-consumer/')
        request.user = self.actor
        request.session = self.client.session
        ids = [card.key for card in cards() if card.kind == 'artist'][-6:]
        self.assertTrue(views.apply_enhancement_order(request, 1, ids))
        request.session.save()
        self.assertEqual(self.deck()['keys'], ids)
        with self.assertRaises(views.storage.Conflict):
            views.apply_enhancement_order(request, 1, ['official:recording:fictional-0'])
        self.assertEqual(self.deck()['keys'], ids)
