"""Synthetic selected-item responses and temporary bytes; no media/server access."""
from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase

from shelf.acquisition_contract import ContractError
from shelf.acquisition_pipeline import (
    Checkpoint, ImportReceipt, Pipeline, ProviderJob, Snapshot, StagedFile,
)
from shelf.jellyfin_native import episode_readback


class JellyfinEpisodeReadbackTests(TestCase):
    def setUp(self):
        self.scratch = TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.path = self.root / 'episode.mkv'
        self.payload = b'Non-playable synthetic episode fixture.\n'
        self.path.write_bytes(self.payload)
        self.snapshot = Snapshot(11, 22, '7c035cd0-51eb-4e66-9096-68e1fe57a301',
        'tmdb', 'tv', 101, 'Fixture edition', 'episode_s01e02',
            'local_library', True, True)
        staged = StagedFile(ProviderJob(self.snapshot, 'SABnzbd_nzo_fixture'),
            str(self.path), hashlib.sha256(self.payload).hexdigest(), len(self.payload), True, False)
        self.imported = ImportReceipt(staged, str(self.path))
        self.binding = dict(server_id='1' * 32, item_id='2' * 32,
                            series_id='3' * 32, library_root=str(self.root))
        self.source = {'Id': '4' * 32, 'Path': str(self.path), 'Size': len(self.payload),
                       'IsRemote': False, 'Protocol': 'File', 'Container': 'mkv'}
        self.episode = {'Id': '2' * 32, 'ServerId': '1' * 32, 'SeriesId': '3' * 32,
            'Type': 'Episode', 'IsFolder': False, 'IsVirtualItem': False,
            'LocationType': 'FileSystem', 'MediaType': 'Video', 'Path': str(self.path),
            'ParentIndexNumber': 1, 'IndexNumber': 2, 'MediaSources': [self.source]}
        self.series = {'Id': '3' * 32, 'ServerId': '1' * 32, 'Type': 'Series',
                       'ProviderIds': {'Tmdb': '101'}}

    def read(self, **overrides):
        return episode_readback(overrides.get('imported', self.imported),
            overrides.get('episode', self.episode), overrides.get('series', self.series),
            **overrides.get('binding', self.binding))

    def test_exact_show_episode_and_current_bytes_supply_existing_library_port(self):
        self.episode['ProviderIds'] = {'Tmdb': '999'}  # Episode ID is not the show ID.
        item = self.read()
        self.assertEqual(item.snapshot, self.snapshot)
        self.assertEqual(item.item_id, self.binding['item_id'])
        self.assertEqual(len(item.media_sources), 1)
        source = item.media_sources[0]
        self.assertEqual((source.job, source.path, source.sha256, source.size),
            (self.imported.staged.job, self.imported.path,
             hashlib.sha256(self.payload).hexdigest(), len(self.payload)))

    def test_similar_title_wrong_show_server_episode_or_media_cannot_verify(self):
        for changes in ({'Id': '5' * 32}, {'ServerId': '5' * 32},
                        {'SeriesId': '5' * 32}, {'Type': 'Person'},
                        {'ParentIndexNumber': 2}, {'IndexNumber': 3},
                        {'IndexNumber': True}, {'IndexNumberEnd': 3},
                        {'IndexNumberEnd': True}, {'IsVirtualItem': True},
                        {'LocationType': 'Remote'}, {'IsFolder': True},
                        {'MediaType': 'Audio'}, {'Path': str(self.root / 'other.mkv')}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.read(episode={**self.episode, **changes})
        for changes in ({'Id': '5' * 32}, {'ServerId': '5' * 32}, {'Type': 'Movie'},
                        {'ProviderIds': {'Tmdb': '999'}}, {'ProviderIds': {'Tmdb': 101}},
                        {'ProviderIds': {}}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.read(series={**self.series, **changes})

    def test_missing_malformed_or_ambiguous_sources_never_verify(self):
        for imported in (None, ImportReceipt(None, str(self.path))):
            with self.subTest(imported=imported), self.assertRaises(ContractError):
                self.read(imported=imported)
        for sources in (None, [], [None], [self.source] * 2,
                        [{**self.source, 'Path': str(self.root / 'elsewhere.mkv')}],
                        [self.source, {**self.source, 'Path': '/elsewhere/file.mkv'}]):
            with self.subTest(sources=sources), self.assertRaises(ContractError):
                self.read(episode={**self.episode, 'MediaSources': sources})
        for changes in ({'Id': ''}, {'Size': True}, {'Size': len(self.payload) + 1},
                        {'IsRemote': True}, {'Protocol': 'Http'}, {'Container': 'strm'}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.read(episode={**self.episode, 'MediaSources': [{**self.source, **changes}]})
        for key in self.episode:
            episode = deepcopy(self.episode)
            del episode[key]
            with self.subTest(missing=key), self.assertRaises(ContractError):
                self.read(episode=episode)

    def test_completed_metadata_cannot_replace_current_file_integrity(self):
        self.path.write_bytes(b'x' * len(self.payload))
        with self.assertRaises(ContractError):
            self.read()
        self.path.unlink()
        with self.assertRaises(ContractError):
            self.read()
        target = self.root / 'target.mkv'
        target.write_bytes(self.payload)
        self.path.symlink_to(target)
        with self.assertRaises(ContractError):
            self.read()

    def test_destination_scope_and_incomplete_output_remain_denied(self):
        for root in ('/', str(self.path), str(self.root / 'other'), '/tmp/../tmp', 'relative'):
            with self.subTest(root=root), self.assertRaises(ContractError):
                self.read(binding={**self.binding, 'library_root': root})
        for changes in ({'complete_work': False}, {'partial': True}, {'sha256': '0' * 64}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.read(imported=replace(self.imported,
                    staged=replace(self.imported.staged, **changes)))
        snapshot = replace(self.snapshot, provider='tmdb', medium='movie', scope='film')
        staged = replace(self.imported.staged, job=replace(self.imported.staged.job, snapshot=snapshot))
        with self.assertRaises(ContractError):
            self.read(imported=replace(self.imported, staged=staged))

    def test_existing_pipeline_needs_matching_readback_before_available(self):
        class MemoryCheckpoint:
            def load(store, request_id):
                return store.row

            def save(store, row):
                store.row = row

        store = MemoryCheckpoint()
        store.row = Checkpoint(self.snapshot, state='importing',
            job=self.imported.staged.job, staged=self.imported.staged, imported=self.imported)
        saved = SimpleNamespace(**self.snapshot.__dict__, status='blocked_configuration')
        pending = Pipeline(saved, store, None, None, SimpleNamespace(lookup=lambda _: None))
        self.assertEqual(pending.advance().state, 'importing')
        self.assertEqual(store.row.reason, 'awaiting_index')
        verified = Pipeline(saved, store, None, None,
                            SimpleNamespace(lookup=lambda imported: self.read(imported=imported)))
        result = verified.advance()
        self.assertEqual(result.state, 'available')
        self.assertEqual(result.emby.item_id, self.binding['item_id'])
        self.assertEqual(result.emby.media_source_id, self.source['Id'])
