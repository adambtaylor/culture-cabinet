"""Real HTTP rehearsal journeys and dispatch regressions, entirely fictional."""
from copy import deepcopy
from datetime import timedelta
import json
import re
from unittest.mock import patch
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from shelf import messages
from shelf.acquisition_contract import Blocked
from shelf.authority import SELECTION_KEY
from shelf.message_provider import CHATS, MESSAGES, RehearsalMessages, provider
from shelf.models import EventPlan, Grant, MessageDraft, Profile

DEMO_OUTPUT = None


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class MessageTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-message-owner')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='SYNTHETIC owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.base = '/profiles/1/messages/'
        self.steps = []
        self.login()

    def login(self):
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = 1
        session.save()

    def start(self, **changes):
        fields = dict(kind='invitation', chat_id=CHATS[0]['id'], session_id='fixture-session-15',
                      message_id='', scenario='sent', access='ready')
        fields.update(changes)
        response = self.client.post(self.base + 'start/', fields)
        if response.status_code == 302:
            self.draft = MessageDraft.objects.get(pk=response.url.strip('/').split('/')[-1])
            self.url = response.url
        return response

    def reply(self, index=0, **changes):
        return self.start(kind='reply', chat_id=CHATS[index]['id'], session_id='',
                          message_id=MESSAGES[index]['id'], **changes)

    def payload(self, action, **changes):
        self.draft.refresh_from_db()
        return dict(action=action, version=self.draft.version, nonce=self.draft.nonce,
                    preview=messages.preview_digest(self.draft), confirmed='on', **changes)

    def act(self, action, *, expected=302, **changes):
        response = self.client.post(self.url + 'action/', self.payload(action, **changes))
        self.assertEqual(response.status_code, expected, response.content.decode()[:500])
        self.draft.refresh_from_db()
        return response

    def approve_reply(self):
        return self.act('approve', sensitive_confirmed='on',
                        recipient_confirmation=self.draft.data['chat']['identity'])

    def snapshot(self, label, url=None):
        response = self.client.get(url or self.url)
        self.assertEqual(response.status_code, 200)
        self.draft.refresh_from_db()
        self.steps.append({'step': label, 'state': self.draft.state, 'kind': self.draft.data['kind'],
            'chat_id': self.draft.data['chat']['id'], 'dispatch': self.draft.data['dispatch'],
            'evidence': self.draft.data['evidence']})
        if DEMO_OUTPUT is not None:
            html = response.content.decode()
            html = re.sub(r'<input[^>]+type="hidden"[^>]*>', '', html)
            html = re.sub(r'<form\b[^>]*>', '<div class="inert-form">', html).replace('</form>', '</div>')
            for tag in ('button', 'input', 'select', 'textarea'):
                html = html.replace('<' + tag, '<' + tag + ' disabled')
            html = re.sub(r'<a\b[^>]*>', '<span>', html).replace('</a>', '</span>')
            (DEMO_OUTPUT / (label + '.html')).write_text(html)
        return response

    def test_demo_invitation_unknown_reconciliation_and_selected_reply(self):
        self.assertEqual(self.start(scenario='unknown_then_sent').status_code, 302)
        self.snapshot('01-index', self.base)
        preview = self.snapshot('02-invitation-review')
        for text in (CHATS[0]['identity'], 'fixture-session-15', '2027-01-15T20:00:00+11:00',
                     '2027-01-15T22:00:00+11:00', 'Australia/Melbourne', 'SYNTHETIC venue',
                     'AUD 44.50', '4.50 fees', 'https://events.example.invalid/concert',
                     'No real account reads, sends or calendar writes'):
            self.assertContains(preview, text)
        self.act('approve')
        self.snapshot('03-queued')
        self.act('dispatch')
        self.assertEqual(self.draft.state, 'unknown')
        self.snapshot('04-unknown')
        self.client.logout()
        self.login()
        self.act('dispatch', expected=409)
        self.act('reconcile')
        self.assertEqual(self.draft.state, 'sent')
        self.assertEqual(self.draft.data['dispatch']['attempts'], 1)
        self.snapshot('05-sent-evidence')
        self.assertEqual(EventPlan.objects.count(), 0)  # Chat never creates a calendar plan.
        self.assertEqual(self.reply(index=2).status_code, 302)
        self.snapshot('06-selected-context', self.base + 'context/' + CHATS[2]['id'] + '/')
        self.act('edit', body='Thanks. I will check the event details first.')
        self.snapshot('07-reply-preview')
        self.approve_reply()
        self.act('dispatch')
        self.snapshot('08-reply-evidence')
        if DEMO_OUTPUT is not None:
            (DEMO_OUTPUT / 'journey.json').write_text(json.dumps({'mode': 'fictional rehearsal',
                'real_account_reads': 0, 'real_sends': 0, 'calendar_writes': 0,
                'steps': self.steps}, indent=2) + '\n')

    def test_selected_context_is_bounded_escaped_and_cannot_grant_authority(self):
        response = self.client.get(self.base + 'context/' + CHATS[2]['id'] + '/')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertNotContains(response, MESSAGES[0]['text'])
        self.assertEqual(MessageDraft.objects.count(), 0)
        self.reply(index=2)
        self.assertNotIn('medical', self.draft.data['body'])
        self.assertEqual(self.draft.state, 'draft')
        self.assertIsNone(self.draft.data['approval'])
        self.act('dispatch', expected=409)

    def test_exact_chat_and_message_binding_rejects_names_groups_or_cross_chat_context(self):
        for values in ({'chat_id': 'Alex'}, {'chat_id': 'fixture-group'},
                       {'kind': 'reply', 'session_id': '', 'message_id': MESSAGES[1]['id']},
                       {'session_id': 'fixture-session-unknown'},
                       {'message_id': MESSAGES[0]['id']}):
            with self.subTest(values=values):
                self.assertEqual(self.start(**values).status_code, 409)
        self.assertEqual(MessageDraft.objects.count(), 0)

    def test_reply_requires_sensitive_review_typed_identity_and_fresh_approval_after_edit(self):
        self.reply()
        self.act('approve', expected=409)
        self.act('approve', sensitive_confirmed='on', recipient_confirmation=CHATS[1]['identity'], expected=409)
        self.act('edit', body='Private detail shared only after review: <b>example</b>')
        response = self.client.get(self.url)
        self.assertContains(response, '&lt;b&gt;example&lt;/b&gt;')
        self.approve_reply()
        consent = self.draft.data['approval']['content_digest']
        self.act('edit', body='A revised harmless reply.')
        self.assertEqual(self.draft.state, 'draft')
        self.assertIsNone(self.draft.data['approval'])
        self.assertNotEqual(consent, messages.content_digest(self.draft.data))
        self.act('dispatch', expected=409)
        self.approve_reply()
        self.act('dispatch')
        self.assertEqual(self.draft.state, 'sent')

    def test_repeated_creation_and_send_forms_reuse_the_exact_intent(self):
        self.start()
        first = self.draft.pk
        self.start()
        self.assertEqual(first, self.draft.pk)
        self.assertEqual(self.start(scenario='failed').status_code, 409)
        self.assertEqual(MessageDraft.objects.count(), 1)
        self.act('approve')
        fields = self.payload('dispatch')
        with patch.object(RehearsalMessages, 'send', autospec=True,
                          side_effect=RehearsalMessages.send) as send:
            for _ in range(2):
                self.assertEqual(self.client.post(self.url + 'action/', fields).status_code, 302)
            self.assertEqual(send.call_count, 1)
        self.act('dispatch', expected=409)
        fields['action'] = 'reconcile'
        self.assertEqual(self.client.post(self.url + 'action/', fields).status_code, 409)
        self.assertEqual(self.draft.data['dispatch']['attempts'], 1)

    def test_browser_multiline_reply_normalizes_before_approval_and_unknown_reconciliation(self):
        self.reply(scenario='unknown_then_sent')
        response = self.client.post(self.url + 'action/',
            urlencode(self.payload('edit', body='x' * 2798 + '\r\n' + 'y')),
            content_type='application/x-www-form-urlencoded')
        self.assertEqual(response.status_code, 302)  # 2801 wire chars, 2800 canonical chars.
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.data['body'], 'x' * 2798 + '\n' + 'y')
        expected = 'First line\nSecond line'
        for submitted in ('First line\r\nSecond line', 'First line\rSecond line'):
            with self.subTest(submitted=repr(submitted)):
                response = self.client.post(self.url + 'action/',
                    urlencode(self.payload('edit', body=submitted)),
                    content_type='application/x-www-form-urlencoded')
                self.assertEqual(response.status_code, 302)
                self.draft.refresh_from_db()
                self.assertEqual(self.draft.data['body'], expected)
                self.assertContains(self.client.get(self.url),
                    '<pre class="message-text">First line\nSecond line</pre>', html=False)
        self.approve_reply()
        approved = self.draft.data['approval']['content_digest']
        self.assertEqual(approved, messages.content_digest(self.draft.data))
        def dispatch(draft):
            self.assertEqual(draft.data['body'], expected)
            self.assertEqual(draft.data['dispatch']['content_digest'], approved)
            return RehearsalMessages().evidence(draft)
        with patch.object(RehearsalMessages, 'send', side_effect=dispatch) as send:
            self.act('dispatch')
            self.assertEqual(self.draft.state, 'unknown')
            self.assertEqual(self.draft.data['evidence']['content_digest'], approved)
            self.act('reconcile')
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.draft.state, 'sent')
        self.assertEqual(self.draft.data['body'], expected)
        self.assertEqual(self.draft.data['evidence']['content_digest'], approved)
        self.assertEqual(self.draft.data['dispatch']['attempts'], 1)

    def test_dispatch_checkpoint_precedes_adapter_and_unknown_reconciles_without_send(self):
        self.start(scenario='unknown_then_sent')
        self.act('approve')
        def dispatch(draft):
            saved = MessageDraft.objects.get(pk=draft.pk)
            self.assertEqual(saved.state, 'sending')
            self.assertEqual(saved.data['dispatch']['attempts'], 1)
            self.assertEqual(saved.data['approval']['content_digest'], saved.data['dispatch']['content_digest'])
            return RehearsalMessages().evidence(draft)
        with patch.object(RehearsalMessages, 'send', side_effect=dispatch) as send:
            self.act('dispatch')
            self.act('reconcile')
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.draft.state, 'sent')

    def test_transport_error_retains_unknown_and_never_stores_private_error_text(self):
        self.start()
        self.act('approve')
        with patch.object(RehearsalMessages, 'send', side_effect=OSError('PRIVATE provider payload')):
            self.act('dispatch')
        self.assertEqual(self.draft.state, 'unknown')
        self.assertNotIn('PRIVATE', json.dumps(self.draft.data))
        self.act('dispatch', expected=409)
        with patch.object(RehearsalMessages, 'send', side_effect=AssertionError('must never resend')):
            self.act('reconcile')
        self.assertEqual(self.draft.state, 'sent')

    def test_unknown_at_audit_caps_can_reconcile_without_resend_or_stale_form_replay(self):
        self.start(scenario='unknown_then_sent')
        self.act('approve')
        dispatch_form = self.payload('dispatch')
        self.assertEqual(self.client.post(self.url + 'action/', dispatch_form).status_code, 302)
        self.draft.refresh_from_db()
        original_dispatch = deepcopy(self.draft.data['dispatch'])
        original_approval = deepcopy(self.draft.data['approval'])
        original_body = self.draft.data['body']
        original_evidence = deepcopy(self.draft.data['evidence'])
        first_check = None
        with patch.object(RehearsalMessages, 'send', side_effect=AssertionError('must never resend')):
            with patch.object(RehearsalMessages, 'reconcile',
                    side_effect=lambda draft: RehearsalMessages().evidence(draft)) as checks:
                # Reach and pass both real limits through HTTP, not by altering
                # saved counters. The fixture keeps returning unknown for now.
                for _ in range(messages.MAX_ACTIONS + 2):
                    check_form = self.payload('reconcile')
                    if first_check is None:
                        first_check = check_form
                    self.assertEqual(self.client.post(self.url + 'action/', check_form).status_code, 302)
                    self.draft.refresh_from_db()
                    self.assertEqual(self.draft.state, 'unknown')
                    self.assertLessEqual(len(self.draft.receipts), messages.MAX_ACTIONS)
                    self.assertLessEqual(len(self.draft.data['timeline']), messages.MAX_ACTIONS)
                    self.assertEqual(self.draft.data['dispatch'], original_dispatch)
                    self.assertEqual(self.draft.data['evidence'], original_evidence)
                self.assertEqual(len(self.draft.receipts), messages.MAX_ACTIONS)
                self.assertEqual(len(self.draft.data['timeline']), messages.MAX_ACTIONS)
                self.assertNotIn(dispatch_form['nonce'], self.draft.receipts)
                self.assertNotIn(first_check['nonce'], self.draft.receipts)
                # A retained replay is a no-op; pruned forms are stale, including
                # the original send form. Neither can call the provider again.
                prior_checks = checks.call_count
                self.assertEqual(self.client.post(self.url + 'action/', check_form).status_code, 302)
                for stale in (first_check, dispatch_form):
                    self.assertEqual(self.client.post(self.url + 'action/', stale).status_code, 409)
                changed_replay = dict(check_form, action='dispatch')
                self.assertEqual(self.client.post(self.url + 'action/', changed_replay).status_code, 409)
                self.assertEqual(checks.call_count, prior_checks)
            def mismatched(draft):
                evidence = RehearsalMessages().evidence(draft, reconcile=True)
                evidence['content_digest'] = '0' * 64
                return evidence
            with patch.object(RehearsalMessages, 'reconcile', side_effect=mismatched):
                self.act('reconcile', expected=409)
            self.assertEqual(self.draft.state, 'unknown')
            self.assertEqual(self.draft.data['evidence'], original_evidence)
            self.act('reconcile')
        self.assertEqual(self.draft.state, 'sent')
        self.assertEqual(self.draft.data['dispatch'], original_dispatch)
        self.assertEqual(self.draft.data['dispatch']['attempts'], 1)
        self.assertEqual(self.draft.data['approval'], original_approval)
        self.assertEqual(self.draft.data['body'], original_body)
        self.assertEqual(self.draft.data['evidence'], dict(original_evidence, state='sent'))
        self.assertEqual(len(self.draft.receipts), messages.MAX_ACTIONS)
        self.assertEqual(len(self.draft.data['timeline']), messages.MAX_ACTIONS)

    def test_crash_leaves_sending_checkpoint_and_can_only_reconcile(self):
        self.start()
        self.act('approve')
        with patch.object(RehearsalMessages, 'send', side_effect=RuntimeError('simulated interruption')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url + 'action/', self.payload('dispatch'))
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.state, 'sending')
        self.assertContains(self.client.get(self.url), 'Outcome uncertain')
        self.act('dispatch', expected=409)
        self.act('reconcile')
        self.assertEqual(self.draft.state, 'sent')

    def test_failed_evidence_is_terminal_and_no_new_draft_can_bypass_it(self):
        self.start(scenario='failed')
        first = self.draft.pk
        self.act('approve')
        self.act('dispatch')
        self.assertEqual(self.draft.state, 'failed')
        self.assertTrue(self.draft.data['evidence']['reference'])
        for action in ('dispatch', 'approve', 'refresh', 'reconcile'):
            self.act(action, expected=409)
        self.start(scenario='failed')
        self.assertEqual(first, self.draft.pk)
        self.assertEqual(self.draft.data['dispatch']['attempts'], 1)

    def test_wrong_provider_evidence_cannot_confirm_another_message(self):
        for field, value in (('chat_id', CHATS[1]['id']), ('content_digest', '0' * 64),
                             ('dispatch_key', 'other-dispatch'), ('fixture', False),
                             ('reference', 'other-reference'), ('state', 'delivered'),
                             ('delivered', 'yes'), ('extra_private_payload', 'must not persist')):
            with self.subTest(field=field):
                MessageDraft.objects.all().delete()
                self.start()
                self.act('approve')
                def wrong(draft):
                    evidence = RehearsalMessages().evidence(draft)
                    evidence[field] = value
                    return evidence
                with patch.object(RehearsalMessages, 'send', side_effect=wrong):
                    self.act('dispatch', expected=409)
                self.assertEqual(self.draft.state, 'sending')
                self.assertIsNone(self.draft.data['evidence'])
                self.assertEqual(self.draft.data['dispatch']['attempts'], 1)

    def test_login_challenge_and_ambiguous_access_pause_approval_and_dispatch(self):
        for state in ('login_required', 'challenge', 'ambiguous_recipient'):
            with self.subTest(state=state):
                MessageDraft.objects.all().delete()
                self.start(access=state)
                with patch.object(RehearsalMessages, 'send', side_effect=AssertionError('blocked')):
                    self.act('approve', expected=409)
                    self.act('dispatch', expected=409)
                self.assertIsNone(self.draft.data['approval'])
                self.assertIsNone(self.draft.data['dispatch'])

    def test_expired_quote_needs_refresh_and_fresh_approval(self):
        self.start()
        self.act('approve')
        future = timezone.now() + timedelta(minutes=11)
        with patch('shelf.messages.timezone.now', return_value=future):
            self.act('dispatch', expected=409)
            self.act('refresh')
            self.assertEqual(self.draft.state, 'draft')
            self.assertIsNone(self.draft.data['approval'])
            self.act('dispatch', expected=409)
            self.act('approve')
            self.act('dispatch')
        self.assertEqual(self.draft.state, 'sent')

    def test_old_reply_approval_expires_without_a_dispatch_attempt(self):
        self.reply()
        self.approve_reply()
        with patch('shelf.messages.timezone.now', return_value=timezone.now() + timedelta(minutes=16)):
            self.act('dispatch', expected=409)
            self.assertIsNone(self.draft.data['dispatch'])
            self.approve_reply()
            self.act('dispatch')

    def test_stale_preview_and_out_of_band_content_change_do_not_inherit_consent(self):
        self.reply()
        stale = self.payload('approve', sensitive_confirmed='on', recipient_confirmation=CHATS[0]['identity'])
        self.act('edit', body='Updated reply')
        self.assertEqual(self.client.post(self.url + 'action/', stale).status_code, 409)
        self.approve_reply()
        data = deepcopy(self.draft.data)
        data['body'] = 'Unexpected changed reply'
        MessageDraft.objects.filter(pk=self.draft.pk).update(data=data)
        self.act('dispatch', expected=409)
        self.assertIsNone(self.draft.data['dispatch'])

    def test_forged_event_source_or_observed_chat_is_refused(self):
        self.start()
        data = deepcopy(self.draft.data)
        data['chat']['identity'] = 'Unobserved recipient'
        MessageDraft.objects.filter(pk=self.draft.pk).update(data=data)
        self.act('approve', expected=409)
        data['chat'] = deepcopy(CHATS[0])
        data['event']['source_verified'] = False
        MessageDraft.objects.filter(pk=self.draft.pk).update(data=data)
        self.act('approve', expected=409)
        self.assertIsNone(self.draft.data['dispatch'])

    def test_changed_text_after_unknown_cannot_reconcile_as_the_original_dispatch(self):
        self.reply(scenario='unknown_then_sent')
        self.approve_reply()
        self.act('dispatch')
        self.assertEqual(self.draft.state, 'unknown')
        data = deepcopy(self.draft.data)
        data['body'] = 'Out-of-band changed text must not inherit sent evidence.'
        MessageDraft.objects.filter(pk=self.draft.pk).update(data=data)
        self.assertEqual(self.client.get(self.url).status_code, 409)
        with patch.object(RehearsalMessages, 'reconcile', side_effect=AssertionError('mismatch')):
            self.act('reconcile', expected=409)
        self.assertEqual(self.draft.state, 'unknown')
        self.assertEqual(self.draft.data['evidence']['state'], 'unknown')

    def test_current_owner_grant_selection_and_actor_are_required_on_read_and_write(self):
        self.start()
        self.grant.can_select = False
        self.grant.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.act('approve', expected=404)
        self.grant.can_select = True
        self.grant.save()
        session = self.client.session
        session[SELECTION_KEY] = 2
        session.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        other = get_user_model().objects.create_user(username='SYNTHETIC-other')
        Grant.objects.create(user=other, profile=self.profile, can_view=True, can_select=True)
        self.client.force_login(other)
        session = self.client.session
        session[SELECTION_KEY] = 1
        session.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.act('approve', expected=404)

    def test_grant_revoked_during_dispatch_retains_uncertain_checkpoint(self):
        self.start()
        self.act('approve')
        def revoke(draft):
            Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
            return RehearsalMessages().evidence(draft)
        with patch.object(RehearsalMessages, 'send', side_effect=revoke):
            self.act('dispatch', expected=404)
        self.assertEqual(self.draft.state, 'sending')
        self.assertIsNone(self.draft.data['evidence'])
        Grant.objects.filter(pk=self.grant.pk).update(can_select=True)
        self.act('dispatch', expected=409)
        self.act('reconcile')
        self.assertEqual(self.draft.state, 'sent')

    def test_concurrent_change_refuses_late_evidence_and_cas_clobber(self):
        self.start()
        self.act('approve')
        def change(draft):
            MessageDraft.objects.filter(pk=draft.pk).update(version=draft.version + 1)
            return RehearsalMessages().evidence(draft)
        with patch.object(RehearsalMessages, 'send', side_effect=change):
            self.act('dispatch', expected=409)
        self.assertEqual(self.draft.state, 'sending')
        self.assertIsNone(self.draft.data['evidence'])
        stale = deepcopy(self.draft)
        data = deepcopy(self.draft.data)
        data['scenario'] = 'unknown'
        MessageDraft.objects.filter(pk=self.draft.pk).update(data=data)
        with self.assertRaises(messages.Conflict):
            messages._save(stale, stale.data, 'sent')

    def test_authentication_csrf_methods_strict_fields_and_confirmation(self):
        self.start()
        self.assertEqual(Client().get(self.base).status_code, 302)
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.actor)
        session = secure.session
        session[SELECTION_KEY] = 1
        session.save()
        self.assertEqual(secure.post(self.url + 'action/', self.payload('approve')).status_code, 403)
        self.assertEqual(self.client.get(self.url + 'action/').status_code, 405)
        for extra in ({'confirmed': ''}, {'recipient': 'Unseen person'}, {'body': 'hidden text'},
                      {'action': ['approve', 'dispatch']}, {'preview': 'x' * 17000}):
            payload = self.payload('approve')
            payload.update(extra)
            self.assertEqual(self.client.post(self.url + 'action/', payload).status_code, 409)
        self.assertIsNone(self.draft.data['approval'])
        self.assertIsNone(self.draft.data['dispatch'])

    def test_text_bounds_receipt_capacity_and_storage_unavailable_state(self):
        self.reply()
        for body in ('', 'x' * 2801, 'hidden\x01control', 'hidden\tcontrol', '\r\n'):
            self.act('edit', body=body, expected=409)
        self.approve_reply()
        data = deepcopy(self.draft.data)
        data['timeline'] = [{'event': 'synthetic', 'at': '2026-10-28'}] * 199
        MessageDraft.objects.filter(pk=self.draft.pk).update(data=data)
        self.act('dispatch', expected=409)
        self.assertIsNone(self.draft.data['dispatch'])
        data['timeline'] = []
        receipts = {f'fixture-command-{i}': '0' * 64 for i in range(messages.MAX_ACTIONS)}
        MessageDraft.objects.filter(pk=self.draft.pk).update(data=data, receipts=receipts)
        for action, fields in (
                ('edit', {'body': 'Otherwise valid reply edit.'}),
                ('approve', {'sensitive_confirmed': 'on', 'recipient_confirmation': CHATS[0]['identity']}),
                ('dispatch', {})):
            self.act(action, expected=409, **fields)
        self.assertIsNone(self.draft.data['dispatch'])
        self.assertEqual(self.draft.receipts, receipts)
        with patch('shelf.messages.available', return_value=False):
            self.assertContains(self.client.get(self.base), 'Draft storage is unavailable')
            self.assertEqual(self.start().status_code, 409)

    def test_live_transport_is_unavailable(self):
        with self.assertRaises(Blocked):
            provider('household')
        self.assertContains(self.client.get(self.base), 'Link or send live · unavailable')
