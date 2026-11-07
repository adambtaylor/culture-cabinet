"""Synthetic sidecars only; injected transfer and local temporary hash probes."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

import mac_album_files as files

REQUEST = '11111111-1111-4111-8111-111111111111'
REL = 'CC-' + REQUEST + '/folder.png'
PNG = b'\x89PNG\r\n\x1a\nSYNTHETIC IMAGE BYTES'
SHA = hashlib.sha256(PNG).hexdigest()


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.calls = []

    def runner(self, command, **kwargs):
        self.calls.append(command)
        kwargs['stdout'].write(PNG)
        return SimpleNamespace(returncode=0, stderr=json.dumps({
            'bytes': len(PNG), 'sha256': SHA, 'image_codec': 'png'}).encode())

    def test_exact_art_stages_without_overwriting(self):
        result = files.fetch_art(REL, self.root, expected_sha256=SHA, expected_bytes=len(PNG), runner=self.runner)
        self.assertEqual(Path(result['local_path']).read_bytes(), PNG)
        with self.assertRaises(FileExistsError):
            files.fetch_art(REL, self.root, expected_sha256=SHA, expected_bytes=len(PNG), runner=self.runner)
        self.assertEqual(Path(result['local_path']).read_bytes(), PNG)
        self.assertIn('omarchy', self.calls[0])

    def test_cue_is_hash_only_and_no_live_payload_is_accepted(self):
        def verifier(command, **kwargs):
            self.assertIn('verify', command[-1])
            return SimpleNamespace(returncode=0, stderr=json.dumps({'bytes': 12, 'sha256': 'a'*64}).encode())
        result = files.verify_sidecar('CC-' + REQUEST + '/album.cue', expected_sha256='a'*64,
                                     expected_bytes=12, runner=verifier)
        self.assertEqual(result, {'sha256': 'a'*64, 'bytes': 12})
        self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaises(ValueError):
            files.fetch_art('CC-' + REQUEST + '/album.cue', self.root,
                            expected_sha256='a'*64, expected_bytes=12, runner=verifier)

    def test_path_escape_unsupported_extensions_and_bounds_refuse_before_transfer(self):
        for path in ('/etc/passwd', '../folder.png', REL.replace('/folder', '/../folder'),
                     REL.replace('.png', '.svg'), REL.replace('.png', '.exe'), 'CC-bad/folder.png'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                files.fetch_art(path, self.root, expected_sha256=SHA, expected_bytes=len(PNG), runner=self.runner)
        for size in (0, True, files.MAX_IMAGE_BYTES + 1):
            with self.assertRaises(ValueError):
                files.fetch_art(REL, self.root, expected_sha256=SHA, expected_bytes=size, runner=self.runner)
        self.assertEqual(self.calls, [])

    def test_changed_size_hash_or_codec_does_not_publish(self):
        for changes in ({'sha256': '0'*64}, {'bytes': len(PNG)+1}, {'image_codec': 'h264'}):
            def changed(command, **kwargs):
                kwargs['stdout'].write(PNG)
                return SimpleNamespace(returncode=0, stderr=json.dumps({
                    'bytes': len(PNG), 'sha256': SHA, 'image_codec': 'png', **changes}).encode())
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                files.fetch_art(REL, self.root, expected_sha256=SHA, expected_bytes=len(PNG), runner=changed)
        self.assertFalse((self.root / REL).exists())

    def test_symlinked_staging_parent_refuses(self):
        (self.root / ('CC-' + REQUEST)).symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            files.fetch_art(REL, self.root, expected_sha256=SHA, expected_bytes=len(PNG), runner=self.runner)
        self.assertEqual(self.calls, [])

    def test_actual_hash_program_reads_opaque_cue_and_refuses_symlink(self):
        directory = self.root / ('CC-' + REQUEST)
        directory.mkdir()
        cue = directory / 'album.cue'
        payload = b'FILE "ignored.flac" WAVE\n'
        cue.write_bytes(payload)
        args = [sys.executable, '-I', '-S', '-c', files.REMOTE_PROGRAM, str(self.root),
                str(cue.relative_to(self.root)), str(files.MAX_CUE_BYTES), 'verify']
        result = subprocess.run(args, capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(json.loads(result.stderr)['sha256'], hashlib.sha256(payload).hexdigest())
        cue.unlink(); cue.symlink_to(directory / 'missing.cue')
        refused = subprocess.run(args, capture_output=True, timeout=3)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(refused.stdout, b'')


if __name__ == '__main__':
    unittest.main()
