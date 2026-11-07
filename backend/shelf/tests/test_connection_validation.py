"""Synthetic owner/validation lifecycle; no live generation or capacity proof."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import threading
from unittest.mock import patch
import uuid

from django.db import connections

from shelf.connection_validation import ConnectionValidation
from shelf.inference_adapters import MODEL, probe_body
from shelf.inference_transport import TransportFault
from shelf.models import PortableConnection, PortableInstallation, PortableProbeAttempt, PortableValidation
from shelf.secret_leases import MemoryCredentials
from .test_portable_authority import BoundaryCase, PASSWORD

CONFIG = {'adapter': 'openai_responses', 'model': MODEL, 'reasoning': 'high'}
SECRET = b'SYNTHETIC-CONNECTION-CANARY-KEY'


class FakeAdapter:
    def __init__(self):
        self.calls, self.bodies = 0, []
        self.failure = self.after = None

    def generate(self, body, permit):
        self.calls += 1
        permit.authorize(body)
        self.bodies.append(body)
        if self.after:
            self.after()
        if self.failure:
            raise self.failure
        return {'observed_model': MODEL, 'observation_limits': ['Isolated test fake; not live proof.']}


class ConnectionCase(BoundaryCase):
    def setUp(self):
        super().setUp()
        self.network_dns = patch('shelf.inference_transport.socket.getaddrinfo', side_effect=AssertionError('Real DNS forbidden'))
        self.network_connect = patch('shelf.inference_transport.socket.socket', side_effect=AssertionError('Real connect forbidden'))
        self.network_dns.start()
        self.network_connect.start()
        self.addCleanup(self.network_dns.stop)
        self.addCleanup(self.network_connect.stop)
        self.ref = self.claim_owner()
        self.vault = MemoryCredentials(self.authority)
        self.config = self.vault.configure(self.ref, CONFIG)
        self.service = ConnectionValidation(self.authority, self.vault)
        self.fake = FakeAdapter()
        self.service.adapter = self.fake

    def restart(self):
        # Recreate only the fixed loopback-bound root fixture; DNS stays denied.
        self.network_connect.stop()
        try:
            super().restart()
        finally:
            self.network_connect.start()

    def put(self):
        self.vault.replace(self.ref, self.config.pk, self.config.config_revision, SECRET)

    def preview_probe(self):
        return self.service.preview(self.ref, self.config.pk, self.config.config_revision)

    def confirm_probe(self, preview=None, intent=None):
        preview = preview or self.preview_probe()
        return self.service.confirm(self.ref, uuid.UUID(preview['preview_ref']), intent or uuid.uuid4(), True)

    def status_probe(self):
        return self.service.status(self.ref, self.config.pk, self.config.config_revision)


class ValidationTests(ConnectionCase):
    def test_missing_secret_preserves_config_and_blocks_generation(self):
        self.assertEqual(self.status_probe()['outcome'], 'missing_credential')
        self.fault('CREDENTIAL_REQUIRED', self.preview_probe)
        self.assertEqual(self.fake.calls, 0)
        self.assertEqual(PortableConnection.objects.count(), 1)

    def test_preview_is_exact_wire_body_without_secret_or_private_binding(self):
        self.put()
        preview = self.preview_probe()
        self.assertEqual(preview['body'].encode(), probe_body())
        self.assertEqual(preview['body_digest'], hashlib.sha256(probe_body()).hexdigest())
        self.confirm_probe(preview)
        self.assertEqual(self.fake.bodies, [preview['body'].encode()])
        for canary in [SECRET.decode(), self.ref, str(self.boundary.installation_id)]:
            self.assertNotIn(canary, preview['body'])
        self.assertEqual(preview['maximum_sends'], 1)
        self.assertEqual(preview['retry_sends'], 0)

    def test_explicit_billable_confirmation_is_required(self):
        self.put()
        preview = self.preview_probe()
        for value in [False, 1, 'yes', None]:
            self.fault('CONTRACT_INVALID', self.service.confirm, self.ref, uuid.UUID(preview['preview_ref']), uuid.uuid4(), value)
        self.assertFalse(PortableProbeAttempt.objects.exists())
        self.assertEqual(self.fake.calls, 0)

    def test_same_intent_replay_is_one_attempt_and_one_send(self):
        self.put()
        preview, intent = self.preview_probe(), uuid.uuid4()
        first = self.confirm_probe(preview, intent)
        second = self.confirm_probe(preview, intent)
        self.assertEqual(first, second)
        self.assertEqual(self.fake.calls, 1)
        self.assertEqual(PortableProbeAttempt.objects.count(), 1)

    def test_reused_intent_with_changed_preview_conflicts(self):
        self.put()
        intent = uuid.uuid4()
        self.confirm_probe(intent=intent)
        next_preview = self.preview_probe()
        self.fault('IDEMPOTENCY_CONFLICT', self.confirm_probe, next_preview, intent)
        self.assertEqual(self.fake.calls, 1)

    def test_expired_preview_sends_nothing(self):
        self.put()
        preview = self.preview_probe()
        self.clock.advance(300)
        self.authority.reauthenticate(self.ref, PASSWORD)
        self.fault('PREVIEW_STALE', self.confirm_probe, preview)
        self.assertEqual(self.fake.calls, 0)

    def test_secret_generation_change_invalidates_preview(self):
        self.put()
        preview = self.preview_probe()
        self.put()
        self.fault('PREVIEW_STALE', self.confirm_probe, preview)
        self.assertFalse(PortableProbeAttempt.objects.exists())

    def test_config_revision_change_invalidates_preview(self):
        self.put()
        preview = self.preview_probe()
        self.vault.configure(self.ref, CONFIG, connection_id=self.config.pk, revision=1)
        self.fault('PREVIEW_STALE', self.confirm_probe, preview)
        self.assertEqual(self.fake.calls, 0)

    def test_isolated_fake_receipt_never_makes_normal_connection_or_run_ready(self):
        self.put()
        self.assertEqual(self.confirm_probe()['state'], 'completed')
        proof = PortableValidation.objects.get()
        self.assertEqual(proof.proof_kind, 'isolated_test_fake')
        result = self.status_probe()
        self.assertFalse(result['connection_ready'])
        self.assertFalse(result['run_ready'])
        self.assertEqual(result['outcome'], 'isolated_test_only')

    def test_mocked_normal_code_branch_requires_current_real_kind_and_deadline(self):
        self.put()
        self.service = ConnectionValidation(self.authority, self.vault)
        with patch.object(self.service.adapter, 'generate', side_effect=self.fake.generate):
            self.confirm_probe()
        self.assertTrue(self.status_probe()['connection_ready'])
        self.assertFalse(self.status_probe()['run_ready'])
        self.clock.advance(900)
        self.assertFalse(self.status_probe()['connection_ready'])
        self.assertEqual(self.status_probe()['outcome'], 'stale')

    def test_wall_clock_rollback_cannot_extend_receipt_monotonic_lifetime(self):
        self.put()
        self.service = ConnectionValidation(self.authority, self.vault)
        with patch.object(self.service.adapter, 'generate', side_effect=self.fake.generate):
            self.confirm_probe()
        self.clock.tick += 900
        self.assertFalse(self.status_probe()['connection_ready'])

    def test_policy_build_or_credential_change_invalidates_prior_proof(self):
        self.put()
        self.service = ConnectionValidation(self.authority, self.vault)
        with patch.object(self.service.adapter, 'generate', side_effect=self.fake.generate):
            self.confirm_probe()
        self.assertTrue(self.status_probe()['connection_ready'])
        with patch('shelf.connection_validation.POLICY_REVISION', 2):
            self.assertFalse(self.status_probe()['connection_ready'])
        with patch('shelf.connection_validation.BUILD', 'different-build'):
            self.assertFalse(self.status_probe()['connection_ready'])
        self.put()
        self.assertFalse(self.status_probe()['connection_ready'])

    def test_known_failure_states_are_distinct_and_preserve_setup(self):
        self.put()
        for outcome in ['denied', 'unsupported', 'exhausted', 'unreachable', 'incompatible']:
            self.fake.failure = TransportFault(outcome)
            result = self.confirm_probe()
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['outcome'], outcome)
            self.assertFalse(self.status_probe()['connection_ready'])
        self.assertEqual(PortableConnection.objects.count(), 1)
        self.assertFalse(PortableValidation.objects.exists())

    def test_unknown_outcome_blocks_new_intent_even_after_configuration_changes(self):
        self.put()
        self.fake.failure = TransportFault('unknown', unknown=True)
        result = self.confirm_probe()
        self.assertEqual(result['state'], 'unknown')
        self.fault('UNKNOWN_RECONCILIATION_REQUIRED', self.preview_probe)
        self.config = self.vault.configure(self.ref, CONFIG, connection_id=self.config.pk, revision=1)
        self.put()
        self.fault('UNKNOWN_RECONCILIATION_REQUIRED', self.preview_probe)
        self.assertEqual(self.fake.calls, 1)

    def test_unknown_unexpected_adapter_exception_keeps_reservation_and_does_not_retry(self):
        self.put()
        self.fake.failure = RuntimeError('synthetic-unexpected-adapter-fault')
        with self.assertRaisesRegex(RuntimeError, 'synthetic-unexpected'):
            self.confirm_probe()
        self.assertEqual(PortableProbeAttempt.objects.get().state, 'unknown')
        self.fault('UNKNOWN_RECONCILIATION_REQUIRED', self.preview_probe)

    def test_post_response_revocation_settles_stale_without_publishing(self):
        self.put()
        self.fake.after = lambda: PortableInstallation.objects.update(owner_revision=2)
        self.fault('UNAUTHENTICATED', self.confirm_probe)
        self.assertFalse(PortableValidation.objects.exists())
        self.assertEqual(PortableProbeAttempt.objects.get().state, 'failed')

    def test_background_status_cannot_renew_owner_idle_boundary(self):
        self.put()
        for _ in range(2):
            self.clock.advance(600)
            self.assertTrue(self.status_probe()['credential_configured'])
        self.clock.advance(600)
        self.fault('UNAUTHENTICATED', self.status_probe)
        self.assertFalse(self.vault._leases)

    def test_foreign_session_and_connection_cannot_use_preview(self):
        self.put()
        preview = self.preview_probe()
        other = self.authority.login(PASSWORD)
        self.fault('PREVIEW_STALE', self.service.confirm, other, uuid.UUID(preview['preview_ref']), uuid.uuid4(), True)
        self.fault('AUTHORITY_DENIED', self.service.preview, self.ref, uuid.uuid4(), 1)

    def test_restart_requires_key_reentry_and_does_not_restore_receipt_authority(self):
        self.put()
        self.confirm_probe()
        self.restart()
        vault = MemoryCredentials(self.authority)
        service = ConnectionValidation(self.authority, vault)
        ref = self.authority.login(PASSWORD)
        status = service.status(ref, self.config.pk, 1)
        self.assertEqual(status['outcome'], 'missing_credential')
        self.assertFalse(status['connection_ready'])

    def test_concurrent_same_intent_does_not_duplicate_send(self):
        self.put()
        preview, intent = self.preview_probe(), uuid.uuid4()
        entered, release = threading.Event(), threading.Event()
        self.fake.after = lambda: (entered.set(), release.wait(3))
        def first():
            try:
                return self.confirm_probe(preview, intent)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first)
            try:
                self.assertTrue(entered.wait(3))
                existing = self.confirm_probe(preview, intent)
                self.assertEqual(existing['state'], 'sending')
            finally:
                release.set()
            self.assertEqual(future.result(timeout=3)['state'], 'completed')
        self.assertEqual(self.fake.calls, 1)
        self.assertEqual(PortableProbeAttempt.objects.count(), 1)


    def test_current_denied_and_exhausted_outcomes_replace_historical_success(self):
        self.put()
        self.service = ConnectionValidation(self.authority, self.vault)
        with patch.object(self.service.adapter, 'generate', side_effect=self.fake.generate):
            for outcome in ['denied', 'exhausted']:
                self.fake.failure = None
                self.confirm_probe()
                self.assertTrue(self.status_probe()['connection_ready'])
                self.fake.failure = TransportFault(outcome)
                self.assertEqual(self.confirm_probe()['outcome'], outcome)
                status = self.status_probe()
                self.assertEqual(status['outcome'], outcome)
                self.assertFalse(status['connection_ready'])
                self.assertIsNone(status['proof_kind'])
        self.assertEqual(PortableValidation.objects.count(), 2)

    def test_fake_historical_success_cannot_hide_current_failure(self):
        self.put()
        self.confirm_probe()
        self.assertEqual(self.status_probe()['outcome'], 'isolated_test_only')
        self.fake.failure = TransportFault('denied')
        self.confirm_probe()
        status = self.status_probe()
        self.assertEqual(status['outcome'], 'denied')
        self.assertFalse(status['connection_ready'])
        self.assertEqual(PortableValidation.objects.count(), 1)

    def test_current_success_after_wall_rollback_uses_generation_then_monotone_expiry(self):
        self.put()
        self.service = ConnectionValidation(self.authority, self.vault)
        with patch.object(self.service.adapter, 'generate', side_effect=self.fake.generate):
            first = self.confirm_probe()
            first_proof = PortableValidation.objects.get(attempt_id=first['attempt_ref'])
            self.clock.advance(100)
            self.clock.wall -= timedelta(seconds=200)
            second = self.confirm_probe()
        second_proof = PortableValidation.objects.get(attempt_id=second['attempt_ref'])
        self.assertLess(second_proof.tested_at, first_proof.tested_at)
        self.assertGreater(second_proof.bindings['validation_generation'], first_proof.bindings['validation_generation'])
        status = self.status_probe()
        self.assertTrue(status['connection_ready'])
        self.assertEqual(status['expires_at'], second_proof.expires_at.isoformat())
        self.clock.tick += 900
        self.assertEqual(self.status_probe()['outcome'], 'stale')
        self.assertFalse(self.status_probe()['connection_ready'])

    def test_changed_config_does_not_inherit_a_historical_failure_as_current(self):
        self.put()
        self.fake.failure = TransportFault('denied')
        self.confirm_probe()
        self.assertEqual(self.status_probe()['outcome'], 'denied')
        self.config = self.vault.configure(self.ref, CONFIG, connection_id=self.config.pk, revision=1)
        self.put()
        status = self.status_probe()
        self.assertEqual(status['outcome'], 'stale')
        self.assertFalse(status['connection_ready'])
