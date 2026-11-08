"""Injected selected observer compatibility; no grants, store reads or transport."""
from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock

from shelf.acquisition_backend import Backend
from shelf.acquisition_contract import Blocked
from shelf.acquisition_pipeline import AdapterUnavailable, Completion, ProviderJob
from . import test_acquisition_pipeline as fixture


class SelectedProviderObservationTests(TestCase):
    def setUp(self):
        self.snapshot = fixture.Snapshot.from_saved_job(fixture.saved_job(
            medium='tv', scope='episode_s01e02'))
        self.evidence = fixture.evidence_for(self.snapshot)
        self.store = Mock(side_effect=AssertionError('No credential read admitted by poll'))
        self.backend = Backend(_open_store=self.store)
        self.job = ProviderJob(self.snapshot, 'SABnzbd_nzo_fixture')

    def provider(self, observer=None):
        return self.backend.provider(actor_id=11, profile_id=22, snapshot=self.snapshot,
            selection=self.evidence.selection, nzb=b'synthetic not submitted', selected_observer=observer)

    def test_default_remains_unavailable_and_unknown_lookup_never_resends(self):
        provider = self.provider()
        with self.assertRaises(AdapterUnavailable):
            provider.poll(self.job)
        with self.assertRaises(AdapterUnavailable):
            provider.lookup(self.snapshot)
        self.store.assert_not_called()

    def test_exact_pending_or_legacy_evidence_delegates_without_key_or_api(self):
        observer = Mock(return_value=None)
        provider = self.provider(observer)
        self.assertIsNone(provider.poll(self.job))
        observer.assert_called_once_with(self.job)
        result = Completion(self.job, 'completed', True, True, 0, None)
        observer.return_value = result
        self.assertIs(provider.poll(self.job), result)
        self.store.assert_not_called()

    def test_foreign_job_unknown_or_richer_unobserved_result_cannot_reach_legacy_pipeline(self):
        observer = Mock(return_value=True)
        provider = self.provider(observer)
        with self.assertRaises(AdapterUnavailable):
            provider.poll(replace(self.job, snapshot=replace(self.snapshot, profile_id=99)))
        observer.assert_not_called()
        with self.assertRaises(AdapterUnavailable):
            provider.poll(self.job)
        observer.return_value = Completion(self.job, 'completed', 'unobserved', True, 0, None)
        with self.assertRaises(AdapterUnavailable):
            provider.poll(self.job)
        with self.assertRaises(Blocked):
            self.provider(True)
        self.store.assert_not_called()
