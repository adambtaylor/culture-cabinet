"""Real composition and rendered CSRF flow; private stores contain fiction only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from email import policy as email_policy
from email.parser import BytesParser
import threading
from uuid import uuid4
from datetime import timedelta
import importlib
import json
import os
import re
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import Client, TransactionTestCase, override_settings

from shelf import acquisition_backend, credential_store as store
from shelf import obtain_acquisition as acquire, obtain_runtime as runtime
from shelf.acquisition_contract import Blocked, COMPLETION_SCRIPT
from shelf import completion_registry as registry
from shelf.acquisition_transport import LoopbackTransport
from shelf.authority import SELECTION_KEY
from shelf.models import AcquisitionJob, FeedbackEvent, Grant, ObtainTransfer
from shelf.tests.fixtures_credentials import FakeTree
from shelf.tests.test_acquisition_backend import grant
from shelf.tests.test_acquisition_contract import CAPS
from shelf.tests.test_acquisition_transport import FakeHTTP
from shelf.tests.test_obtain_retrieval import NZB
from shelf.tests.test_obtain_ui import Page, listing
from shelf.tests.test_saved import SavedFixture


class ProviderHTTP(FakeHTTP):
    def request(self, method, path, *, body, headers):
        super().request(method, path, body=body, headers=headers)
        operation = parse_qs(urlsplit(path).query).get('t', ['addfile'])[0]
        self.headers = {}
        if operation == 'caps':
            self.payload = CAPS
        elif operation == 'search':
            self.payload = json.dumps({'channel': {'item': [listing()]}}).encode()
        elif operation == 'get':
            self.payload = NZB
            self.headers = {'Content-Type': 'application/x-nzb'}
        else:
            self.payload = b'{"status":true,"nzo_ids":["SABnzbd_nzo_runtime_fixture"]}'


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class RuntimeTests(SavedFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.saved = self.item()
        self.series = self.item('tv')
        self.folder = tempfile.TemporaryDirectory(prefix='runtime-synthetic-')
        self.addCleanup(self.folder.cleanup)
        self.complete = Path(self.folder.name) / 'Complete'
        self.complete.mkdir()
        self.control = Path(self.folder.name) / 'control'
        self.control.mkdir(mode=0o700)
        self.registry = self.control / 'registry.json'
        self.receipts = Path(self.folder.name) / 'receipts'
        self.receipts.mkdir(mode=0o700)
        self.policy = runtime.SubmissionPolicy(True, str(self.complete), 20000, 4096, True,
            str(self.registry), True, COMPLETION_SCRIPT)
        self.paths, self.grants, self.reads = {}, {}, []
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
        self.enterContext(patch.object(acquisition_backend, 'LoopbackTransport',
            return_value=LoopbackTransport(_connection_factory=self.http.factory)))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('network forbidden')))
        self.enterContext(override_settings(OBTAIN_SUBMISSION_POLICY=vars(self.policy)))
        self.client = Client(enforce_csrf_checks=True)
        self.login()

    def login(self):
        self.client.force_login(self.owner)
        session = self.client.session
        session[SELECTION_KEY] = 1
        session.save()

    def hidden(self, response, action):
        return {name: field['value'] for name, field in Page(response).form(action)['fields'].items()
                if field.get('type') == 'hidden'}

    def select(self):
        start = f'{self.base}{self.saved.pk}/preview/'
        page = self.client.get(start)
        self.assertEqual(self.client.post(start + 'search/').status_code, 403)
        found = self.client.post(start + 'search/', self.hidden(page, start + 'search/'))
        self.assertEqual(found.status_code, 302)
        results = self.client.get(found.url)
        choice = self.client.get(results.context['obtain_preview']['results'][0]['review_url'])
        action = choice.context['obtain_result_confirm_url']
        data = self.hidden(choice, action) | {
            'authorized_content': 'on', 'storage_check': 'on', 'storage': 'local_library'}
        selected = self.client.post(action, data)
        self.assertEqual(selected.status_code, 302)
        self.job = AcquisitionJob.objects.latest('created_at')
        self.status = selected.url
        self.review_url, self.submit_url = self.status + 'submit/review/', self.status + 'submit/'
        return self.client.get(self.status)

    def review(self):
        page = self.select()
        response = self.client.post(self.review_url, self.hidden(page, self.review_url))
        self.assertEqual(response.status_code, 200)
        self.data = self.hidden(response, self.submit_url) | {name: 'on' for name in
            ('exact_selection', 'rights', 'scope', 'size', 'duplicates')}
        return response

    def submit(self):
        return self.client.post(self.submit_url, self.data)

    def addfile_calls(self):
        return [call for call in self.http.calls if call[0] == 'POST']

    def observe_history(self, reader):
        from shelf.completion_observer import observe
        return observe(actor=self.owner, profile_id=1, job_id=self.job.pk,
            context=acquire.OperatorContext(1, 1, 'offline_test', enabled=True), reader=reader)

    def test_final_provider_failure_is_durable_visible_and_never_resubmitted(self):
        from shelf import saved
        self.review(); self.submit()
        transfer = ObtainTransfer.objects.get(job=self.job)
        slot = {'nzo_id': transfer.provider_job,
                'name': transfer.binding['completion_intent']['nzbname'], 'status': 'Failed'}
        result = self.observe_history(lambda nzo: slot)
        self.assertEqual(result['state'], 'failed')
        transfer.refresh_from_db(); revision = transfer.revision
        self.assertEqual((transfer.state, transfer.reason), ('failed', 'provider_failed'))
        self.assertEqual(saved.job_context(self.job)['status'], 'failed')
        self.assertContains(self.client.get(self.status), 'The downloader reported that this release could not be completed')
        with patch('shelf.completion_observer.read_history', side_effect=AssertionError('terminal receipt retained')):
            self.assertEqual(self.observe_history(lambda nzo: self.fail('No history reread')), result)
        transfer.refresh_from_db(); self.assertEqual(transfer.revision, revision)
        self.submit()
        self.assertEqual(len(self.addfile_calls()), 1)

    def test_foreign_missing_or_unreadable_history_never_marks_failed(self):
        self.review(); self.submit()
        transfer = ObtainTransfer.objects.get(job=self.job)
        slot = {'nzo_id': transfer.provider_job, 'name': 'CC-someone-else', 'status': 'Failed'}
        self.assertEqual(self.observe_history(lambda nzo: slot)['state'], 'unknown')
        self.assertEqual(self.observe_history(lambda nzo: None)['state'], 'pending')
        def unavailable(nzo):
            raise OSError('synthetic history unavailable')
        self.assertEqual(self.observe_history(unavailable)['state'], 'unknown')
        transfer.refresh_from_db(); self.assertEqual(transfer.state, 'queued')

    def test_cancellation_during_history_observation_is_not_overwritten(self):
        self.review(); self.submit()
        transfer = ObtainTransfer.objects.get(job=self.job)
        def cancelling(nzo):
            acquire.cancel_local(self.job)
            return {'nzo_id': nzo, 'name': transfer.binding['completion_intent']['nzbname'], 'status': 'Failed'}
        self.assertEqual(self.observe_history(cancelling)['state'], 'unknown')
        transfer.refresh_from_db(); self.assertEqual(transfer.state, 'cancel_pending')

    def test_real_factory_csrf_sequence_restart_doubleclick_and_private_credentials(self):
        response = self.review()
        self.assertContains(response, 'completeness remain unverified')
        selection = response.context['obtain_submission']['selection']
        self.assertFalse(selection['edition_verified'])
        self.assertFalse(selection['complete_work_verified'])
        self.assertEqual(self.client.get(self.submit_url).status_code, 405)
        self.assertEqual(self.client.post(self.submit_url, self.data | {'csrfmiddlewaretoken': ''}).status_code, 403)
        self.assertEqual(self.client.post(self.submit_url, self.data | {'duplicates': ''}).status_code, 409)
        self.assertEqual(self.addfile_calls(), [])
        self.assertEqual(self.submit().status_code, 302)
        calls = len(self.http.calls)
        # Rebuild the service module and HTTP client, retaining only durable DB/session state.
        importlib.reload(acquire)
        cookies = self.client.cookies
        self.client = Client(enforce_csrf_checks=True)
        self.client.cookies = cookies
        self.assertEqual(self.submit().status_code, 302)
        self.assertEqual(len(self.http.calls), calls)
        self.assertEqual(len(self.addfile_calls()), 1)
        transfer = ObtainTransfer.objects.get()
        self.assertEqual(transfer.state, 'queued')
        self.assertEqual(transfer.provider_job, 'SABnzbd_nzo_runtime_fixture')
        self.assertEqual(transfer.binding['complete_root'], str(self.complete))
        self.assertTrue(transfer.binding['test_only'])
        self.assertEqual(set((h, p) for h, p, _ in self.http.connections),
                         {('127.0.0.1', 5076), ('127.0.0.1', 8080)})
        self.assertIn(b'FICTION-ONLY-sab', self.addfile_calls()[0][2])
        self.assertNotIn(b'FICTION-ONLY-hydra', self.addfile_calls()[0][2])
        for call in self.http.calls:
            if call[0] == 'GET':
                self.assertEqual(parse_qs(urlsplit(call[1]).query)['apikey'], ['FICTION-ONLY-hydra'])
        self.assertNotIn('FICTION-ONLY', response.content.decode() + str(transfer.binding))
        self.assertEqual(FeedbackEvent.objects.count(), 0)

    def test_default_disabled_policy_never_opens_private_store(self):
        self.select()
        self.reads.clear()
        with override_settings(OBTAIN_SUBMISSION_POLICY=None):
            with self.assertRaises(Blocked):
                runtime.submission_factory(actor=self.owner, profile_id=1, job=self.job)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.addfile_calls(), [])

    def test_caller_cannot_enable_policy_supply_keys_or_select_urls(self):
        page = self.select()
        data = self.hidden(page, self.review_url)
        self.reads.clear()
        for key in ('enabled', 'policy', 'grant', 'apikey', 'url', 'complete_root', 'max_bytes',
                    'nzbname', 'script', 'pp', 'category', 'registry_path', 'completion_intent'):
            self.assertEqual(self.client.post(self.review_url, data | {key: 'untrusted'}).status_code, 400)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.addfile_calls(), [])

    def test_wrong_scope_binding_expiry_or_missing_record_never_release(self):
        self.select()
        calls = len(self.http.calls)
        for provider in ('hydra', 'sab'):
            path = self.paths[provider] / store.RECORD
            original = path.read_bytes()
            for change in ('actor_id', 'profile_id', 'purpose', 'scopes', 'expiry', 'provider'):
                body = json.loads(original)
                if change in ('actor_id', 'profile_id'):
                    body['binding'][change] = '99'
                elif change in ('purpose', 'provider'):
                    body['binding'][change] = 'unrelated'
                elif change == 'scopes':
                    body['scopes'] = ['unrelated']
                else:
                    body['expires_at'] = body['entered_at']
                path.write_text(json.dumps(body))
                with self.subTest(provider=provider, change=change), self.assertRaises(Blocked):
                    runtime.submission_factory(actor=self.owner, profile_id=1, job=self.job)
                path.write_bytes(original)
            path.unlink()
            with self.assertRaises(Blocked):
                runtime.submission_factory(actor=self.owner, profile_id=1, job=self.job)
            path.write_bytes(original)
            path.chmod(0o600)
        self.assertEqual(len(self.http.calls), calls)

    def test_factory_wipes_private_records_and_backend_rereads_current_expiry(self):
        self.select()
        records = []
        read = store.read_record
        def capture(handle):
            record = read(handle)
            records.append(record)
            return record
        with patch.object(store, 'read_record', side_effect=capture):
            services = runtime.submission_factory(actor=self.owner, profile_id=1, job=self.job)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(set(record.secret) == {0} for record in records))
        exact, _ = acquire.exact_result(self.job, fresh=True)
        with patch.object(store, 'utc_now', return_value=self.grants['hydra'].expires_at):
            with self.assertRaises(Blocked):
                services.retriever.review(exact)
        self.assertEqual(self.addfile_calls(), [])

    def test_capacity_and_size_limits_fail_before_private_store_read(self):
        self.select()
        self.reads.clear()
        for policy in (replace(self.policy, max_bytes=12344), replace(self.policy, enabled=False),
                       replace(self.policy, complete_root=str(self.complete / 'missing'))):
            with override_settings(OBTAIN_SUBMISSION_POLICY=vars(policy)), self.assertRaises(Blocked):
                runtime.submission_factory(actor=self.owner, profile_id=1, job=self.job)
        with patch.object(runtime.os, 'fstatvfs') as usage:
            usage.return_value.f_bavail = 0
            usage.return_value.f_frsize = 4096
            with self.assertRaises(Blocked):
                runtime.submission_factory(actor=self.owner, profile_id=1, job=self.job)
        self.assertEqual(self.reads, [])

    def test_policy_change_after_review_requires_new_confirmation(self):
        self.review()
        for policy in (replace(self.policy, enabled=False), replace(self.policy, max_bytes=19000),
                       replace(self.policy, test_only=False)):
            with override_settings(OBTAIN_SUBMISSION_POLICY=vars(policy)):
                self.assertEqual(self.submit().status_code, 409)
        self.assertFalse(ObtainTransfer.objects.exists())
        self.assertEqual(self.addfile_calls(), [])

    def test_revoked_application_authority_blocks_real_factory(self):
        self.review()
        self.reads.clear()
        Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
        self.assertEqual(self.submit().status_code, 404)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.addfile_calls(), [])

    def wire_fields(self, call=None):
        _, _, body, headers = call or self.addfile_calls()[-1]
        message = BytesParser(policy=email_policy.default).parsebytes(
            ('Content-Type: ' + headers['Content-Type'] + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
        return {part.get_param('name', header='content-disposition'):
                part.get_payload(decode=True) for part in message.iter_parts()}

    def registered(self):
        entries = json.loads(self.registry.read_text())['requests']
        return next(entry for entry in entries if entry['request_id'] == str(self.job.request_id))

    def load_hook(self):
        spec = importlib.util.spec_from_file_location('integrated_completion',
            Path(__file__).resolve().parents[3] / 'tools/sab_completion.py')
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        return hook

    def hook_media(self, nzo):
        # Simulate SAB's approved fixed layout from the actual encoded request.
        name = self.wire_fields()['nzbname'].decode()
        job_root = self.complete / name
        job_root.mkdir()
        marker = b'fLaC' if self.job.scope == 'album_flac' else b''
        (job_root / 'observed.flac').write_bytes(marker + b'synthetic bytes, not playable media')
        return {'SAB_STATUS': 'Running', 'SAB_PP_STATUS': '0', 'SAB_NZO_ID': nzo,
                'SAB_COMPLETE_DIR': str(job_root), 'SAB_FILES': '["observed.flac"]'}

    def test_automatic_registration_and_wire_options_have_exact_durable_binding(self):
        self.review()
        original = self.http.request
        def inspect_intent(method, *args, **kwargs):
            if method == 'POST':
                transfer = ObtainTransfer.objects.get()
                self.assertEqual(transfer.state, 'outcome_unknown')
                intent = transfer.binding['completion_intent']
                self.assertEqual(intent, self.policy.intent(str(self.job.request_id)))
                self.assertEqual(transfer.provider_job, '')
                self.assertFalse(self.registry.exists())
            original(method, *args, **kwargs)
        with patch.object(self.http, 'request', side_effect=inspect_intent):
            self.assertEqual(self.submit().status_code, 302)
        fields = self.wire_fields()
        self.assertEqual(fields['nzbname'], ('CC-' + str(self.job.request_id)).encode())
        self.assertEqual(fields['script'], COMPLETION_SCRIPT.encode())
        self.assertEqual(fields['pp'], b'2')
        self.assertEqual(fields['cat'], b'*')
        self.assertEqual(fields['name'], NZB)
        self.assertEqual(set(fields), {'mode', 'output', 'apikey', 'nzbname', 'script', 'pp', 'cat', 'name'})
        entry, transfer = self.registered(), ObtainTransfer.objects.get()
        self.assertIsNone(entry['files'])
        self.assertEqual(entry['root'], str(self.complete / fields['nzbname'].decode()))
        self.assertEqual(entry['result_id'], transfer.binding['exact']['result_id'])
        self.assertEqual(entry['result_digest'], transfer.binding['exact']['digest'])
        self.assertEqual(entry['nzb_sha256'], transfer.nzb_sha256)
        self.assertEqual(entry['scope'], self.job.scope)
        self.assertEqual(self.registry.stat().st_mode & 0o777, 0o600)

    def test_real_submission_registration_hook_and_kodi_contract_remain_separate(self):
        self.review()
        self.assertEqual(self.submit().status_code, 302)
        entry = self.registered()
        hook = self.load_hook()
        env = self.hook_media(entry['nzo_id'])
        self.assertEqual(hook.run(str(self.registry), str(self.receipts), env), 0)
        receipt = json.loads((self.receipts / (entry['request_id'] + '.json')).read_text())
        exported = receipt['handoff'][0]
        self.assertEqual(exported['relative_path'], self.wire_fields()['nzbname'].decode() + '/observed.flac')
        self.assertEqual(exported['provenance']['result_id'], entry['result_id'])
        self.assertEqual(exported['selected_scope'], 'film')
        self.assertEqual(exported['observed_media'], 'audio')
        self.assertEqual(exported['content_identity'], 'unverified')
        self.assertFalse(exported['full_work_verified'])
        self.assertIsNone(exported['acquired_at'])
        self.assertFalse(exported['kodi_music_catalog_eligible'])
        self.assertEqual(receipt['state'], 'POSTPROCESS_FILE_VERIFIED')
        self.assertFalse(receipt['sab_history_completed'])
        self.assertEqual(ObtainTransfer.objects.get().state, 'queued')
        self.assertEqual(self.client.get(self.status + 'local-handoff/').status_code, 409)
        self.assertEqual(FeedbackEvent.objects.count(), 0)
        for change in ({'SAB_STATUS': 'Completed'}, {'SAB_COMPLETE_DIR': str(self.complete / 'wrong')},
                       {'SAB_PP_STATUS': '1'}):
            self.assertEqual(hook.run(str(self.registry), str(self.receipts), env | change), 1)

    def test_publication_failure_repairs_on_status_after_restart_without_provider_calls(self):
        self.review()
        with patch.object(registry, 'publish_registration', side_effect=OSError('fixture disk failure')):
            self.assertEqual(self.submit().status_code, 302)
        transfer = ObtainTransfer.objects.get()
        self.assertEqual((transfer.state, transfer.reason), ('queued', 'registration_pending'))
        self.assertTrue(transfer.provider_job)
        self.assertFalse(self.registry.exists())
        calls = len(self.http.calls)
        importlib.reload(acquire)
        self.assertEqual(self.client.get(self.status).status_code, 200)
        self.assertEqual(self.registered()['nzo_id'], transfer.provider_job)
        self.assertEqual(ObtainTransfer.objects.get().reason, '')
        before = self.registry.read_bytes()
        self.assertEqual(self.submit().status_code, 302)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(len(self.http.calls), calls)

    def test_fast_hook_waits_for_registration_after_acknowledgement(self):
        self.review()
        hook = self.load_hook()
        waiting = threading.Event()
        original_read = hook.read_json
        def observe_wait(*args, **kwargs):
            try:
                return original_read(*args, **kwargs)
            except FileNotFoundError:
                waiting.set()
                raise
        original_request = self.http.request
        future = None
        with ThreadPoolExecutor(max_workers=1) as executor:
            def start_hook(method, *args, **kwargs):
                nonlocal future
                original_request(method, *args, **kwargs)
                if method == 'POST':
                    env = self.hook_media('SABnzbd_nzo_runtime_fixture')
                    future = executor.submit(hook.run, str(self.registry), str(self.receipts), env)
                    self.assertTrue(waiting.wait(2))
            with patch.object(hook, 'read_json', side_effect=observe_wait), patch.object(
                    self.http, 'request', side_effect=start_hook):
                self.assertEqual(self.submit().status_code, 302)
                self.assertEqual(future.result(timeout=6), 0)
        self.assertEqual(len(self.addfile_calls()), 1)
        self.assertEqual(ObtainTransfer.objects.get().state, 'queued')

    def test_atomic_publication_failures_before_and_after_rename_repair_locally(self):
        self.review()
        with patch.object(registry.os, 'replace', side_effect=OSError('fixture before rename')):
            self.assertEqual(self.submit().status_code, 302)
        self.assertFalse(self.registry.exists())
        self.assertEqual(ObtainTransfer.objects.get().reason, 'registration_pending')
        calls = len(self.http.calls)
        rename = registry.os.replace
        def after_rename(*args, **kwargs):
            rename(*args, **kwargs)
            raise OSError('fixture after atomic rename')
        with patch.object(registry.os, 'replace', side_effect=after_rename):
            acquire.transfer_for(self.job)
        before = self.registry.read_bytes()
        self.assertEqual(ObtainTransfer.objects.get().reason, 'registration_pending')
        importlib.reload(acquire)
        self.assertEqual(self.client.get(self.status).status_code, 200)
        self.assertEqual(ObtainTransfer.objects.get().reason, '')
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(len(self.http.calls), calls)

    def test_registry_conflicts_and_duplicate_publishers_preserve_exact_binding(self):
        self.review()
        self.assertEqual(self.submit().status_code, 302)
        entry = self.registered()
        before = self.registry.read_bytes()
        for change in ({'nzb_sha256': 'f' * 64}, {'root': str(self.complete / 'wrong')},
                       {'nzo_id': str(uuid4())}, {'request_id': str(uuid4())},
                       {'nzo_id': str(uuid4()), 'request_id': str(uuid4())}):
            with self.subTest(change=change), self.assertRaises(registry.Conflict):
                registry.publish_registration(str(self.registry), entry | change)
            self.assertEqual(self.registry.read_bytes(), before)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(registry.publish_registration, str(self.registry), entry)
                       for _ in range(4)]
            for future in futures:
                future.result(timeout=6)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_unsafe_registry_collisions_never_complete_or_overwrite(self):
        self.review()
        protected = self.control / 'protected'
        protected.write_bytes(b'fixture: preserve me')
        protected.chmod(0o600)
        self.registry.symlink_to(protected)
        self.assertEqual(self.submit().status_code, 302)
        transfer = ObtainTransfer.objects.get()
        self.assertEqual((transfer.state, transfer.reason), ('queued', 'registration_pending'))
        env, hook = self.hook_media(transfer.provider_job), self.load_hook()
        calls = len(self.http.calls)
        for kind in ('symlink', 'fifo', 'hardlink', 'directory'):
            if kind != 'symlink':
                if kind == 'fifo':
                    os.mkfifo(self.registry, 0o600)
                elif kind == 'hardlink':
                    os.link(protected, self.registry)
                else:
                    self.registry.mkdir(mode=0o700)
            self.assertEqual(self.client.get(self.status).status_code, 200)
            self.assertEqual(hook.run(str(self.registry), str(self.receipts), env), 1)
            self.assertEqual(protected.read_bytes(), b'fixture: preserve me')
            self.assertEqual(list(self.receipts.iterdir()), [])
            if kind == 'directory':
                self.registry.rmdir()
            else:
                self.registry.unlink()
        self.assertEqual(self.client.get(self.status).status_code, 200)
        self.assertEqual(hook.run(str(self.registry), str(self.receipts), env), 0)
        self.assertEqual(len(self.http.calls), calls)

    def test_mismatched_persisted_intent_cannot_be_registered_on_status(self):
        self.review()
        with patch.object(registry, 'publish_registration', side_effect=OSError('fixture failure')):
            self.assertEqual(self.submit().status_code, 302)
        transfer = ObtainTransfer.objects.get()
        original = transfer.binding
        calls = len(self.http.calls)
        for change in ({'root': str(self.complete / 'wrong')}, {'script': 'None'},
                       {'nzbname': 'CC-other-request'}, {'fixed_layout': False}):
            binding = original | {'completion_intent': original['completion_intent'] | change}
            ObtainTransfer.objects.filter(pk=transfer.pk).update(binding=binding)
            self.assertEqual(self.client.get(self.status).status_code, 200)
            self.assertFalse(self.registry.exists())
            self.assertEqual(ObtainTransfer.objects.get().reason, 'registration_pending')
        ObtainTransfer.objects.filter(pk=transfer.pk).update(binding=original)
        self.assertEqual(self.client.get(self.status).status_code, 200)
        self.assertEqual(self.registered()['nzo_id'], transfer.provider_job)
        self.assertEqual(len(self.http.calls), calls)

    def test_distinct_requests_and_uuid_and_legacy_acknowledgements(self):
        self.review()
        self.assertEqual(self.submit().status_code, 302)
        first = self.registered()
        # A second synthetic selection has a distinct source and Hydra identity.
        self.saved = self.series
        original = self.http.request
        new_nzo = str(uuid4())
        def second(method, path, **kwargs):
            original(method, path, **kwargs)
            operation = parse_qs(urlsplit(path).query).get('t', ['addfile'])[0]
            if operation == 'search':
                self.http.payload = json.dumps({'channel': {'item': [listing(
                    title='Fiction Series S01E01 1080p', guid='123456789', category='5000')]}}).encode()
            elif operation == 'get':
                self.http.payload = NZB.replace(b'opaque-private-message', b'another-fiction-message')
            elif operation == 'addfile':
                self.http.payload = json.dumps({'status': True, 'nzo_ids': [new_nzo]}).encode()
        with patch.object(self.http, 'request', side_effect=second):
            self.review()
            self.assertEqual(self.submit().status_code, 302)
        second_entry = self.registered()
        self.assertEqual(second_entry['nzo_id'], new_nzo)
        self.assertNotEqual(first['request_id'], second_entry['request_id'])
        self.assertNotEqual(first['root'], second_entry['root'])
        self.assertNotEqual(self.wire_fields(self.addfile_calls()[0])['nzbname'], self.wire_fields()['nzbname'])
        self.assertEqual(len(json.loads(self.registry.read_text())['requests']), 2)

    def test_invalid_layout_script_and_unsafe_existing_job_refuse_before_send(self):
        self.review()
        for policy in (replace(self.policy, fixed_layout=False), replace(self.policy, script='None'),
                       replace(self.policy, script='../other'), replace(self.policy, pp=3),
                       replace(self.policy, category='unapproved')):
            with override_settings(OBTAIN_SUBMISSION_POLICY=vars(policy)):
                self.assertEqual(self.submit().status_code, 409)
        name = self.policy.intent(str(self.job.request_id))['nzbname']
        collision = self.complete / name
        for kind in ('file', 'directory', 'symlink'):
            if kind == 'file':
                collision.write_bytes(b'untouched')
            elif kind == 'directory':
                collision.mkdir()
            else:
                collision.symlink_to(self.receipts)
            self.assertEqual(self.submit().status_code, 409)
            if kind == 'directory':
                collision.rmdir()
            else:
                collision.unlink()
        self.assertEqual(self.addfile_calls(), [])

    def assert_unknown_acknowledgement(self, response):
        self.review()
        original = self.http.request
        def uncertain(method, *args, **kwargs):
            original(method, *args, **kwargs)
            if method == 'POST':
                self.http.payload = response
        with patch.object(self.http, 'request', side_effect=uncertain):
            self.assertEqual(self.submit().status_code, 302)
        transfer = ObtainTransfer.objects.get()
        self.assertEqual((transfer.state, transfer.provider_job), ('outcome_unknown', ''))
        self.assertFalse(self.registry.exists())
        count = len(self.http.calls)
        importlib.reload(acquire)
        self.assertEqual(self.client.get(self.status).status_code, 200)
        self.assertEqual(self.submit().status_code, 302)
        self.assertEqual(len(self.http.calls), count)
        self.assertEqual(len(self.addfile_calls()), 1)
        self.assertFalse(self.registry.exists())

    def test_missing_acknowledgement_never_registers_or_resends(self):
        self.assert_unknown_acknowledgement(b'{}')

    def test_empty_acknowledgement_never_registers_or_resends(self):
        self.assert_unknown_acknowledgement(b'{"status":true,"nzo_ids":[]}')

    def test_ambiguous_acknowledgement_never_registers_or_resends(self):
        self.assert_unknown_acknowledgement(
            b'{"status":true,"nzo_ids":["SABnzbd_nzo_a","SABnzbd_nzo_b"]}')

    def test_invalid_job_id_never_registers_or_resends(self):
        self.assert_unknown_acknowledgement(b'{"status":true,"nzo_ids":["invented-id"]}')

    def test_malformed_acknowledgement_never_registers_or_resends(self):
        self.assert_unknown_acknowledgement(b'not-json')

    def test_lost_reply_persists_unknown_across_factory_reconstruction(self):
        self.review()
        original = self.http.request
        def lose(method, *args, **kwargs):
            original(method, *args, **kwargs)
            if method == 'POST':
                raise TimeoutError('FICTION-ONLY-secret')
        with patch.object(self.http, 'request', side_effect=lose):
            self.assertEqual(self.submit().status_code, 302)
        self.assertEqual(ObtainTransfer.objects.get().state, 'outcome_unknown')
        self.assertFalse(self.registry.exists())
        self.assertEqual(self.submit().status_code, 302)
        self.assertEqual(len(self.addfile_calls()), 1)

    def completed_fixture(self):
        self.enterContext(override_settings(OBTAIN_COMPLETION_RECEIPTS=str(self.receipts)))
        self.review()
        self.assertEqual(self.submit().status_code, 302)
        transfer = ObtainTransfer.objects.get(job=self.job)
        env = self.hook_media(transfer.provider_job)
        self.assertEqual(self.load_hook().run(str(self.registry), str(self.receipts), env), 0)
        return env

    def consume_fixture(self):
        from shelf.completion_consumer import consume
        return consume(actor=self.owner, profile_id=1, job_id=self.job.pk)

    def test_final_history_export_is_opt_in_and_revalidates_current_files(self):
        from shelf.completion_history import export_completed
        from shelf.completion_consumer import export
        from shelf.acquisition_contract import ContractError, Blocked
        from django.utils import timezone
        env = self.completed_fixture()
        context = acquire.OperatorContext(self.owner.pk, 1, 'offline_test', True)
        args = dict(actor=self.owner, profile_id=1, job_id=self.job.pk, context=context)
        slot = {'nzo_id': ObtainTransfer.objects.get(job=self.job).provider_job,
                'storage': env['SAB_COMPLETE_DIR'], 'status': 'Completed',
                'script': 'culture-cabinet-completion', 'fail_message': '',
                'completed': int(timezone.now().timestamp())}
        calls = len(self.http.calls)
        result = export_completed(**args, history={'history': {'slots': [slot]}})
        repeated = export_completed(**args, history={'history': {'slots': [slot]}})
        self.assertEqual(result['completion_evidence_sha256'], repeated['completion_evidence_sha256'])
        self.assertEqual(result['files'][0]['acquired_at_basis'], 'sab_history_completed_at')
        old = export(actor=self.owner, profile_id=1, job_id=self.job.pk)
        self.assertEqual(old['version'], 1)
        self.assertIsNone(old['files'][0]['acquired_at'])
        self.assertFalse(old['sab_history_completed'])
        self.assertEqual(len(self.http.calls), calls)
        with self.assertRaises(ContractError):
            export_completed(**args, history={'history': {'slots': [{**slot, 'status': 'Running'}]}})
        with self.assertRaises(Blocked):
            export_completed(**{**args, 'context': acquire.OperatorContext(self.owner.pk, 1,
                             'offline_test', False)}, history={'history': {'slots': [slot]}})
        (Path(env['SAB_COMPLETE_DIR']) / 'observed.flac').write_bytes(b'fLaCchanged')
        with self.assertRaises(Blocked):
            export_completed(**args, history={'history': {'slots': [slot]}})

    def test_album_segment_gap_refuses_before_any_addfile(self):
        from shelf.acquisition_contract import ContractError
        self.album_selection()
        original = self.http.request
        segment = re.search(rb'<segment[^>]*>.*?</segment>', NZB).group(0)
        broken = NZB.replace(segment, segment + segment.replace(b'number="1"', b'number="3"'))
        def missing_segment(method, path, **kwargs):
            original(method, path, **kwargs)
            if 't=get' in path:
                self.http.payload = broken
        with patch.object(self.http, 'request', side_effect=missing_segment):
            with self.assertRaisesRegex(ContractError, 'Invalid or unsupported NZB XML'):
                self.submit_album()
        self.assertEqual(self.addfile_calls(), [])
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_owned_hook_automatically_appears_and_exports_without_final_attestation(self):
        from shelf.models import PostprocessFile, PostprocessReceipt, ObtainLocalFile
        from shelf import completion_consumer
        self.completed_fixture()
        calls = len(self.http.calls)
        page = self.client.get('/profiles/1/acquired-files/')
        self.assertContains(page, 'Postprocessing files verified')
        self.assertContains(page, 'observed.flac')
        self.assertContains(page, 'Unknown')
        self.assertEqual(PostprocessFile.objects.count(), 1)
        self.assertFalse(ObtainLocalFile.objects.exists())
        first = PostprocessReceipt.objects.get().receipt
        for _ in range(2):
            importlib.reload(completion_consumer)
            self.assertEqual(self.client.get(self.status).status_code, 200)
            self.assertEqual(self.consume_fixture().receipt, first)
        export = self.client.get(f'/profiles/1/acquired-files/{self.job.pk}/handoff/')
        self.assertEqual(export.status_code, 200)
        item = export.json()['files'][0]
        self.assertEqual(item['media_identity'], 'sha256:' + item['sha256'])
        self.assertEqual(item['provenance']['request_id'], str(self.job.request_id))
        self.assertIsNone(item['acquired_at'])
        self.assertFalse(item['kodi_music_catalog_eligible'])
        self.assertFalse(item['full_work_verified'])
        self.assertIsNone(export.json()['progress'])
        self.assertEqual(PostprocessFile.objects.count(), 1)
        self.assertEqual(ObtainTransfer.objects.get().state, 'queued')
        self.assertEqual(FeedbackEvent.objects.count(), 0)
        self.assertEqual(len(self.http.calls), calls)

    def test_missing_changed_partial_and_extra_bytes_invalidate_previous_library_readiness(self):
        from shelf.models import PostprocessReceipt
        env = self.completed_fixture()
        path = Path(env['SAB_COMPLETE_DIR']) / 'observed.flac'
        original = path.read_bytes()
        self.assertIsNotNone(self.consume_fixture())
        for bad in ('changed', 'missing', 'partial', 'extra', 'symlink', 'hardlink'):
            with self.subTest(bad=bad):
                extra = path.with_name('extra.exe')
                partial = path.with_name('observed.flac.part')
                if bad == 'changed':
                    path.write_bytes(b'x' * len(original))
                elif bad == 'missing':
                    path.unlink()
                elif bad == 'partial':
                    path.rename(partial)
                elif bad == 'extra':
                    extra.write_bytes(b'synthetic executable name only')
                elif bad == 'symlink':
                    path.rename(partial)
                    path.symlink_to(partial)
                else:
                    os.link(path, extra)
                self.assertIsNone(self.consume_fixture())
                self.assertEqual(PostprocessReceipt.objects.get().state, 'unavailable')
                self.assertNotContains(self.client.get('/profiles/1/acquired-files/'), 'observed.flac')
                if path.is_symlink():
                    path.unlink()
                if partial.exists():
                    partial.unlink()
                if extra.exists():
                    extra.unlink()
                path.write_bytes(original)
                self.assertIsNotNone(self.consume_fixture())

    def test_receipt_bindings_and_generated_fields_fail_closed_without_reading_media(self):
        from shelf import completion_consumer
        from shelf.models import PostprocessReceipt
        self.completed_fixture()
        target = self.receipts / (str(self.job.request_id) + '.json')
        original = json.loads(target.read_text())
        self.assertIsNotNone(self.consume_fixture())
        changes = ({'nzo_id': 'SABnzbd_nzo_foreign'}, {'result_digest': 'f' * 64},
                   {'nzb_sha256': 'f' * 64}, {'result_id': str(uuid4())},
                   {'request_id': str(uuid4())}, {'sab_status': 'Completed'},
                   {'sab_pp_status': 1}, {'sab_history_completed': True},
                   {'full_work_verified': True}, {'repair_verified': True},
                   {'files': None}, {'extra': True})
        for change in changes:
            with self.subTest(change=change):
                target.write_text(json.dumps(original | change))
                with patch.object(completion_consumer, 'verify_media') as verify:
                    self.assertIsNone(self.consume_fixture())
                    verify.assert_not_called()
                self.assertEqual(PostprocessReceipt.objects.get().state, 'unavailable')
        target.write_text(json.dumps(original))
        self.assertIsNotNone(self.consume_fixture())
        target.unlink()
        self.assertIsNone(self.consume_fixture())

    def test_cancel_foreign_owner_revocation_unknown_and_stale_binding_cannot_consume(self):
        from django.http import Http404
        from shelf import completion_consumer, saved
        from shelf.models import PostprocessFile
        self.completed_fixture()
        transfer = ObtainTransfer.objects.get()
        from django.contrib.auth import get_user_model
        foreign = get_user_model().objects.create_user(username='foreign-fixture')
        with self.assertRaises(Http404):
            completion_consumer.consume(actor=foreign, profile_id=1, job_id=self.job.pk)
        self.assertFalse(PostprocessFile.objects.exists())
        for state, provider_job in [('outcome_unknown', ''), ('failed', transfer.provider_job),
                                    ('cancel_pending', transfer.provider_job)]:
            ObtainTransfer.objects.filter(pk=transfer.pk).update(state=state, provider_job=provider_job)
            self.assertIsNone(self.consume_fixture())
        ObtainTransfer.objects.filter(pk=transfer.pk).update(state='queued', provider_job=transfer.provider_job)
        original = transfer.result_guid
        ObtainTransfer.objects.filter(pk=transfer.pk).update(result_guid='9876')
        self.assertIsNone(self.consume_fixture())
        ObtainTransfer.objects.filter(pk=transfer.pk).update(result_guid=original)
        self.assertIsNotNone(self.consume_fixture())
        Grant.objects.filter(pk=self.grant.pk).update(can_select=False)
        with self.assertRaises(Http404):
            self.consume_fixture()
        Grant.objects.filter(pk=self.grant.pk).update(can_select=True)
        saved.cancel(actor=self.owner, profile_id=1, job_id=self.job.pk)
        self.assertIsNone(self.consume_fixture())
        self.assertEqual(self.client.get(f'/profiles/1/acquired-files/{self.job.pk}/handoff/').status_code, 409)

    def test_consumer_atomic_failure_and_local_retry_do_not_resubmit(self):
        from shelf.models import PostprocessFile, PostprocessReceipt
        self.completed_fixture()
        calls = len(self.http.calls)
        with patch.object(PostprocessFile.objects, 'bulk_create', side_effect=OSError('synthetic disk failure')):
            self.assertIsNone(self.consume_fixture())
        self.assertFalse(PostprocessReceipt.objects.exists())
        self.assertFalse(PostprocessFile.objects.exists())
        self.assertIsNotNone(self.consume_fixture())
        self.assertEqual(len(self.http.calls), calls)

    def album_selection(self, *, identity=None, title='Fiction Artist - Fiction Album FLAC',
                        category='3040', caps=None):
        from shelf.album_acquisition import prepare_album
        self.album_identity = identity or {
            'namespace': 'licensed_catalogue', 'artist_id': 'synthetic-artist',
            'release_id': 'synthetic-release', 'artist': 'Fiction Artist',
            'album': 'Fiction Album', 'track_count': 2, 'source': 'Synthetic release catalogue'}
        music_caps = caps or CAPS.replace(b'</categories>',
            b'<category id="3040" name="Lossless"/></categories>')
        original = self.http.request
        def music(method, path, **kwargs):
            original(method, path, **kwargs)
            operation = parse_qs(urlsplit(path).query).get('t', ['addfile'])[0]
            if operation == 'caps':
                self.http.payload = music_caps
            elif operation == 'search':
                self.assertEqual(parse_qs(urlsplit(path).query)['cat'], ['3040'])
                self.http.payload = json.dumps({'channel': {'item': [listing(
                    title=title, category=category)]}}).encode()
        with patch.object(self.http, 'request', side_effect=music):
            self.job = prepare_album(actor=self.owner, profile_id=1, identity=self.album_identity,
                authorization={'rights_basis': 'Synthetic fixture permission only', 'one_album': True,
                               'storage_checked': True, 'duplicates_checked': True},
                selected_guid=listing()['guid'], selected_title=title)
        return self.job

    def submit_album(self):
        services = runtime.submission_factory(actor=self.owner, profile_id=1, job=self.job)
        options = dict(actor=self.owner, profile_id=1, job_id=self.job.pk, context=services.context,
                       retriever=services.retriever, max_bytes=services.max_bytes,
                       complete_root=services.complete_root, test_only=True,
                       completion_intent=services.completion_intent)
        review = acquire.review_submission(**options, evidence=services.evidence)
        return acquire.submit(**options, backend=services.backend, token=review['token'], confirmed=True)

    def test_album_release_separators_cannot_hide_exclusions_before_dispatch(self):
        from shelf.acquisition_contract import ContractError
        from shelf.models import AlbumRequest
        for separator in ('_', '.', '-', '/', '\u2014'):
            for marker in ('pack', 'discography', 'encrypted', 'protected', 'box' + separator + 'set'):
                with self.subTest(separator=separator, marker=marker):
                    with self.assertRaises(ContractError):
                        self.album_selection(title='Fiction Artist - Fiction Album FLAC '
                                             + separator + marker + separator)
                        self.submit_album()
                    self.assertEqual(self.addfile_calls(), [])
        self.assertFalse(AlbumRequest.objects.exists())
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_album_subject_markers_refuse_real_retrieval_before_submission_review(self):
        from shelf.acquisition_contract import ContractError
        self.album_selection()
        original = self.http.request
        for separator in (' ', '_', '.', '-', '/', '\u2014'):
            for marker in ('passworded', 'password-protected', 'encrypted', 'protected',
                           'discography', 'pack'):
                with self.subTest(separator=separator, marker=marker):
                    subject = 'album.flac' + separator + marker.upper() + separator
                    def protected(method, path, **kwargs):
                        original(method, path, **kwargs)
                        if 't=get' in path:
                            self.http.payload = NZB.replace(b'subject="fiction"',
                                ('subject="' + subject + '"').encode())
                    calls = len(self.http.calls)
                    with patch.object(self.http, 'request', side_effect=protected):
                        with self.assertRaisesRegex(ContractError, 'Invalid or unsupported NZB XML'):
                            self.submit_album()
                    self.assertEqual(len(self.http.calls), calls + 1)
                    self.assertIn('t=get', self.http.calls[-1][1])
                    self.assertEqual(self.addfile_calls(), [])
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_album_cached_descriptor_tampering_refuses_before_addfile(self):
        from shelf.acquisition_contract import ContractError
        from shelf.models import ObtainReviewedNZB
        self.album_selection()
        services = runtime.submission_factory(actor=self.owner, profile_id=1, job=self.job)
        options = dict(actor=self.owner, profile_id=1, job_id=self.job.pk, context=services.context,
                       retriever=services.retriever, max_bytes=services.max_bytes,
                       complete_root=services.complete_root, test_only=True,
                       completion_intent=services.completion_intent)
        review = acquire.review_submission(**options, evidence=services.evidence)
        ObtainReviewedNZB.objects.update(payload=NZB.replace(b'subject="fiction"',
                                                b'subject="album.flac_PASSWORDED_"'))
        calls = len(self.http.calls)
        with self.assertRaisesRegex(ContractError, 'Reviewed NZB changed'):
            acquire.submit(**options, backend=services.backend, token=review['token'], confirmed=True)
        self.assertEqual(len(self.http.calls), calls)
        self.assertEqual(self.addfile_calls(), [])
        self.assertFalse(ObtainTransfer.objects.exists())

    def test_benign_underscore_flac_release_and_subject_reach_addfile(self):
        self.album_selection(title='Fiction_Artist_-_Fiction_Album_fLaC_Packard_Shellac_24bit')
        original = self.http.request
        def benign(method, path, **kwargs):
            original(method, path, **kwargs)
            if 't=get' in path:
                self.http.payload = NZB.replace(b'subject="fiction"',
                                               b'subject="01_Packard-Shellac_Unprotected.FLAC"')
        calls = len(self.http.calls)
        with patch.object(self.http, 'request', side_effect=benign):
            self.assertEqual(self.submit_album().state, 'queued')
        self.assertEqual(sum('t=get' in call[1] for call in self.http.calls[calls:]), 1)
        self.assertEqual(len(self.addfile_calls()), 1)

    def test_complete_album_uses_real_factory_registration_hook_consumer_and_separate_authorization(self):
        from shelf.models import AlbumRequest, PostprocessFile, SavedItem
        from shelf.completion_consumer import export
        self.enterContext(override_settings(OBTAIN_COMPLETION_RECEIPTS=str(self.receipts)))
        before_saved = SavedItem.objects.count()
        job = self.album_selection()
        self.assertEqual(len(self.addfile_calls()), 0)
        review_page = self.client.get(f'/profiles/1/saved/jobs/{job.pk}/')
        self.assertContains(review_page, 'One complete FLAC album')
        review_url = f'/profiles/1/saved/jobs/{job.pk}/submit/review/'
        review_page = self.client.post(review_url, self.hidden(review_page, review_url))
        self.assertContains(review_page, 'One complete FLAC album')
        self.assertNotContains(review_page, 'Series — S01E01 only')
        transfer = self.submit_album()
        self.assertEqual(transfer.binding['selection']['media'], 'music')
        self.assertEqual(transfer.state, 'queued')
        self.assertEqual(self.registered()['scope'], 'album_flac')
        env = self.hook_media(transfer.provider_job)
        path = Path(env['SAB_COMPLETE_DIR'])
        (path / 'observed.flac').rename(path / '01.flac')
        (path / '02.flac').write_bytes(b'fLaCsecond synthetic track, marker only; no encoded audio')
        env['SAB_FILES'] = '["01.flac", "02.flac"]'
        self.assertEqual(self.load_hook().run(str(self.registry), str(self.receipts), env), 0)
        self.assertIsNotNone(self.consume_fixture())
        result = export(actor=self.owner, profile_id=1, job_id=job.pk)
        self.assertEqual(len(result['files']), 2)
        self.assertEqual(result['authorization']['album_identity'], self.album_identity)
        self.assertIn('rights_basis', result['authorization']['user_authorization'])
        self.assertTrue(all(item['content_identity'] == 'unverified' for item in result['files']))
        self.assertTrue(all(item['acquired_at'] is None for item in result['files']))
        self.assertEqual(AlbumRequest.objects.get().pk, job.source_id)
        self.assertEqual(PostprocessFile.objects.count(), 2)
        self.assertEqual(SavedItem.objects.count(), before_saved)
        self.assertEqual(FeedbackEvent.objects.count(), 0)
        self.assertContains(self.client.get('/profiles/1/acquired-files/'), '02.flac')
        (path / '02.flac').unlink()
        self.assertIsNone(self.consume_fixture())

    def test_album_partial_track_set_and_identity_mutation_are_not_imported(self):
        from shelf.models import AlbumRequest, PostprocessFile
        self.enterContext(override_settings(OBTAIN_COMPLETION_RECEIPTS=str(self.receipts)))
        self.album_selection()
        transfer = self.submit_album()
        env = self.hook_media(transfer.provider_job)  # only one of the selected two tracks
        self.assertEqual(self.load_hook().run(str(self.registry), str(self.receipts), env), 0)
        self.assertIsNone(self.consume_fixture())
        self.assertFalse(PostprocessFile.objects.exists())
        album = AlbumRequest.objects.get()
        album.identity = album.identity | {'track_count': 1}
        album.save(update_fields=['identity'])
        self.assertIsNone(self.consume_fixture())
        self.assertFalse(PostprocessFile.objects.exists())

    def test_album_actual_lossless_caps_and_exact_artist_album_and_single_release_required(self):
        from shelf.models import AlbumRequest
        from shelf.acquisition_contract import ContractError
        for options in ({'caps': CAPS}, {'category': '3030'}, {'category': '3000'},
                        {'title': 'Other Artist - Fiction Album FLAC'},
                        {'title': 'Fiction Artist - Other Album FLAC'},
                        {'title': 'Fiction Artist - Fiction Album MP3'},
                        {'title': 'Fiction Artist - Fiction Album FLAC discography'},
                        {'title': 'Fiction Artist - Fiction Album FLAC pack'},
                        {'title': 'Fiction Artist - Fiction Album FLAC protected'},
                        {'title': 'Fiction Artist - Fiction Album FLAC exe'}):
            with self.subTest(options=options), self.assertRaises(ContractError):
                self.album_selection(**options)
        self.assertFalse(AlbumRequest.objects.exists())
        self.assertEqual(self.addfile_calls(), [])

    def test_album_ids_are_explicit_and_authorization_change_blocks_submission(self):
        from shelf.models import AlbumRequest
        from shelf.acquisition_contract import ContractError
        identity = {'namespace': 'musicbrainz', 'artist_id': str(uuid4()), 'release_id': str(uuid4()),
                    'artist': 'Fiction Artist', 'album': 'Fiction Album', 'track_count': 2,
                    'source': 'Invented UUID fixture; no MusicBrainz lookup'}
        with self.assertRaises(ContractError):
            self.album_selection(identity=identity | {'release_id': '123'})
        self.album_selection(identity=identity)
        services = runtime.submission_factory(actor=self.owner, profile_id=1, job=self.job)
        options = dict(actor=self.owner, profile_id=1, job_id=self.job.pk, context=services.context,
                       retriever=services.retriever, max_bytes=services.max_bytes,
                       complete_root=services.complete_root, test_only=True,
                       completion_intent=services.completion_intent)
        review = acquire.review_submission(**options, evidence=services.evidence)
        album = AlbumRequest.objects.get()
        album.authorization = album.authorization | {'rights_basis': 'Changed permission'}
        album.save(update_fields=['authorization'])
        with self.assertRaises(ContractError):
            acquire.submit(**options, backend=services.backend, token=review['token'], confirmed=True)
        self.assertEqual(self.addfile_calls(), [])

    def test_consumer_restart_on_reopened_synthetic_database_recovers_one_file(self):
        import sqlite3
        from django.db import connection
        from django.db.backends.sqlite3._functions import register
        from shelf.models import PostprocessFile
        self.completed_fixture()
        database = Path(self.folder.name) / 'consumer-restart.sqlite3'
        raw = sqlite3.connect(database, isolation_level=None)
        connection.connection.backup(raw)
        register(raw)
        try:
            with patch.object(connection, 'connection', raw):
                self.assertIsNotNone(self.consume_fixture())
                first = list(PostprocessFile.objects.values_list('relative_path', 'sha256', 'verified_at'))
        finally:
            raw.close()
        raw = sqlite3.connect(database, isolation_level=None)
        register(raw)
        try:
            with patch.object(connection, 'connection', raw):
                from shelf import completion_consumer
                importlib.reload(completion_consumer)
                self.assertIsNotNone(self.consume_fixture())
                self.assertEqual(list(PostprocessFile.objects.values_list(
                    'relative_path', 'sha256', 'verified_at')), first)
        finally:
            raw.close()

    def test_cancellation_during_file_check_cannot_publish_stale_readiness(self):
        from shelf import completion_consumer, saved
        from shelf.models import PostprocessFile
        self.completed_fixture()
        verify = completion_consumer.verify_media
        def cancel_after_read(entry):
            files = verify(entry)
            saved.cancel(actor=self.owner, profile_id=1, job_id=self.job.pk)
            return files
        with patch.object(completion_consumer, 'verify_media', side_effect=cancel_after_read):
            self.assertIsNone(self.consume_fixture())
        self.assertFalse(PostprocessFile.objects.exists())
        self.assertEqual(ObtainTransfer.objects.get().state, 'cancel_pending')

    def test_album_marker_mismatch_and_duplicate_tracks_never_form_complete_set(self):
        from shelf.models import PostprocessFile
        self.enterContext(override_settings(OBTAIN_COMPLETION_RECEIPTS=str(self.receipts)))
        self.album_selection()
        transfer = self.submit_album()
        env = self.hook_media(transfer.provider_job)
        path = Path(env['SAB_COMPLETE_DIR'])
        env['SAB_FILES'] = '["observed.flac", "02.flac"]'
        (path / '02.flac').write_bytes(b'MZsynthetic executable marker, not executable code')
        hook = self.load_hook()
        self.assertEqual(hook.run(str(self.registry), str(self.receipts), env), 1)
        self.assertIsNone(self.consume_fixture())
        (path / '02.flac').write_bytes((path / 'observed.flac').read_bytes())
        self.assertEqual(hook.run(str(self.registry), str(self.receipts), env), 0)
        self.assertIsNone(self.consume_fixture())
        self.assertFalse(PostprocessFile.objects.exists())

    def test_interleaved_duplicate_consumers_converge_without_invalidating_each_other(self):
        from shelf import completion_consumer
        from shelf.models import PostprocessFile, PostprocessReceipt
        self.completed_fixture()
        verify = completion_consumer.verify_media
        nested = False
        def interleave(entry):
            nonlocal nested
            files = verify(entry)
            if not nested:
                nested = True
                self.assertIsNotNone(self.consume_fixture())
            return files
        with patch.object(completion_consumer, 'verify_media', side_effect=interleave):
            self.assertIsNotNone(self.consume_fixture())
        self.assertEqual(PostprocessFile.objects.count(), 1)
        self.assertEqual(PostprocessReceipt.objects.get().state, 'postprocess_verified')

    def test_live_factory_submits_the_reviewed_nzb_without_a_second_hydra_get(self):
        self.review()
        original = self.http.request
        def no_refetch(method, path, **kwargs):
            self.assertNotEqual(parse_qs(urlsplit(path).query).get('t'), ['get'])
            original(method, path, **kwargs)
        with patch.object(self.http, 'request', side_effect=no_refetch):
            self.assertEqual(self.submit().status_code, 302)
            self.assertEqual(self.submit().status_code, 302)
        self.assertEqual(len(self.addfile_calls()), 1)
        self.assertEqual(self.wire_fields()['name'], NZB)

    def test_exact_history_observer_binds_final_identity_and_preserves_unknown_states(self):
        from shelf.completion_observer import observe
        from django.utils import timezone
        env = self.completed_fixture()
        entry = self.registered()
        args = dict(actor=self.owner, profile_id=1, job_id=self.job.pk,
                    context=acquire.OperatorContext(self.owner.pk, 1, 'offline_test', True))
        slot = {'nzo_id': entry['nzo_id'], 'name': self.wire_fields()['nzbname'].decode(),
                'storage': env['SAB_COMPLETE_DIR'], 'status': 'Completed',
                'script': 'culture-cabinet-completion', 'fail_message': '',
                'completed': int(timezone.now().timestamp())}
        self.assertEqual(observe(**args, reader=lambda _: None)['state'], 'pending')
        result = observe(**args, reader=lambda _: slot)
        self.assertEqual(result['state'], 'completed')
        identity = result['handoff']['source_identity']
        self.assertEqual(identity['source_id'], self.job.source_id)
        self.assertEqual(identity['edition'], self.job.edition)
        self.assertEqual(identity['basis'], 'selected_catalogue_identity_not_content_verification')
        self.assertEqual(result['handoff']['completion_evidence']['source_identity'], identity)
        self.assertFalse(result['handoff']['files'][0]['full_work_verified'])
        for change, state in (({'status': 'Running'}, 'pending'),
                              ({'nzo_id': 'SABnzbd_nzo_foreign'}, 'unknown'),
                              ({'name': 'foreign'}, 'unknown'),
                              ({'storage': env['SAB_COMPLETE_DIR'] + '-wrong'}, 'unknown')):
            response = observe(**args, reader=lambda _: slot | change)
            self.assertEqual(response['state'], state)
            self.assertNotIn('private detail', str(response))
            self.assertNotIn('handoff', response)
        (Path(env['SAB_COMPLETE_DIR']) / 'observed.flac').write_bytes(b'changed')
        self.assertEqual(observe(**args, reader=lambda _: slot)['state'], 'unknown')
        # Failure is now durable and terminal, so exercise it after nonterminal
        # observations rather than expecting this request to become queued again.
        response = observe(**args, reader=lambda _: slot | {'status': 'Failed', 'fail_message': 'private detail'})
        self.assertEqual(response['state'], 'failed')
        self.assertNotIn('private detail', str(response))
        self.assertNotIn('handoff', response)
