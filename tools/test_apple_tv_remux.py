"""Small synthetic bytes and injected subprocesses; no remote or live media actions."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import apple_tv_remux as remux
from apple_tv_media import digest, MediaError
from test_mac_episode_consumer import fixture
from test_mac_kodi_consumer import PAYLOAD
from test_apple_tv_media import source_probe, derivative_probe, ATOMS

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp()
OUTPUT = b'synthetic validated mp4'


def contract_fixture(scope='series_s01e01'):
    completion, selection = fixture(scope)
    from episode_scope import parse_scope, episode_code
    season, number = parse_scope(scope)
    identity = selection['source_identity']
    item = completion['files'][0]
    result = {'version': 1, 'destination': 'apple_tv', 'completion': completion, 'selection': selection,
              'episode': {'show': identity['title'], 'season': season, 'episode': number, 'title': episode_code(scope),
                          'source_id': identity['source_id'], 'basis': 'selected_job_scope'},
              'source': {'relative_path': item['relative_path'], 'sha256': item['sha256'], 'bytes': item['size']}}
    result['contract_sha256'] = digest(result)
    return result


def atom(kind, data):
    return struct.pack('>I4s', len(data) + 8, kind) + data


class RemuxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project, self.complete = self.root / 'project', self.root / 'Complete'
        self.project.mkdir()
        self.complete.mkdir()
        self.contract = contract_fixture()
        self.source = self.complete / self.contract['source']['relative_path']
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(PAYLOAD)
        self.calls = []

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == 'ffmpeg':
            Path(args[-1]).write_bytes(OUTPUT)
            return b''
        if '-show_packets' in args:
            return b'SHA256:' + b'a' * 64 + b'\n'
        if str(args[-1]).startswith('/proc/'):
            return json.dumps(source_probe()).encode()
        plan = remux.plan_media(source_probe(), self.contract['episode'])
        return json.dumps(derivative_probe(source_probe(), plan)).encode()

    def run_remux(self, **kwargs):
        with patch.object(remux, 'inspect_atoms', return_value=ATOMS):
            return remux.run_remux(self.contract, clock=lambda: NOW, runner=self.runner,
                                   project_root=self.project, complete=self.complete, **kwargs)

    @property
    def folder(self):
        return self.project / '.apple-tv-work' / self.contract['contract_sha256']

    def test_contract_source_and_episode_mutation_rejected(self):
        self.assertEqual(remux.validate_contract(self.contract), self.contract)
        self.contract['source']['relative_path'] = '../escape.mkv'
        self.contract['contract_sha256'] = digest({k: v for k, v in self.contract.items() if k != 'contract_sha256'})
        with self.assertRaisesRegex(MediaError, 'source differs'):
            remux.validate_contract(self.contract)

    def test_ready_restart_no_second_remux_and_original_preserved(self):
        before = self.source.read_bytes(), self.source.stat().st_mtime_ns
        first = self.run_remux()
        self.assertTrue(first['validation']['valid'])
        self.assertEqual(self.run_remux(), first)
        self.assertEqual(sum(call[0] == 'ffmpeg' for call in self.calls), 1)
        self.assertEqual(before, (self.source.read_bytes(), self.source.stat().st_mtime_ns))
        self.assertEqual(Path(first['remote_path']).read_bytes(), OUTPUT)
        self.assertEqual(json.loads((self.folder / 'receipt.json').read_text())['status'], 'ready')

    def test_explicit_s02e02_remux_metadata_and_restart(self):
        self.contract = contract_fixture('episode_s02e02')
        result = self.run_remux()
        self.assertEqual(result['validation']['metadata']['episode_id'], 'S02E02')
        self.assertEqual(result['validation']['metadata']['season_number'], '2')
        self.assertEqual(result['validation']['metadata']['episode_sort'], '2')
        self.assertEqual(self.run_remux(), result)
        self.assertEqual(sum(call[0] == 'ffmpeg' for call in self.calls), 1)
        self.assertEqual(self.source.read_bytes(), PAYLOAD)

    def test_failed_reserved_and_unknown_states_never_retry(self):
        for state in ('reserved', 'blocked', 'unknown'):
            folder = self.folder
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            (folder.parent).chmod(0o700)
            remux.write_state(folder / 'receipt.json', {'status': state, 'contract_sha256': self.contract['contract_sha256']})
            with self.subTest(state=state), self.assertRaisesRegex(MediaError, 'already reserved'):
                self.run_remux()
        self.assertEqual(self.calls, [])

    def test_validated_state_recovers_staged_and_published_exact_evidence(self):
        result = self.run_remux()
        final = Path(result['remote_path'])
        final.rename(self.folder / 'episode.partial')
        receipt = json.loads((self.folder / 'receipt.json').read_text())
        receipt['status'] = 'validated'
        remux.write_state(self.folder / 'receipt.json', receipt)
        self.assertEqual(self.run_remux(), result)
        receipt['status'] = 'validated'
        remux.write_state(self.folder / 'receipt.json', receipt)
        self.assertEqual(self.run_remux(), result)
        self.assertEqual(sum(call[0] == 'ffmpeg' for call in self.calls), 1)
        final.write_bytes(b'tampered')
        with self.assertRaisesRegex(MediaError, 'does not match'):
            self.run_remux()

    def test_source_symlink_bad_hash_and_low_disk_refused_before_reservation(self):
        self.source.unlink()
        other = self.root / 'other'
        other.write_bytes(PAYLOAD)
        self.source.symlink_to(other)
        with self.assertRaises(OSError):
            self.run_remux()
        self.source.unlink()
        self.source.write_bytes(b'bad')
        with self.assertRaisesRegex(MediaError, 'Source bytes/hash'):
            self.run_remux()
        self.source.write_bytes(PAYLOAD)
        with patch.object(remux.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 0})()):
            with self.assertRaisesRegex(MediaError, 'disk space'):
                self.run_remux()
        self.assertEqual(json.loads((self.folder / 'receipt.json').read_text())['reason'], 'insufficient_disk_space')
        count = len(self.calls)
        with self.assertRaises(MediaError) as raised:
            self.run_remux()
        self.assertEqual(raised.exception.code, 'insufficient_disk_space')
        self.assertEqual(len(self.calls), count)

    def test_failed_ffmpeg_preserves_blocked_receipt_and_never_retries(self):
        original = self.runner
        def runner(args, **kwargs):
            if args[0] == 'ffmpeg':
                raise MediaError('Synthetic failure', 'unsupported_audio_codec')
            return original(args, **kwargs)
        self.runner = runner
        with self.assertRaisesRegex(MediaError, 'Synthetic'):
            self.run_remux()
        self.assertEqual(json.loads((self.folder / 'receipt.json').read_text())['status'], 'blocked')
        with self.assertRaises(MediaError) as raised:
            self.run_remux()
        self.assertEqual(raised.exception.code, 'unsupported_audio_codec')
        self.assertEqual(json.loads((self.folder / 'receipt.json').read_text())['reason'], 'unsupported_audio_codec')

    def test_extra_source_entry_and_symlink_directory_refused(self):
        (self.source.parent / 'unexpected.nfo').write_text('unregistered')
        with self.assertRaisesRegex(MediaError, 'Unexpected entry'):
            self.run_remux()
        self.assertEqual(self.calls, [])
        (self.source.parent / 'unexpected.nfo').unlink()
        original = self.source.parent
        moved = self.root / 'elsewhere'
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OSError):
            self.run_remux()

    def test_equivalent_utc_expiry_formats_share_the_fixed_deadline(self):
        for value in ('2026-11-06T04:17:29Z', '2026-11-06T04:17:29+00:00'):
            with self.subTest(value=value):
                remux.check_deadline(value, lambda: NOW)
                with self.assertRaises(MediaError) as raised:
                    remux.check_deadline(value, lambda: remux.epoch(remux.EXPIRY))
                self.assertEqual(raised.exception.code, 'session_expired')
        first = self.run_remux(expires='2026-11-06T04:17:29+00:00')
        self.assertEqual(self.run_remux(expires=remux.EXPIRY), first)
        self.assertEqual(sum(call[0] == 'ffmpeg' for call in self.calls), 1)
        for invalid in ('2026-11-06T04:17:30+00:00', '2026-11-06T04:17:29',
                        '2026-11-06T05:17:29+01:00', 'not-a-date'):
            with self.subTest(invalid=invalid), self.assertRaises(MediaError):
                remux.check_deadline(invalid, lambda: NOW)

    def test_expired_and_renewed_expiry_refused(self):
        with self.assertRaisesRegex(MediaError, 'expired'):
            self.run_remux(expires='2026-11-07T04:17:29Z')
        with self.assertRaisesRegex(MediaError, 'expired'):
            remux.check_deadline(remux.EXPIRY, lambda: remux.epoch(remux.EXPIRY))
        self.assertEqual(self.calls, [])

    def test_cli_structured_policy_failure_is_bound_static_and_exit_zero(self):
        stdin = type('Input', (), {'buffer': io.BytesIO(json.dumps(self.contract).encode())})()
        stdout, stderr = io.StringIO(), io.StringIO()
        error = MediaError('/private/provider/secret should never escape', 'unsupported_audio_codec')
        with patch.object(sys, 'argv', ['apple_tv_remux.py', '--expires', remux.EXPIRY]), \
             patch.object(sys, 'stdin', stdin), patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), patch.object(remux, 'run_remux', side_effect=error):
            self.assertEqual(remux.main(), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'unsupported_audio_codec')
        self.assertEqual(result['contract_sha256'], self.contract['contract_sha256'])
        self.assertEqual(result['source_sha256'], self.contract['source']['sha256'])
        self.assertEqual(result['message'], remux.BLOCKED_REASONS[result['reason']])
        self.assertNotIn('secret', stdout.getvalue() + stderr.getvalue())
        self.assertEqual(stderr.getvalue(), '')

    def test_cli_unexpected_oserror_is_sanitized_nonzero(self):
        stdin = type('Input', (), {'buffer': io.BytesIO(json.dumps(self.contract).encode())})()
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['apple_tv_remux.py', '--expires', remux.EXPIRY]), \
             patch.object(sys, 'stdin', stdin), patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(remux, 'run_remux', side_effect=OSError('/private/provider/secret')):
            self.assertEqual(remux.main(), 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertNotIn('secret', stderr.getvalue())
        self.assertEqual(json.loads(stderr.getvalue())['reason'], 'remux_reconciliation_required')

    def test_structural_atoms_joc_and_malformed_sizes(self):
        dec3 = bytes.fromhex('1800200f000110')
        self.assertTrue(remux.joc_extension(dec3))
        self.assertFalse(remux.joc_extension(dec3[:-2]))
        sample = atom(b'hvc1', bytes(78) + atom(b'dvvC', bytes(24)) + atom(b'colr', b'nclx1234567'))
        audio = atom(b'ec-3', bytes(28) + atom(b'dec3', dec3))
        stsd = atom(b'stsd', bytes(4) + (2).to_bytes(4, 'big') + sample + audio)
        movie = atom(b'moov', atom(b'trak', atom(b'mdia', atom(b'minf', atom(b'stbl', stsd))))) + atom(b'mdat', b'payload')
        path = self.root / 'sample.mp4'
        path.write_bytes(movie)
        with remux.open_pinned(path) as fd:
            proof = remux.inspect_atoms(fd)
        self.assertTrue(proof['joc_extension_retained'] and proof['faststart'])
        self.assertEqual(proof['video_sample_entry'], 'hvc1')
        path.write_bytes(b'\x00\x00\x00\xffmoov')
        with remux.open_pinned(path) as fd, self.assertRaises(MediaError):
            remux.inspect_atoms(fd)

    def test_subprocess_expiry_kills_process_group(self):
        ticks = iter([NOW, NOW, NOW, remux.epoch(remux.EXPIRY)])
        proc = type('Process', (), {'pid': 999999, 'returncode': None, 'poll': lambda self: None,
                                   'wait': lambda self: None})()
        with patch.object(remux.subprocess, 'Popen', return_value=proc), patch.object(remux.os, 'killpg') as kill:
            with self.assertRaisesRegex(MediaError, 'interrupted'):
                remux.run_bounded(['synthetic'], expires=remux.EXPIRY, clock=lambda: next(ticks))
        self.assertTrue(all(call.args == (999999, signal.SIGKILL) for call in kill.call_args_list))


if __name__ == '__main__':
    unittest.main()
