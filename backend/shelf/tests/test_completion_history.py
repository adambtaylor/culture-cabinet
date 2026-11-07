"""Final-history evidence is distinct from hook time and local import time."""
from copy import deepcopy
from unittest import TestCase

from shelf.acquisition_contract import ContractError
from shelf.completion_history import completed_export


class CompletedHistoryTests(TestCase):
    def setUp(self):
        self.root = '/fixture/Complete/CC-request'
        self.handoff = {'version': 1, 'state': 'POSTPROCESS_FILE_VERIFIED',
            'checked_at': '2026-10-29T09:00:01+00:00', 'sab_history_completed': False,
            'files': [{'nzo_id': 'SABnzbd_nzo_fixture',
                'provenance': {'request_id': 'fixture', 'result_digest': 'a' * 64},
                'relative_path': 'CC-request/01.flac', 'sha256': 'b' * 64, 'size': 42,
                'verified_at': '2026-10-29T09:00:00.900000+00:00',
                'acquired_at': None, 'acquired_at_basis': 'unavailable',
                'kodi_music_catalog_eligible': False, 'playback_started': False}]}
        self.slot = {'nzo_id': 'SABnzbd_nzo_fixture', 'storage': self.root,
            'status': 'Completed', 'fail_message': '', 'script': 'culture-cabinet-completion',
            'completed': 1793264400}

    def export(self, slot=None):
        return completed_export(self.handoff, {'history': {'slots': [slot or self.slot]}},
                                root=self.root)

    def test_provider_seconds_are_preserved_without_promoting_catalog_or_playback(self):
        original = deepcopy(self.handoff)
        result = self.export()
        self.assertEqual(result['version'], 2)
        self.assertEqual(result['state'], 'COMPLETED_FILES_VERIFIED')
        self.assertTrue(result['sab_history_completed'])
        self.assertEqual(result['files'][0]['acquired_at'], '2026-10-29T09:00:00+00:00')
        self.assertEqual(result['files'][0]['acquired_at_basis'], 'sab_history_completed_at')
        self.assertFalse(result['files'][0]['kodi_music_catalog_eligible'])
        self.assertFalse(result['files'][0]['playback_started'])
        self.assertEqual(self.handoff, original)
        self.handoff['checked_at'] = '2026-10-29T10:00:00+00:00'
        self.assertEqual(self.export()['completion_evidence_sha256'],
                         result['completion_evidence_sha256'])

    def test_missing_running_failed_foreign_or_wrong_directory_history_refuses(self):
        changes = {'status': ['Running', 'Queued', 'Failed', 'completed'],
                   'nzo_id': ['SABnzbd_nzo_other'], 'storage': [self.root + '/other'],
                   'script': ['None'], 'fail_message': ['failed'],
                   'completed': [None, True, '1793264400', 1793264400.0, 0,
                                 1793264399, 1793264402, 10 ** 100]}
        for key, values in changes.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ContractError):
                    self.export({**self.slot, key: value})
        for key in self.slot:
            slot = dict(self.slot)
            del slot[key]
            with self.subTest(missing=key), self.assertRaises(ContractError):
                self.export(slot)
        for slots in ([], [self.slot, self.slot], None, {}):
            with self.assertRaises(ContractError):
                completed_export(self.handoff, {'history': {'slots': slots}}, root=self.root)

    def test_mixed_bindings_naive_or_newer_file_observations_refuse(self):
        for change in ({'nzo_id': 'SABnzbd_nzo_other'}, {'provenance': {'request_id': 'other'}},
                       {'verified_at': '2026-10-29T09:00:01+00:00'},
                       {'verified_at': '2026-10-29T09:00:00'}):
            self.handoff['files'] = [self.handoff['files'][0],
                                     {**self.handoff['files'][0], **change}]
            with self.assertRaises(ContractError):
                self.export()
            self.handoff['files'].pop()

    def test_revision_binds_file_bytes_and_request_but_ignores_unneeded_provider_data(self):
        first = self.export()['completion_evidence_sha256']
        self.assertEqual(self.export({**self.slot, 'password': 'must-not-be-exported'})
                         ['completion_evidence_sha256'], first)
        self.assertNotIn('password', str(self.export({**self.slot, 'password': 'private'})))
        self.handoff['files'][0]['sha256'] = 'c' * 64
        self.assertNotEqual(self.export()['completion_evidence_sha256'], first)
