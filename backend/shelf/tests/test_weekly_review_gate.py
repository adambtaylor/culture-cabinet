"""A scheduled snapshot cannot reserve inference before current human review."""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase, override_settings

from shelf import synthesis
from shelf.acquisition_contract import Blocked
from shelf.models import SynthesisRun
from shelf.tests import test_synthesis as fixtures


@override_settings(ROOT_URLCONF='shelf.tests.test_synthesis', ALLOWED_HOSTS=['testserver'])
class WeeklyReviewGateTests(TestCase):
    setUp = fixtures.SynthesisTests.setUp
    current = fixtures.SynthesisTests.current
    post = fixtures.SynthesisTests.post
    reserve = fixtures.SynthesisTests.reserve
    evidence = fixtures.SynthesisTests.evidence
    record = fixtures.SynthesisTests.record
    # Use the existing real CSRF/session setup without inheriting its test cases.
    def weekly(self):
        value = self.current()
        return SynthesisRun.objects.create(actor=self.actor, profile=self.profile,
            input_digest=value['input_digest'], input_snapshot=value,
            evidence={'scheduled_review': {'due_utc': '2026-11-01T21:00:00+00:00',
                'approved': False, 'automatic_model_call_cap': 0}})

    def test_weekly_pending_review_blocks_operator_then_explicit_current_post_approves(self):
        row = self.weekly()
        self.assertEqual(synthesis.status(row, row.input_digest)['state'], 'pending_review')
        with self.assertRaises(Blocked):
            self.reserve(row)
        row.refresh_from_db()
        self.assertEqual(row.attempts, 0)
        self.assertEqual(self.post().status_code, 200)
        row.refresh_from_db()
        self.assertTrue(row.evidence['scheduled_review']['approved'])
        self.assertEqual(row.evidence['scheduled_review']['approved_by'], 'user:1')
        self.assertEqual(synthesis.status(row, row.input_digest)['state'], 'awaiting_operator')
        approval = deepcopy(row.evidence['scheduled_review'])
        reserved, packet = self.reserve(row)
        self.assertFalse(packet['reconciliation_only'])
        self.assertEqual(reserved.attempts, 1)
        self.assertTrue(reserved.evidence['scheduled_review']['approved'])
        self.assertEqual(reserved.evidence['scheduled_review']['due_utc'], '2026-11-01T21:00:00+00:00')
        self.assertEqual(reserved.evidence['scheduled_review'], approval)
        self.assertTrue(self.reserve(reserved)[1]['reconciliation_only'])
        recorded = self.record(reserved)
        self.assertEqual(recorded.evidence['scheduled_review'], approval)
        self.assertEqual(self.record(recorded).revision, recorded.revision)

    def test_stale_or_unauthenticated_post_cannot_approve_weekly_snapshot(self):
        row = self.weekly()
        self.data['revision_map']['discovery'] = 99
        self.assertEqual(self.post(digest=row.input_digest).status_code, 409)
        row.refresh_from_db()
        self.assertFalse(row.evidence['scheduled_review']['approved'])
        self.assertEqual(synthesis.status(row, self.current()['input_digest'])['state'], 'stale')
        self.client.logout()
        self.client.get('/')
        # This synthetic service fixture uses authority denial, not the production login decorator.
        self.assertEqual(self.post().status_code, 404)
        row.refresh_from_db()
        self.assertFalse(row.evidence['scheduled_review']['approved'])
