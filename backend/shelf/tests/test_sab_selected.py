"""Exact saved SAB text and private synthetic output files; no service calls."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shelf import completion_files, sab_selected as sab
from shelf.obtain_tv_flow import SelectedJobBinding, ServiceGeneration


class SelectedSabEvidenceTests(TestCase):
    def setUp(self):
        self.scratch = TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.complete_root = Path(self.scratch.name)
        self.request = '7c035cd0-51eb-4e66-9096-68e1fe57a301'
        self.name = 'CC-' + self.request
        self.root = self.complete_root / self.name
        self.root.mkdir()
        self.payload = b'Synthetic selected video bytes.\n'
        (self.root / 'S01E02.mkv').write_bytes(self.payload)
        self.now = datetime(2026, 11, 8, tzinfo=timezone.utc)
        self.job = SelectedJobBinding(request_id=self.request, job_id=11, actor_id=22,
            profile_id=33, publication_origin_digest=None, binding_digest='1' * 64,
            selection_digest='2' * 64, review_digest='3' * 64, episode_scope='episode_s01e02',
            result_id='7c035cd0-51eb-4e66-9096-68e1fe57a302', result_guid='123',
            result_digest='4' * 64, nzb_sha256='5' * 64, nzo_id='SABnzbd_nzo_fixture',
            completion_policy_digest='6' * 64, complete_root=str(self.complete_root),
            destination_ref='fixture-destination', test_only=True, content_identity_digest='7' * 64,
            service_generations=(ServiceGeneration('sab_history', '8' * 64, '9' * 32, 'a' * 32),))
        self.entry = {'nzo_id': self.job.nzo_id, 'request_id': self.request,
            'result_id': self.job.result_id, 'result_digest': self.job.result_digest,
            'nzb_sha256': self.job.nzb_sha256, 'complete_root': str(self.complete_root),
            'root': str(self.root), 'files': None, 'scope': self.job.episode_scope,
            'max_bytes': 1024, 'test_only': True}
        self.files = [{'path': 'S01E02.mkv', 'bytes': len(self.payload),
                       'sha256': hashlib.sha256(self.payload).hexdigest()}]
        self.hook = self.make_hook(self.files)
        self.history = {'nzo_id': self.job.nzo_id, 'name': self.name,
            'status': 'Completed', 'storage': str(self.root), 'script': 'culture-cabinet-completion',
            'fail_message': '', 'completed': int((self.now - timedelta(seconds=1)).timestamp())}

    def make_hook(self, files):
        return completion_files.completion_receipt(
            completion_files.postprocess_evidence(self.entry, files, sum(f['bytes'] for f in files)),
            (self.now - timedelta(seconds=2)).isoformat())

    def observe(self, *, entry=None, hook=None, history=None):
        return sab.check_selected_evidence(self.job, self.entry if entry is None else entry,
            self.hook if hook is None else hook, self.history if history is None else history,
            observed_at=self.now)

    def test_completed_requires_actual_registered_bytes_and_preserves_unobserved_repair(self):
        result = self.observe()
        self.assertEqual(result.state, 'completed')
        self.assertEqual((result.repair, result.unpack), ('unobserved', 'unobserved'))
        self.assertEqual(result.files[0].sha256, hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(len(result.source_manifest_digest), 64)
        self.assertEqual((result.job.actor_id, result.job.profile_id), (22, 33))
        self.assertNotIn('title', repr(result))

    def test_pending_and_failure_are_selected_and_do_not_hash_outputs(self):
        with patch.object(completion_files, 'verify_media') as verify:
            result = sab.check_selected_evidence(self.job, self.entry, None, None, observed_at=self.now)
            self.assertEqual(result.state, 'pending')
            self.assertEqual(self.observe(history={**self.history, 'status': 'Repairing'}).state, 'pending')
            failed = self.observe(history={**self.history, 'status': 'Failed', 'fail_message': 'PRIVATE_ERROR'})
            self.assertEqual(failed.state, 'failed')
            self.assertNotIn('PRIVATE_ERROR', repr(failed))
            verify.assert_not_called()
        hook = {**self.hook, 'sab_pp_status': 1}
        self.assertEqual(self.observe(hook=hook).state, 'failed')

    def test_foreign_registration_hook_history_and_guessed_repair_never_complete(self):
        for changes in ({'nzo_id': 'SABnzbd_nzo_other'}, {'request_id': 'foreign'},
                        {'result_digest': '0' * 64}, {'nzb_sha256': '0' * 64},
                        {'root': str(self.complete_root / 'foreign')}, {'scope': 'episode_s02e02'},
                        {'test_only': False}):
            with self.subTest(entry=changes):
                self.assertEqual(self.observe(entry={**self.entry, **changes}).state, 'unavailable')
        for changes in ({'nzo_id': 'SABnzbd_nzo_other'}, {'name': 'foreign'},
                        {'storage': str(self.complete_root)}, {'script': 'Default'},
                        {'completed': int(self.now.timestamp()) + 1}, {'fail_message': 'warning'}):
            with self.subTest(history=changes):
                self.assertEqual(self.observe(history={**self.history, **changes}).state, 'unavailable')
        for changes in ({'repair_verified': True}, {'crc_verified': True},
                        {'nzb_sha256': '0' * 64}, {'total_bytes': 1}, {'sab_pp_status': True},
                        {'verified_at': (self.now + timedelta(seconds=1)).isoformat()}):
            with self.subTest(hook=changes):
                self.assertEqual(self.observe(hook={**self.hook, **changes}).state, 'unavailable')

    def test_missing_extra_duplicate_symlink_and_corrupt_files_never_complete(self):
        file = self.root / 'S01E02.mkv'
        file.write_bytes(b'x' * len(self.payload))
        self.assertEqual(self.observe().state, 'unavailable')
        file.unlink()
        self.assertEqual(self.observe().state, 'unavailable')
        outside = self.complete_root / 'outside.fixture'
        outside.write_bytes(self.payload)
        file.symlink_to(outside)
        self.assertEqual(self.observe().state, 'unavailable')
        file.unlink()
        file.write_bytes(self.payload)
        (self.root / 'unregistered.fixture').write_bytes(b'Unexpected')
        self.assertEqual(self.observe().state, 'unavailable')
        (self.root / 'unregistered.fixture').unlink()
        duplicated = deepcopy(self.hook)
        duplicated['files'] *= 2
        self.assertEqual(self.observe(hook=duplicated).state, 'unavailable')
        extra = self.root / 'S01E03.mkv'
        extra.write_bytes(self.payload)
        files = self.files + [{**self.files[0], 'path': extra.name}]
        self.assertEqual(self.observe(hook=self.make_hook(files)).state, 'unavailable')

    def test_manifest_digest_binds_the_exact_job_and_verified_file(self):
        first = self.observe()
        different = replace(self.job, binding_digest='f' * 64)
        second = sab.check_selected_evidence(different, self.entry, self.hook, self.history, observed_at=self.now)
        self.assertEqual(second.state, 'completed')
        self.assertNotEqual(first.source_manifest_digest, second.source_manifest_digest)
        self.assertEqual(first.files, second.files)
