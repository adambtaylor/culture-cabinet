"""Dynamic upstream articles must not change the bytes approved for submission."""
from datetime import timedelta
from dataclasses import replace
from unittest.mock import Mock, patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from shelf import obtain_acquisition as acquire
from shelf.acquisition_contract import Blocked, ConfirmedSelection, ContractError
from shelf.acquisition_pipeline import Evidence, Snapshot
from shelf.models import Grant, ObtainReviewedNZB, ObtainTransfer
from shelf.reviewed_nzb import ReviewedRetriever
from shelf.tests import test_obtain_acquisition as fixtures
from shelf.tests.test_obtain_acquisition import FixtureBackend
from shelf.tests.test_obtain_retrieval import NZB
from shelf.tests.test_saved import SavedFixture


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class ReviewedDescriptorTests(SavedFixture, TransactionTestCase):
    choice = fixtures.ObtainAcquisitionTests.choice

    def setUp(self):
        super().setUp()
        self.job = self.choice()
        self.context = acquire.OperatorContext(1, 1, 'offline_test', True)
        self.source = Mock()
        self.source.retrieve.return_value = NZB
        self.retriever = ReviewedRetriever(self.source)
        self.backend = FixtureBackend()
        snap = Snapshot.from_saved_job(self.job)
        self.evidence = Evidence(snap, ConfirmedSelection('tmdb:movie:11', 'film',
            snap.edition, snap.storage, True, True, True, True, True, True))
        self.review = self.prepare()

    def prepare(self):
        return acquire.review_submission(actor=self.owner, profile_id=1, job_id=self.job.pk,
            context=self.context, evidence=self.evidence, max_bytes=20000,
            retriever=self.retriever)

    def submit(self, **changes):
        return acquire.submit(**(dict(actor=self.owner, profile_id=1, job_id=self.job.pk,
            context=self.context, token=self.review['token'], confirmed=True,
            retriever=self.retriever, backend=self.backend) | changes))

    def test_changing_upstream_is_not_refetched_and_exact_reviewed_bytes_are_sent_once(self):
        self.source.retrieve.side_effect = AssertionError('confirmation must not refetch')
        self.assertEqual(self.submit().state, 'queued')
        self.assertEqual(self.backend.binding['nzb'], NZB)
        self.assertEqual(self.submit().state, 'queued')
        self.assertEqual(self.source.retrieve.call_count, 1)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertNotIn('payload', str(ObtainTransfer.objects.get().binding))

    def test_changed_cached_bytes_or_identity_never_enqueue(self):
        for changes in ({'payload': NZB + b'changed'}, {'exact': {}},
                        {'expires_at': timezone.now() - timedelta(seconds=1)}):
            self.review = self.prepare()
            ObtainReviewedNZB.objects.update(**changes)
            with self.subTest(changes=list(changes)), self.assertRaises(ContractError):
                self.submit()
        ObtainReviewedNZB.objects.all().delete()
        with self.assertRaises(Blocked): self.submit()
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_second_review_invalidates_old_token_without_submitting_different_bytes(self):
        self.source.retrieve.return_value = NZB + b'\n'
        latest = self.prepare()
        with self.assertRaises(ContractError): self.submit()
        self.review = latest
        self.assertEqual(self.submit().state, 'queued')
        self.assertEqual(self.backend.binding['nzb'], NZB + b'\n')
        self.assertEqual(self.source.retrieve.call_count, 2)

    def test_current_ownership_and_cancellation_still_gate_cached_descriptor(self):
        from django.http import Http404
        Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
        with self.assertRaises(Http404): self.submit()
        Grant.objects.filter(pk=self.grant.pk).update(can_select=True)
        from shelf import saved
        saved.cancel(actor=self.owner, profile_id=1, job_id=self.job.pk)
        with self.assertRaises(Blocked): self.submit()
        self.assertEqual(self.backend.calls, [])

    def test_cache_is_bounded_and_expired_rows_are_pruned_on_review(self):
        row = ObtainReviewedNZB.objects.get()
        self.assertEqual(bytes(row.payload), NZB)
        self.assertLessEqual((row.expires_at - timezone.now()).total_seconds(), 300)
        self.source.retrieve.return_value = b'x' * (1024 * 1024 + 1)
        with self.assertRaises(ContractError): self.prepare()
        self.assertEqual(bytes(ObtainReviewedNZB.objects.get().payload), NZB)
