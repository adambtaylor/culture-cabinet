"""Real HTTP, durable receipts and server-owned card-local queue invariants."""
from copy import deepcopy
from html.parser import HTMLParser
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, SimpleTestCase, override_settings

from shelf import durable_intake as durable, intake, provider_views as views
from shelf.authority import SELECTION_KEY
from shelf.models import Grant, IntakeRun, Profile
from shelf.tests.test_durable_intake import fiction_cards


class RenderedForms(HTMLParser):
    """Read successful hidden fields and submit buttons from actual rendered forms."""
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'form':
            self.current = {'attrs': attrs, 'fields': {}, 'names': set(), 'buttons': []}
            self.forms.append(self.current)
        elif self.current is not None:
            if tag in ('input', 'select', 'textarea') and attrs.get('name'):
                self.current['names'].add(attrs['name'])
                if tag == 'input' and attrs.get('type') == 'hidden':
                    self.current['fields'][attrs['name']] = attrs.get('value', '')
            elif tag == 'button':
                self.current['buttons'].append(attrs)

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


class FeedbackPayloadTests(SimpleTestCase):
    def test_absent_optional_fields_keep_legacy_receipt_bytes(self):
        data = dict(action='choose', revision=4, nonce='receipt', candidate_id='tmdb:movie:100', choice='interested')
        form = views.ActionForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        payload = views._payload(form.cleaned_data)
        self.assertNotIn('note', payload)
        self.assertNotIn('queue', payload)
        self.assertEqual(payload, dict(action='choose', revision='4', run_id='', medium='', query='',
                                      candidate_id='tmdb:movie:100', choice='interested'))

    def test_note_is_exact_and_both_bounds_are_enforced(self):
        data = dict(action='choose', revision=0, nonce='receipt', candidate_id='tmdb:movie:100', choice='love_it')
        note = '  Literal owner text\nwith a tab\t  '
        form = views.ActionForm({**data, 'note': note})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(views._payload(form.cleaned_data)['note'], note)
        for bad in ('a' * 501, 'a\x00b', '\u200b'):
            form = views.ActionForm({**data, 'note': bad})
            self.assertFalse(form.is_valid())


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class ProviderCardFeedbackTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='fictional-card-feedback')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Fictional card feedback')
        Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.run = durable.new_run(self.actor, 1)
        state = durable.hydrate(self.run)
        intake.merge(state, fiction_cards())
        state['started'] = True
        intake.select_round_v2(state)
        self.client.force_login(self.actor)
        self.store(state)
        self.url = '/profiles/1/intake/action/'

    def store(self, state):
        session = self.client.session
        session[SELECTION_KEY] = 1
        session[intake.SESSION_KEY] = state
        session.save()

    def state(self):
        return self.client.session[intake.SESSION_KEY]

    def payload(self, state=None, **extra):
        state = state or self.state()
        return dict(action='choose', revision=state['revision'], nonce=state['nonce'],
                    run_id=state['run_id'], queue=state.get('queue', 'all'),
                    candidate_id=state['current'][0], choice='love_it', **extra)

    def submit(self, data):
        return self.client.post(self.url, data, HTTP_ACCEPT='application/json')

    def test_one_card_replaced_in_place_and_remaining_next_click_accepted(self):
        before = self.state()
        data = self.payload()
        # The submitted position need not be the first card.
        data['candidate_id'] = before['current'][4]
        with patch('shelf.provider_catalogue._request', side_effect=AssertionError('GET/feedback cannot fetch')):
            response = self.submit(data)
            self.assertEqual(response.status_code, 200)
            body = response.json()
            after = self.state()
            self.assertEqual(after['current'][:4], before['current'][:4])
            self.assertEqual(after['current'][5:], before['current'][5:])
            replacement = after['current'][4]
            self.assertNotIn(replacement, before['current'])
            self.assertIn(replacement, body['replacement_html'])
            self.assertEqual(body['replacement_html'].count('data-feedback-card'), 1)
            self.client.get('/profiles/1/?queue=all')
            self.assertEqual(self.state()['current'], after['current'])
            followup = self.payload()
            followup['candidate_id'] = before['current'][7]
            self.assertEqual(self.submit(followup).status_code, 200)
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.history), 2)

    def test_exact_replay_and_lost_session_response_recover_without_second_event(self):
        before = deepcopy(self.state())
        data = self.payload(note='  exact note\n  ')
        first = self.submit(data)
        self.assertEqual(first.status_code, 200)
        body = first.json()
        replay = self.submit(data)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual({k: v for k, v in replay.json().items() if k != 'replacement_html'},
                         {k: v for k, v in body.items() if k != 'replacement_html'})
        # Simulate DB commit succeeding while the session response was lost.
        self.store(before)
        recovered = self.submit(data)
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual({k: v for k, v in recovered.json().items() if k != 'replacement_html'},
                         {k: v for k, v in body.items() if k != 'replacement_html'})
        replacement = self.state()['current'][0]
        self.assertIn(replacement, recovered.json()['replacement_html'])
        self.assertIn(replacement, body['replacement_html'])
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.history), 1)
        self.assertEqual(self.run.history[0]['note'], data['note'])
        changed = {**data, 'note': 'different uncertain retry'}
        self.assertEqual(self.submit(changed).status_code, 409)
        self.assertEqual(len(IntakeRun.objects.get(pk=self.run.pk).history), 1)

    def test_note_correction_keeps_visible_deck_and_legacy_choice(self):
        first = self.payload()
        first.update(choice='interested', note='original note')
        self.assertEqual(self.submit(first).status_code, 200)
        before = deepcopy(self.state())
        correction = self.payload()
        correction.update(action='correct', candidate_id=first['candidate_id'], choice='interested', note='  revised note  ')
        response = self.submit(correction)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['operation'], 'correct')
        self.assertEqual(response.json()['replacement_html'], '')
        self.assertEqual(self.state()['current'], before['current'])
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.history), 2)
        self.assertEqual(self.run.history[0]['note'], 'original note')
        self.assertEqual(self.run.history[-1]['note'], '  revised note  ')
        self.assertEqual(self.run.history[-1]['choice'], 'interested')

    def test_filter_uses_full_cached_pool_and_replacement_stays_in_queue(self):
        with patch('shelf.provider_catalogue._request', side_effect=AssertionError('no implicit source fetch')):
            response = self.client.get('/profiles/1/?queue=book')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['cards'])
            self.assertTrue(all(card['medium'] == 'book' for card in response.context['cards']))
            before = self.state()
            self.assertGreater(len(before['current']), 3)
            response = self.submit(self.payload())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['queue'], 'book')
            self.assertTrue(all(self.state()['candidates'][key]['medium'] == 'book' for key in self.state()['current']))
            self.assertEqual(self.state()['current'][1:], before['current'][1:])

    def test_category_back_restores_pinned_order_at_the_same_revision(self):
        self.assertEqual(self.client.get('/profiles/1/?queue=book').status_code, 200)
        data = self.payload()
        data['candidate_id'] = self.state()['current'][5]
        data['choice'] = 'hate_it'
        self.assertEqual(self.submit(data).status_code, 200)
        book = deepcopy(self.state())
        self.assertEqual(self.client.get('/profiles/1/?queue=movie').status_code, 200)
        self.assertEqual(self.client.get('/profiles/1/?queue=book').status_code, 200)
        self.assertEqual(self.state()['current'], book['current'])
        self.assertEqual(self.state()['reasons'], book['reasons'])
        self.assertEqual(self.state()['revision'], book['revision'])
        self.run.refresh_from_db()
        self.assertEqual(self.run.revision, book['revision'])

    def test_same_revision_book_form_survives_a_movie_category_get(self):
        self.client.get('/profiles/1/?queue=book')
        book = deepcopy(self.state())
        data = self.payload()
        data['candidate_id'] = book['current'][3]
        self.client.get('/profiles/1/?queue=movie')
        self.assertEqual(self.state()['queue'], 'movie')
        response = self.submit(data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['queue'], 'book')
        self.assertEqual(self.state()['current'][:3], book['current'][:3])
        self.assertEqual(self.state()['current'][4:], book['current'][4:])
        self.assertTrue(all(self.state()['candidates'][key]['medium'] == 'book'
                            for key in self.state()['current']))

    def test_lost_response_after_switch_recovers_the_receipted_queue_only(self):
        self.client.get('/profiles/1/?queue=book')
        data = self.payload()
        self.client.get('/profiles/1/?queue=movie')
        before_response = deepcopy(self.state())
        first = self.submit(data)
        self.assertEqual(first.status_code, 200)
        after = deepcopy(self.state())
        self.store(before_response)
        recovered = self.submit(data)
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()['nonce'], first.json()['nonce'])
        self.assertEqual(recovered.json()['queue'], 'book')
        self.assertEqual(self.state()['current'], after['current'])
        self.assertIn(after['current'][0], recovered.json()['replacement_html'])
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.history), 1)

    def test_unvisited_queue_cannot_be_admitted_by_a_posted_category(self):
        before = deepcopy(self.state())
        data = self.payload()
        book = next(key for key in before['current'] if before['candidates'][key]['medium'] == 'book')
        response = self.submit({**data, 'queue': 'book', 'candidate_id': book})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.state()['current'], before['current'])
        self.assertEqual(self.state().get('queue', 'all'), 'all')
        self.run.refresh_from_db()
        self.assertEqual(self.run.history, [])

    def test_new_feedback_revision_rejects_an_older_other_queue_form(self):
        self.client.get('/profiles/1/?queue=book')
        stale_book = self.payload()
        self.client.get('/profiles/1/?queue=movie')
        self.assertEqual(self.submit(self.payload()).status_code, 200)
        before = deepcopy(self.state())
        self.assertEqual(self.submit(stale_book).status_code, 409)
        self.assertEqual(self.state()['current'], before['current'])
        self.assertEqual(self.state()['queue'], before['queue'])
        self.assertEqual(self.state()['choices'], before['choices'])
        self.assertEqual(self.state()['queue_decks']['book']['revision'], stale_book['revision'])
        self.client.get('/profiles/1/?queue=book')
        self.assertEqual(self.state()['queue_decks']['book']['revision'], before['revision'])
        self.assertEqual(self.submit(stale_book).status_code, 409)
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.history), 1)

    def test_bad_queue_duplicate_fields_and_invented_card_do_not_write(self):
        before = deepcopy(self.state())
        for query in ('queue=bogus', 'queue=book&queue=movie', 'queue='):
            self.assertEqual(self.client.get('/profiles/1/?' + query).status_code, 400)
        for extra, expected in (({'queue': 'bogus'}, 400), ({'queue': 'book'}, 409),
                                ({'candidate_id': 'tmdb:movie:999999'}, 400)):
            self.assertEqual(self.submit({**self.payload(), **extra}).status_code, expected)
        self.assertEqual(self.state()['current'], before['current'])
        self.run.refresh_from_db()
        self.assertEqual(self.run.history, [])

    def test_old_replay_after_another_response_is_non_destructive_conflict(self):
        first = self.payload()
        self.assertEqual(self.submit(first).status_code, 200)
        self.assertEqual(self.submit(self.payload()).status_code, 200)
        before = deepcopy(self.state())
        self.assertEqual(self.submit(first).status_code, 409)
        self.assertEqual(self.state()['current'], before['current'])
        self.assertEqual(self.state()['choices'], before['choices'])

    def test_empty_queue_and_normal_post_keep_honest_filter_and_anchor(self):
        state = self.state()
        state['queued'] = [key for key in state['queued'] if state['candidates'][key]['medium'] != 'book']
        self.store(state)
        empty = self.client.get('/profiles/1/?queue=book')
        self.assertEqual(empty.context['cards'], [])
        self.client.get('/profiles/1/?queue=movie')
        data = self.payload()
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302)
        self.assertIn('?queue=movie#catalogue-card-', response['Location'])

    def test_csrf_and_current_authority_still_protect_partial_post(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        session = client.session
        session[SELECTION_KEY] = 1
        session[intake.SESSION_KEY] = self.state()
        session.save()
        self.assertEqual(client.post(self.url, self.payload(), HTTP_ACCEPT='application/json').status_code, 403)
        Grant.objects.filter(user=self.actor).update(can_select=False)
        self.assertEqual(self.submit(self.payload()).status_code, 404)
        self.run.refresh_from_db()
        self.assertEqual(self.run.history, [])

    def rendered_undo(self, response, candidate=None):
        forms = RenderedForms(response.content.decode()).forms
        return next(form for form in forms
                    if any(button.get('value') == 'undo' for button in form['buttons'])
                    and (form['fields'].get('candidate_id') == candidate if candidate else
                         'data-feedback-latest-undo' in form['attrs']))

    def test_rendered_item_clear_is_note_free_with_empty_and_nonempty_notes(self):
        for note in ('', '  exact stored note\n'):
            with self.subTest(note=note):
                chosen = self.payload(note=note)
                self.assertEqual(self.submit(chosen).status_code, 200)
                page = self.client.get('/profiles/1/?queue=all')
                clear = self.rendered_undo(page, chosen['candidate_id'])
                self.assertNotIn('note', clear['names'])
                self.assertNotIn('choice', clear['names'])
                response = self.client.post(clear['attrs']['action'], {**clear['fields'], 'action': 'undo'})
                self.assertEqual(response.status_code, 302)
                self.run.refresh_from_db()
                self.assertNotIn(chosen['candidate_id'], durable.effective(self.run)[0])
                self.assertEqual(self.run.history[-2]['note'], note)
                self.assertNotIn('note', self.run.history[-1])

    def test_latest_undo_after_ajax_resolves_current_response_and_correction(self):
        initial = self.client.get('/profiles/1/?queue=all')
        latest = self.rendered_undo(initial)
        self.assertNotIn('candidate_id', latest['fields'])
        self.assertIn('disabled', latest['buttons'][0])
        first = self.payload()
        receipt_a = self.submit(first).json()
        self.assertEqual(sum(receipt_a['counts'].values()), 1)
        second = self.payload()
        receipt_b = self.submit(second).json()
        # Simulate refreshAuthority on the original DOM form, without a page reload.
        data = {**latest['fields'], **{name: receipt_b[name] for name in ('revision', 'nonce', 'run_id', 'queue')}, 'action': 'undo'}
        self.assertEqual(self.client.post(latest['attrs']['action'], data).status_code, 302)
        self.run.refresh_from_db()
        self.assertEqual(set(durable.effective(self.run)[0]), {first['candidate_id']})
        # A correction becomes latest even if that identity was originally judged earlier.
        self.assertEqual(self.submit(self.payload()).status_code, 200)
        state = self.state()
        correction = dict(action='correct', candidate_id=first['candidate_id'], choice='intrigued',
                          revision=state['revision'], nonce=state['nonce'], run_id=state['run_id'], queue='all')
        receipt = self.submit(correction).json()
        data.update({name: receipt[name] for name in ('revision', 'nonce', 'run_id', 'queue')})
        self.assertEqual(self.client.post(latest['attrs']['action'], data).status_code, 302)
        self.run.refresh_from_db()
        self.assertNotIn(first['candidate_id'], durable.effective(self.run)[0])
        self.assertEqual(self.run.history[-1]['card']['key'], first['candidate_id'])

    def test_clear_still_rejects_even_empty_submitted_note(self):
        first = self.payload(note='stored')
        self.assertEqual(self.submit(first).status_code, 200)
        state = self.state()
        for note in ('', 'unexpected'):
            response = self.client.post(self.url, dict(action='undo', candidate_id=first['candidate_id'],
                revision=state['revision'], nonce=state['nonce'], run_id=state['run_id'], queue='all', note=note))
            self.assertEqual(response.status_code, 400)
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.history), 1)
