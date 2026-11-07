"""Synthetic persisted signals and private ledgers; no runtime or inference I/O."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase

from shelf import synthesis, weekly_synthesis as weekly
from shelf.acquisition_contract import Blocked
from shelf import authored_discovery
from shelf.models import AuthoredDiscoveryState, Grant, Profile, SavedItem, SynthesisRun


class WeeklySynthesisTests(TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory).resolve() / 'cursor.json'
        self.actor = get_user_model().objects.create_user(pk=1, username='demo-curator')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='Synthetic owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.saved = SavedItem.objects.create(actor=self.actor, profile=self.profile,
            provider='hardcover', medium='book', source_id=123, title='Synthetic persisted book',
            list_kind='favorites', url='https://example.invalid/book', fetched_at=weekly.FIRST_DUE)
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('No network')))
        self.enterContext(patch.object(synthesis, 'reserve_operator', side_effect=AssertionError('No inference reservation')))

    def tick(self, **kwargs):
        return weekly.tick(now=kwargs.pop('now', weekly.FIRST_DUE),
            session_guard=kwargs.pop('session_guard', lambda: weekly.FIXED_EXPIRY),
            ledger_path=self.path, **kwargs)

    def test_before_due_is_database_free(self):
        with self.assertNumQueries(0):
            status = self.tick(now=weekly.FIRST_DUE - timedelta(seconds=1))
        self.assertEqual(status['status'], 'not_due')
        self.assertFalse(self.path.exists())

    def test_due_uses_persisted_only_and_restart_cannot_repeat(self):
        status = self.tick()
        row = SynthesisRun.objects.get()
        self.assertEqual(status['status'], 'PENDING_REVIEW')
        self.assertEqual(row.input_snapshot['signals'][0]['title'], self.saved.title)
        self.assertEqual(row.evidence, {'scheduled_review': {'due_utc': '2026-11-01T21:00:00Z',
            'cadence': weekly.CADENCE, 'approved': False, 'automatic_model_call_cap': 0}})
        self.assertEqual((row.state, row.attempts), ('queued', 0))
        self.assertEqual(status['next_due_utc'], '2026-11-08T21:00:00Z')
        self.assertTrue(status['schedule_expired'])
        self.assertEqual(status['automatic_model_calls'], 0)
        self.saved.title = 'Later persisted change'
        self.saved.save()
        with patch.object(synthesis, 'snapshot', side_effect=AssertionError('No recapture')):
            self.assertEqual(self.tick()['status'], 'expired')
        self.assertEqual(SynthesisRun.objects.count(), 1)

    def test_existing_states_reused_without_any_mutation(self):
        current = synthesis.snapshot(self.actor, 1)
        for state in ('queued', 'completed', 'failed', 'unknown', 'running'):
            with self.subTest(state=state):
                if self.path.exists():
                    self.path.unlink()
                SynthesisRun.objects.all().delete()
                row = SynthesisRun.objects.create(actor=self.actor, profile=self.profile,
                    input_digest=current['input_digest'], input_snapshot=current,
                    state=state, attempts=0 if state == 'queued' else 1,
                    evidence={'manual': 'preserve'}, output={'synthetic': 'preserve'})
                before = SynthesisRun.objects.values().get(pk=row.pk)
                status = self.tick()
                self.assertTrue(status['reused'])
                self.assertEqual(status['status'], 'reused')
                self.assertEqual(status['run_state'], state)
                self.assertEqual(SynthesisRun.objects.values().get(pk=row.pk), before)

    def test_reservation_survives_crash_without_new_snapshot(self):
        with patch.object(SynthesisRun.objects, 'get_or_create', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                self.tick()
        reserved = json.loads(self.path.read_text())
        self.assertEqual(reserved['phase'], 'reserved')
        self.saved.title = 'Changed after reservation'
        self.saved.save()
        with patch.object(synthesis, 'snapshot', side_effect=AssertionError('No recapture')):
            self.tick()
        self.assertEqual(SynthesisRun.objects.get().input_digest, reserved['input_digest'])
        self.assertEqual(SynthesisRun.objects.get().input_snapshot['signals'][0]['title'], 'Synthetic persisted book')

    def test_commit_cursor_failure_reconciles_existing_receipt(self):
        real_write = weekly.write_cursor
        def fail_completion(path, cursor):
            if cursor['phase'] == 'completed':
                raise OSError('synthetic completion write failure')
            real_write(path, cursor)
        with patch.object(weekly, 'write_cursor', side_effect=fail_completion):
            with self.assertRaises(OSError):
                self.tick()
        row = SynthesisRun.objects.get()
        with patch.object(synthesis, 'snapshot', side_effect=AssertionError('No recapture')):
            status = self.tick()
        self.assertTrue(status['reused'])
        self.assertEqual(status['run_id'], str(row.pk))
        self.assertEqual(SynthesisRun.objects.count(), 1)

    def test_uncertain_reservation_write_never_creates_before_recovery(self):
        real_replace = os.replace
        def uncertain(source, target):
            real_replace(source, target)
            raise OSError('synthetic lost acknowledgement')
        with patch.object(weekly.os, 'replace', side_effect=uncertain):
            with self.assertRaises(OSError):
                self.tick()
        self.assertFalse(SynthesisRun.objects.exists())
        with patch.object(synthesis, 'snapshot', side_effect=AssertionError('No recapture')):
            self.tick()
        self.assertEqual(SynthesisRun.objects.count(), 1)

    def test_expiry_and_next_anchor_never_queue(self):
        for now in (weekly.FIXED_EXPIRY, weekly.FIRST_DUE + weekly.WEEK * 20):
            with self.assertNumQueries(0):
                self.assertEqual(self.tick(now=now)['status'], 'expired')
        self.assertFalse(SynthesisRun.objects.exists())

    def test_guard_rechecked_before_creation_and_current_grant_required(self):
        with self.assertRaises(Blocked):
            self.tick(session_guard=lambda: None)
        guard = iter([weekly.FIXED_EXPIRY, weekly.FIXED_EXPIRY, weekly.FIRST_DUE])
        self.assertEqual(self.tick(session_guard=lambda: next(guard))['status'], 'expired')
        self.assertFalse(SynthesisRun.objects.exists())
        self.grant.can_select = False
        self.grant.save()
        with self.assertRaises(Blocked):
            self.tick()

    def test_revision_provenance_is_part_of_digest(self):
        self.tick()
        row = SynthesisRun.objects.get()
        AuthoredDiscoveryState.objects.create(actor=self.actor, profile=self.profile,
            mode='household', revision=1, nonce='synthetic', data=authored_discovery.empty_data())
        current = synthesis.snapshot(self.actor, 1)
        self.assertEqual(current['signals'], row.input_snapshot['signals'])
        self.assertNotEqual(current['input_digest'], row.input_digest)

    def test_corrupt_oversized_and_unsafe_cursors_fail_closed(self):
        for raw in ('{', '{}', 'x' * (weekly.MAX_LEDGER + 1)):
            self.path.write_text(raw)
            self.path.chmod(0o600)
            with self.assertRaises(Blocked):
                self.tick()
        self.path.unlink()
        target = self.path.parent / 'target'
        target.write_text('{}')
        target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.tick()
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaises(Blocked):
            self.tick()
        self.assertFalse(SynthesisRun.objects.exists())


class WeeklyLockTests(SimpleTestCase):
    def test_concurrent_reservations_are_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'cursor.json'
            def claim(_):
                with weekly.locked(path):
                    if path.exists():
                        return False
                    weekly.write_cursor(path, {'synthetic': 'one claim'})
                    return True
            with ThreadPoolExecutor(max_workers=4) as pool:
                claimed = list(pool.map(claim, range(12)))
            self.assertEqual(sum(claimed), 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
