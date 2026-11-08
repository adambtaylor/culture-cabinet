"""Selected destination worker with temporary synthetic bytes; no remote channel."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shelf import jellyfin_tv_import as imp


class Bomb:
    def read(self, *_):
        raise AssertionError('Reconciliation must not request source bytes')


class SelectedDestinationTests(TestCase):
    def setUp(self):
        self.scratch = TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.root = self.base / 'library'
        self.root.mkdir()
        self.payload = b'Synthetic non-playable selected episode.\n'
        self.source = self.base / 'source.fixture'
        self.source.write_bytes(self.payload)
        self.now = datetime(2026, 11, 8, tzinfo=timezone.utc)
        self.plan = imp.CopyPlan(
            request_id='7c035cd0-51eb-4e66-9096-68e1fe57a301',
            job_id=303,
            nzo_id='SABnzbd_nzo_fixture', binding_digest='1' * 64,
            content_identity_digest='2' * 64, source_manifest_digest='3' * 64,
            destination_binding_digest='4' * 64,
            intent='7c035cd0-51eb-4e66-9096-68e1fe57a303',
            host_id=socket.gethostname(), root=str(self.root),
            relative_path='Fixture.Release.S01E02/S01E02.mkv', size=len(self.payload),
            sha256=hashlib.sha256(self.payload).hexdigest(), scope='episode_s01e02',
            expires_at=(self.now + timedelta(seconds=60)).isoformat(),
            test_only=True, create_parent=True)
        self.target = self.root / self.plan.relative_path

    def call(self, operation='receive', source=None, plan=None):
        return imp.receive_selected(plan or self.plan,
            io.BytesIO(self.payload) if source is None else source, operation=operation,
            allowed_roots=(str(self.root),), clock=lambda: self.now)

    def test_inspect_is_read_only_then_copy_hashes_actual_destination(self):
        result = self.call('inspect', Bomb())
        self.assertEqual((result.status, result.reason), ('pending', 'intent_not_started'))
        self.assertEqual(list(self.root.iterdir()), [])
        with self.source.open('rb', buffering=0) as source:
            result = self.call(source=source)
        self.assertEqual(result.status, 'verified')
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertEqual(result.receipt.sha256, hashlib.sha256(self.target.read_bytes()).hexdigest())
        self.assertEqual(result.receipt.host_id, socket.gethostname())
        self.assertTrue(result.receipt.test_only)
        self.assertEqual(self.call('inspect', Bomb()).status, 'verified')
        self.assertEqual(self.call(source=Bomb()).status, 'verified')

    def test_existing_foreign_destination_is_never_overwritten_or_adopted(self):
        self.target.parent.mkdir()
        self.target.write_bytes(self.payload)
        result = self.call(source=Bomb())
        self.assertEqual(result.status, 'conflict')
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertIsNone(result.receipt)

    def test_lost_ack_after_atomic_link_reconciles_without_recopy(self):
        link = os.link
        def crash(*args, **kwargs):
            link(*args, **kwargs)
            raise SystemExit('Simulated abrupt child exit')
        with patch.object(imp.os, 'link', side_effect=crash), self.assertRaises(SystemExit):
            self.call()
        self.assertTrue(self.target.exists())
        result = self.call('inspect', Bomb())
        self.assertEqual((result.status, result.reason), ('pending', 'awaiting_finalize'))
        self.assertEqual(self.target.stat().st_nlink, 2)
        result = self.call(source=Bomb())
        self.assertEqual(result.status, 'verified')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertEqual(result.receipt.sha256, self.plan.sha256)

    def test_prepublication_crash_finalizes_verified_staging_without_recopy(self):
        with patch.object(imp.os, 'link', side_effect=SystemExit), self.assertRaises(SystemExit):
            self.call()
        self.assertFalse(self.target.exists())
        result = self.call('inspect', Bomb())
        self.assertEqual((result.status, result.reason), ('pending', 'awaiting_finalize'))
        self.assertEqual(self.call(source=Bomb()).status, 'verified')
        self.assertEqual(self.target.read_bytes(), self.payload)

    def test_partial_corrupt_and_extra_input_remain_unverified_without_reset(self):
        for payload in (self.payload[:-1], b'x' * len(self.payload), self.payload + b'extra'):
            with self.subTest(payload=payload), TemporaryDirectory(dir=self.base) as root:
                plan = replace(self.plan, root=root)
                result = imp.receive_selected(plan, io.BytesIO(payload), operation='receive',
                                              allowed_roots=(root,), clock=lambda: self.now)
                self.assertEqual(result.status, 'unknown')
                self.assertFalse((Path(root) / plan.relative_path).exists())
                result = imp.receive_selected(plan, Bomb(), operation='receive',
                                              allowed_roots=(root,), clock=lambda: self.now)
                self.assertEqual(result.status, 'unknown')

    def test_corrupted_replaced_or_symlinked_destination_loses_verification(self):
        self.assertEqual(self.call().status, 'verified')
        self.target.write_bytes(b'x' * len(self.payload))
        self.assertEqual(self.call('inspect', Bomb()).status, 'unknown')
        self.target.unlink()
        self.target.symlink_to(self.source)
        self.assertEqual(self.call('inspect', Bomb()).status, 'conflict')
        self.assertEqual(self.source.read_bytes(), self.payload)

    def test_changed_intent_or_file_identity_refuses_existing_journal(self):
        self.assertEqual(self.call().status, 'verified')
        for changes in ({'intent': '7c035cd0-51eb-4e66-9096-68e1fe57a304'},
                        {'sha256': '0' * 64}, {'binding_digest': '0' * 64},
                        {'destination_binding_digest': '0' * 64}):
            with self.subTest(changes=changes):
                self.assertEqual(self.call(source=Bomb(), plan=replace(self.plan, **changes)).status, 'conflict')
        self.assertEqual(self.target.read_bytes(), self.payload)

    def test_path_scope_host_deadline_and_symlink_parents_refuse(self):
        for changes in ({'relative_path': '../S01E02.mkv'}, {'root': '/tmp'},
                        {'relative_path': 'Fixture.Release/S01E02E03.mkv'},
                        {'relative_path': 'Fixture.Release/S01E02-E03.mkv'},
                        {'relative_path': 'Fixture.Release/S01E02.mkv.part'},
                        {'host_id': 'a-different-host'}, {'expires_at': self.now.isoformat()}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self.call(source=Bomb(), plan=replace(self.plan, **changes)).status, 'verified')
        outside = self.base / 'outside'
        outside.mkdir()
        self.target.parent.symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.call(source=Bomb()).status, 'verified')
        self.assertEqual(list(outside.iterdir()), [])

    def test_actual_worker_control_path_is_bounded_and_sanitized(self):
        script = Path(__file__).resolve().parents[3] / 'tools/jellyfin_tv_file_worker.py'
        spec = importlib.util.spec_from_file_location('selected_file_worker', script)
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        original = imp.receive_selected
        def receiver(plan, source, *, operation):
            return original(plan, source, operation=operation,
                            allowed_roots=(str(self.root),), clock=lambda: self.now)
        frame = json.dumps(asdict(self.plan)).encode() + b'\n' + self.payload
        with patch.object(worker, 'receive_selected', side_effect=receiver):
            result = worker.run('receive', io.BytesIO(frame))
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['receipt']['sha256'], self.plan.sha256)
        for body in (b'{"request_id":1,"request_id":2}\n', b'x' * (worker.MAX_CONTROL + 1), b'PRIVATE_BAD_JSON\n'):
            result = worker.run('receive', io.BytesIO(body))
            self.assertEqual(result['status'], 'unknown')
            self.assertNotIn('PRIVATE_BAD_JSON', repr(result))
        self.assertEqual(worker.run('delete', io.BytesIO(frame))['status'], 'unknown')
