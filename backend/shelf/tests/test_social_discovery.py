"""Synthetic browser adapter and actual Django journeys; no social account access."""
from copy import deepcopy
from datetime import timedelta
import json
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from shelf import social_discovery as storage, social_sources as sources
from shelf.authority import SELECTION_KEY
from shelf.discovery_sources import Candidate, SourceError, Outcome
from shelf.discovery_views import PUBLIC_CACHE, cache_key, candidates
from shelf.models import DiscoveryState, Grant, Profile, SocialDiscoveryState

DEMO_OUTPUT = None
SOURCE = dict(platform='facebook', url='https://www.facebook.com/fixture-cultural-studio',
    label='SYNTHETIC cultural studio', kind='artist', scope='followed')


class SocialContractTests(SimpleTestCase):
    def test_clean_explicit_source_urls_only(self):
        for platform, url in (
            ('facebook', 'https://www.facebook.com/..'),
            ('instagram', 'https://www.instagram.com/.'),
            ('facebook', 'https://www.facebook.com/messages'),
            ('facebook', 'https://www.facebook.com/friends'),
            ('facebook', 'https://www.facebook.com/groups/private'),
            ('facebook', 'https://www.facebook.com/profile.php?id=1'),
            ('instagram', 'https://www.instagram.com/direct/inbox'),
            ('instagram', 'https://www.instagram.com/explore'),
            ('facebook', 'https://user@www.facebook.com/venue'),
            ('facebook', 'https://www.facebook.com.evil.test/venue'),
            ('facebook', 'https://www.facebook.com:443/venue'),
            ('facebook', 'https://www.facebook.com/venue?token=secret'),
            ('facebook', 'https://www.facebook.com/%6cogin'),
            ('facebook', 'http://www.facebook.com/venue'),
            ('facebook', 'https://www.facebook.com/venue\n')):
            with self.subTest(url=url), self.assertRaises(SourceError):
                sources.social_url(platform, url, source=True)
        self.assertEqual(sources.social_url('instagram', 'https://instagram.com/example.venue/', source=True),
                         'https://www.instagram.com/example.venue')
        with self.assertRaises(SourceError):
            sources.source_record(**dict(SOURCE, scope='public_event'))
        self.assertEqual(sources.source_record('facebook', 'https://www.facebook.com/events/123',
                         'Related public event', 'event', 'public_event')['scope'], 'public_event')

    def test_projection_rejects_private_payloads_scope_changes_and_unbounded_reads(self):
        now = timezone.now()
        source = sources.source_record(**SOURCE)
        request = sources.BrowserRead(source['url'], 'facebook', 'artist', now.isoformat())
        good = sources.SampleBrowser('ready').read(request)
        bad_items = [dict(good.items[0], cookies='secret'), dict(good.items[0], title='x' * 161),
            dict(good.items[0], post_url='https://www.facebook.com/other-source/posts/123'),
            dict(good.items[0], captured_at=(now + timedelta(seconds=1)).isoformat()),
            dict(good.items[0], published_at=(now + timedelta(days=1)).isoformat()),
            dict(good.items[0], event_url='https://evil.test/event'),
            dict(good.items[0], event_url='https://moshtix.com.au/v2/event/' + 'a' * 2048 + '/123'),
            dict(good.items[0], event_url='https://www.rrr.org.au/events')]
        for item in bad_items:
            with self.subTest(item=item), self.assertRaises(SourceError):
                sources.normalize_outcome(sources.BrowserOutcome('ready', source['url'], (item,)),
                    source, now.isoformat(), now, provenance='fixture')
        outcomes = [sources.BrowserOutcome('ready', 'https://www.facebook.com/other', good.items),
            sources.BrowserOutcome('challenge', source['url'], good.items),
            sources.BrowserOutcome('ready', source['url'], good.items * 6),
            sources.BrowserOutcome('ready', source['url'], ()),
            sources.BrowserOutcome('ready', source['url'], good.items * 2)]
        for outcome in outcomes:
            with self.assertRaises(SourceError):
                sources.normalize_outcome(outcome, source, now.isoformat(), now, provenance='fixture')
        with self.assertRaises(SourceError):
            sources.normalize_outcome(good, source, now.isoformat(), now + timedelta(seconds=61), provenance='fixture')
        self.assertEqual((request.max_pages, request.max_items, request.max_ui_actions, request.timeout_seconds), (1, 5, 8, 60))

    def test_household_adapter_has_no_connected_path_or_browser_session_parameters(self):
        request = sources.BrowserRead(SOURCE['url'], 'facebook', 'artist', timezone.now().isoformat())
        self.assertIsInstance(sources.adapter('household', 'ready'), sources.DisconnectedBrowser)
        self.assertEqual(sources.adapter('household').read(request).status, 'not_connected')


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class SocialDiscoveryTests(TestCase):
    def setUp(self):
        PUBLIC_CACHE.clear()
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-social-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='SYNTHETIC owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.login()
        self.base = '/profiles/1/social-discovery/'
        self.mode = 'household'

    def login(self):
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = 1
        session.save()

    def state(self):
        return SocialDiscoveryState.objects.filter(actor=self.actor, profile=self.profile, mode=self.mode).first()

    def payload(self, action, **changes):
        state = self.state()
        return dict(mode=self.mode, action=action, revision=state.revision if state else 0,
            nonce=state.nonce if state else storage.initial_nonce(1, 1, self.mode), confirmed='on', **changes)

    def act(self, action, expected=302, **changes):
        response = self.client.post(self.base + 'action/', self.payload(action, **changes))
        self.assertEqual(response.status_code, expected, response.content.decode()[:500])
        return response

    def add(self, **changes):
        self.act('add_source', **dict(SOURCE, **changes))
        return self.state().data['sources'][-1]

    def queue(self, source=None, scenario='ready'):
        source = source or self.state().data['sources'][0]
        fields = {'identity': source['id']}
        if self.mode == 'sample':
            fields['scenario'] = scenario
        self.act('queue', **fields)
        return self.state().data['tasks'][-1]

    def manual(self, source=None, **changes):
        source = source or self.state().data['sources'][0]
        data = dict(identity=source['id'], title='SYNTHETIC piano & light',
            post_url=source['url'] + '/posts/123', listed_when='Friday; exact time unconfirmed')
        self.act('manual', **dict(data, **changes))
        return self.state().data['observations'][-1]

    def snapshot(self, label):
        response = self.client.get(self.base + '?mode=' + self.mode)
        self.assertEqual(response.status_code, 200)
        if DEMO_OUTPUT is not None:
            # Inert snapshots contain synthetic data only and no usable form tokens.
            html = re.sub(r'value="[^"]*"', 'value="redacted"', response.content.decode())
            html = re.sub(r'<form[^>]*>', '<div class="snapshot-form">', html).replace('</form>', '</div>')
            html = html.replace('<button', '<button disabled').replace('<input', '<input disabled').replace('<select', '<select disabled')
            (DEMO_OUTPUT / (label + '.html')).write_text(html)
        return response

    def test_demo_selected_source_queue_challenge_review_and_import(self):
        self.snapshot('01-not-connected')
        self.mode = 'sample'
        source = self.add()
        task = self.queue(scenario='challenge')
        self.act('run', identity=task['id'])
        self.assertContains(self.snapshot('02-challenge'), 'Browser challenge; this read stopped')
        task = self.queue(scenario='ready')
        self.snapshot('03-queued')
        self.act('run', identity=task['id'])
        row = self.state().data['observations'][0]
        self.assertContains(self.snapshot('04-review'), 'SAMPLE: Light, piano &amp; moving image')
        self.act('import', identity=row['id'])
        self.assertContains(self.snapshot('05-sample-import'), 'It cannot enter your discovery')
        self.assertEqual(DiscoveryState.objects.count(), 0)
        self.mode = 'household'
        source = self.add()
        task = self.queue(source)
        self.act('run', identity=task['id'])
        self.assertEqual(self.state().data['tasks'][0]['status'], 'not_connected')
        lead = self.manual()
        self.act('import', identity=lead['id'])
        response = self.client.get('/profiles/1/discovery/')
        self.assertContains(response, 'SYNTHETIC piano &amp; light')
        self.assertContains(response, 'Reviewed social announcement; independently unverified')
        self.assertContains(self.client.get('/profiles/1/recommendations/'), 'SYNTHETIC piano &amp; light')
        self.snapshot('06-manual-import')
        self.assertEqual(DiscoveryState.objects.count(), 0)
        if DEMO_OUTPUT is not None:
            (DEMO_OUTPUT / 'journey.json').write_text(json.dumps({'mode': 'synthetic test-client journey',
                'real_social_reads': 0, 'browser_connected': False, 'sample_and_household_stores': 2,
                'human_taste_history_created': False, 'queue': self.state().data['tasks'],
                'reviewed_manual_lead': lead}, indent=2) + '\n')

    def test_get_is_pure_and_does_not_create_selection_or_read(self):
        with patch('shelf.social_sources.adapter', side_effect=AssertionError('GET must not read')):
            self.assertContains(self.client.get(self.base), 'Browser adapter not connected')
        self.assertEqual(SocialDiscoveryState.objects.count(), 0)
        self.add()
        self.queue()
        before = deepcopy(self.state().data)
        with patch('shelf.social_sources.adapter', side_effect=AssertionError('GET must not read')):
            self.client.get(self.base)
            self.client.get('/profiles/1/discovery/')
            self.client.get('/profiles/1/recommendations/')
        self.assertEqual(self.state().data, before)

    def test_exact_replay_does_not_read_twice_and_changed_nonce_payload_conflicts(self):
        self.mode = 'sample'
        self.add()
        task = self.queue()
        payload = self.payload('run', identity=task['id'])
        with patch.object(sources.SampleBrowser, 'read', autospec=True, side_effect=sources.SampleBrowser.read) as read:
            for _ in range(2):
                self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 302)
            self.assertEqual(read.call_count, 1)
        payload['action'] = 'cancel'
        self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 409)
        self.act('run', expected=409, identity=task['id'])
        self.assertEqual(len(self.state().data['observations']), 1)

    def test_stale_forms_and_same_source_active_task_are_rejected(self):
        source = self.add()
        stale = self.payload('remove_source', identity=source['id'])
        self.queue()
        self.assertEqual(self.client.post(self.base + 'action/', stale).status_code, 409)
        self.act('queue', expected=409, identity=source['id'])
        self.assertEqual(len(self.state().data['tasks']), 1)

    def test_login_challenge_policy_empty_and_unavailable_all_stop_without_leads(self):
        self.mode = 'sample'
        self.add()
        for scenario in sources.SCENARIOS[1:]:
            with self.subTest(scenario=scenario):
                task = self.queue(scenario=scenario)
                self.act('run', identity=task['id'])
                self.assertEqual(self.state().data['tasks'][-1]['status'], scenario)
                self.assertEqual(self.state().data['observations'], [])

    def test_adapter_failures_and_invalid_payload_do_not_echo_or_store_secrets(self):
        self.add()
        task = self.queue()
        with patch.object(sources.DisconnectedBrowser, 'read', side_effect=OSError('SYNTHETIC-secret')):
            self.act('run', identity=task['id'])
        self.assertEqual(self.state().data['tasks'][-1]['status'], 'unavailable')
        self.assertNotIn('SYNTHETIC-secret', json.dumps(self.state().data))
        self.assertNotContains(self.client.get(self.base), 'SYNTHETIC-secret')
        task = self.queue()
        with patch.object(sources.DisconnectedBrowser, 'read', return_value=sources.BrowserOutcome('ready', SOURCE['url'], ({'cookies': 'SYNTHETIC-secret'},))):
            self.act('run', identity=task['id'])
        self.assertEqual(self.state().data['observations'], [])

    def test_grant_revocation_during_read_cannot_publish_results(self):
        self.add()
        task = self.queue()
        def revoke(adapter, read):
            Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
            return sources.SampleBrowser('ready').read(read)
        with patch.object(sources.DisconnectedBrowser, 'read', revoke):
            self.act('run', expected=404, identity=task['id'])
        self.assertEqual(self.state().data['observations'], [])
        self.assertEqual(self.state().data['tasks'][0]['status'], 'reading')

    def test_logout_during_read_cannot_publish_results(self):
        self.add()
        task = self.queue()
        def logout(adapter, read):
            self.client.session.flush()
            return sources.SampleBrowser('ready').read(read)
        with patch.object(sources.DisconnectedBrowser, 'read', logout):
            self.act('run', expected=404, identity=task['id'])
        self.assertEqual(self.state().data['observations'], [])

    def test_source_removal_during_read_discards_result_and_purges_owned_metadata(self):
        source = self.add()
        task = self.queue()
        def remove(adapter, read):
            self.act('remove_source', identity=source['id'])
            return sources.SampleBrowser('ready').read(read)
        with patch.object(sources.DisconnectedBrowser, 'read', remove):
            self.act('run', expected=409, identity=task['id'])
        self.assertEqual(self.state().data, storage.empty_data())

    def test_unexpected_adapter_error_leaves_reserved_task_without_retry(self):
        self.add()
        task = self.queue()
        payload = self.payload('run', identity=task['id'])
        with patch.object(sources.DisconnectedBrowser, 'read', side_effect=RuntimeError('synthetic bug')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.base + 'action/', payload)
        self.assertEqual(self.state().data['tasks'][0]['status'], 'reading')
        with patch.object(sources.DisconnectedBrowser, 'read', side_effect=AssertionError('No repeat')):
            self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 302)
        self.act('cancel', identity=task['id'])
        self.assertEqual(self.state().data['tasks'][0]['status'], 'cancelled')

    def test_queued_request_expires_before_adapter_call(self):
        self.add()
        task = self.queue()
        with patch('shelf.social_discovery.timezone.now', return_value=timezone.now() + timedelta(minutes=11)), \
             patch('shelf.social_sources.adapter', side_effect=AssertionError('Expired read')):
            self.act('run', identity=task['id'])
        self.assertEqual(self.state().data['tasks'][0]['status'], 'expired')

    def test_pending_leads_require_review_and_expired_imports_leave_current_discovery(self):
        self.add()
        row = self.manual()
        self.assertNotContains(self.client.get('/profiles/1/discovery/'), row['title'])
        self.act('import', identity=row['id'])
        self.assertContains(self.client.get('/profiles/1/discovery/'), 'SYNTHETIC piano &amp; light')
        stored = self.state()
        data = deepcopy(stored.data)
        data['observations'][0]['captured_at'] = (timezone.now() - timedelta(hours=25)).isoformat()
        SocialDiscoveryState.objects.filter(pk=stored.pk).update(data=data)
        self.assertNotContains(self.client.get('/profiles/1/discovery/'), row['title'])
        self.act('import', expected=409, identity=row['id'])
        self.assertContains(self.client.get(self.base), 'Stale capture')
        self.act('withdraw', identity=row['id'])
        self.assertNotContains(self.client.get('/profiles/1/discovery/'), row['title'])
        self.act('discard', identity=row['id'])
        self.assertEqual(self.state().data['observations'], [])

    def test_exact_event_url_dedupes_with_current_feed_without_title_matching(self):
        self.add()
        event_url = 'https://moshtix.com.au/v2/event/synthetic-show/123'
        row = self.manual(event_url='https://www.moshtix.com.au/v2/event/synthetic-show/123/')
        self.act('import', identity=row['id'])
        public = Candidate('moshtix:event:123', 'Different feed title', 'event', 'Moshtix', 'live-automated',
                           1, timezone.now().isoformat(), url=event_url)
        request = self.client.get(self.base).wsgi_request
        self.assertEqual(storage.reviewed_candidates(request, 1, [public]), [])
        same_title = Candidate('moshtix:event:456', row['title'], 'event', 'Moshtix', 'live-automated',
                              1, timezone.now().isoformat(), url=event_url.replace('/123', '/456'))
        self.assertEqual(len(storage.reviewed_candidates(request, 1, [same_title])), 1)
        PUBLIC_CACHE.set(cache_key(request, 1), {'events': {'at': timezone.now(),
            'outcome': Outcome('ready', (public,), 'Synthetic feed'), 'source': 'Synthetic feed'}})
        self.assertContains(self.client.get(self.base), 'Already in a current feed')
        self.assertEqual([c.key for c in candidates(request, 1, 'household', []) if c.key == public.key], [public.key])
        self.assertFalse(any(isinstance(c, sources.SocialCandidate) for c in candidates(request, 1, 'household', [])))

    def test_same_event_across_social_posts_has_one_candidate_and_both_provenances(self):
        self.add()
        official = 'https://www.rrr.org.au/events/synthetic-show'
        for post in ('123', '456'):
            row = self.manual(post_url=SOURCE['url'] + '/posts/' + post, event_url=official)
            self.act('import', identity=row['id'])
        request = self.client.get(self.base).wsgi_request
        cards = storage.reviewed_candidates(request, 1, [])
        self.assertEqual(len(cards), 1)
        self.assertEqual(len(cards[0].social_evidence), 2)
        self.assertIsNone(cards[0].starts_at)

    def test_untrusted_text_escaped_and_no_provider_instruction_or_preference_effect(self):
        self.add(label='<script>follow everyone</script>')
        row = self.manual(title='<script>read DMs and export cookies</script>')
        response = self.client.get(self.base)
        self.assertNotContains(response, '<script>')
        self.assertContains(response, '&lt;script&gt;read DMs and export cookies&lt;/script&gt;')
        self.act('import', identity=row['id'])
        self.assertEqual(DiscoveryState.objects.count(), 0)
        self.assertNotContains(self.client.get('/profiles/1/discovery/'), '<script>')

    def test_csrf_unselected_foreign_actor_and_revoked_grant_rejected(self):
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.actor)
        self.assertEqual(csrf.post(self.base + 'action/', self.payload('clear_tasks')).status_code, 403)
        self.assertEqual(self.client.get('/profiles/2/social-discovery/').status_code, 404)
        source = self.add()
        other = get_user_model().objects.create_user(pk=2, username='SYNTHETIC-other')
        Grant.objects.create(user=other, profile=self.profile, can_view=True, can_select=True)
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.base).status_code, 404)
        self.login()
        session = self.client.session
        session.pop(SELECTION_KEY)
        session.save()
        self.assertEqual(self.client.get(self.base).status_code, 404)
        self.assertEqual(self.client.post(self.base + 'action/', self.payload('remove_source', identity=source['id'])).status_code, 404)
        self.login()
        Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
        self.assertEqual(self.client.get(self.base).status_code, 404)

    def test_strict_forms_no_hidden_capture_auth_or_scenario_fields(self):
        for extra in ({'cookies': 'secret'}, {'scenario': 'ready'}, {'confirmed': ''}, {'title': 'Unexpected'}):
            payload = self.payload('add_source', **SOURCE)
            payload.update(extra)
            self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 400)
        payload = self.payload('add_source', **SOURCE)
        payload['url'] = [SOURCE['url'], 'https://www.facebook.com/other']
        self.assertEqual(self.client.post(self.base + 'action/', payload).status_code, 400)
        self.assertEqual(SocialDiscoveryState.objects.count(), 0)

    def test_bounded_sources_tasks_inbox_and_receipts_fail_explicitly(self):
        for index in range(sources.MAX_SOURCES):
            self.add(url='https://www.facebook.com/fixture-' + str(index))
        self.act('add_source', expected=409, **dict(SOURCE, url='https://www.facebook.com/one-too-many'))
        state = self.state()
        data = deepcopy(state.data)
        source = data['sources'][0]
        data['tasks'] = [dict(id=f'{i:032x}', source_id=source['id'], status='not_connected',
            queued_at=timezone.now().isoformat(), checked_at='', scenario='ready') for i in range(sources.MAX_TASKS)]
        SocialDiscoveryState.objects.filter(pk=state.pk).update(data=data)
        self.act('queue', expected=409, identity=source['id'])
        self.act('clear_tasks')
        self.assertEqual(self.state().data['tasks'], [])
        row = self.manual(source)
        data = deepcopy(self.state().data)
        data['observations'] = [dict(row, id=f'{i:064x}') for i in range(sources.MAX_INBOX)]
        SocialDiscoveryState.objects.filter(pk=state.pk).update(data=data)
        self.act('manual', expected=409, identity=source['id'], title='one too many', post_url=source['url'] + '/posts/new')
        SocialDiscoveryState.objects.filter(pk=state.pk).update(receipts={f'n{i}': 'digest' for i in range(500)})
        self.act('queue', expected=409, identity=source['id'])

    def test_cleanup_at_receipt_cap_preserves_replay_safety_task_evidence_and_taste_history(self):
        old_source_form = self.payload('add_source', **SOURCE)
        self.assertEqual(self.client.post(self.base + 'action/', old_source_form).status_code, 302)
        source = self.state().data['sources'][0]
        lead = self.manual(source)
        self.act('import', identity=lead['id'])
        other_lead = self.manual(source, post_url=source['url'] + '/posts/456')
        response = self.client.get('/profiles/1/discovery/')
        card = next(row['card'] for row in response.context['events']
                    if isinstance(row['card'], sources.SocialCandidate))
        self.assertEqual(self.client.post('/profiles/1/discovery/action/', {
            'mode': 'household', 'operation': 'respond', 'key': card.key, 'choice': 'interested',
            'revision': response.context['revision'], 'nonce': response.context['nonce']}).status_code, 302)
        history = deepcopy(DiscoveryState.objects.get().history)
        reading = self.queue(source)
        other_source = self.add(url='https://www.facebook.com/fixture-other-source')
        queued = self.queue(other_source)
        state = self.state()
        data = deepcopy(state.data)
        data['tasks'][0].update(status='reading', checked_at=timezone.now().isoformat())
        reading_evidence = deepcopy(data['tasks'][0])
        receipts = dict(state.receipts)
        receipts.update({f'old-receipt-{index}': 'digest' for index in range(500 - len(receipts))})
        SocialDiscoveryState.objects.filter(pk=state.pk).update(data=data, receipts=receipts)

        withdraw = self.payload('withdraw', identity=lead['id'])
        self.assertEqual(self.client.post(self.base + 'action/', withdraw).status_code, 302)
        after_withdraw = self.state()
        self.assertEqual(len(after_withdraw.receipts), 500)
        self.assertEqual(after_withdraw.data['observations'][0]['review'], 'pending')
        # Exact cleanup replay retains one effect and the same state revision.
        self.assertEqual(self.client.post(self.base + 'action/', withdraw).status_code, 302)
        self.assertEqual(self.state().revision, after_withdraw.revision)
        self.act('discard', identity=other_lead['id'])
        self.assertFalse(any(row['id'] == other_lead['id'] for row in self.state().data['observations']))
        self.act('clear_tasks')
        self.assertEqual(self.state().data['tasks'][0], reading_evidence)
        self.assertEqual(self.state().data['tasks'][1]['id'], queued['id'])
        self.act('cancel', identity=reading['id'])
        retained = self.state().data['tasks'][0]
        self.assertEqual(retained, dict(reading_evidence, status='cancelled'))
        self.act('remove_source', identity=source['id'])
        self.assertEqual([row['id'] for row in self.state().data['sources']], [other_source['id']])
        self.assertEqual(self.state().data['observations'], [])
        self.assertEqual([row['id'] for row in self.state().data['tasks']], [queued['id']])
        self.assertEqual(DiscoveryState.objects.get().history, history)
        self.assertEqual(len(self.state().receipts), 500)
        # The oldest source form was pruned. It still cannot recreate the source.
        self.assertNotIn(old_source_form['nonce'], self.state().receipts)
        self.assertEqual(self.client.post(self.base + 'action/', old_source_form).status_code, 409)
        self.assertEqual([row['id'] for row in self.state().data['sources']], [other_source['id']])
        self.act('queue', expected=409, identity=other_source['id'])

    def test_restart_retains_sources_and_reviewed_leads_and_sample_never_enters_household(self):
        self.mode = 'sample'
        self.add()
        task = self.queue()
        self.act('run', identity=task['id'])
        row = self.state().data['observations'][0]
        self.act('import', identity=row['id'])
        self.client.logout()
        self.login()
        self.assertContains(self.client.get(self.base + '?mode=sample'), 'SAMPLE: Light, piano &amp; moving image')
        self.assertNotContains(self.client.get('/profiles/1/discovery/'), 'SAMPLE: Light, piano')
        self.assertNotContains(self.client.get('/profiles/1/recommendations/'), 'SAMPLE: Light, piano')
        self.mode = 'household'
        self.act('import', expected=409, identity=row['id'])

    def test_explicit_taste_response_retains_only_judged_identity_after_source_removal(self):
        source = self.add()
        lead = self.manual()
        self.act('import', identity=lead['id'])
        response = self.client.get('/profiles/1/discovery/')
        card = next(row['card'] for row in response.context['events']
                    if isinstance(row['card'], sources.SocialCandidate))
        posted = self.client.post('/profiles/1/discovery/action/', {
            'mode': 'household', 'operation': 'respond', 'key': card.key, 'choice': 'interested',
            'revision': response.context['revision'], 'nonce': response.context['nonce']})
        self.assertEqual(posted.status_code, 302)
        history = DiscoveryState.objects.get().history
        self.assertEqual(history[0]['choice'], 'interested')
        self.assertEqual(history[0]['source']['provenance'], 'manual-entry')
        self.act('remove_source', identity=source['id'])
        response = self.client.get('/profiles/1/discovery/')
        self.assertNotContains(response, 'SYNTHETIC piano &amp; light')
        self.assertContains(response, 'Previously judged identity (source details expired)')
        self.assertEqual(DiscoveryState.objects.get().history, history)
        self.assertEqual(self.state().data, storage.empty_data())

    def test_missing_optional_table_is_read_only(self):
        with patch('shelf.social_discovery.available', return_value=False):
            self.assertContains(self.client.get(self.base), 'Storage unavailable')
            self.act('add_source', expected=409, **SOURCE)
            self.assertEqual(storage.reviewed_candidates(None, 1, []), [])
