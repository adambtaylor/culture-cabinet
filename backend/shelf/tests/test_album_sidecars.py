"""Synthetic manifests preserve complete audio while isolating non-audio files."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
from unittest import TestCase

from shelf.album_files import file_kind, validate_manifest
from shelf.completion_files import verify_media
from shelf.completion_history import completed_export
from shelf.tests import test_completion_history as history_fixtures


class AlbumManifestTests(TestCase):
    def tracks(self):
        return [{'path': '01.flac', 'bytes': 12, 'sha256': 'a'*64},
                {'path': '02.flac', 'bytes': 12, 'sha256': 'b'*64}]

    def test_art_and_cue_do_not_replace_missing_tracks(self):
        files = self.tracks() + [{'path': 'folder.jpg', 'bytes': 20, 'sha256': 'c'*64},
                                 {'path': 'album.cue', 'bytes': 20, 'sha256': 'd'*64}]
        self.assertEqual(validate_manifest(files, 2), {'track': 2, 'art': 1, 'cue': 1})
        with self.assertRaises(ValueError):
            validate_manifest(files[1:], 2)
        with self.assertRaises(ValueError):
            validate_manifest(files, 4)

    def test_duplicate_casefold_paths_tracks_unsafe_types_and_size_refuse(self):
        bad_sets = [self.tracks() + [dict(self.tracks()[0], path='01.FLAC')],
                    [self.tracks()[0], dict(self.tracks()[1], sha256='a'*64)],
                    self.tracks() + [{'path': 'album.nfo', 'bytes': 12, 'sha256': 'c'*64}],
                    self.tracks() + [{'path': 'album.cue', 'bytes': 1024**2+1, 'sha256': 'c'*64}],
                    self.tracks() + [{'path': 'folder.jpg', 'bytes': 20*1024**2+1, 'sha256': 'c'*64}]]
        for files in bad_sets:
            with self.subTest(files=files), self.assertRaises(ValueError):
                validate_manifest(files, 2)
        for path in ('../folder.png', '/folder.png', 'disc\\folder.png', 'disc//folder.png'):
            with self.assertRaises(ValueError):
                file_kind(path, 12)

    def test_bounded_art_and_cue_counts(self):
        for suffix, count in (('png', 17), ('cue', 5)):
            files = self.tracks() + [{'path': str(i)+'.'+suffix, 'bytes': 10, 'sha256': 'c'*64}
                                     for i in range(count)]
            with self.assertRaises(ValueError):
                validate_manifest(files, 2)

    def test_actual_tree_checks_audio_and_image_headers_but_never_interprets_cue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            payloads = {'01.flac': b'fLaCsynthetic', 'folder.png': b'\x89PNG\r\n\x1a\nsynthetic',
                        'album.cue': b'FILE "../../must-not-open" WAVE\n'}
            manifest = []
            for name, payload in payloads.items():
                (root/name).write_bytes(payload)
                manifest.append({'path': name, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()})
            entry = {'root': str(root), 'scope': 'album_flac', 'max_bytes': 1024, 'files': manifest}
            observed = verify_media(entry)
            self.assertEqual({item['path'] for item in observed}, set(payloads))
            (root/'folder.png').write_bytes(b'x'*len(payloads['folder.png']))
            with self.assertRaises(ValueError):
                verify_media(entry)


class AlbumFinalIdentityTests(TestCase):
    def test_final_evidence_binds_selected_album_identity_without_mutating_hook(self):
        fixture = history_fixtures.CompletedHistoryTests(); fixture.setUp()
        album = {'namespace': 'licensed_catalogue', 'artist_id': 'fiction-artist',
                 'release_id': 'fiction-release', 'artist': 'Fiction Artist', 'album': 'Fiction Album',
                 'track_count': 1, 'source': 'Fiction licensed catalogue'}
        fixture.handoff['authorization'] = {'scope': 'album_flac', 'album_identity': album}
        original = deepcopy(fixture.handoff)
        identity = {'provider': 'album_request', 'medium': 'music', 'scope': 'album_flac', 'source_id': 1,
                    'title': 'Fiction Artist - Fiction Album', 'edition': 'Fixture edition',
                    'basis': 'selected_catalogue_identity_not_content_verification'}
        def export():
            return completed_export(fixture.handoff, {'history': {'slots': [fixture.slot]}},
                                    root=fixture.root, source_identity=identity)
        first = export()
        self.assertEqual(first['album_identity'], album)
        self.assertEqual(first['completion_evidence']['album_identity'], album)
        self.assertEqual(fixture.handoff, original)
        fixture.handoff['authorization']['album_identity'] = dict(album, release_id='other-release')
        self.assertNotEqual(export()['completion_evidence_sha256'], first['completion_evidence_sha256'])
