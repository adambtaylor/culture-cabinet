"""Logical receipt comparisons using the actual synthetic resolver; no file opens."""
from dataclasses import asdict, replace
from datetime import timedelta
import hashlib
from unittest import TestCase
from unittest.mock import patch

from shelf import obtain_library_verification as verify
from shelf.jellyfin_tv_import import DestinationFileReceipt
from shelf.obtain_tv_flow import SelectedCompletion, SelectedJobBinding, ServiceGeneration, VerifiedSourceFile
from shelf.sab_selected import canonical_digest
from . import test_jellyfin_resolution as fixture


class LibraryEvidenceTests(TestCase):
    def setUp(self):
        self.fixture = fixture.EpisodeResolutionTests()
        self.fixture.setUp()
        self.now = self.fixture.clock.now()
        self.fixture.binding = replace(self.fixture.binding, expires_at=self.now + timedelta(seconds=1000))
        self.fixture.target = replace(self.fixture.target, facts_valid_until=self.now + timedelta(seconds=1000))
        self.resolution = self.fixture.run_resolve()
        self.assertEqual(self.resolution.status, 'resolved')
        self.observation = self.resolution.observation
        library = ServiceGeneration('jellyfin', self.resolution.service_binding_digest,
            self.resolution.configuration_generation, self.resolution.credential_generation)
        destination = ServiceGeneration('destination_file', 'd' * 64, 'e' * 32, 'f' * 32)
        self.job = SelectedJobBinding(request_id=self.fixture.target.request_id, job_id=101,
            actor_id=11, profile_id=22, publication_origin_digest=None,
            binding_digest=self.fixture.target.binding_digest, selection_digest='1' * 64,
            review_digest='2' * 64, episode_scope=self.fixture.target.scope,
            result_id='7c035cd0-51eb-4e66-9096-68e1fe57a301', result_guid='123',
            result_digest='3' * 64, nzb_sha256='4' * 64, nzo_id='SABnzbd_nzo_fixture',
            completion_policy_digest='5' * 64, complete_root='/synthetic/complete',
            destination_ref='fixture-destination', test_only=True,
            content_identity_digest=self.fixture.target.content_identity_digest,
            service_generations=(library, destination))
        self.file = VerifiedSourceFile('S01E02.mkv', 4096, hashlib.sha256(b'x' * 4096).hexdigest())
        manifest = canonical_digest({'request_id': self.job.request_id, 'nzo_id': self.job.nzo_id,
            'binding_digest': self.job.binding_digest, 'files': [asdict(self.file)]},
            domain='obtain-tv-source-manifest/v1')
        self.completion = SelectedCompletion(self.job, 'completed', '', manifest, (self.file,),
            'unobserved', 'unobserved', '6' * 64, '7' * 64, self.now - timedelta(seconds=3))
        self.destination = DestinationFileReceipt(self.job.request_id, self.job.job_id, self.job.nzo_id,
            self.job.binding_digest, self.job.content_identity_digest, manifest,
            self.fixture.target.destination_binding_digest,
            '7c035cd0-51eb-4e66-9096-68e1fe57a302', 'fixture-mac', self.resolution.root,
            'FixtureShow/S01E02.mkv', self.file.size, self.file.sha256,
            (self.now - timedelta(seconds=2)).isoformat(), True)

    def check(self, **overrides):
        args = dict(completion=self.completion, destination=self.destination,
            resolution=self.resolution, observation=self.observation,
            observed_at=self.now, authority_deadline=self.now + timedelta(seconds=1000))
        args.update(overrides)
        return verify.check_library_evidence(**args)

    def test_matching_independent_receipts_verify_without_opening_remote_path(self):
        with patch('builtins.open', side_effect=AssertionError('Remote Mac path is data only')):
            result = self.check()
        self.assertEqual(result.status, 'verified')
        self.assertEqual(result.verification.request_id, self.job.request_id)
        self.assertEqual(result.verification.valid_until, self.now + timedelta(seconds=300))
        self.assertEqual(result.verification.item_ref, self.observation.item_id)

    def test_actual_authority_and_freshness_shorter_deadlines_win(self):
        result = self.check(authority_deadline=self.now + timedelta(seconds=20))
        self.assertEqual(result.verification.valid_until, self.now + timedelta(seconds=20))
        result = self.check(resolution=replace(self.resolution, valid_until=self.now + timedelta(seconds=10)))
        self.assertEqual(result.verification.valid_until, self.now + timedelta(seconds=10))
        self.assertEqual(self.check(observed_at=self.now + timedelta(seconds=301)).status, 'unavailable')
        self.assertEqual(self.check(authority_deadline=self.now).status, 'unavailable')

    def test_wrong_destination_bytes_identity_scope_and_synthetic_marker_refuse(self):
        for changes in ({'sha256': '0' * 64}, {'size': 4097}, {'size': True},
                        {'nzo_id': 'SABnzbd_nzo_foreign'}, {'job_id': 102},
                        {'request_id': 'foreign'}, {'binding_digest': '0' * 64},
                        {'source_manifest_digest': '0' * 64}, {'content_identity_digest': '0' * 64},
                        {'destination_binding_digest': '0' * 64}, {'test_only': False},
                        {'relative_path': '../S01E02.mkv'}, {'root': '/foreign'},
                        {'observed_at': (self.now + timedelta(seconds=1)).isoformat()}):
            with self.subTest(changes=changes):
                self.assertEqual(self.check(destination=replace(self.destination, **changes)).status, 'mismatch')

    def test_metadata_only_unindexed_or_wrong_episode_cannot_verify(self):
        self.assertEqual(self.check(destination=None).status, 'mismatch')
        self.assertEqual(self.check(completion=replace(self.completion, state='pending')).status, 'mismatch')
        self.assertEqual(self.check(resolution=replace(self.resolution, status='not_indexed')).status, 'unavailable')
        for changes in ({'item_id': '0' * 32}, {'server_id': '0' * 32}, {'user_id': '0' * 32},
                        {'library_id': '0' * 32}, {'scope': 'episode_s01e03'},
                        {'series_id': '0' * 32}, {'configuration_generation': '0' * 32},
                        {'credential_generation': '0' * 32}, {'operation_digest': '0' * 64}):
            with self.subTest(changes=changes):
                self.assertEqual(self.check(observation=replace(self.observation, **changes)).status, 'mismatch')
        duplicate = replace(self.observation, media_sources=self.observation.media_sources * 2)
        self.assertEqual(self.check(observation=duplicate,
            resolution=replace(self.resolution, observation=duplicate)).status, 'mismatch')

    def test_forged_resolution_refs_cannot_reuse_another_bound_service_digest(self):
        foreign = replace(self.observation, user_id='f' * 32)
        resolution = replace(self.resolution, user_id='f' * 32, observation=foreign)
        self.assertEqual(self.check(resolution=resolution, observation=foreign).status, 'mismatch')
        foreign_binding = replace(self.resolution.binding, profile_id=999)
        self.assertEqual(self.check(resolution=replace(self.resolution, binding=foreign_binding)).status, 'mismatch')
        self.assertEqual(self.check(resolution=replace(self.resolution, binding=None)).status, 'mismatch')

    def test_missing_hook_manifest_wrong_completion_and_future_clocks_refuse(self):
        for changes in ({'hook_digest': None}, {'history_digest': None},
                        {'source_manifest_digest': None}, {'repair': 'failed'},
                        {'unpack': 'failed'}, {'files': self.completion.files * 2},
                        {'observed_at': self.now + timedelta(seconds=1)}):
            with self.subTest(changes=changes):
                self.assertEqual(self.check(completion=replace(self.completion, **changes)).status, 'mismatch')
        self.assertEqual(self.check(resolution=replace(self.resolution,
            observed_at=self.now + timedelta(seconds=1))).status, 'mismatch')
