"""Synthetic credentials only; never an API key, inference or wire proof."""
import json
import sys
from django.views.debug import ExceptionReporter
from unittest.mock import patch
import uuid

from shelf.models import PortableConnection, PortableInstallation
from shelf.secret_leases import MemoryCredentials
from .test_portable_authority import BoundaryCase, PASSWORD

CONFIG = {'adapter': 'openai_responses', 'model': 'gpt-6-astra', 'reasoning': 'high'}
SECRET = b'SYNTHETIC-NOT-A-CREDENTIAL-42'


class CredentialTests(BoundaryCase):
    def setUp(self):
        super().setUp()
        self.ref = self.claim_owner()
        self.vault = MemoryCredentials(self.authority)
        self.config = self.vault.configure(self.ref, CONFIG)

    def put(self):
        return self.vault.replace(self.ref, self.config.pk, self.config.config_revision, SECRET)

    def test_memory_secret_returns_masked_status_and_no_handle(self):
        status = self.put()
        self.assertEqual(status, {'connection_ref': str(self.config.pk), 'configured': True, 'validation': 'UNPROVEN'})
        self.assertEqual(self.vault.resolve(self.ref, self.config.pk, 1), SECRET)
        self.assertNotIn(SECRET.decode(), repr(self.vault._leases[self.config.pk]))
        self.assertNotIn(SECRET.decode(), json.dumps(list(PortableConnection.objects.values()), default=str))

    def test_secret_is_absent_from_durable_model_fields(self):
        self.put()
        for model in [PortableConnection, PortableInstallation]:
            self.assertFalse({'secret', 'key', 'handle', 'credential'} & {f.name for f in model._meta.fields})
        self.config.refresh_from_db()
        self.assertEqual(self.config.credential_generation, 1)

    def test_different_session_cannot_borrow_key(self):
        self.put()
        other = self.authority.login(PASSWORD)
        self.fault('CREDENTIAL_REQUIRED', self.vault.resolve, other, self.config.pk, 1)
        self.assertEqual(self.vault.resolve(self.ref, self.config.pk, 1), SECRET)

    def test_replacement_changes_generation_and_secret(self):
        self.put()
        self.vault.replace(self.ref, self.config.pk, 1, b'SYNTHETIC-REPLACEMENT')
        self.config.refresh_from_db()
        self.assertEqual(self.config.credential_generation, 2)
        self.assertEqual(self.vault.resolve(self.ref, self.config.pk, 1), b'SYNTHETIC-REPLACEMENT')

    def test_remove_revokes_memory_and_increments_generation(self):
        self.put()
        self.vault.remove(self.ref, self.config.pk, 1)
        self.fault('CREDENTIAL_REQUIRED', self.vault.resolve, self.ref, self.config.pk, 1)
        self.assertFalse(self.vault._leases)
        self.config.refresh_from_db()
        self.assertEqual(self.config.credential_generation, 2)

    def test_disable_invalidates_configuration(self):
        self.put()
        self.vault.remove(self.ref, self.config.pk, 1, disable=True)
        self.fault('REVISION_CONFLICT', self.vault.resolve, self.ref, self.config.pk, 1)
        self.assertFalse(self.vault._leases)

    def test_reconfigure_invalidates_secret_and_old_revision(self):
        self.put()
        new = self.vault.configure(self.ref, CONFIG, connection_id=self.config.pk, revision=1)
        self.assertEqual(new.config_revision, 2)
        self.assertFalse(self.vault._leases)
        self.fault('REVISION_CONFLICT', self.vault.resolve, self.ref, self.config.pk, 1)
        self.fault('CREDENTIAL_REQUIRED', self.vault.resolve, self.ref, self.config.pk, 2)

    def test_unknown_and_foreign_connection_are_denied(self):
        self.fault('AUTHORITY_DENIED', self.vault.resolve, self.ref, uuid.uuid4(), 1)
        self.config.installation = PortableInstallation.objects.create(id=uuid.uuid4(), root_identity='f' * 64,
            process_generation=uuid.uuid4())
        self.config.save(update_fields=['installation'])
        self.fault('AUTHORITY_DENIED', self.vault.resolve, self.ref, self.config.pk, 1)

    def test_no_client_selected_origin_or_secret_configuration(self):
        for extra in [{'origin': 'https://foreign.invalid'}, {'secret': SECRET.decode()}, {'validation': 'PASS'}]:
            self.fault('CONTRACT_INVALID', self.vault.configure, self.ref, {**CONFIG, **extra})
        self.fault('CONTRACT_INVALID', self.vault.configure, self.ref, {**CONFIG, 'model': 'other'})

    def test_mutations_require_recent_reauthentication(self):
        self.put()
        self.clock.advance(300)
        self.fault('UNAUTHENTICATED', self.vault.remove, self.ref, self.config.pk, 1)
        self.fault('UNAUTHENTICATED', self.vault.replace, self.ref, self.config.pk, 1, SECRET)
        self.fault('UNAUTHENTICATED', self.vault.configure, self.ref, CONFIG)
        self.authority.reauthenticate(self.ref, PASSWORD)
        self.vault.remove(self.ref, self.config.pk, 1)

    def test_expiry_logout_and_shutdown_clear_memory(self):
        self.put()
        self.authority.logout(self.ref)
        self.assertFalse(self.vault._leases)
        self.ref = self.authority.login(PASSWORD)
        self.put()
        self.clock.advance(1800)
        self.fault('UNAUTHENTICATED', self.vault.resolve, self.ref, self.config.pk, 1)
        self.assertFalse(self.vault._leases)
        self.ref = self.authority.login(PASSWORD)
        self.put()
        self.boundary.close()
        self.assertFalse(self.vault._leases)

    def test_restart_cannot_resurrect_memory_or_session(self):
        self.put()
        self.restart()
        fresh = MemoryCredentials(self.authority)
        ref = self.authority.login(PASSWORD)
        self.fault('CREDENTIAL_REQUIRED', fresh.resolve, ref, self.config.pk, 1)
        self.assertFalse(self.vault._leases)

    def test_changed_generation_blocks_old_lease(self):
        self.put()
        PortableConnection.objects.filter(pk=self.config.pk).update(credential_generation=9)
        self.fault('CREDENTIAL_REQUIRED', self.vault.resolve, self.ref, self.config.pk, 1)

    def test_failed_core_dump_disable_accepts_no_secret(self):
        with patch('shelf.portable_authority.resource.getrlimit', return_value=(1, 1)), patch(
                'shelf.portable_authority.resource.setrlimit', side_effect=OSError):
            self.fault('CAPABILITY_UNAVAILABLE', self.vault.replace, self.ref, self.config.pk, 1, SECRET)
        self.assertFalse(self.vault._leases)
        self.config.refresh_from_db()
        self.assertEqual(self.config.credential_generation, 0)

    def test_invalid_credential_bytes_are_never_accepted(self):
        for secret in ['', b'', b'x' * 8193, bytearray(b'not-bytes')]:
            self.fault('CONTRACT_INVALID', self.vault.replace, self.ref, self.config.pk, 1, secret)
        self.assertFalse(self.vault._leases)


    def test_background_reads_do_not_renew_original_idle_boundary(self):
        self.put()
        original = self.authority._sessions[self.ref].last_seen
        for _ in range(2):
            self.clock.advance(600)
            self.assertEqual(self.vault.resolve(self.ref, self.config.pk, 1), SECRET)
        self.assertEqual(self.authority._sessions[self.ref].last_seen, original)
        self.clock.advance(600)
        self.fault('UNAUTHENTICATED', self.vault.resolve, self.ref, self.config.pk, 1)
        self.assertFalse(self.vault._leases)

    def test_genuine_owner_activity_extends_idle_but_background_does_not(self):
        self.put()
        self.clock.advance(900)
        self.authority.authorize(self.ref)
        self.clock.advance(900)
        self.assertEqual(self.vault.resolve(self.ref, self.config.pk, 1), SECRET)
        self.clock.advance(900)
        self.fault('UNAUTHENTICATED', self.vault.resolve, self.ref, self.config.pk, 1)

    def test_unexpected_vault_error_redacts_credential_in_django_diagnostics(self):
        with patch('shelf.secret_leases.PortableConnection.objects.filter',
                   side_effect=RuntimeError('SYNTHETIC-VAULT-DIAGNOSTIC-FAULT')):
            try:
                self.vault.replace(self.ref, self.config.pk, 1, SECRET)
            except RuntimeError:
                diagnostic = ExceptionReporter(None, *sys.exc_info()).get_traceback_text()
            else:
                self.fail('Synthetic unexpected failure did not run')
        self.assertIn('SYNTHETIC-VAULT-DIAGNOSTIC-FAULT', diagnostic)
        self.assertIn('secret_leases.py', diagnostic)
        self.assertIn('*********', diagnostic)
        self.assertNotIn(SECRET.decode(), diagnostic)
