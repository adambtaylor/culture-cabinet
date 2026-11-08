"""Real disposable credential stores and native sessions; no provider requests."""
from dataclasses import replace
from contextlib import contextmanager
from datetime import timedelta
import os
from unittest.mock import patch
import uuid

from django.test import TestCase

from shelf import credential_store as store, grant_authority as ga
from shelf.native_source_credentials import SourceCredentialReuse, import_existing_source_credential
from shelf.secret_leases import MemoryCredentials
from shelf.portable_authority import AuthorityFault
from .fixtures_credentials import CANARY, FakeTree, metadata
from .test_portable_authority import BoundaryCase, PASSWORD


class NativeSourceCredentialTests(TestCase, BoundaryCase):
    def setUp(self):
        super().setUp()
        self.clock.wall = store.utc_now()
        self.ref = self.claim_owner()
        self.vault = MemoryCredentials(self.authority)
        self.profile = ga.create_owner_profile(self.authority, self.ref, expected_owner_revision=1)
        self.row = self.vault.configure(self.ref, {'adapter': 'tmdb_movie', 'model': '', 'reasoning': ''})
        self.binding = store.Binding('FICTION_ACCOUNT', '17', '23', 'tmdb', 'culture-cabinet-tmdb-backend')
        self.intent = SourceCredentialReuse(self.authority, self.ref, self.boundary.installation_id,
            self.boundary.process_generation, self.authority.authorize(self.ref).owner_id,
            1, self.profile, str(self.row.pk), 1, self.binding)
        self.handles, self.records = [], []
        self.tree = FakeTree(store, provider='tmdb')
        self.addCleanup(self.tree.close)
        self.write_record()
        self.tree.store.close()
        def open_fixture(*, provider):
            self.assertEqual(provider, self.binding.provider)
            handle = store._fixture_store(self.tree.path, provider=provider)
            self.handles.append(handle)
            return handle
        read_record = store.read_record
        def read_fixture(handle):
            record = read_record(handle)
            self.records.append(record)
            return record
        self.enterContext(patch.object(store, 'open_store', side_effect=open_fixture))
        self.enterContext(patch.object(store, 'read_record', side_effect=read_fixture))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('No provider calls')))

    def write_record(self, **changes):
        values = dict(binding=self.binding, scopes=store.TMDB_SCOPES, entered_at=self.clock.wall)
        values.update(changes)
        self.assertEqual(store.commit_entry(self.tree.store, metadata(store, **values), bytearray(CANARY)).outcome,
                         'stored')

    def transfer(self, **changes):
        arguments = dict(profile_ref=self.profile, connection_ref=str(self.row.pk),
                         expected_revision=1, approved_reuse=self.intent)
        arguments.update(changes)
        return import_existing_source_credential(self.authority, self.ref, **arguments)

    def assert_closed(self):
        self.assertTrue(all(handle.fd == -1 and handle._lock is None for handle in self.handles))
        self.assertTrue(all(not any(record.secret) for record in self.records))

    def test_exact_legacy_to_native_intent_imports_only_session_secret(self):
        result = self.transfer()
        self.assertTrue(result['configured'])
        self.assertEqual(self.vault.resolve(self.ref, self.row.pk, 1), CANARY)
        self.assertNotIn(CANARY.decode(), repr(result))
        self.assertNotIn(CANARY.decode(), repr(self.intent))
        self.assert_closed()
        self.assertEqual(self.records[0].metadata.binding, self.binding)

    def test_native_identity_revision_and_provider_mismatches_deny_before_store_read(self):
        for changes in ({'session_ref': 'foreign'}, {'installation_id': uuid.uuid4()},
                        {'process_generation': uuid.uuid4()}, {'owner_id': 999},
                        {'owner_revision': 99}, {'profile_ref': str(uuid.uuid4())},
                        {'connection_ref': str(uuid.uuid4())}, {'config_revision': 2}):
            with self.subTest(changes=changes):
                self.fault('AUTHORITY_DENIED', self.transfer,
                           approved_reuse=replace(self.intent, **changes))
        self.fault('REVISION_CONFLICT', self.transfer, expected_revision=2,
                   approved_reuse=replace(self.intent, config_revision=2))
        self.row.adapter = 'openai_responses'
        self.row.save(update_fields=['adapter'])
        self.fault('CAPABILITY_UNAVAILABLE', self.transfer)
        self.assertFalse(self.handles)
        self.assertFalse(self.vault._leases)

    def test_wrong_legacy_identity_refuses_and_closes_real_record(self):
        for changes in ({'account_id': 'OTHER'}, {'actor_id': '18'}, {'profile_id': '24'}):
            with self.subTest(changes=changes):
                intent = replace(self.intent, legacy_binding=replace(self.binding, **changes))
                self.fault('AUTHORITY_DENIED', self.transfer, approved_reuse=intent)
                self.assert_closed()
        self.fault('CREDENTIAL_REQUIRED', self.transfer, approved_reuse=replace(self.intent,
            legacy_binding=replace(self.binding, provider='hardcover')))
        self.assertFalse(self.vault._leases)

    def test_pending_and_short_lifetime_records_fail_without_import(self):
        pending = self.tree.path / store.PENDING
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        self.fault('CREDENTIAL_REQUIRED', self.transfer)
        pending.unlink()
        self.clock.wall += timedelta(hours=20)
        self.fault('CREDENTIAL_LIFETIME_REQUIRED', self.transfer)
        self.assertFalse(self.vault._leases)
        self.assert_closed()

    def test_expired_metadata_fails_and_cleans_open_store(self):
        self.clock.wall += timedelta(days=2)
        self.fault('CREDENTIAL_REQUIRED', self.transfer)
        self.assertFalse(self.vault._leases)
        self.assert_closed()

    def test_import_retains_rotation_logout_and_session_separation(self):
        self.transfer()
        self.vault.replace(self.ref, self.row.pk, 1, b'SYNTHETIC-ROTATED')
        self.assertEqual(self.vault.resolve(self.ref, self.row.pk, 1), b'SYNTHETIC-ROTATED')
        other = self.authority.login(PASSWORD)
        self.fault('CREDENTIAL_REQUIRED', self.vault.resolve, other, self.row.pk, 1)
        self.authority.logout(self.ref)
        self.assertFalse(self.vault._leases)
        self.fault('UNAUTHENTICATED', self.transfer)
        self.assert_closed()

    def test_failed_final_recovery_check_removes_imported_lease(self):
        original = ga.recovery_guard
        @contextmanager
        def fail_after_transfer(*args, **kwargs):
            with original(*args, **kwargs) as checkpoint:
                yield checkpoint
                raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503)
        with patch.object(ga, 'recovery_guard', fail_after_transfer):
            self.fault('CAPABILITY_UNAVAILABLE', self.transfer)
        self.assertFalse(self.vault._leases)
        self.assert_closed()

    def test_existing_media_provider_contexts_do_not_change_or_borrow_bindings(self):
        for adapter, provider, purpose, scopes in (
                ('tmdb_tv', 'tmdb', 'culture-cabinet-tmdb-backend', store.TMDB_SCOPES),
                ('hardcover', 'hardcover', 'culture-cabinet-hardcover-backend', store.SCOPES)):
            with self.subTest(adapter=adapter):
                self.tree.close()
                self.row = self.vault.configure(self.ref, {'adapter': adapter, 'model': '', 'reasoning': ''})
                self.binding = store.Binding('FICTION_ACCOUNT', '17', '23', provider, purpose)
                self.intent = replace(self.intent, connection_ref=str(self.row.pk), legacy_binding=self.binding)
                self.tree = FakeTree(store, provider=provider)
                self.addCleanup(self.tree.close)
                self.write_record(scopes=scopes)
                self.tree.store.close()
                self.transfer()
                self.assertEqual(self.vault.resolve(self.ref, self.row.pk, 1), CANARY)
                self.assert_closed()
