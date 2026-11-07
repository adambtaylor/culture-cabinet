"""Synthetic candidate submission, durable dispatch, and known-path receipts."""
from dataclasses import replace
from hashlib import sha256
import importlib
import os
import subprocess
import sys
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from unittest.mock import Mock, patch

from django.core import signing
from django.db import IntegrityError, OperationalError, connection, connections, transaction
from django.db.backends.sqlite3._functions import register
from django.http import Http404
from django.test import TransactionTestCase, override_settings

from shelf import obtain_acquisition as acquire, obtain_preview as preview, saved
from shelf.acquisition_backend import Backend, PrivateSearch
from shelf.acquisition_contract import Blocked, ConfirmedSelection, ContractError
from shelf.acquisition_pipeline import (AdapterUnavailable, Completion, Evidence,
    ProviderJob, QueueRejected, scope_family, Snapshot, StagedFile)
from shelf.models import AcquisitionJob, Grant, ObtainLocalFile, ObtainTransfer
from shelf.tests.test_obtain_preview import item, payload
from shelf.tests.test_saved import SavedFixture

NZB = b'<?xml version="1.0"?><nzb><file subject="synthetic fixture"/></nzb>'
MEDIA = b'Synthetic local file: no actual media, codec, rights or playback evidence.\n'


class FixtureRetriever:
    def __init__(self):
        self.calls = []
        self.payload = NZB
        self.change = lambda result: result

    def retrieve(self, exact):
        self.calls.append(exact)
        return acquire.RetrievedNZB(self.change(exact), self.payload)


class FixtureBackend:
    def __init__(self):
        self.calls = []
        self.error = None
        self.action = None

    def provider(self, **binding):
        self.binding = binding
        return self

    def enqueue(self, snapshot, request):
        self.calls.append((snapshot, request))
        if self.action:
            self.action()
        if self.error:
            raise self.error
        return ProviderJob(snapshot, 'SABnzbd_nzo_synthetic')


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class ObtainAcquisitionTests(SavedFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.context = acquire.OperatorContext(1, 1, 'offline_test', enabled=True)
        self.retriever, self.backend = FixtureRetriever(), FixtureBackend()
        self.folder = tempfile.TemporaryDirectory(prefix='obtain-local-')
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'synthetic.mkv'
        self.path.write_bytes(MEDIA)
        self.job = self.choice()
        snapshot = Snapshot.from_saved_job(self.job)
        media, episode = scope_family(snapshot.provider, snapshot.medium, snapshot.scope)
        self.evidence = Evidence(snapshot, ConfirmedSelection(
            f'{snapshot.provider}:{snapshot.medium}:{snapshot.source_id}', media,
            snapshot.edition, snapshot.storage, True, True, True, True, True, True, episode))
        self.submission_review = self.reviewed()

    def choice(self, medium='movie', title='Fiction Film 2024 1080p', category='2000', guid='-9223372036854775808', season=None, episode=None):
        saved_item = self.item(medium)
        backend = Mock()
        backend.search.return_value = PrivateSearch(payload(item(title=title, category=category, guid=guid)))
        receipt = preview.search(actor=self.owner, profile_id=1, item_id=saved_item.pk,
                                 factory=lambda **kw: backend, season=season, episode=episode)
        result = receipt.results.get()
        job, _ = preview.confirm(actor=self.owner, profile_id=1, preview_id=receipt.pk,
            result_id=result.pk, data={'review_token': preview.review_token(receipt, result),
            'scope': receipt.scope, 'edition': result.metadata['title'], 'authorized_content': 'on',
            'storage': 'local_library', 'storage_check': 'on'})
        return job

    def reviewed(self, **changes):
        args = dict(actor=self.owner, profile_id=1, job_id=self.job.pk,
                    context=self.context, evidence=self.evidence, max_bytes=20000,
                    retriever=self.retriever)
        return acquire.review_submission(**(args | changes))

    def submit(self, **changes):
        args = dict(actor=self.owner, profile_id=1, job_id=self.job.pk,
                    context=self.context, token=self.submission_review['token'], confirmed=True,
                    retriever=self.retriever, backend=self.backend)
        return acquire.submit(**(args | changes))

    def completion(self, **changes):
        job = ProviderJob(self.evidence.snapshot, 'SABnzbd_nzo_synthetic')
        staged = StagedFile(job, str(self.path), sha256(MEDIA).hexdigest(), len(MEDIA), True, False)
        evidence = acquire.LocalCompletion(Completion(job, 'completed', True, True, 0, staged),
            self.submission_review['binding']['exact']['digest'], self.submission_review['binding']['nzb_sha256'], str(self.path))
        return replace(evidence, **changes)

    def handoff(self, evidence=None, **changes):
        args = dict(actor=self.owner, profile_id=1, job_id=self.job.pk,
                    context=self.context, evidence=evidence or self.completion())
        return acquire.handoff(**(args | changes))

    def test_repeat_click_reconstructed_service_queues_once_and_persists_exact_bytes(self):
        transfer = self.submit()
        self.assertEqual(transfer.state, 'queued')
        self.assertEqual(transfer.binding['nzb_sha256'], sha256(NZB).hexdigest())
        self.assertEqual(self.backend.binding['nzb'], NZB)
        self.assertEqual(transfer.provider_job, 'SABnzbd_nzo_synthetic')
        # A new service/store instance reads the database receipt, never retrieves
        # or enqueues again, even after process-local provider state is discarded.
        importlib.reload(acquire)
        second = FixtureBackend()
        with patch.object(self.retriever, 'retrieve', side_effect=AssertionError('no retry')):
            again = self.submit(backend=second)
        self.assertEqual(again.pk, transfer.pk)
        self.assertEqual(second.calls, [])
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(acquire.DjangoCheckpoints(again).load(str(self.job.request_id)).job.job_id,
                         transfer.provider_job)
        self.assertNotIn('payload', str(transfer.binding))
        self.assertEqual(saved.job_context(self.job)['status'], 'queued')

    def test_unconfirmed_disabled_saved_only_and_default_retriever_never_enqueue(self):
        for changes in ({'confirmed': False}, {'context': replace(self.context, enabled=False)},
                        {'context': replace(self.context, purpose='web')}, {'token': 'tampered'}):
            with self.subTest(changes=changes), self.assertRaises((Blocked, ContractError)):
                self.submit(**changes)
        with self.assertRaises(Blocked):
            self.reviewed(retriever=None)
        saved_item = self.item('book')
        legacy, _ = saved.confirm(actor=self.owner, profile_id=1, item_id=saved_item.pk,
                                  data=self.confirmation(saved_item))
        with self.assertRaises(Blocked):
            self.reviewed(job_id=legacy.pk)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(ObtainTransfer.objects.count(), 0)

    def test_exact_result_nzb_and_token_tampering(self):
        for field, value in (('guid', '123'), ('result_id', 'other'), ('digest', '0'*64),
                             ('size', 123)):
            self.retriever.change = lambda exact, f=field, v=value: replace(exact, **{f: v})
            with self.subTest(field=field), self.assertRaises(ContractError):
                self.submit()
        self.retriever.change = lambda result: result
        self.retriever.payload = NZB + b'changed'
        with self.assertRaises(ContractError): self.submit()
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_ownership_and_revocation_before_and_after_retrieval(self):
        for actor, profile_id in ((self.other, 1), (self.owner, 2)):
            with self.assertRaises((Blocked, Http404)):
                self.submit(actor=actor, profile_id=profile_id)
        other_context = acquire.OperatorContext(2, 2, 'offline_test', enabled=True)
        with self.assertRaises(Http404):
            self.submit(actor=self.other, profile_id=2, context=other_context)
        original = self.retriever.retrieve
        def revoke(exact):
            Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
            return original(exact)
        with patch.object(self.retriever, 'retrieve', side_effect=revoke), self.assertRaises(Http404):
            self.submit()
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_expired_review_and_preview_nonce_changes(self):
        with patch('django.core.signing.time.time', return_value=time.time()+301):
            with self.assertRaises(Blocked): self.submit()
        from shelf.models import ObtainPreview
        ObtainPreview.objects.filter(pk=self.job.request_id).update(version=2)
        with self.assertRaises(ContractError): self.submit()
        self.assertEqual(self.backend.calls, [])

    def test_lost_response_is_durable_unknown_with_no_retry_or_completion_claim(self):
        self.backend.error = AdapterUnavailable('synthetic response lost')
        transfer = self.submit()
        self.assertEqual(transfer.state, 'outcome_unknown')
        self.assertEqual(transfer.provider_job, '')
        self.backend.error = None
        self.assertEqual(self.submit().state, 'outcome_unknown')
        self.assertEqual(len(self.backend.calls), 1)
        with self.assertRaises(Blocked): self.handoff()
        self.assertFalse(ObtainLocalFile.objects.exists())

    def test_crash_after_queueing_preserves_intent_and_refuses_retry(self):
        self.backend.error = RuntimeError('synthetic crash after remote acceptance')
        with self.assertRaises(RuntimeError): self.submit()
        self.assertEqual(ObtainTransfer.objects.get().state, 'outcome_unknown')
        self.assertEqual(self.submit().state, 'outcome_unknown')
        self.assertEqual(len(self.backend.calls), 1)

    def test_failure_saving_ack_keeps_unknown(self):
        original = acquire.DjangoCheckpoints.save
        def fail_ack(store, checkpoint):
            if checkpoint.state == 'queued':
                raise OSError('synthetic checkpoint write failure')
            return original(store, checkpoint)
        with patch.object(acquire.DjangoCheckpoints, 'save', fail_ack), self.assertRaises(OSError):
            self.submit()
        self.assertEqual(self.submit().state, 'outcome_unknown')
        self.assertEqual(len(self.backend.calls), 1)

    def test_rejected_queue_is_not_implicitly_retried(self):
        self.backend.error = QueueRejected('synthetic definite rejection')
        self.assertEqual(self.submit().state, 'failed')
        self.assertEqual(self.submit().state, 'failed')
        self.assertEqual(len(self.backend.calls), 1)

    def test_dispatch_intent_visible_to_independent_sqlite_connection_before_send(self):
        with tempfile.TemporaryDirectory(prefix='obtain-durable-') as folder:
            path = Path(folder) / 'synthetic.sqlite3'
            raw = sqlite3.connect(path, isolation_level=None)
            connection.connection.backup(raw)
            register(raw)
            def observe():
                observer = sqlite3.connect(path)
                try:
                    state = observer.execute('SELECT state FROM shelf_obtaintransfer').fetchone()[0]
                    self.assertEqual(state, 'outcome_unknown')
                finally: observer.close()
            self.backend.action = observe
            try:
                with patch.object(connection, 'connection', raw):
                    self.submit()
            finally: raw.close()
        self.assertEqual(len(self.backend.calls), 1)

    def test_stale_checkpoint_owner_cannot_send_or_overwrite_cancel(self):
        transfer = self.submit()
        stale = acquire.DjangoCheckpoints(transfer)
        checkpoint = stale.load(str(self.job.request_id))
        saved.cancel(actor=self.owner, profile_id=1, job_id=self.job.pk)
        with self.assertRaises(ContractError): stale.save(checkpoint)
        transfer.refresh_from_db()
        self.assertEqual(transfer.state, 'cancel_pending')
        self.job.refresh_from_db()
        self.assertIsNotNone(self.job.active_key)
        with self.assertRaises(Blocked): self.handoff()
        self.assertEqual(self.submit().state, 'cancel_pending')
        self.assertEqual(len(self.backend.calls), 1)

    def test_cancel_before_dispatch_never_submits_and_unknown_cancel_is_only_local(self):
        saved.cancel(actor=self.owner, profile_id=1, job_id=self.job.pk)
        with self.assertRaises(Blocked): self.submit()
        self.assertEqual(self.backend.calls, [])

    def test_verified_local_receipt_repeat_is_idempotent_and_has_no_emby_identity(self):
        self.submit()
        receipt = self.handoff()
        self.assertEqual(self.handoff().pk, receipt.pk)
        self.assertEqual(ObtainLocalFile.objects.count(), 1)
        self.assertEqual(receipt.path, str(self.path))
        self.assertEqual(receipt.sha256, sha256(MEDIA).hexdigest())
        transfer = ObtainTransfer.objects.get()
        self.assertEqual(transfer.state, 'local_verified')
        self.assertEqual(transfer.reason, 'awaiting_library_index')
        self.assertFalse(hasattr(receipt, 'emby_id'))
        saved.cancel(actor=self.owner, profile_id=1, job_id=self.job.pk)
        self.assertTrue(self.path.exists())
        self.assertEqual(ObtainLocalFile.objects.count(), 1)

    def test_selection_without_preverification_does_not_bypass_postdownload_completeness(self):
        self.evidence = replace(self.evidence, selection=replace(self.evidence.selection,
            edition_verified=False, complete_work_verified=False))
        self.submission_review = self.reviewed()
        self.assertEqual(self.submit().state, 'queued')
        good = self.completion()
        incomplete = replace(good, completion=replace(good.completion,
            staged=replace(good.completion.staged, complete_work=False)))
        with self.assertRaises(ContractError):
            self.handoff(incomplete)
        self.assertFalse(ObtainLocalFile.objects.exists())
        self.assertEqual(ObtainTransfer.objects.get().state, 'queued')

    def test_local_handoff_checks_complete_root_before_reading_any_bytes(self):
        transfer = self.submit()
        transfer.binding['complete_root'] = str(self.path.parent / 'different-complete-root')
        transfer.save(update_fields=['binding'])
        with patch.object(acquire, 'verify_staging') as verify:
            with self.assertRaises(ContractError):
                self.handoff()
        verify.assert_not_called()
        self.assertFalse(ObtainLocalFile.objects.exists())

    def test_observation_time_cannot_be_relabelled_as_acquisition_time(self):
        from django.utils import timezone
        self.submit()
        for metadata in (None, {'source': 'synthetic'},
                         {'source': 'synthetic', 'acquired_at_basis': 'completion_observed_at'}):
            with self.assertRaises(ContractError):
                self.handoff(self.completion(acquired_at=timezone.now(), metadata=metadata))
        self.assertFalse(ObtainLocalFile.objects.exists())
        local = self.handoff()
        # Legacy receipts without explicit authoritative provenance cannot export
        # their stored timestamp under the new acquisition-time contract.
        local.acquired_at = timezone.now()
        local.save(update_fields=['acquired_at'])
        exported = acquire.export_local(actor=self.owner, profile_id=1, job_id=self.job.pk)
        self.assertIsNone(exported['acquired_at'])
        self.assertEqual(exported['acquired_at_basis'], 'unavailable')

    def test_partial_symlink_directory_symlink_oversize_and_hash_mismatch(self):
        self.submit()
        good = self.completion()
        staged = good.completion.staged
        cases = [replace(staged, partial=True), replace(staged, complete_work=False),
                 replace(staged, size=20001), replace(staged, sha256='0'*64)]
        for name in ('synthetic.part', 'synthetic.part.mkv', 'synthetic.partial', 'synthetic.tmp'):
            path = self.path.with_name(name); path.write_bytes(MEDIA)
            cases.append(replace(staged, path=str(path)))
        link = self.path.with_name('link.mkv'); link.symlink_to(self.path)
        cases.append(replace(staged, path=str(link)))
        directory_link = self.path.parent / 'linked'; directory_link.symlink_to(self.path.parent)
        cases.append(replace(staged, path=str(directory_link / self.path.name)))
        cases.append(replace(staged, path=str(self.path.parent)))
        fifo = self.path.parent / 'fifo'; import os; os.mkfifo(fifo)
        cases.append(replace(staged, path=str(fifo)))
        for candidate in cases:
            evidence = replace(good, expected_path=candidate.path,
                               completion=replace(good.completion, staged=candidate))
            with self.subTest(path=candidate.path), self.assertRaises(ContractError):
                self.handoff(evidence)
        self.assertFalse(ObtainLocalFile.objects.exists())
        self.assertEqual(ObtainTransfer.objects.get().state, 'queued')

    def test_wrong_completion_identity_provenance_and_expected_path(self):
        self.submit()
        good = self.completion()
        wrong_job = replace(good.completion.job, job_id='SABnzbd_nzo_other')
        for evidence in (replace(good, result_digest='0'*64), replace(good, nzb_sha256='0'*64),
                         replace(good, expected_path='/unrelated/file'),
                         replace(good, completion=replace(good.completion, job=wrong_job)),
                         replace(good, completion=replace(good.completion,
                             staged=replace(good.completion.staged, job=wrong_job)))):
            with self.assertRaises(ContractError): self.handoff(evidence)
        self.assertFalse(ObtainLocalFile.objects.exists())

    def test_failed_unpack_after_queued_cannot_handoff(self):
        self.submit()
        good = self.completion()
        with self.assertRaises(ContractError):
            self.handoff(replace(good, completion=replace(good.completion, unpack_ok=False)))
        self.assertEqual(ObtainTransfer.objects.get().state, 'failed')
        self.assertEqual(self.submit().state, 'failed')
        self.assertEqual(len(self.backend.calls), 1)

    def test_same_file_cannot_be_imported_for_another_job(self):
        self.submit(); self.handoff()
        first = self.job
        self.job = self.choice('tv', 'Fiction Series S01E01 1080p', '5000', guid='2')
        self.retriever.payload = NZB + b'other synthetic selection'
        snapshot = Snapshot.from_saved_job(self.job)
        self.evidence = Evidence(snapshot, ConfirmedSelection('tmdb:tv:22', 'episode',
            snapshot.edition, snapshot.storage, True, True, True, True, True, True, 'S01E01'))
        self.submission_review = self.reviewed(); self.submit()
        with self.assertRaises(IntegrityError): self.handoff()
        self.assertEqual(ObtainLocalFile.objects.count(), 1)
        self.assertEqual(ObtainLocalFile.objects.get().transfer_id, first.pk)
        self.assertEqual(ObtainTransfer.objects.get(job=self.job).state, 'queued')

    def test_isolated_s01e01_only_and_no_pack_or_multi_episode_dispatch(self):
        for title in ('Fiction Series S01E01E02', 'Fiction Series S01E01 pack',
                      'Fiction Series S01E01-02', 'Fiction Series S01E01 and E02',
                      'Fiction Series S01E01/02', 'Fiction Series S01 complete'):
            with self.subTest(title=title), self.assertRaises(saved.SavedError):
                self.choice('tv', title, '5000')
        self.assertFalse(ObtainTransfer.objects.exists())
        self.assertEqual(self.backend.calls, [])

    def test_outer_transaction_refused_before_any_send(self):
        with transaction.atomic(), self.assertRaises(RuntimeError): self.submit()
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_duplicate_result_or_nzb_across_jobs_never_enqueues(self):
        self.submit()
        first = self.job
        self.job = self.choice('tv', 'Fiction Series S01E01 1080p', '5000', guid='2')
        snapshot = Snapshot.from_saved_job(self.job)
        self.evidence = Evidence(snapshot, ConfirmedSelection('tmdb:tv:22', 'episode',
            snapshot.edition, snapshot.storage, True, True, True, True, True, True, 'S01E01'))
        self.submission_review = self.reviewed()
        with self.assertRaises(IntegrityError): self.submit()
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(ObtainTransfer.objects.get().job_id, first.pk)

    def test_cancel_unknown_remains_pending_and_repeat_is_idempotent(self):
        self.backend.error = AdapterUnavailable('synthetic unknown')
        self.submit()
        for _ in range(2):
            saved.cancel(actor=self.owner, profile_id=1, job_id=self.job.pk)
            self.assertEqual(ObtainTransfer.objects.get().state, 'cancel_pending')
        self.job.refresh_from_db()
        self.assertIsNotNone(self.job.active_key)
        self.assertEqual(len(self.backend.calls), 1)

    def test_cancel_definite_rejection_is_local_and_does_not_claim_remote_cancel(self):
        self.backend.error = QueueRejected('synthetic rejection')
        self.submit()
        for _ in range(2):
            saved.cancel(actor=self.owner, profile_id=1, job_id=self.job.pk)
            transfer = ObtainTransfer.objects.get()
            self.assertEqual(transfer.state, 'cancelled')
            self.assertEqual(transfer.reason, 'cancelled_before_dispatch')
        self.job.refresh_from_db()
        self.assertIsNone(self.job.active_key)

    def test_two_connections_compete_for_one_dispatch_and_restart_retains_receipt(self):
        with tempfile.TemporaryDirectory(prefix='obtain-race-') as folder:
            path = Path(folder) / 'synthetic.sqlite3'
            raw = sqlite3.connect(path)
            connection.connection.backup(raw)
            raw.close()
            start = threading.Barrier(3)
            results = []
            backends = [FixtureBackend(), FixtureBackend()]
            def worker(backend):
                local = connections['default']
                local.ensure_connection()
                db = sqlite3.connect(path, isolation_level=None, timeout=1, check_same_thread=False)
                register(db)
                try:
                    with patch.object(local, 'connection', db):
                        local.set_autocommit(True)
                        start.wait(timeout=5)
                        try:
                            results.append(self.submit(backend=backend).state)
                        except OperationalError:
                            results.append('reload')
                except BaseException as error:
                    results.append(type(error).__name__)
                finally:
                    db.close(); local.close()
            threads = [threading.Thread(target=worker, args=(backend,)) for backend in backends]
            for thread in threads: thread.start()
            start.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            self.assertEqual(sum(len(backend.calls) for backend in backends), 1, results)
            self.assertTrue(all(state in {'blocked', 'outcome_unknown', 'queued', 'reload'}
                                for state in results), results)
            environment = dict(os.environ)
            environment.pop('DJANGO_SETTINGS_MODULE', None)
            code = """
import os, sqlite3, sys
from unittest.mock import patch
from cabinet.boundary import prepare
prepare(['test'], os.environ)
os.environ['DJANGO_SETTINGS_MODULE'] = 'cabinet.settings'
import django
django.setup()
from django.db import connection
from django.db.backends.sqlite3._functions import register
from shelf.models import ObtainTransfer
from shelf.obtain_acquisition import DjangoCheckpoints
connection.ensure_connection()
raw = sqlite3.connect(sys.argv[1], isolation_level=None)
register(raw)
with patch.object(connection, 'connection', raw):
    transfer = ObtainTransfer.objects.get()
    checkpoint = DjangoCheckpoints(transfer).load(transfer.binding['exact']['snapshot']['request_id'])
    assert checkpoint.state == 'queued'
    assert checkpoint.job.job_id == 'SABnzbd_nzo_synthetic'
print('queued receipt recovered in fresh interpreter')
raw.close()
"""
            result = subprocess.run([sys.executable, '-B', '-c', code, str(path)],
                cwd=Path(__file__).resolve().parents[2], env=environment,
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('fresh interpreter', result.stdout)

    def test_existing_backend_transport_submits_bound_nzb_with_add_only_key(self):
        from shelf import credential_store as store
        from shelf.acquisition_transport import LoopbackTransport
        from shelf.tests.fixtures_credentials import FakeTree
        from shelf.tests.test_acquisition_backend import grant
        from shelf.tests.test_acquisition_transport import FakeHTTP
        metadata = grant('sab')
        metadata = replace(metadata, binding=replace(metadata.binding, actor_id='1', profile_id='1'))
        tree = FakeTree(store, provider='sab')
        self.addCleanup(tree.temporary.cleanup)
        store.commit_entry(tree.store, metadata, bytearray(b'SYNTHETIC-ADD-ONLY-KEY'))
        tree.store.close()
        opened = []
        def opener(*, provider):
            opened.append(provider)
            return store._fixture_store(tree.path, provider=provider)
        http = FakeHTTP(b'{"status":true,"nzo_ids":["SABnzbd_nzo_synthetic"]}')
        backend = Backend(sab_grant=metadata, _open_store=opener,
                          _transport=LoopbackTransport(_connection_factory=http.factory))
        with patch('socket.socket', side_effect=AssertionError('offline fixtures only')):
            transfer = self.submit(backend=backend)
            local = self.handoff()
            saved.cancel(actor=self.owner, profile_id=1, job_id=self.job.pk)
        self.assertEqual(transfer.state, 'queued')
        self.assertEqual(local.sha256, sha256(MEDIA).hexdigest())
        self.assertEqual(opened, ['sab'])
        self.assertEqual(len(http.calls), 1)
        method, path, body, _ = http.calls[0]
        self.assertEqual((method, path), ('POST', '/api'))
        self.assertIn(NZB, body)
        self.assertIn(b'addfile', body)
        for forbidden in (b'addurl', b'history', b'cancel', b'queue'):
            self.assertNotIn(forbidden, body)

    def test_file_path_replacement_during_hashing_rejects_handoff(self):
        self.submit()
        import shelf.acquisition_pipeline as pipeline
        original = pipeline._open_known_file
        calls = 0
        def replace_path(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                old = self.path.with_name('retained.fixture')
                self.path.rename(old)
                self.path.symlink_to(old)
            return original(path)
        with patch.object(pipeline, '_open_known_file', side_effect=replace_path):
            with self.assertRaises(ContractError): self.handoff()
        self.assertFalse(ObtainLocalFile.objects.exists())

    def test_revocation_during_hashing_blocks_receipt(self):
        self.submit()
        original = acquire.verify_staging
        def revoke(staged, **kwargs):
            original(staged, **kwargs)
            Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
        with patch.object(acquire, 'verify_staging', side_effect=revoke):
            with self.assertRaises(Http404): self.handoff()
        self.assertFalse(ObtainLocalFile.objects.exists())

    def test_local_receipt_commit_failure_rolls_back_and_retry_verifies_again(self):
        self.submit()
        with patch.object(ObtainLocalFile.objects, 'get_or_create', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError): self.handoff()
        self.assertEqual(ObtainTransfer.objects.get().state, 'queued')
        self.assertFalse(ObtainLocalFile.objects.exists())
        self.handoff()
        self.assertEqual(ObtainLocalFile.objects.count(), 1)
        self.assertEqual(len(self.backend.calls), 1)

    def test_same_hydra_result_cannot_queue_again_with_different_nzb_bytes(self):
        self.submit()
        self.job = self.choice('tv', 'Fiction Series S01E01 1080p', '5000')
        snapshot = Snapshot.from_saved_job(self.job)
        self.evidence = Evidence(snapshot, ConfirmedSelection('tmdb:tv:22', 'episode',
            snapshot.edition, snapshot.storage, True, True, True, True, True, True, 'S01E01'))
        self.retriever.payload = NZB + b'changed listing bytes'
        self.submission_review = self.reviewed()
        with self.assertRaises(IntegrityError): self.submit()
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(ObtainTransfer.objects.count(), 1)


    def selected_episode(self):
        self.job = self.choice('tv', 'Fiction Series S02E02 1080p', '5000',
                               guid='2', season=2, episode=2)
        snapshot = Snapshot.from_saved_job(self.job)
        self.evidence = Evidence(snapshot, ConfirmedSelection('tmdb:tv:22', 'episode',
            snapshot.edition, snapshot.storage, True, True, True, True, True, True, 'S02E02'))
        self.submission_review = self.reviewed()

    def test_selected_episode_repeat_and_scope_persist_exactly(self):
        self.selected_episode()
        first = self.submit()
        self.assertEqual(first.state, 'queued')
        self.assertEqual(self.submit().pk, first.pk)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.job.scope, 'episode_s02e02')
        transfer = ObtainTransfer.objects.get()
        self.assertIn('episode_s02e02', str(transfer.binding))
        self.assertEqual(self.evidence.selection.episode, 'S02E02')

    def test_selected_episode_cancel_before_dispatch(self):
        self.selected_episode()
        saved.cancel(actor=self.owner, profile_id=1, job_id=self.job.pk)
        with self.assertRaises(Blocked):
            self.submit()
        self.assertEqual(self.backend.calls, [])

    def test_selected_episode_nzb_and_token_tampering(self):
        self.selected_episode()
        with self.assertRaises((Blocked, ContractError)):
            self.submit(token='tampered')
        self.retriever.payload = NZB + b'changed'
        with self.assertRaises(ContractError):
            self.submit()
        self.assertEqual(self.backend.calls, [])

    def test_selected_episode_wrong_episode_or_series_cannot_review(self):
        self.selected_episode()
        for changes in ({'episode': 'S01E01'}, {'item_id': 'tmdb:tv:23'}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.reviewed(evidence=Evidence(self.evidence.snapshot,
                    replace(self.evidence.selection, **changes)))
        self.assertEqual(self.backend.calls, [])

    def test_selected_scope_maximum_survives_raw_sqlite_backup(self):
        job = self.choice('tv', 'Fiction Series S999E999 1080p', '5000',
                          guid='3', season=999, episode=999)
        self.assertEqual(job.scope, 'episode_s999e999')
        self.assertEqual(len(job.scope), 16)
        # A raw restore/backup connection intentionally has no Django functions.
        with sqlite3.connect(':memory:') as raw:
            connection.connection.backup(raw)
            self.assertEqual(raw.execute('PRAGMA integrity_check').fetchone(), ('ok',))
            scope = raw.execute('SELECT scope FROM shelf_acquisitionjob WHERE id = ?',
                                (job.pk,)).fetchone()[0]
            self.assertEqual(scope, job.scope)
