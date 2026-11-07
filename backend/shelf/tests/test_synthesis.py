"""Real HTTP/session/grant and durable operator flow; synthetic inference only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.test import Client, TestCase, override_settings
from django.urls import path
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from shelf import synthesis
from shelf.acquisition_contract import Blocked
from shelf.calendar_handoff import digest
from shelf.discovery_state import Conflict
from shelf.models import Grant, Profile, SynthesisRun


@ensure_csrf_cookie
def form(request):
    return JsonResponse({'action': 'Explicit Synergize'})


@require_POST
def action(request, profile_id):
    try:
        row = synthesis.request(request, profile_id, request.POST.get('digest'))
    except (Blocked, Conflict) as error:
        return JsonResponse({'error': str(error)}, status=409)
    return JsonResponse(synthesis.status(row, row.input_digest))


urlpatterns = [path('', form), path('profiles/<int:profile_id>/synergize/', action)]


@override_settings(ROOT_URLCONF=__name__, ALLOWED_HOSTS=['testserver'])
class SynthesisTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='demo-curator')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Synthetic owner')
        Profile.objects.create(pk=2, kind='partner', active=False, label='Synthetic partner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.actor)
        session = self.client.session
        session['selected_profile_id'] = 1
        session.save()
        self.client.get('/')
        self.data = {'actor_id': 1, 'profile_id': 1, 'mode': 'household',
            'revision_map': {'discovery': 4, 'saved': 'synthetic-full-revision-digest'},
            'signals': [
                {'key': 'tmdb:movie:123', 'title': 'Synthetic river film', 'medium': 'movie',
                 'source': 'TMDB', 'basis': 'interest', 'value': 4},
                {'key': 'hardcover:book:456', 'title': 'Synthetic river book', 'medium': 'book',
                 'source': 'Hardcover', 'basis': 'enjoyment', 'value': 5},
                {'key': 'tmdb:movie:123', 'title': 'Synthetic river film', 'medium': 'movie',
                 'source': 'TMDB', 'basis': 'legacy_choice',
                 'value': 'Conflicting historical responses: enjoyed / not_for_me'}]}
        self.enterContext(patch('shelf.taste_funnel.synthesis_snapshot', side_effect=self.current))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('No network')))

    def current(self, actor=None, profile_id=None):
        return dict(deepcopy(self.data), input_digest=digest(self.data))

    def post(self, profile=1, **extra):
        fields = dict(digest=self.current()['input_digest'],
            csrfmiddlewaretoken=self.client.cookies[settings.CSRF_COOKIE_NAME].value)
        fields.update(extra)
        return self.client.post(f'/profiles/{profile}/synergize/', fields)

    def queued(self):
        self.assertEqual(self.post().status_code, 200)
        return SynthesisRun.objects.latest('created_at')

    def reserve(self, row, **extra):
        fields = dict(actor=self.actor, profile_id=1, run_id=row.pk, session_guard=lambda: None)
        fields.update(extra)
        return synthesis.reserve_operator(**fields)

    def evidence(self, row, outcome='completed'):
        output = {'basis': 'inferred_taste_association', 'clusters': [
            {'label': 'River imagery', 'members': ['tmdb:movie:123', 'hardcover:book:456'],
             'explanation': 'These selected signals suggest a possible interest in river imagery.'}],
            'links': [{'from': 'tmdb:movie:123', 'to': 'hardcover:book:456',
                       'explanation': 'A tentative cross-media taste association, not a factual connection.'}]}
        return dict(synthesis.binding(row), evidence_id='synthetic:task:123', outcome=outcome,
            output=output if outcome == 'completed' else None,
            reason='' if outcome == 'completed' else 'response_unavailable')

    def record(self, row, evidence=None, **extra):
        fields = dict(actor=self.actor, profile_id=1, run_id=row.pk,
            evidence=evidence if evidence is not None else self.evidence(row), session_guard=lambda: None)
        fields.update(extra)
        return synthesis.record_operator(**fields)

    def test_http_request_reserve_complete_exact_replays_and_no_automatic_call(self):
        row = self.queued()
        self.assertEqual(row.state, 'queued')
        self.assertEqual(row.attempts, 0)
        self.assertEqual(self.post().json()['state'], 'awaiting_operator')
        self.assertEqual(SynthesisRun.objects.count(), 1)
        row, packet = self.reserve(row)
        row.refresh_from_db()
        self.assertEqual((row.state, row.attempts, row.revision), ('running', 1, 1))
        self.assertEqual(packet['model'], 'gpt-6-astra')
        self.assertEqual(packet['reasoning'], 'high')
        self.assertEqual(packet['signals'], self.data['signals'])
        self.assertNotIn('actor_id', packet)
        self.assertEqual(set(packet), {'reconciliation_only', 'run_id', 'request_identity', 'input_digest',
            'revision_map', 'reservation_revision', 'model', 'reasoning', 'signals', 'instruction', 'execution'})
        _, repeated = self.reserve(row)
        self.assertTrue(repeated['reconciliation_only'])
        self.assertNotIn('signals', repeated)
        self.assertNotIn('instruction', repeated)
        completed = self.record(row)
        original = (completed.revision, completed.evidence, completed.updated_at)
        again = self.record(completed)
        self.assertEqual((again.revision, again.evidence, again.updated_at), original)
        self.assertEqual(self.post().json()['state'], 'completed')
        self.assertEqual(SynthesisRun.objects.count(), 1)
        self.assertEqual(again.attempts, 1)

    def test_csrf_post_current_session_profile_and_grant_are_required(self):
        self.assertEqual(self.client.get('/profiles/1/synergize/').status_code, 405)
        self.assertEqual(self.client.post('/profiles/1/synergize/', {'digest': self.current()['input_digest']}).status_code, 403)
        self.assertEqual(self.post(profile=2).status_code, 404)
        self.grant.can_select = False
        self.grant.save()
        self.assertEqual(self.post().status_code, 404)
        self.grant.can_select = True
        self.grant.save()
        session = self.client.session
        session['selected_profile_id'] = 2
        session.save()
        self.assertEqual(self.post().status_code, 404)
        self.assertFalse(SynthesisRun.objects.exists())

    def test_stale_post_reservation_and_completed_status_follow_full_revision_map(self):
        old_digest = self.current()['input_digest']
        row = self.queued()
        self.data['revision_map']['saved'] = 'changed-even-with-the-same-signals'
        self.assertEqual(self.post(digest=old_digest).status_code, 409)
        with self.assertRaises(Conflict):
            self.reserve(row)
        row.refresh_from_db()
        self.assertEqual(row.attempts, 0)
        current = self.queued()
        self.assertNotEqual(row.pk, current.pk)
        current, _ = self.reserve(current)
        current = self.record(current)
        self.data['signals'][0]['value'] = 1
        view = synthesis.status(current, self.current()['input_digest'])
        self.assertEqual(view['state'], 'stale')
        self.assertIsNone(view['output'])
        self.assertEqual(current.state, 'completed')

    def test_unknown_can_be_reconciled_but_never_authorizes_second_inference(self):
        row, packet = self.reserve(self.queued())
        unknown = self.evidence(row, 'unknown')
        row = self.record(row, unknown)
        self.assertEqual(synthesis.status(row, row.input_digest)['state'], 'unknown')
        row, replay = self.reserve(row)
        self.assertTrue(replay['reconciliation_only'])
        self.assertEqual(self.post().json()['state'], 'unknown')
        self.assertEqual(self.record(row, unknown).revision, 2)
        changed = dict(unknown, evidence_id='synthetic:other')
        with self.assertRaises(Conflict):
            self.record(row, changed)
        row = self.record(row)
        self.assertEqual(row.state, 'completed')
        self.assertEqual(row.evidence['uncertain_record'], unknown)
        self.assertEqual(row.evidence['reservation']['revision'], packet['reservation_revision'])
        self.assertEqual(row.attempts, 1)
        self.assertEqual(self.record(row, unknown).state, 'completed')
        self.assertEqual(self.record(row, unknown).revision, row.revision)

    def test_failed_is_not_complete_and_cannot_be_implicitly_retried_or_overwritten(self):
        row, _ = self.reserve(self.queued())
        row = self.record(row, self.evidence(row, 'failed'))
        self.assertEqual(self.post().json()['state'], 'failed')
        self.assertTrue(self.reserve(row)[1]['reconciliation_only'])
        with self.assertRaises(Conflict):
            self.record(row)
        self.assertIsNone(synthesis.status(row, row.input_digest)['output'])
        row.refresh_from_db()
        self.assertEqual((row.state, row.attempts), ('failed', 1))

    def test_binding_model_reasoning_and_reserved_revision_cannot_be_forged(self):
        row, _ = self.reserve(self.queued())
        for field, value in [('run_id', 'other'), ('request_identity', 'other'), ('input_digest', '0'*64),
                ('revision_map', {'discovery': 999}), ('reservation_revision', 9),
                ('reservation_revision', True),
                ('model', 'gpt-6.1-sol'), ('reasoning', 'low'), ('evidence_id', '')]:
            with self.subTest(field=field), self.assertRaises(Blocked):
                self.record(row, dict(self.evidence(row), **{field: value}))
        row.refresh_from_db()
        self.assertEqual((row.state, row.revision), ('running', 1))

    def test_forged_members_links_duplicate_links_and_non_inferred_output_refused(self):
        row, _ = self.reserve(self.queued())
        edits = [lambda o: o['clusters'][0]['members'].append('invented:key'),
            lambda o: o['links'][0].update(to='invented:key'),
            lambda o: o['links'][0].update(to='tmdb:movie:123'),
            lambda o: o['links'].append(deepcopy(o['links'][0])),
            lambda o: o.update(basis='factual_relationship'), lambda o: o.update(clusters=[]),
            lambda o: o['clusters'][0].update(explanation='x'*1201),
            lambda o: o.update(private_notes='not permitted')]
        for edit in edits:
            evidence = self.evidence(row)
            edit(evidence['output'])
            with self.subTest(evidence=evidence), self.assertRaises(Blocked):
                self.record(row, evidence)
        self.assertEqual(self.record(row).state, 'completed')

    def test_expired_operator_or_revoked_grant_rolls_back_reservation_and_result(self):
        row = self.queued()
        def expired():
            raise ValueError('expired')
        with self.assertRaises(ValueError):
            self.reserve(row, session_guard=expired)
        row.refresh_from_db()
        self.assertEqual((row.state, row.attempts), ('queued', 0))
        row, _ = self.reserve(row)
        with self.assertRaises(ValueError):
            self.record(row, session_guard=expired)
        self.grant.can_select = False
        self.grant.save()
        with self.assertRaises(Blocked):
            self.record(row)
        row.refresh_from_db()
        self.assertEqual(row.state, 'running')

    def test_empty_or_unbounded_or_nonminimal_snapshot_never_queues(self):
        original = deepcopy(self.data)
        variants = [[], [dict(original['signals'][0], address='private')],
            [dict(original['signals'][0], title='x'*301)], [original['signals'][0]]*501,
            [dict(original['signals'][0], value=1.5)]]
        for signals in variants:
            self.data['signals'] = signals
            self.assertEqual(self.post().status_code, 409)
        self.assertFalse(SynthesisRun.objects.exists())

    def test_older_favourite_labels_share_identity_but_cannot_change_its_medium(self):
        favourite = dict(self.data['signals'][0], basis='favourite', value=True,
                         source='tmdb', title='Older captured title')
        self.data['signals'].append(favourite)
        row, packet = self.reserve(self.queued())
        self.assertEqual(packet['signals'][-1], favourite)
        self.assertEqual(self.record(row).state, 'completed')
        favourite['medium'] = 'book'
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(SynthesisRun.objects.count(), 1)

    def test_signal_values_preserve_rating_favourite_and_legacy_types(self):
        signal = deepcopy(self.data['signals'][0])
        for basis, value in [('interest', 0), ('enjoyment', 6), ('interest', True), ('interest', '5'),
                ('enjoyment', 3.0), ('favourite', False), ('favourite', 1), ('favourite', 'true'),
                ('legacy_choice', 5), ('legacy_choice', True), ('legacy_choice', ''), ('other', 3)]:
            with self.subTest(basis=basis, value=value):
                self.data['signals'] = [dict(signal, basis=basis, value=value)]
                self.assertEqual(self.post().status_code, 409)
        self.assertFalse(SynthesisRun.objects.exists())

    def test_completed_after_input_change_is_retained_only_as_historical(self):
        row, _ = self.reserve(self.queued())
        original = deepcopy(row.input_snapshot)
        self.data['revision_map']['discovery'] = 5
        row = self.record(row)
        self.assertEqual(row.input_snapshot, original)
        self.assertEqual(synthesis.status(row, self.current()['input_digest'])['state'], 'stale')

    def test_operator_cannot_record_before_reservation_and_missing_guard_refused(self):
        row = self.queued()
        with self.assertRaises(Blocked):
            self.record(row, {'outcome': 'completed'})
        with self.assertRaises(Blocked):
            self.reserve(row, session_guard=None)

    def test_authority_is_checked_again_after_snapshot_and_before_reservation_commit(self):
        original = self.current
        def revoke_during_snapshot(*args):
            value = original(*args)
            Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
            return value
        with patch('shelf.taste_funnel.synthesis_snapshot', side_effect=revoke_during_snapshot):
            self.assertEqual(self.post().status_code, 404)
        self.assertFalse(SynthesisRun.objects.exists())
        row = self.queued()
        calls = []
        def expires_before_commit():
            calls.append(True)
            if len(calls) == 2:
                raise ValueError('expired before reservation commit')
        with self.assertRaises(ValueError):
            self.reserve(row, session_guard=expires_before_commit)
        row.refresh_from_db()
        self.assertEqual((row.state, row.attempts), ('queued', 0))


class SynthesisCLIContractTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tools = Path(__file__).resolve().parents[3] / 'tools'
        sys.path.insert(0, str(tools))
        try:
            spec = importlib.util.spec_from_file_location('synthesis_cli_test', tools / 'record_taste_synthesis.py')
            cls.cli = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.cli)
        finally:
            sys.path.remove(str(tools))

    def test_cli_fixed_path_refuses_before_reading_session_or_input(self):
        with patch.object(self.cli, 'SERVER') as server:
            with self.assertRaisesRegex(ValueError, 'Deployed operator path'):
                self.cli.run(SimpleNamespace())
            server.read_text.assert_not_called()

    def test_cli_bounded_session_and_duplicate_nonfinite_or_oversize_json(self):
        now = datetime.now(timezone.utc)
        server = {'expires_utc': (now+timedelta(minutes=10)).isoformat(), 'renewal_id': 'synthetic'}
        valid = SimpleNamespace(expires=(now+timedelta(minutes=5)).isoformat(), renewal='synthetic')
        self.cli.validate_session(valid, server, now)
        for args in [SimpleNamespace(expires=(now-timedelta(seconds=1)).isoformat(), renewal='synthetic'),
                SimpleNamespace(expires=(now+timedelta(minutes=11)).isoformat(), renewal='synthetic'),
                SimpleNamespace(expires=valid.expires, renewal='stale')]:
            with self.assertRaises(ValueError):
                self.cli.validate_session(args, server, now)
        for value in [b'{"a":1,"a":2}', b'{"a":NaN}', b' ' * 131073]:
            with self.assertRaises(ValueError):
                self.cli.read_evidence(io.BytesIO(value))
