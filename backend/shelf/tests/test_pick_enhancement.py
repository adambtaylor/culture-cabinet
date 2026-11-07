"""Synthetic association consumers: inference never invents candidate inventory."""
from types import SimpleNamespace
from unittest import TestCase

from shelf.pick_enhancement import consume, positive_keys, progress


class EnhancementConsumerTests(TestCase):
    def run_fixture(self, signals=None):
        return SimpleNamespace(input_snapshot={'signals': signals or [
            {'key': 'owner-root', 'basis': 'anticipated_reaction', 'value': 'love_it'}]},
            output={'clusters': [{'members': ['owner-root'], 'label': 'Synthetic association',
                                   'explanation': 'Synthetic hypothesis'}]},
            input_digest='current', state='completed', pk='synthetic-run', attempts=1)

    def test_only_source_roots_in_actual_cluster_support_reordering(self):
        cards = {'unrelated': {'roots': []}, 'supported': {'roots': ['owner-root']}}
        after, why = consume(self.run_fixture(), cards, ['unrelated'])
        self.assertEqual(after, ['supported', 'unrelated'])
        self.assertEqual(why['supported']['anchor_keys'], ['owner-root'])
        self.assertNotIn('unrelated', why)

    def test_model_text_and_invented_ids_cannot_add_inventory(self):
        run = self.run_fixture()
        run.output['clusters'][0]['members'] = ['invented']
        after, why = consume(run, {'cached': {'roots': ['owner-root']}}, ['cached'])
        self.assertEqual(after, ['cached'])
        self.assertEqual(why, {})

    def test_negative_conflict_vetoes_positive_anchor_and_notes_are_not_scored(self):
        signals = [
            {'key': 'a', 'basis': 'anticipated_reaction', 'value': ['love_it', 'hate_it']},
            {'key': 'b', 'basis': 'feedback_note', 'value': 'love_it'},
            {'key': 'a', 'basis': 'favourite', 'value': True}]
        self.assertEqual(positive_keys(signals), set())

    def test_empty_pool_honestly_returns_no_picks(self):
        self.assertEqual(consume(self.run_fixture(), {}, []), ([], {}))

    def test_cancelled_consumer_does_not_claim_external_cancellation(self):
        result = progress(self.run_fixture(), {'state': 'cancelled', 'request_id': 'request'}, 'current')
        self.assertEqual(result['state'], 'cancelled')
        self.assertFalse(result['external_work_cancelled'])

    def test_changed_input_is_historical_even_with_completed_result(self):
        result = progress(self.run_fixture(), {'state': 'pending', 'request_id': 'request'}, 'edited')
        self.assertEqual(result['state'], 'stale')


from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase as DatabaseTestCase
from shelf import pick_enhancement as enhancement
from shelf.discovery_state import Conflict
from shelf.models import Grant, Profile, SynthesisRun
from shelf.calendar_handoff import digest


class EnhancementBindingTests(DatabaseTestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='demo-curator')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Synthetic owner')
        Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.binding = {'actor_id': 1, 'profile_id': 1, 'category': 'book',
            'input_digest': 'synthetic-current', 'before_ids': ['cached'], 'pool_digest': 'synthetic-pool'}
        self.request = SimpleNamespace(user=self.actor, method='POST')
        self.row = SynthesisRun.objects.create(actor=self.actor, profile=self.profile,
            input_digest='synthetic-current', input_snapshot={'signals': [], 'revision_map': {}},
            evidence={})
        self.enterContext(patch.object(enhancement, 'preview', return_value=self.binding))
        self.enterContext(patch.object(enhancement, 'public_authorized'))
        self.enterContext(patch.object(enhancement.synthesis, 'request', return_value=self.row))

    def token(self, **changes):
        return signing.dumps({**self.binding, **changes}, salt=enhancement.SALT)

    def test_signed_preview_binds_actor_category_pool_and_visible_before_ids(self):
        for field, value in [('actor_id', 2), ('category', 'movie'),
                             ('pool_digest', 'changed'), ('before_ids', ['invented'])]:
            with self.subTest(field=field), self.assertRaises(Conflict):
                enhancement.enroll(self.request, 1, 'book', self.token(**{field: value}), 'synthetic-current')
        self.row.refresh_from_db()
        self.assertEqual(self.row.evidence, {})

    def test_repeated_explicit_request_reuses_one_consumer(self):
        _, first = enhancement.enroll(self.request, 1, 'book', self.token(), 'synthetic-current')
        _, second = enhancement.enroll(self.request, 1, 'book', self.token(), 'synthetic-current')
        self.assertEqual(first['request_id'], second['request_id'])
        self.row.refresh_from_db()
        self.assertEqual(list(self.row.evidence['enhancements']), ['book'])
        self.assertEqual(self.row.attempts, 0)

    def test_ready_excludes_cancelled_stale_and_reserved_without_disclosing_signals(self):
        self.row.evidence = {'enhancements': {'book': {'request_id': 'approved',
            'human_approved': True, 'state': 'pending'}, 'movie': {'request_id': 'cancelled',
            'human_approved': True, 'state': 'cancelled'}}}
        self.row.save()
        with patch.object(enhancement.synthesis, 'snapshot', return_value={'input_digest': 'synthetic-current'}):
            result = enhancement.ready(actor=self.actor, profile_id=1, session_guard=lambda: None)
            self.assertEqual(result, {'state': 'ready', 'jobs': [{'run_id': str(self.row.pk),
                'input_digest': 'synthetic-current', 'model': 'gpt-6-astra', 'reasoning': 'high',
                'request_ids': ['approved']}]})
            self.row.state, self.row.attempts = 'running', 1
            self.row.save()
            self.assertEqual(enhancement.ready(actor=self.actor, profile_id=1,
                session_guard=lambda: None), {'state': 'ready', 'jobs': []})

    def test_changed_pool_enrolls_new_consumer_and_preserves_stale_receipt(self):
        _, first = enhancement.enroll(self.request, 1, 'book', self.token(), 'synthetic-current')
        self.binding['pool_digest'] = 'new-cached-pool'
        _, second = enhancement.enroll(self.request, 1, 'book', self.token(), 'synthetic-current')
        self.assertNotEqual(first['request_id'], second['request_id'])
        self.row.refresh_from_db()
        archived = self.row.evidence['enhancement_archive'][0]
        self.assertEqual(archived['request_id'], first['request_id'])
        self.assertEqual(enhancement.progress(self.row, archived, 'synthetic-current')['state'], 'stale')
        self.assertEqual(self.row.attempts, 0)

    def applied_consumer(self):
        self.row.state = 'completed'
        self.row.attempts = 1
        self.row.evidence = {'reservation': {'revision': 1}}
        record = {**enhancement.synthesis.binding(self.row), 'outcome': 'completed', 'output': self.row.output,
                  'evidence_id': 'synthetic:applied-consumer', 'reason': ''}
        self.row.evidence.update(record=record, record_digest=digest(record), enhancements={'book': {
            **self.binding, 'state': 'applied', 'request_id': 'applied',
            'after_ids': ['new-cached'], 'why': {}, 'result_record_digest': digest(record),
            'applied_input_digest': 'synthetic-current'}})
        self.row.save()

    def test_application_survives_visible_reordering_but_not_pool_changes(self):
        self.applied_consumer()
        self.binding['before_ids'] = ['new-cached']
        with patch.object(enhancement, 'pool', return_value=({'new-cached': {}}, ['new-cached'])):
            self.assertEqual(enhancement.current_application(self.request, 1, 'book'), (['new-cached'], {}))
            self.binding['pool_digest'] = 'changed'
            self.assertEqual(enhancement.current_application(self.request, 1, 'book'), ([], {}))

    def test_applied_read_helper_checks_current_select_grant(self):
        self.applied_consumer()
        self.assertIsNotNone(enhancement.applied_result(self.actor, 1, 'book', 'synthetic-current'))
        Grant.objects.filter(user=self.actor, profile=self.profile).update(can_select=False)
        self.assertIsNone(enhancement.applied_result(self.actor, 1, 'book', 'synthetic-current'))

    def test_catalogue_all_never_admits_artist_pool(self):
        state = {'candidates': {}, 'choices': {}, 'current': []}
        with patch.object(enhancement, 'catalogue_state', return_value=state), \
                patch('shelf.discovery_views.candidates') as artists:
            self.assertEqual(enhancement.pool(self.request, 1, 'all'), ({}, []))
            artists.assert_not_called()

    def test_context_reports_actual_pending_state_and_action_urls(self):
        enhancement.enroll(self.request, 1, 'book', self.token(), 'synthetic-current')
        with patch.object(enhancement, 'reverse', side_effect=lambda name, kwargs: '/' + name):
            result = enhancement.context(self.request, 1, 'book')
        self.assertEqual(result['enhancement']['state'], 'awaiting_authorized_worker')
        self.assertEqual(result['enhancement']['label'], 'Enhancement queued')
        self.assertEqual(result['enhancement']['apply_url'], '/enhancement_apply')
        self.assertEqual(result['enhancement']['cancel_url'], '/enhancement_cancel')

    def test_applied_order_accepts_only_approved_before_or_recorded_after(self):
        self.applied_consumer()
        consumer = self.row.evidence['enhancements']['book']
        self.assertTrue(enhancement.consumer_current(self.row, consumer, self.binding))
        self.assertTrue(enhancement.consumer_current(self.row, consumer,
            {**self.binding, 'before_ids': ['new-cached']}))
        self.assertFalse(enhancement.consumer_current(self.row, consumer,
            {**self.binding, 'before_ids': ['newer-visible-deck']}))

    def test_changed_output_or_record_binding_cannot_be_materialized(self):
        self.applied_consumer()
        self.row.output = {'clusters': []}
        self.assertFalse(enhancement.recorded_result(self.row))
        self.applied_consumer()
        self.row.evidence['record']['model'] = 'another-model'
        self.row.evidence['record_digest'] = digest(self.row.evidence['record'])
        self.assertFalse(enhancement.recorded_result(self.row))

    def test_stale_consumer_retains_unknown_receipt_for_reconciliation(self):
        self.row.state, self.row.attempts = 'unknown', 1
        consumer = {**self.binding, 'state': 'pending', 'request_id': 'uncertain'}
        result = enhancement.consumer_progress(self.row, consumer, {**self.binding, 'pool_digest': 'expired'})
        self.assertEqual(result['state'], 'stale')
        self.assertEqual(result['receipt_state'], 'unknown')
        self.assertEqual(result['attempts'], 1)
