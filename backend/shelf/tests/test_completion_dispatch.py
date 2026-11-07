"""Actual isolated ledger/hook fixtures; discovery never uses provider or Kodi I/O."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import uuid4
from unittest import TestCase as PlainTestCase
from unittest.mock import patch
from contextlib import redirect_stdout
from django.db import connection, OperationalError
from django.http import Http404
from django.test import Client, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from shelf import acquisition_backend, credential_store as store
from shelf import obtain_acquisition as acquire, obtain_runtime as runtime
from shelf import completion_dispatch as dispatch
from shelf.acquisition_contract import Blocked, COMPLETION_SCRIPT
from shelf.acquisition_transport import LoopbackTransport
from shelf.models import AcquisitionJob, ObtainSelection, ObtainResult, ObtainTransfer, ObtainCompletionStatus, Grant
from shelf.obtain_parser import digest
from shelf.tests.fixtures_credentials import FakeTree
from shelf.tests.test_acquisition_backend import grant
from shelf.tests import test_obtain_runtime as runtime_tests, test_completion_status as completion_tests
from shelf.tests.test_saved import SavedFixture
ProviderHTTP = runtime_tests.ProviderHTTP

@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class DispatchTests(SavedFixture, TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.saved = self.item()
        self.series = self.item('tv')
        self.folder = tempfile.TemporaryDirectory(prefix='runtime-synthetic-')
        self.addCleanup(self.folder.cleanup)
        self.complete = Path(self.folder.name) / 'Complete'
        self.complete.mkdir()
        self.control = Path(self.folder.name) / 'control'
        self.control.mkdir(mode=448)
        self.registry = self.control / 'registry.json'
        self.receipts = Path(self.folder.name) / 'receipts'
        self.receipts.mkdir(mode=448)
        self.policy = runtime.SubmissionPolicy(True, str(self.complete), 20000, 4096, True, str(self.registry), True, COMPLETION_SCRIPT)
        (self.paths, self.grants, self.reads) = ({}, {}, [])
        for provider in ('hydra', 'sab'):
            tree = FakeTree(store, provider=provider)
            self.addCleanup(tree.temporary.cleanup)
            metadata = grant(provider)
            metadata = replace(metadata, binding=replace(metadata.binding, actor_id='1', profile_id='1'))
            self.grants[provider] = metadata
            result = store.commit_entry(tree.store, metadata, bytearray(b'FICTION-ONLY-' + provider.encode()))
            self.assertEqual(result.outcome, 'stored')
            self.paths[provider] = tree.path
            tree.store.close()

        def opener(*, provider):
            self.reads.append(provider)
            return store._fixture_store(self.paths[provider], provider=provider)
        self.http = ProviderHTTP()
        self.enterContext(patch.object(store, 'open_store', side_effect=opener))
        self.enterContext(patch.object(acquisition_backend, 'LoopbackTransport', return_value=LoopbackTransport(_connection_factory=self.http.factory)))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('network forbidden')))
        self.enterContext(override_settings(OBTAIN_SUBMISSION_POLICY=vars(self.policy)))
        self.client = Client(enforce_csrf_checks=True)
        self.login()
        self.context = acquire.OperatorContext(1, 1, 'offline_test', True)
    login = runtime_tests.RuntimeTests.login
    hidden = runtime_tests.RuntimeTests.hidden
    select = runtime_tests.RuntimeTests.select
    review = runtime_tests.RuntimeTests.review
    submit = runtime_tests.RuntimeTests.submit
    addfile_calls = runtime_tests.RuntimeTests.addfile_calls
    observe_history = runtime_tests.RuntimeTests.observe_history
    registered = runtime_tests.RuntimeTests.registered
    load_hook = runtime_tests.RuntimeTests.load_hook
    completed = completion_tests.CompletionStatusTests.completed
    receipt = completion_tests.CompletionStatusTests.receipt
    record = completion_tests.CompletionStatusTests.record
    completed_episode = completion_tests.CompletionStatusTests.completed_episode
    completed_album = completion_tests.CompletionStatusTests.completed_album
    album_selection = runtime_tests.RuntimeTests.album_selection
    submit_album = runtime_tests.RuntimeTests.submit_album

    def pending(self):
        self.review()
        self.submit()
        return ObtainTransfer.objects.get(job=self.job)

    def page(self, **kwargs):
        return dispatch.list_jobs(actor=self.owner, profile_id=1, context=self.context, **kwargs)

    def clone_fixture(self):
        # Synthetic test ledger only; never create production bindings from manual files.
        original = ObtainTransfer.objects.get(job=self.job)
        selected = ObtainSelection.objects.select_related('result__preview').get(job=self.job)
        job = deepcopy(self.job)
        job.pk = None
        job.request_id = uuid4()
        job.source_id += AcquisitionJob.objects.count() + 100
        job.active_key = uuid4().hex
        job.saved_item = None
        job.save(force_insert=True)
        preview = deepcopy(selected.result.preview)
        preview.pk = job.request_id
        preview.source_id = job.source_id
        preview.save(force_insert=True)
        result = deepcopy(selected.result)
        result.pk = uuid4()
        result.preview = preview
        result.guid = str(job.pk + 1000)
        result.metadata = {**result.metadata, 'guid': result.guid}
        result.digest = digest(result.metadata)
        result.save(force_insert=True)
        ObtainSelection.objects.create(job=job, result=result)
        (exact, _) = acquire.exact_result(job, fresh=False)
        binding = deepcopy(original.binding)
        binding['exact'] = asdict(exact)
        binding['selection']['item_id'] = 'tmdb:movie:' + str(job.source_id)
        binding['completion_intent'] = self.policy.intent(str(job.request_id))
        nzb = hashlib.sha256(('SYNTHETIC descriptor ' + str(job.request_id)).encode()).hexdigest()
        binding['nzb_sha256'] = nzb
        return ObtainTransfer.objects.create(job=job, binding=binding, result_guid=result.guid, nzb_sha256=nzb, state='queued', reason='', provider_job='SABnzbd_nzo_fixture' + str(job.pk))

    def test_pending_page_is_exact_read_only_minimal_and_repeatable(self):
        transfer = self.pending()
        calls = len(self.http.calls)
        registry = self.registry.read_bytes()
        with CaptureQueriesContext(connection) as queries, patch('shelf.completion_observer.observe', side_effect=AssertionError('no observation')):
            page = self.page()
        self.assertTrue(all((q['sql'].lstrip().upper().startswith('SELECT') for q in queries)))
        self.assertEqual(page, self.page())
        self.assertEqual((page['actor_id'], page['profile_id'], page['scope']), (1, 1, 'supported_media'))
        self.assertEqual(page['version'], 2)
        row = page['jobs'][0]
        self.assertEqual((row['state'], row['dispatchable'], row['delivery_state']), ('pending', True, 'not_reported'))
        self.assertEqual(row['request_id'], str(self.job.request_id))
        self.assertEqual(row['nzo_id'], transfer.provider_job)
        self.assertEqual(row['nzb_sha256'], transfer.nzb_sha256)
        self.assertEqual(row['source_identity']['source_id'], self.job.source_id)
        self.assertIsNone(row['completion_evidence_sha256'])
        self.assertIsNone(row['delivery_sha256'])
        encoded = json.dumps(page)
        for private in (str(self.complete), str(self.registry), 'FICTION-ONLY', 'complete_root', 'registry_path'):
            self.assertNotIn(private, encoded)
        self.assertEqual(self.registry.read_bytes(), registry)
        self.assertEqual(len(self.http.calls), calls)
        self.assertEqual(ObtainTransfer.objects.get(job=self.job).revision, transfer.revision)
        self.assertEqual(ObtainCompletionStatus.objects.count(), 0)

    def test_completed_is_recorded_history_not_current_file_availability(self):
        receipt = self.completed()
        (Path(self.registered()['root']) / 'observed.mkv').unlink()
        row = self.page()['jobs'][0]
        self.assertEqual((row['state'], row['reason']), ('completed', 'recorded_final_history'))
        self.assertEqual(row['completion_evidence_sha256'], receipt.completion_sha256)
        self.assertTrue(row['dispatchable'])
        self.assertNotIn('files', row)

    def test_selected_pilot_is_discovered_without_generalizing_episode_scope(self):
        completed = self.completed_episode()
        row = self.page()['jobs'][0]
        self.assertEqual(row['source_identity']['medium'], 'tv')
        self.assertEqual(row['source_identity']['scope'], 'series_s01e01')
        self.assertEqual(row['completion_evidence_sha256'], completed.completion_sha256)
        self.assertNotIn('album_identity', row)
        self.assertTrue(row['dispatchable'])
        self.job.scope = 'series_s02e01'
        with self.assertRaises(ValueError):
            dispatch.media.source_identity(self.job)

    def test_explicit_s02e02_is_discovered_and_cancelled_job_is_excluded(self):
        completed = self.completed_episode('episode_s02e02')
        row = self.page()['jobs'][0]
        self.assertEqual(row['source_identity']['scope'], 'episode_s02e02')
        self.assertEqual(row['completion_evidence_sha256'], completed.completion_sha256)
        self.assertTrue(row['dispatchable'])
        from django.utils import timezone
        ObtainTransfer.objects.filter(job=self.job).update(local_cancelled_at=timezone.now())
        self.assertEqual(self.page()['jobs'], [])

    def test_album_discovery_requires_current_selected_release(self):
        completed = self.completed_album()
        row = self.page()['jobs'][0]
        self.assertEqual(row['source_identity']['medium'], 'music')
        self.assertEqual(row['album_identity'], self.album_identity)
        self.assertEqual(row['completion_evidence_sha256'], completed.completion_sha256)
        self.assertTrue(row['dispatchable'])
        from shelf.models import AlbumRequest
        selected = AlbumRequest.objects.get()
        selected.authorization['one_album'] = False
        selected.save(update_fields=['authorization'])
        row = self.page()['jobs'][0]
        self.assertEqual((row['state'], row['dispatchable']), ('unknown', False))
        self.assertNotIn('album_identity', row)

    def test_blocked_unknown_reconcile_but_imported_is_terminal(self):
        self.completed()
        for (state, dispatchable) in (('blocked', True), ('unknown', True), ('imported', False)):
            receipt = self.receipt(state)
            if state == 'blocked':
                receipt['scan_attempts'] = 0
            recorded = self.record(receipt)
            row = self.page()['jobs'][0]
            self.assertEqual((row['state'], row['delivery_state'], row['dispatchable']), ('completed', state, dispatchable))
            self.assertEqual(row['delivery_sha256'], recorded.delivery_sha256)

    def test_provider_failure_is_visible_and_not_dispatchable(self):
        self.pending()
        ObtainTransfer.objects.filter(job=self.job).update(state='failed', reason='provider_failed')
        row = self.page()['jobs'][0]
        self.assertEqual((row['state'], row['reason'], row['dispatchable']), ('failed', 'provider_failed', False))

    def test_bad_binding_advances_page_and_does_not_starve_later_valid_job(self):
        first = self.pending()
        second = self.clone_fixture()
        first.binding['nzb_sha256'] = '0' * 64
        first.save(update_fields=['binding'])
        page = self.page(limit=1)
        self.assertEqual(page['jobs'], [{'job_id': first.pk, 'state': 'unknown', 'reason': 'binding_unavailable', 'dispatchable': False}])
        self.assertTrue(page['has_more'])
        self.assertEqual(page['next_cursor'], first.pk)
        page = self.page(after=page['next_cursor'], limit=1)
        self.assertEqual(page['jobs'][0]['job_id'], second.pk)
        self.assertTrue(page['jobs'][0]['dispatchable'])
        self.assertFalse(page['has_more'])
        empty = self.page(after=page['next_cursor'], limit=1)
        self.assertEqual(empty['jobs'], [])
        self.assertEqual(empty['next_cursor'], second.pk)

    def test_duplicate_provider_job_is_rejected_across_page_boundaries(self):
        first = self.pending()
        second = self.clone_fixture()
        ObtainTransfer.objects.filter(pk=second.pk).update(provider_job=first.provider_job)
        for cursor in (0, first.pk):
            row = self.page(after=cursor, limit=1)['jobs'][0]
            self.assertEqual((row['state'], row['dispatchable']), ('unknown', False))
            self.assertNotIn('nzo_id', row)

    def test_foreign_cancelled_unacknowledged_and_unsupported_rows_are_excluded(self):
        first = self.pending()
        foreign = self.clone_fixture()
        foreign.job.actor = self.other
        foreign.job.profile = self.partner
        foreign.job.save()
        cancelled = self.clone_fixture()
        acquire.cancel_local(cancelled.job)
        unack = self.clone_fixture()
        unack.provider_job = ''
        unack.save(update_fields=['provider_job'])
        television = self.clone_fixture()
        television.job.provider = 'hardcover'
        television.job.medium = 'book'
        television.job.scope = 'audiobook'
        television.job.save()
        self.assertEqual([r['job_id'] for r in self.page()['jobs']], [first.pk])
        with self.assertRaises(Blocked):
            dispatch.list_jobs(actor=self.other, profile_id=2, context=acquire.OperatorContext(2, 2, 'offline_test', True))

    def test_disabled_policy_revoked_grant_and_synthetic_live_context_refuse(self):
        self.pending()
        with override_settings(OBTAIN_SUBMISSION_POLICY={**vars(self.policy), 'enabled': False}), self.assertRaises(Blocked):
            self.page()
        with self.assertRaises(Blocked):
            dispatch.list_jobs(actor=self.owner, profile_id=1, context=acquire.OperatorContext(1, 1, 'candidate_operator', True), session_guard=lambda : None)
        Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
        with self.assertRaises(Http404):
            self.page()

    def test_expired_or_replaced_session_never_returns_partial_page(self):
        self.pending()
        calls = []

        def guard():
            calls.append(1)
            if len(calls) == 3:
                raise ValueError('session changed')
        with self.assertRaises(ValueError):
            self.page(session_guard=guard)
        self.assertEqual(len(calls), 3)

    def test_revocation_during_row_validation_discards_the_whole_page(self):
        self.pending()
        original = dispatch._job

        def revoke(*args, **kwargs):
            row = original(*args, **kwargs)
            Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
            return row
        with patch.object(dispatch, '_job', side_effect=revoke), self.assertRaises(Http404):
            self.page()

    def test_cancellation_during_scan_cannot_return_a_dispatchable_binding(self):
        self.pending()
        original = dispatch._job

        def cancel(actor, profile_id, transfer, context):
            acquire.cancel_local(transfer.job)
            return original(actor, profile_id, transfer, context)
        with patch.object(dispatch, '_job', side_effect=cancel):
            row = self.page()['jobs'][0]
        self.assertEqual((row['state'], row['dispatchable']), ('unknown', False))

    def test_corrupt_recorded_delivery_cannot_supply_a_foreign_binding(self):
        self.completed()
        stored = self.record(self.receipt('unknown'))
        stored.delivery_receipt['selection']['nzo_id'] = 'SABnzbd_nzo_foreign'
        stored.save(update_fields=['delivery_receipt'])
        row = self.page()['jobs'][0]
        self.assertEqual((row['state'], row['dispatchable']), ('unknown', False))
        self.assertNotIn('delivery_sha256', row)

    def test_bounds_and_unavailable_store_refuse_without_observation(self):
        self.pending()
        for kwargs in ({'limit': 0}, {'limit': 33}, {'limit': True}, {'after': -1}, {'after': True}, {'after': 2 ** 63}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Blocked):
                self.page(**kwargs)
        with patch.object(ObtainTransfer.objects, 'filter', side_effect=OperationalError('private path')):
            with self.assertRaises(OperationalError):
                self.page()

class CommandTests(PlainTestCase):

    def setUp(self):
        spec = importlib.util.spec_from_file_location('completion_list_cli', Path(__file__).resolve().parents[3] / 'tools/list_completion_jobs.py')
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)
        self.now = datetime.now(timezone.utc)
        self.args = SimpleNamespace(after=0, limit=16, expires=(self.now + timedelta(minutes=5)).isoformat(), renewal='fixture')
        self.server = {'expires_utc': (self.now + timedelta(minutes=10)).isoformat(), 'renewal_id': 'fixture'}

    def test_session_replacement_expiry_extension_naive_time_and_bad_cursor_refuse(self):
        self.cli.validate_session(self.args, self.server, self.now)
        for changes in ({'renewal': 'different'}, {'expires': (self.now - timedelta(seconds=1)).isoformat()}, {'expires': (self.now + timedelta(minutes=20)).isoformat()}, {'expires': self.now.replace(tzinfo=None).isoformat()}, {'after': -1}, {'limit': 33}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.cli.validate_session(SimpleNamespace(**vars(self.args) | changes), self.server, self.now)

    def test_candidate_path_refuses_before_setup_and_unavailable_errors_are_sanitized(self):
        with self.assertRaises(ValueError):
            self.cli.run(self.args)
        out = io.StringIO()
        argv = ['list_completion_jobs.py', '--expires', self.args.expires, '--renewal', 'fixture']
        with patch('sys.argv', argv), patch.object(self.cli, 'run', side_effect=OSError('private credential/path')), redirect_stdout(out):
            self.assertEqual(self.cli.main(), 1)
        value = json.loads(out.getvalue())
        self.assertEqual(value['state'], 'unavailable')
        self.assertEqual(value['jobs'], [])
        self.assertNotIn('private', out.getvalue())
        self.assertIsNone(value['next_cursor'])
