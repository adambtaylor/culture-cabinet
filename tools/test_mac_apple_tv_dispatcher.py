"""Offline orchestration tests: fake SSH and importer, real private journals."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import mac_apple_tv_dispatcher as module

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
REQUEST = '11111111-1111-4111-8111-111111111111'


class Runtime:
    def __init__(self):
        self.now = NOW
        self.calls, self.children, self.feedback = [], [], []
        self.fail_feedback = False
        self.interrupt_child = False
        self.page_version, self.page_scope = 2, 'apple_tv_episodes'
        self.contract = {'version': 1, 'destination': 'apple_tv', 'contract_sha256': 'c'*64,
            'selection': {'provenance': {'request_id': REQUEST},
                'source_identity': {'provider': 'tmdb', 'medium': 'tv', 'scope': 'series_s01e01'}}}
        self.job = {'job_id': 1, 'request_id': REQUEST, 'test_only': False, 'dispatchable': True,
            'apple_tv_state': 'not_reported', 'source_identity': {'provider': 'tmdb', 'medium': 'tv', 'scope': 'series_s01e01'},
            'contract_sha256': 'c'*64}
        self.receipt = {'version': 1, 'contract_sha256': 'c'*64, 'request_id': REQUEST,
            'status': 'unknown', 'checked_at': NOW.isoformat(), 'import_attempts': 1}

    def validate_receipt(self, receipt, contract):
        if receipt['request_id'] != contract['selection']['provenance']['request_id'] or receipt['contract_sha256'] != contract['contract_sha256']:
            raise ValueError('foreign fixture receipt')
        return receipt

    def run(self, command, payload=b'', **kwargs):
        self.calls.append((command, payload))
        if command[0] != 'ssh':
            self.children.append(command)
            if self.interrupt_child:
                raise TimeoutError('synthetic child result lost')
            state = Path(command[command.index('--state-dir')+1])
            module.files.save_receipt(state / (REQUEST+'.json'), self.receipt)
            return 0, b'{}'
        args = shlex.split(command[-1])
        script = Path(args[2]).name
        if script == 'list_apple_tv_jobs.py':
            value = {'version': self.page_version, 'scope': self.page_scope, 'state': 'listed', 'actor_id': 1,
                'profile_id': 1, 'after': int(args[args.index('--after')+1]), 'limit': 16,
                'next_cursor': 1, 'has_more': False, 'jobs': [self.job]}
        elif script == 'export_apple_tv_job.py':
            value = self.contract
        else:
            self.feedback.append(payload)
            if self.fail_feedback:
                raise TimeoutError('synthetic feedback acknowledgement lost')
            receipt = json.loads(payload)
            value = {'recorded': True, 'job': 1, 'state': receipt['status'], 'playback_started': False}
        return 0, json.dumps(value).encode()


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.runtime = Runtime()
        fake_consumer = SimpleNamespace(validate_receipt=self.runtime.validate_receipt)
        fake_media = SimpleNamespace(validate_contract=lambda value: value)
        adapters = patch.dict(sys.modules, {'mac_apple_tv_consumer': fake_consumer, 'apple_tv_media': fake_media})
        adapters.start()
        self.addCleanup(adapters.stop)
        self.worker = module.Dispatcher(module.base.EXPIRY, module.base.RENEWAL,
            root=Path(self.folder.name).resolve()/'private', runner=self.runtime.run, clock=lambda: self.runtime.now)

    def journal(self):
        return self.worker.read(self.worker.jobs/'1.json')

    def advance(self):
        self.runtime.now += timedelta(seconds=61)

    def test_explicit_s02e02_uses_same_durable_dispatch_protocol(self):
        identity = {'provider': 'tmdb', 'medium': 'tv', 'scope': 'episode_s02e02'}
        self.runtime.job['source_identity'] = deepcopy(identity)
        self.runtime.contract['selection']['source_identity'] = deepcopy(identity)
        self.worker.tick()
        self.assertEqual(len(self.runtime.children), 1)
        self.assertEqual(self.journal()['contract']['selection']['source_identity'], identity)
        self.assertEqual(len(self.runtime.feedback), 1)

    def test_legacy_pilot_listing_still_dispatches_legacy_default(self):
        self.runtime.page_version, self.runtime.page_scope = 1, 'apple_tv_pilots'
        self.worker.tick()
        self.assertEqual(len(self.runtime.children), 1)
        self.assertEqual(len(self.runtime.feedback), 1)

    def test_legacy_pilot_listing_rejects_explicit_episode_before_dispatch(self):
        self.runtime.page_version, self.runtime.page_scope = 1, 'apple_tv_pilots'
        for scope in ('episode_s02e02', 'episode_s01e01'):
            self.runtime.job['source_identity']['scope'] = scope
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, 'Legacy pilot listing'):
                self.worker.tick()
        self.assertEqual(self.runtime.children, [])
        self.assertEqual(self.runtime.feedback, [])
        self.assertFalse((self.worker.root / 'cursor.json').exists())

    def test_listing_version_scope_pair_is_strict(self):
        for version, scope in ((1, 'apple_tv_episodes'), (2, 'apple_tv_pilots'),
                               (3, 'apple_tv_episodes'), (True, 'apple_tv_pilots')):
            self.runtime.page_version, self.runtime.page_scope = version, scope
            with self.subTest(version=version, scope=scope), self.assertRaisesRegex(ValueError, 'Invalid Apple TV page'):
                self.worker.tick()
        self.assertEqual(self.runtime.children, [])
        self.assertEqual(self.runtime.feedback, [])

    def test_independent_commands_and_unknown_feedback(self):
        self.worker.tick()
        self.assertEqual(len(self.runtime.children), 1)
        self.assertEqual(self.journal()['ack_state'], 'unknown')
        commands = [shlex.split(call[0][-1])[2] for call in self.runtime.calls if call[0][0] == 'ssh']
        self.assertEqual([Path(value).name for value in commands],
            ['list_apple_tv_jobs.py', 'export_apple_tv_job.py', 'record_apple_tv_completion.py'])
        self.assertIn('--expires', self.runtime.children[0])
        self.assertNotIn('Kodi', str(self.journal()))

    def test_feedback_retries_frozen_bytes_before_resuming_same_job(self):
        self.runtime.fail_feedback = True
        self.worker.tick()
        original = self.runtime.feedback[0]
        frozen = self.journal()['pending']
        self.assertEqual(frozen.encode(), original)
        changed = dict(self.runtime.receipt, checked_at=(NOW+timedelta(seconds=20)).isoformat())
        module.files.save_receipt(self.worker.consumer_state/(REQUEST+'.json'), changed)
        self.advance()
        self.runtime.fail_feedback = False
        self.worker.tick()
        self.assertEqual(self.runtime.feedback[1], original)
        self.assertEqual(len(self.runtime.children), 1)
        self.assertEqual(self.journal()['ack_state'], 'unknown')

    def test_child_uncertainty_without_receipt_never_launches_again(self):
        self.runtime.interrupt_child = True
        self.worker.tick()
        self.assertEqual(self.journal()['phase'], 'reserved')
        self.advance()
        self.worker.tick()
        self.assertEqual(len(self.runtime.children), 1)
        self.assertEqual(self.runtime.feedback, [])

    def test_interrupted_child_cleanup_keeps_reservation_and_never_relaunches(self):
        child = SimpleNamespace(pid=12345, stdout=SimpleNamespace(close=lambda: None),
                                wait=lambda timeout=None: 0)
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.register.side_effect = TimeoutError('synthetic interrupted read')
        def run(command, payload=b'', **kwargs):
            if command[0] == 'ssh':
                return self.runtime.run(command, payload, **kwargs)
            self.runtime.children.append(command)
            return module.bounded_run(command, payload, **kwargs)
        self.worker.runner = run
        with patch.object(module.base.subprocess, 'Popen', return_value=child), patch.object(
                module.base.selectors, 'DefaultSelector', return_value=selector), patch.object(
                module.base.os, 'killpg') as kill:
            self.worker.tick()
            self.assertEqual(kill.call_args_list[0].args[1], module.base.signal.SIGTERM)
        self.assertEqual(self.journal()['phase'], 'reserved')
        self.advance()
        self.worker.tick()
        self.assertEqual(len(self.runtime.children), 1)
        self.assertEqual(self.runtime.feedback, [])

    def test_missing_unknown_receipt_cannot_start_import(self):
        self.runtime.job['apple_tv_state'] = 'unknown'
        self.worker.tick()
        self.assertEqual(self.runtime.children, [])

    def test_terminal_observed_is_not_reimported(self):
        self.runtime.receipt['status'] = 'library_import_observed'
        self.worker.tick()
        self.advance()
        self.worker.tick()
        self.assertEqual(len(self.runtime.children), 1)
        self.assertEqual(self.journal()['ack_state'], 'library_import_observed')

    def test_foreign_contract_is_rejected_before_child(self):
        self.runtime.contract['contract_sha256'] = 'd'*64
        self.worker.tick()
        self.assertEqual(self.runtime.children, [])

    def test_expiry_prevents_all_remote_or_child_work(self):
        self.runtime.now = self.worker.expires
        with self.assertRaises(ValueError):
            self.worker.tick()
        self.assertEqual(self.runtime.calls, [])

    def test_unknown_does_not_upload_changed_same_state_or_regress(self):
        self.worker.tick()
        self.runtime.receipt.update(status='blocked')
        self.advance()
        self.worker.tick()
        self.assertEqual(len(self.runtime.feedback), 1)
        self.assertEqual(self.journal()['ack_state'], 'unknown')


class ChildCleanupTests(unittest.TestCase):
    def lifecycle(self, *, grace, stubborn=False):
        events = []
        child = SimpleNamespace(pid=12345, stdout=SimpleNamespace(close=lambda: events.append(('close',))))
        def wait(timeout=None):
            events.append(('wait', timeout))
            if stubborn and timeout == 5:
                raise module.base.subprocess.TimeoutExpired('synthetic child', timeout)
            return 0
        child.wait = wait
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.register.side_effect = TimeoutError('synthetic interrupted child read')
        with patch.object(module.base.subprocess, 'Popen', return_value=child), patch.object(
                module.base.selectors, 'DefaultSelector', return_value=selector), patch.object(
                module.base.os, 'killpg', side_effect=lambda pid, sig: events.append(('signal', sig))):
            with self.assertRaises(TimeoutError):
                module.base.bounded_run(['synthetic-child'], timeout=10, expires=NOW+timedelta(hours=1),
                    clock=lambda: NOW, cleanup_grace=grace)
        return events

    def test_opted_in_child_gets_sigterm_and_wait_before_reaping(self):
        self.assertEqual(self.lifecycle(grace=5), [('signal', module.base.signal.SIGTERM),
            ('wait', 5), ('wait', None), ('close',)])

    def test_stubborn_child_gets_sigkill_only_after_bounded_grace(self):
        self.assertEqual(self.lifecycle(grace=5, stubborn=True), [('signal', module.base.signal.SIGTERM),
            ('wait', 5), ('signal', module.base.signal.SIGKILL), ('wait', None), ('close',)])

    def test_existing_default_retains_immediate_kill_and_reap(self):
        self.assertEqual(self.lifecycle(grace=0), [('signal', module.base.signal.SIGKILL),
            ('wait', None), ('close',)])

    def test_apple_tv_wrapper_explicitly_opts_in(self):
        with patch.object(module.base, 'bounded_run', return_value=(0, b'{}')) as run:
            module.bounded_run(['synthetic-child'], timeout=10, expires=NOW+timedelta(hours=1), clock=lambda: NOW)
        self.assertEqual(run.call_args.kwargs['cleanup_grace'], 5)


if __name__ == '__main__':
    unittest.main()
