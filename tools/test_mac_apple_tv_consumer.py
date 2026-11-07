"""Synthetic delivery/recovery tests. No SSH, TV, Swift or real media access."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import timedelta
import errno
import hashlib
import os
from pathlib import Path
import shutil
import struct
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

import mac_apple_tv_consumer as tv
from test_mac_episode_consumer import fixture as episode_fixture
from test_mac_kodi_consumer import NOW, PAYLOAD


def fixture(request_id=None):
    completion, selection = episode_fixture(request_id=request_id)
    episode = {'show': selection['source_identity']['title'], 'season': 1, 'episode': 1,
               'title': 'S01E01', 'source_id': selection['source_identity']['source_id'], 'basis': 'selected_job_scope'}
    item = completion['files'][0]
    value = {'version': 1, 'destination': 'apple_tv', 'completion': completion, 'selection': selection,
             'episode': episode, 'source': {'relative_path': item['relative_path'], 'sha256': item['sha256'], 'bytes': item['size']}}
    value['contract_sha256'] = tv.digest(value)
    return value


def native():
    return {'is_playable': True, 'duration_seconds': 2.5, 'playback_started': False,
            'tracks': [{'type': 'vide', 'formats': [{'codec': 'hvc1'}]},
                       {'type': 'soun', 'language': 'eng', 'formats': [{'codec': 'ec-3'}]}]}


def derivative(contract):
    from apple_tv_media import episode_metadata
    return {'version': 1, 'contract_sha256': contract['contract_sha256'], 'source_sha256': contract['source']['sha256'],
            'remote_path': tv.REMOTE + '/.apple-tv-work/' + contract['contract_sha256'] + '/episode.mp4',
            'sha256': hashlib.sha256(PAYLOAD).hexdigest(), 'bytes': len(PAYLOAD),
            'validation': {'valid': True, 'plan_sha256': 'a' * 64, 'duration_seconds': 2.5,
                           'metadata': episode_metadata(contract['episode']),
                           'packet_integrity': {kind: {'source_packets': 1, 'derivative_packets': 1,
                                                      'identical_packet_payload_hashes': True} for kind in ('video', 'audio')},
                           'hdr_display_verified': False,
                           'atoms': {'video_sample_entry': 'hvc1', 'audio_sample_entry': 'ec-3'}, 'atmos': True}}


@contextmanager
def interrupted_child():
    """Fake a signal while an owned child is blocked in its stdout selector."""
    import mac_completion_dispatcher as dispatcher
    child = MagicMock()
    child.pid = 123456
    child.wait.return_value = 0
    selector = MagicMock()
    selector.__enter__.return_value = selector
    selector.get_map.return_value = {'stdout': True}
    def terminate(*args):
        tv.signal.getsignal(tv.signal.SIGTERM)(tv.signal.SIGTERM, None)
    selector.select.side_effect = terminate
    with patch.object(tv.subprocess, 'Popen', return_value=child), \
            patch.object(tv.selectors, 'DefaultSelector', return_value=selector), \
            patch.object(dispatcher.os, 'killpg') as kill:
        yield child, kill


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.watch = self.root / 'Media' / 'Automatically Add to TV.localized'
        self.watch.mkdir(parents=True)
        self.state = self.root / 'state'
        self.contract = fixture()
        self.calls = []
        self.library_result = None
        self.watch_patch = patch.object(tv, 'WATCHFOLDER', self.watch)
        self.watch_patch.start()
        self.addCleanup(self.watch_patch.stop)

    def remux(self, contract, **kwargs):
        self.calls.append('remux')
        return derivative(contract)

    def transfer(self, result, path, **kwargs):
        self.calls.append('transfer')
        with path.open('xb') as stream:
            stream.write(PAYLOAD)

    def probe(self, path, **kwargs):
        self.calls.append('native')
        return native()

    def library(self, episode, result, **kwargs):
        self.calls.append('library')
        return deepcopy(self.library_result)

    def proof(self):
        return {'path': str(self.root / 'Library' / 'asset.mp4'), 'bytes': len(PAYLOAD),
                'sha256': hashlib.sha256(PAYLOAD).hexdigest(),
                'metadata': {**{k: self.contract['episode'][k] for k in ('show', 'season', 'episode')},
                             'title': derivative(self.contract)['validation']['metadata']['title']}, 'native': native()}

    def consume(self, **kwargs):
        options = dict(execute=True, remux=self.remux, transfer=self.transfer,
                       native_probe=self.probe, library=self.library, clock=lambda: NOW, timeout=10)
        options.update(kwargs)
        return tv.consume(self.contract, self.state, **options)

    def test_default_validation_does_not_create_state_or_call_adapter(self):
        result = self.consume(execute=False)
        self.assertEqual(result['reason'], 'execution_not_authorized')
        self.assertFalse(self.state.exists())
        self.assertEqual(self.calls, [])

    def test_full_contract_binding_rejects_changed_episode(self):
        self.contract['episode']['episode'] = 2
        with self.assertRaises(ValueError):
            self.consume()
        self.assertFalse(self.state.exists())

    def test_delivery_is_pending_not_library_observation(self):
        result = self.consume()
        self.assertEqual(result['status'], 'handoff_pending')
        self.assertEqual(result['import_attempts'], 1)
        self.assertFalse(result['playback_started'])
        self.assertEqual(Path(result['handoff']['path']).read_bytes(), PAYLOAD)
        self.assertFalse(list((self.watch.parent / '.cabinet-apple-tv-staging').glob('*.mp4')))
        tv.validate_receipt(result, self.contract)

    def test_disappeared_handoff_stays_unknown_without_second_import(self):
        first = self.consume()
        Path(first['handoff']['path']).unlink()
        self.calls.clear()
        second = self.consume()
        self.assertEqual(second['status'], 'unknown')
        self.assertEqual(self.calls, ['library'])
        self.assertEqual(second['import_attempts'], 1)

    def test_exact_library_observation_is_terminal(self):
        first = self.consume()
        self.library_result = self.proof()
        second = self.consume()
        self.assertEqual(second['status'], 'library_import_observed')
        self.assertEqual(second['library']['native'], second['native'])
        tv.validate_receipt(second, self.contract)
        self.calls.clear()
        self.library_result = None
        self.assertEqual(self.consume(), second)
        self.assertEqual(self.calls, [])

    def test_library_before_transfer_avoids_copy_and_import(self):
        self.library_result = self.proof()
        result = self.consume()
        self.assertEqual(result['status'], 'library_import_observed')
        self.assertEqual(result['import_attempts'], 0)
        self.assertEqual(self.calls, ['remux', 'library'])
        tv.validate_receipt(result, self.contract)

    def test_wrong_library_metadata_never_claims_observation(self):
        self.library_result = self.proof()
        self.library_result['metadata']['episode'] = 2
        self.assertEqual(self.consume()['status'], 'blocked')
        self.assertNotIn('transfer', self.calls)

    def test_native_failure_blocks_handoff_and_reuses_staging(self):
        result = self.consume(native_probe=lambda *a, **kw: dict(native(), is_playable=False))
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['import_attempts'], 0)
        self.assertFalse(list(self.watch.iterdir()))
        self.calls.clear()
        self.assertEqual(self.consume()['status'], 'handoff_pending')
        self.assertNotIn('transfer', self.calls)

    def test_native_cannot_change_staged_media(self):
        def corrupt(path, **kwargs):
            path.write_bytes(b'wrong')
            return native()
        result = self.consume(native_probe=corrupt)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['import_attempts'], 0)

    def test_failed_rename_reservation_is_not_retried(self):
        with patch.object(tv, 'atomic_handoff', side_effect=OSError(errno.EXDEV, 'cross-device')):
            first = self.consume()
        self.assertEqual(first['status'], 'unknown')
        self.assertEqual(first['import_attempts'], 1)
        self.calls.clear()
        with patch.object(tv, 'atomic_handoff', side_effect=AssertionError('second handoff')):
            self.assertEqual(self.consume()['status'], 'unknown')
        self.assertEqual(self.calls, ['library'])

    def test_crash_after_reservation_before_move_is_read_only_on_restart(self):
        with patch.object(tv, 'atomic_handoff', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.consume()
        self.calls.clear()
        result = self.consume()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.calls, ['library'])

    def test_crash_after_move_before_checkpoint_does_not_reimport(self):
        real_save = tv.save_receipt
        def crash(path, receipt):
            if (receipt.get('handoff') or {}).get('delivered_at'):
                raise KeyboardInterrupt
            real_save(path, receipt)
        with patch.object(tv, 'save_receipt', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.consume()
        self.assertEqual(len(list(self.watch.iterdir())), 1)
        self.calls.clear()
        self.assertEqual(self.consume()['status'], 'handoff_pending')
        self.assertEqual(self.calls, ['library'])

    def test_signal_after_handoff_reaps_native_child_and_preserves_reservation(self):
        reads = []
        def interrupted_library(episode, result, **kwargs):
            reads.append('library')
            if len(reads) == 1:
                return None
            return tv.live_native_probe(self.root / 'synthetic-library.mp4', timeout=5)
        with interrupted_child() as (child, kill), tv.termination_signals():
            result = self.consume(library=interrupted_library)
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(result['import_attempts'], 1)
            kill.assert_called_once_with(child.pid, tv.signal.SIGKILL)
            child.wait.assert_called_once_with()
            child.stdout.close.assert_called_once_with()
            # Further shutdown signals must not abort the final receipt write.
            tv.signal.getsignal(tv.signal.SIGHUP)(tv.signal.SIGHUP, None)
        persisted = tv.read_json(self.state / (result['request_id'] + '.json'), private=True)
        self.assertEqual(persisted, result)
        self.calls.clear()
        with patch.object(tv, 'atomic_handoff', side_effect=AssertionError('second import')):
            resumed = self.consume()
        self.assertEqual(resumed['status'], 'handoff_pending')
        self.assertEqual(resumed['import_attempts'], 1)
        self.assertEqual(self.calls, ['library'])

    def assert_advancing_checkpoint_survives_crash(self, delivered):
        current, snapshots = [NOW], []
        def clock():
            current[0] += timedelta(milliseconds=1)
            return current[0]
        real_save = tv.save_receipt
        def crash_after_durable_checkpoint(path, receipt):
            tv.validate_receipt(receipt, self.contract)
            snapshots.append(deepcopy(receipt))
            real_save(path, receipt)
            handoff = receipt.get('handoff')
            if handoff and bool(handoff['delivered_at']) == delivered:
                raise KeyboardInterrupt
        with patch.object(tv, 'save_receipt', side_effect=crash_after_durable_checkpoint):
            with self.assertRaises(KeyboardInterrupt):
                self.consume(clock=clock)
        persisted = tv.read_json(self.state / (self.contract['selection']['provenance']['request_id'] + '.json'), private=True)
        self.assertEqual(persisted, snapshots[-1])
        self.assertEqual(persisted['status'], 'handoff_pending')
        handoff = persisted['handoff']
        self.assertLess(persisted['created_at'], handoff['reserved_at'])
        self.assertLess(handoff['reserved_at'], persisted['checked_at'])
        if delivered:
            self.assertLess(handoff['reserved_at'], handoff['delivered_at'])
            self.assertLess(handoff['delivered_at'], persisted['checked_at'])
        self.calls.clear()
        with patch.object(tv, 'atomic_handoff', side_effect=AssertionError('second import')):
            result = self.consume(clock=clock)
        self.assertEqual(result['status'], 'handoff_pending' if delivered else 'unknown')
        self.assertEqual(result['import_attempts'], 1)
        self.assertEqual(self.calls, ['library'])

    def test_advancing_clock_reservation_checkpoint_is_valid_after_crash(self):
        self.assert_advancing_checkpoint_survives_crash(delivered=False)

    def test_advancing_clock_delivery_checkpoint_is_valid_after_crash(self):
        self.assert_advancing_checkpoint_survives_crash(delivered=True)

    def test_insufficient_disk_does_not_transfer(self):
        with patch.object(tv.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 99, 1)):
            result = self.consume()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'insufficient_disk_space')
        self.assertEqual(result['import_attempts'], 0)
        self.assertNotIn('transfer', self.calls)

    def test_cross_device_staging_blocks_before_transfer_or_reservation(self):
        original = Path.stat
        def different_device(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path.name == '.cabinet-apple-tv-staging':
                values = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
                values['st_dev'] += 1
                return types.SimpleNamespace(**values)
            return info
        with patch.object(Path, 'stat', different_device):
            result = self.consume()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['import_attempts'], 0)
        self.assertNotIn('transfer', self.calls)

    def test_expiry_after_transfer_prevents_native_and_handoff(self):
        current = [NOW]
        def expire(result, path, **kwargs):
            self.transfer(result, path, **kwargs)
            current[0] = tv.datetime.fromisoformat(tv.EXPIRY)
        result = self.consume(transfer=expire, clock=lambda: current[0])
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['import_attempts'], 0)
        self.assertNotIn('native', self.calls)

    def test_native_wrong_codec_and_language_block(self):
        for change in ('codec', 'language'):
            with self.subTest(change=change):
                def wrong(*args, **kwargs):
                    result = native()
                    if change == 'codec':
                        result['tracks'][0]['formats'][0]['codec'] = 'avc1'
                    else:
                        result['tracks'][1]['language'] = 'und'
                    return result
                result = self.consume(native_probe=wrong)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['import_attempts'], 0)

    def test_watchfolder_missing_is_not_created(self):
        self.watch.rmdir()
        self.assertEqual(self.consume()['status'], 'blocked')
        self.assertFalse(self.watch.exists())

    def test_changed_remote_identity_blocks_transfer(self):
        def bad(contract, **kwargs):
            return dict(derivative(contract), remote_path='/tmp/not-authorized.mp4')
        self.assertEqual(self.consume(remux=bad)['status'], 'blocked')
        self.assertNotIn('transfer', self.calls)

    def test_actionable_refusal_preserves_safe_category_not_child_message(self):
        def refused(contract, **kwargs):
            return {'version': 1, 'status': 'blocked', 'contract_sha256': contract['contract_sha256'],
                    'source_sha256': contract['source']['sha256'], 'reason': 'unsupported_subtitles',
                    'message': 'untrusted private child payload'}
        result = self.consume(remux=refused)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'unsupported_subtitles')
        self.assertNotIn(b'untrusted private child payload', tv.encoded(result))
        self.assertNotIn('transfer', self.calls)
        self.assertEqual(result['import_attempts'], 0)
        self.assertEqual(self.consume(remux=refused)['reason'], 'unsupported_subtitles')

    def test_unbound_or_unrecognized_refusal_cannot_persist_external_text(self):
        for change in ({'source_sha256': 'f' * 64}, {'reason': 'external arbitrary error'}):
            with self.subTest(change=change):
                def refused(contract, **kwargs):
                    return dict({'version': 1, 'status': 'blocked', 'contract_sha256': contract['contract_sha256'],
                                 'source_sha256': contract['source']['sha256'], 'reason': 'unsupported_subtitles'}, **change)
                result = self.consume(remux=refused)
                self.assertEqual(result['reason'], 'verification_or_adapter_blocked')
                self.assertNotIn('transfer', self.calls)

    def test_transfer_hash_mismatch_blocks_native_and_handoff(self):
        def bad(result, path, **kwargs):
            path.write_bytes(b'x' * len(PAYLOAD))
        result = self.consume(transfer=bad)
        self.assertEqual(result['status'], 'blocked')
        self.assertNotIn('native', self.calls)
        self.assertEqual(result['import_attempts'], 0)

    def test_same_source_episode_new_request_only_reconciles(self):
        self.consume()
        self.calls.clear()
        self.contract = fixture('33333333-3333-4333-8333-333333333333')
        result = self.consume()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'duplicate_source_episode_reconciliation_required')
        self.assertEqual(self.calls, ['library'])
        self.assertEqual(len(list(self.watch.iterdir())), 1)
        tv.validate_receipt(result, self.contract)

    def test_expired_session_refuses_adapters(self):
        with self.assertRaises(ValueError):
            self.consume(expires='2026-10-29T00:00:00+00:00')
        self.assertEqual(self.calls, [])

    def test_exclusive_atomic_move_preserves_existing_target(self):
        source, target = self.root / 'source', self.root / 'target'
        source.write_bytes(b'new')
        target.write_bytes(b'old')
        with self.assertRaises(OSError):
            tv.atomic_handoff(source, target)
        self.assertEqual(source.read_bytes(), b'new')
        self.assertEqual(target.read_bytes(), b'old')


def atom(kind, payload):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def metadata_mp4(episode, title):
    items = b''
    for kind, value in ((b'tvsh', episode['show']), (b'tvsn', episode['season']),
                        (b'tves', episode['episode']), (b'\xa9nam', title)):
        value = value.to_bytes(4, 'big') if type(value) is int else value.encode()
        items += atom(kind, atom(b'data', b'\x00' * 8 + value))
    return atom(b'moov', atom(b'udta', atom(b'meta', b'\x00' * 4 + atom(b'ilst', items))))


class ExactObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / 'known-library.mp4'
        self.episode = {'show': 'Synthetic Show', 'season': 2, 'episode': 1}
        payload = metadata_mp4(self.episode, 'Synthetic title')
        self.path.write_bytes(payload)
        self.record = {'basis': 'owner_confirmed_existing_import', 'source_sha256': 'a' * 64,
                       'library_path': str(self.path), 'sha256': hashlib.sha256(payload).hexdigest(),
                       'bytes': len(payload), 'duration_seconds': 2.5, 'title': 'Synthetic title', 'episode': self.episode,
                       'video_sample_entry': 'hvc1', 'audio_sample_entry': 'ec-3', 'atmos': True}

    def test_manual_basis_proof_hash_metadata_native_no_cabinet_receipt(self):
        original = self.path.stat()
        result = tv.reconcile_existing_import(self.record, native_probe=lambda *a, **kw: native())
        self.assertEqual(result['status'], 'library_import_observed')
        self.assertEqual(result['basis'], 'owner_confirmed_existing_import')
        self.assertEqual(result['import_attempts'], 0)
        self.assertNotIn('destination', result)
        self.assertNotIn('request_id', result)
        self.assertEqual(original.st_ino, self.path.stat().st_ino)

    def test_wrong_exact_hash_fails_before_native(self):
        self.record['sha256'] = 'b' * 64
        with self.assertRaises(ValueError):
            tv.reconcile_existing_import(self.record, native_probe=lambda *a, **kw: self.fail('Native called'))

    def test_wrong_metadata_fails_before_native(self):
        self.record['episode'] = dict(self.episode, episode=2)
        with self.assertRaises(ValueError):
            tv.reconcile_existing_import(self.record, native_probe=lambda *a, **kw: self.fail('Native called'))

    def test_symlink_is_never_followed(self):
        link = self.path.with_name('link.mp4')
        link.symlink_to(self.path)
        self.record['library_path'] = str(link)
        with self.assertRaises(OSError):
            tv.reconcile_existing_import(self.record, native_probe=lambda *a, **kw: self.fail('Native called'))

    def test_missing_exact_library_path_stays_unknown(self):
        self.path.unlink()
        result = tv.reconcile_existing_import(self.record, native_probe=lambda *a, **kw: self.fail('Native called'))
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['import_attempts'], 0)


class AdapterBoundaryTests(unittest.TestCase):
    def test_scoped_signal_handlers_restore_on_error(self):
        signals = (tv.signal.SIGTERM, tv.signal.SIGHUP, tv.signal.SIGINT)
        previous = {signum: tv.signal.getsignal(signum) for signum in signals}
        with self.assertRaises(InterruptedError), tv.termination_signals():
            for signum in signals:
                self.assertIsNot(tv.signal.getsignal(signum), previous[signum])
            tv.signal.getsignal(tv.signal.SIGINT)(tv.signal.SIGINT, None)
        self.assertEqual({signum: tv.signal.getsignal(signum) for signum in signals}, previous)

    def test_signal_unwinds_transfer_and_reaps_its_owned_process_group(self):
        with tempfile.TemporaryDirectory() as folder, interrupted_child() as (child, kill), tv.termination_signals():
            with self.assertRaises(InterruptedError):
                tv.live_transfer(derivative(fixture()), Path(folder) / 'partial.mp4', expires=tv.EXPIRY, timeout=5)
            kill.assert_called_once_with(child.pid, tv.signal.SIGKILL)
            child.wait.assert_called_once_with()
            child.stdout.close.assert_called_once_with()

    def test_remux_uses_fixed_ssh_command_bounded_stdin_and_expiry(self):
        contract = fixture()
        result = derivative(contract)
        with patch.object(tv, 'bounded_run', return_value=(0, tv.encoded(result))) as run:
            self.assertEqual(tv.live_remux(contract, expires=tv.EXPIRY, timeout=5), result)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:2], ['ssh', '-T'])
        self.assertEqual(args[0][-2], 'omarchy')
        self.assertIn(tv.REMOTE + '/tools/apple_tv_remux.py', args[0][-1])
        self.assertIn('--expires', args[0][-1])
        # Exercise the real remote admission using the exact emitted adapter value.
        from apple_tv_remux import check_deadline
        emitted = shlex.split(args[0][-1])
        check_deadline(emitted[emitted.index('--expires') + 1], lambda: NOW.timestamp())
        self.assertEqual(args[1], tv.encoded(contract))
        self.assertEqual(kwargs['timeout'], 5)

    def test_native_uses_private_temporary_module_cache_and_bounded_runner(self):
        with patch.object(tv, 'bounded_run', return_value=(0, tv.encoded(native()))) as run:
            result = tv.live_native_probe(Path('/synthetic/asset.mp4'), timeout=9)
            command = run.call_args.args[0]
            self.assertEqual(command[:2], ['/usr/bin/swift', '-module-cache-path'])
            self.assertTrue(command[3].endswith('/tools/apple_tv_native.swift'))
            self.assertEqual(command[4], '/synthetic/asset.mp4')
        self.assertFalse(Path(command[2]).exists())
        self.assertEqual(result, native())

    def test_transfer_stream_is_bounded_and_oversized_child_is_stopped(self):
        original = subprocess.Popen
        children = []
        def local_child(command, **kwargs):
            self.assertEqual(command[:2], ['ssh', '-T'])
            self.assertEqual(command[-2], 'omarchy')
            self.assertIn('O_NOFOLLOW', command[-1])
            child = original([sys.executable, '-c', 'import sys;sys.stdout.buffer.write(b"x" * 100)'], **kwargs)
            children.append(child)
            return child
        with tempfile.TemporaryDirectory() as folder, patch.object(tv.subprocess, 'Popen', side_effect=local_child):
            destination = Path(folder) / 'partial.mp4'
            expected = derivative(fixture())
            with self.assertRaises(ValueError):
                tv.live_transfer(expected, destination, expires=tv.EXPIRY, timeout=5)
            self.assertLessEqual(destination.stat().st_size, expected['bytes'])
        self.assertIsNotNone(children[0].poll())

    def test_existing_transfer_target_is_not_overwritten(self):
        original = subprocess.Popen
        def local_child(command, **kwargs):
            return original([sys.executable, '-c', 'pass'], **kwargs)
        with tempfile.TemporaryDirectory() as folder, patch.object(tv.subprocess, 'Popen', side_effect=local_child):
            destination = Path(folder) / 'existing.mp4'
            destination.write_bytes(b'preserved')
            with self.assertRaises(FileExistsError):
                tv.live_transfer(derivative(fixture()), destination, expires=tv.EXPIRY, timeout=5)
            self.assertEqual(destination.read_bytes(), b'preserved')


if __name__ == '__main__':
    unittest.main()
