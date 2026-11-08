"""Fresh synthetic authority/floor regressions; authoring does not execute these.

Reuse the accepted bound-root/neutral-fake fixture. TestCase wraps all added
rows in a rollback: teardown never deletes retained authority tombstones.
"""
from contextlib import contextmanager, ExitStack
from datetime import timedelta
from pathlib import Path
import sqlite3
from unittest.mock import PropertyMock, patch
import uuid

from django.db import DatabaseError, connection, connections, transaction
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.test import TestCase

from cabinet.portable_boundary import BoundRoot

from shelf import grant_authority as ga, recovery
from shelf.connection_validation import ConnectionValidation
from shelf.models import (Grant, PortableDisclosureGrant, Profile, ProfileCabinetState,
                          RecoveryAnchor, RecoveryRestriction, PortableDisclosurePreview)
from shelf.portable_disclosure import DisclosureServices
from shelf.secret_leases import MemoryCredentials
from .test_connection_validation import ConnectionCase
from .test_portable_authority import PASSWORD


class AuthorityCase(TestCase, ConnectionCase):
    def test_media_catalogue_scope_is_distinct_from_neutral_inference(self):
        profile = self.bind()
        vault = self.authority._source_vault
        row = vault.configure(self.ref, {'adapter': 'hardcover', 'model': '', 'reasoning': ''})
        vault.replace(self.ref, row.pk, row.config_revision, b'SYNTHETIC-HARDCOVER')
        ga.change_grant(self.authority, self.ref, profile_ref=profile, connection_ref=str(row.pk),
            purpose='catalogue_lookup', allowed_data_classes=['canonical_seed'],
            expected_revision=0, expires_at=self.expires, action='grant')
        view = ga.current_view(self.authority, self.ref, profile_ref=profile,
                               connection_ref=str(row.pk), purpose='catalogue_lookup')
        self.assertEqual(view.context['adapter_id'], 'hardcover')
        self.fault('AUTHORITY_DENIED', ga.current_view, self.authority, self.ref,
            profile_ref=profile, connection_ref=str(row.pk), purpose='inference')

    def setUp(self):
        super().setUp()
        self.disclosure = DisclosureServices.isolated_test(self.authority, self.service)
        self.put()
        self.confirm_probe()
        self.expires = self.clock.wall + timedelta(minutes=10)

    def bind(self):
        return ga.create_owner_profile(self.authority, self.ref,
            expected_owner_revision=self.authority.authorize(self.ref).owner_revision)

    def consent(self, profile_ref, *, action='grant', revision=0, classes=None, **overrides):
        arguments = dict(profile_ref=profile_ref, expected_revision=revision, purpose='inference',
            connection_ref=str(self.config.pk), allowed_data_classes=classes or ['canonical_seed'],
            expires_at=self.expires, action=action)
        arguments.update(overrides)
        return ga.change_grant(self.authority, self.ref, **arguments)

    def view(self, profile_ref):
        return ga.current_view(self.authority, self.ref, profile_ref=profile_ref,
            connection_ref=str(self.config.pk), purpose='inference')

    def rejected_write(self, callback):
        with self.assertRaises(DatabaseError), transaction.atomic():
            callback()

    @contextmanager
    def controlled_recovery_state(self, state):
        """FX1: a real foreign live witness, not an impossible second singleton."""
        self.assertEqual(state, 'foreign_witness')
        raw = sqlite3.connect(':memory:')
        try:
            foreign_id = str(uuid.uuid4())
            raw.execute('CREATE TABLE shelf_recoveryanchor (id INTEGER PRIMARY KEY, store_id TEXT)')
            raw.execute('INSERT INTO shelf_recoveryanchor VALUES (1, ?)', (foreign_id,))
            for table in ('feedbackevent', 'feedbackprojection', 'feedbacksubject',
                          'mutationreceipt', 'recoveryrestriction'):
                raw.execute(f'CREATE TABLE shelf_{table} (id INTEGER PRIMARY KEY)')
            raw.commit()
            current = recovery.CurrentRecoveryCoordinator.provision_empty(store_id=foreign_id,
                journal_directory=Path(self.directory.name) / ('foreign-' + uuid.uuid4().hex),
                trusted_empty_db=raw)
            try:
                with recovery.using_recovery(current):
                    yield current
            finally:
                current.close()
        finally:
            raw.close()



class GrantAuthorityTests(AuthorityCase):
    def test_fresh_binding_is_explicit_repeatable_and_uses_actual_owner(self):
        self.assertFalse(Profile.objects.exists())
        ref = self.bind()
        self.assertEqual(self.bind(), ref)
        state = ProfileCabinetState.objects.get()
        self.assertEqual(state.actor_id, self.authority.authorize(self.ref).owner_id)
        self.assertEqual(str(state.profile_ref), ref)
        self.assertEqual(state.profile.kind, 'owner')
        self.assertNotEqual(state.subject_ref, state.actor_ref)
        self.assertTrue(Grant.objects.get(profile=state.profile).can_mutate)
        self.assertEqual((state.policy_epoch, state.publication_revision), (0, 0))
        self.assertIsNone(state.active_manifest_ref)

    def test_existing_household_profile_is_never_adopted(self):
        Profile.objects.create(kind='owner', label='Synthetic legacy owner', active=True)
        self.fault('AUTHORITY_DENIED', self.bind)
        self.assertFalse(ProfileCabinetState.objects.exists())

    def test_wrong_owner_revision_session_profile_and_recovery_are_denied(self):
        self.fault('REVISION_CONFLICT', ga.create_owner_profile, self.authority, self.ref,
                   expected_owner_revision=99)
        ref = self.bind()
        self.fault('UNAUTHENTICATED', ga.current_view, self.authority, 'unknown-session',
                   profile_ref=ref, connection_ref=str(self.config.pk), purpose='inference')
        self.fault('AUTHORITY_DENIED', self.view, str(uuid.uuid4()))
        with self.controlled_recovery_state('foreign_witness'):
            self.fault('CAPABILITY_UNAVAILABLE', self.bind)

    def test_revoke_regrant_keeps_identity_and_advances_both_floors(self):
        ref = self.bind()
        key, revision = self.consent(ref)
        self.assertEqual(revision, 1)
        self.assertEqual(self.consent(ref, action='revoke', revision=1), (key, 2))
        self.assertEqual(self.consent(ref, action='revoke', revision=2), (key, 2))
        self.assertEqual(self.consent(ref, action='regrant', revision=2), (key, 3))
        state = ProfileCabinetState.objects.get()
        self.assertEqual((state.policy_epoch, state.restriction_watermark), (3, 3))
        self.assertEqual(PortableDisclosureGrant.objects.count(), 1)
        self.assertEqual(self.view(ref).grants[0]['grant_revision'], 3)

    def test_stale_competing_grant_loses_without_extra_floor_or_row(self):
        ref = self.bind()
        self.consent(ref)
        self.fault('REVISION_CONFLICT', self.consent, ref)
        self.assertEqual(PortableDisclosureGrant.objects.count(), 1)
        self.assertEqual(ProfileCabinetState.objects.get().policy_epoch, 1)
        self.assertEqual(self.consent(ref, revision=1),
                         (str(PortableDisclosureGrant.objects.get().pk), 1))

    def test_grant_scope_classes_and_expiry_are_validated_without_echoes(self):
        ref = self.bind()
        for overrides in ({'classes': ['not-a-data-class']}, {'purpose': 'unknown-purpose'},
                          {'expires_at': self.clock.wall},
                          {'expires_at': self.clock.wall + timedelta(hours=9)},
                          {'classes': ['raw_notes', 'raw_notes']}):
            self.fault('CONTRACT_INVALID', self.consent, ref, **overrides)
        self.fault('AUTHORITY_DENIED', self.consent, ref, connection_ref=str(uuid.uuid4()))
        self.assertFalse(PortableDisclosureGrant.objects.exists())

    def test_raw_and_bulk_writes_require_the_owning_transaction(self):
        ref = self.bind()
        key, _ = self.consent(ref)
        self.rejected_write(lambda: PortableDisclosureGrant.objects.filter(pk=key).update(state='revoked', revision=2))
        state = ProfileCabinetState.objects.get()
        self.rejected_write(lambda: ProfileCabinetState.objects.bulk_update(
            [ProfileCabinetState(profile_id=state.profile_id, policy_epoch=99)], ['policy_epoch']))
        def raw():
            with connection.cursor() as cursor:
                cursor.execute('UPDATE shelf_profilecabinetstate SET policy_epoch=99')
        self.rejected_write(raw)
        self.assertEqual(ProfileCabinetState.objects.get().policy_epoch, 1)

    def test_authorized_raw_regression_rebinding_and_deletion_still_fail(self):
        ref = self.bind()
        key, _ = self.consent(ref)
        def attempt(callback):
            with transaction.atomic(), ga._write_window():
                callback()
        self.rejected_write(lambda: attempt(lambda: PortableDisclosureGrant.objects.filter(pk=key).update(revision=0)))
        self.rejected_write(lambda: attempt(lambda: PortableDisclosureGrant.objects.filter(pk=key).delete()))
        self.rejected_write(lambda: attempt(lambda: ProfileCabinetState.objects.update(actor_ref=uuid.uuid4())))
        self.rejected_write(lambda: attempt(lambda: ProfileCabinetState.objects.update(policy_epoch=0)))
        self.assertEqual(PortableDisclosureGrant.objects.get().revision, 1)

    def test_rollback_reverts_consent_and_floor_and_closes_write_window(self):
        ref = self.bind()
        with self.assertRaisesRegex(RuntimeError, 'synthetic rollback'):
            with transaction.atomic():
                self.consent(ref)
                raise RuntimeError('synthetic rollback')
        self.assertFalse(PortableDisclosureGrant.objects.exists())
        self.assertEqual(ProfileCabinetState.objects.get().policy_epoch, 0)
        self.rejected_write(lambda: ProfileCabinetState.objects.update(policy_epoch=9))

    def test_restart_retains_floors_but_never_revives_proof_or_session(self):
        ref = self.bind()
        self.consent(ref)
        old_authority, old_session = self.authority, self.ref
        self.restart()
        self.fault('UNAUTHENTICATED', ga.current_view, old_authority, old_session,
            profile_ref=ref, connection_ref=str(self.config.pk), purpose='inference')
        self.ref = self.authority.login(PASSWORD)
        self.assertEqual(self.bind(), ref)
        self.vault = MemoryCredentials(self.authority)
        self.service = ConnectionValidation(self.authority, self.vault)
        DisclosureServices(self.authority, self.service)
        self.fault('VALIDATION_STALE', self.view, ref)
        self.assertEqual(ProfileCabinetState.objects.get().restriction_watermark, 1)


class RecoveryCase(ConnectionCase):
    """Non-test shared fixture; real coordinator protocol on disposable stores.

    Protected rows live only in a dedicated autocommit connection. Restoring the
    original wrapper before Django teardown avoids deleting retained tombstones,
    and avoids provisioning a coordinator inside TestCase's outer transaction.
    """
    def setUp(self):
        original = connections['default']
        original.ensure_connection()
        self.assertFalse(original.in_atomic_block)
        wrapper = DatabaseWrapper({**original.settings_dict,
            'NAME': 'file:memorydb_repair_' + uuid.uuid4().hex + '?mode=memory&cache=shared'}, alias='default')
        wrapper.ensure_connection()
        original.connection.backup(wrapper.connection)
        connections['default'] = wrapper
        def restore():
            try:
                if wrapper.connection is not None:
                    wrapper.connection.close()
                    wrapper.connection = None
            finally:
                connections['default'] = original
        self.addCleanup(restore)
        super().setUp()
        RecoveryAnchor.objects.get_or_create(id=1, defaults={'store_id': uuid.uuid4()})
        self.disclosure = DisclosureServices.isolated_test(self.authority, self.service)
        self.put()
        self.confirm_probe()
        self.expires = self.clock.wall + timedelta(minutes=10)

    bind = AuthorityCase.bind
    consent = AuthorityCase.consent
    view = AuthorityCase.view

    def prepare_restriction(self, current):
        state = ProfileCabinetState.objects.get()
        restriction = dict(kind='revoke', profile_id=state.profile_id, target_id=None,
            signal=None, scope=None, subject_id=None, restriction_code='deny_export')
        return current.prepare(str(uuid.uuid4()), [restriction])

    def recovery_rows(self):
        return {model._meta.db_table: list(model.objects.order_by('pk').values())
            for model in (RecoveryAnchor, Profile, Grant, ProfileCabinetState,
                          PortableDisclosureGrant, PortableDisclosurePreview)}

    @contextmanager
    def controlled_recovery_state(self, state):
        if state == 'foreign_witness':
            with AuthorityCase.controlled_recovery_state(self, state) as current:
                yield current
            return
        # Clone the clean migrated fixture, not an adopted external database.
        # Each denial is independent: committed restrictions are never cleared.
        original = connections['default']
        self.assertFalse(original.in_atomic_block)
        name = ('file:memorydb_repair_' + uuid.uuid4().hex + '?mode=memory&cache=shared')
        if state == 'normal_disk':
            name = str(Path(self.directory.name) / (uuid.uuid4().hex + '.sqlite3'))
        wrapper = DatabaseWrapper({**original.settings_dict, 'NAME': name}, alias='default')
        raw = None

        def restore_wrapper():
            # A partially opened wrapper can exist even if ensure_connection
            # raises. Retain the opened raw handle as well, in case body code
            # replaces the wrapper connection; in-memory wrapper.close() is inert.
            try:
                connections['default'] = original
            finally:
                remaining = wrapper.connection
                try:
                    if raw is not None:
                        raw.close()
                finally:
                    try:
                        if remaining is not None and remaining is not raw:
                            remaining.close()
                    finally:
                        wrapper.connection = None

        with ExitStack() as cleanup:
            # Register before acquisition/backup/switch. The inner coordinator
            # stack unwinds first, then the descriptor, then raw connection close.
            cleanup.callback(restore_wrapper)
            with patch.object(BoundRoot, 'database_name', new_callable=PropertyMock) as database:
                database.return_value = name
                wrapper.ensure_connection()
                raw = wrapper.connection
                original.connection.backup(wrapper.connection)
                connections['default'] = wrapper
                current = None
                with ExitStack() as stack:
                    if state not in ('empty', 'normal_disk'):
                        current = recovery.CurrentRecoveryCoordinator.provision_empty(
                            store_id=str(RecoveryAnchor.objects.get(pk=1).store_id),
                            journal_directory=Path(self.directory.name) / ('journal-' + uuid.uuid4().hex),
                            trusted_empty_db=wrapper.connection)
                        stack.callback(current.close)
                        stack.enter_context(recovery.using_recovery(current))
                        if state in ('pending', 'committed'):
                            pending = self.prepare_restriction(current)
                            if state == 'committed':
                                current.apply_pending(wrapper.connection)
                                current.finalize(pending, pending['local_commit_uuid'])
                        elif state == 'closed':
                            current.close()
                        elif state == 'blocked':
                            (current.directory / 'journal').chmod(0o644)
                            with self.assertRaises(recovery.RecoveryBlocked):
                                current.verify_current()
                        elif state == 'lost':
                            (current.directory / 'journal').unlink()
                        else:
                            self.assertEqual(state, 'clear')
                    yield current


class RecoveryAuthorityTests(RecoveryCase):
    def test_pending_before_mirror_denies_authority_without_writes(self):
        profile = self.bind()
        self.consent(profile)
        view = self.view(profile)
        anchor = list(RecoveryAnchor.objects.values())
        with self.controlled_recovery_state('pending') as current:
            self.assertIsNotNone(current.pending)
            self.assertFalse(RecoveryRestriction.objects.exists())
            before = self.recovery_rows()
            # If mirror verification runs first, this sentinel makes the ordering
            # failure visible; it does not manufacture or bypass recovery authority.
            with patch.object(current, 'verify_mirror', side_effect=AssertionError('pending must deny first')):
                for operation in (self.bind, lambda: self.consent(profile, action='revoke', revision=1),
                                  lambda: self.view(profile), lambda: ga.assert_current(view)):
                    self.fault('CAPABILITY_UNAVAILABLE', operation)
                    self.assertEqual(self.recovery_rows(), before)

            self.assertEqual(list(RecoveryAnchor.objects.values()), anchor)
            self.assertFalse(RecoveryRestriction.objects.exists())

    def test_current_witness_and_committed_restriction_fail_closed(self):
        profile = self.bind()
        self.consent(profile)
        view = self.view(profile)
        for state in ('empty', 'clear'):
            with self.subTest(state=state), self.controlled_recovery_state(state):
                self.assertEqual(self.bind(), profile)
                self.assertEqual(ga.assert_current(view), view)
                self.assertEqual(RecoveryAnchor.objects.count(), 1)
        for state in ('committed', 'closed', 'blocked', 'foreign_witness', 'lost', 'normal_disk'):
            with self.subTest(state=state), self.controlled_recovery_state(state) as current:
                if state == 'committed':
                    self.assertIsNone(current.pending)
                    self.assertEqual(len(current.committed_restrictions()), 1)
                    self.assertEqual(RecoveryRestriction.objects.count(), 1)
                before = self.recovery_rows()
                for operation in (self.bind, lambda: self.consent(profile, action='revoke', revision=1),
                                  lambda: self.view(profile), lambda: ga.assert_current(view)):
                    self.fault('CAPABILITY_UNAVAILABLE', operation)
                    self.assertEqual(self.recovery_rows(), before)


class BrokerHistoryCase(AuthorityCase):
    """Reuse the released guarded broker fixture without importing it cyclically."""
    def setUp(self):
        super().setUp()
        from shelf.enhance_contract import RegistryView
        from shelf.portable_cabinet_store import CabinetStore
        from .test_portable_cabinet_store import IsolatedStateProvider
        self.profile_ref = self.bind()
        self.consent(self.profile_ref)
        self.consent(self.profile_ref, purpose='catalogue_lookup')
        self.state = ProfileCabinetState.objects.get()
        self.registry = RegistryView('1', ('books', 'films', 'tv', 'music', 'events', 'outings'))
        self.provider = IsolatedStateProvider(self)
        self.store = CabinetStore.isolated_test(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), state_provider=self.provider)

    def intent(self):
        from shelf.enhance_contract import EnhanceIntent
        return EnhanceIntent(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()),
                             self.store.current().bindings)


class BrokerHistoryAuthorityTests(BrokerHistoryCase):
    def test_guarded_broker_progress_and_publication_leave_input_revision_stable(self):
        from shelf import profile_snapshot
        from .test_portable_cabinet_store import StoreCase, candidate
        before = self.view(self.profile_ref)
        snapshot = profile_snapshot.capture(before)
        row = self.store.admit(self.intent())
        self.assertEqual(self.view(self.profile_ref), before)
        self.assertEqual(profile_snapshot.capture(before), snapshot)
        row = StoreCase.sealed(self, self.intent())
        row = self.store.reserve(str(row.pk), body_digest=row.body_digest, expected_revision=row.revision)
        self.assertEqual(self.view(self.profile_ref), before)
        self.assertEqual(profile_snapshot.capture(before), snapshot)
        from .test_portable_cabinet_store import result
        row = self.store.record_result(str(row.pk), result())
        self.store.publish(str(row.pk), manifest=StoreCase.manifest(self),
            expected_publication_revision=0, allowed_candidates=[candidate()])
        self.assertEqual(self.view(self.profile_ref), before)
        self.assertEqual(profile_snapshot.capture(before), snapshot)
        # A separate pre-send cancellation is metadata, too; no output becomes evidence.
        other = self.store.admit(self.intent())
        self.store.cancel(str(other.pk), expected_revision=other.revision)
        self.assertEqual(profile_snapshot.capture(before), snapshot)
        self.assertEqual(snapshot['evidence'], [])
        self.assertEqual(self.fake.calls, 1)

    def test_broker_guards_refuse_raw_bulk_conversion_rebinding_and_deletion(self):
        from shelf.models import SynthesisRun
        row = self.store.admit(self.intent())
        self.rejected_write(lambda: SynthesisRun.objects.filter(pk=row.pk).update(revision=1))
        changed = SynthesisRun.objects.get(pk=row.pk)
        changed.portable_bindings = {}
        self.rejected_write(lambda: SynthesisRun.objects.bulk_update([changed], ['portable_bindings']))
        attacks = [dict(contract_version='legacy/v1', mode='household', installation=None, client_key=None),
                   dict(contract_version='unrecognized/v99'),
                   dict(input_snapshot={'not': 'bookkeeping'}), dict(input_digest='f' * 64),
                   dict(portable_bindings={}), dict(portable_connection_id=uuid.uuid4()),
                   dict(request_identity=uuid.uuid4()), dict(refresh_nonce=uuid.uuid4())]
        for values in attacks:
            with self.subTest(fields=tuple(values)):
                def update():
                    with transaction.atomic(), ga._write_window():
                        SynthesisRun.objects.filter(pk=row.pk).update(revision=1, **values)
                self.rejected_write(update)
        def delete():
            with transaction.atomic(), ga._write_window():
                SynthesisRun.objects.filter(pk=row.pk).delete()
        self.rejected_write(delete)
        legacy = SynthesisRun.objects.create(actor_id=self.state.actor_id, profile_id=self.state.profile_id,
            input_snapshot={}, input_digest='a' * 64)
        def convert():
            with transaction.atomic(), ga._write_window():
                SynthesisRun.objects.filter(pk=legacy.pk).update(contract_version='enhance/v1',
                    mode='portable', installation_id=row.installation_id,
                    portable_connection_id=row.portable_connection_id, client_key=uuid.uuid4(),
                    refresh_nonce=uuid.uuid4(), revision=1)
        self.rejected_write(convert)
        row.refresh_from_db()
        self.assertEqual((row.revision, row.attempts, row.state), (0, 0, 'queued'))

    def test_malformed_foreign_intents_fail_closed_and_legacy_rows_keep_full_hash(self):
        import copy
        from shelf import profile_snapshot
        from shelf.enhance_contract import digest
        from shelf.disclosure import canonical_bytes
        from shelf.models import SynthesisRun
        original = self.store.admit(self.intent())
        before = self.view(self.profile_ref)
        attacks = [dict(portable_bindings={}), dict(input_digest='0' * 64),
                   dict(input_snapshot={'forged': True}), dict(portable_stage='published'),
                   dict(cancel_requested=True)]
        foreign = copy.deepcopy(original.portable_bindings)
        foreign['bindings']['actor_ref'] = str(uuid.uuid4())
        attacks.append(dict(portable_bindings=foreign))
        for values in attacks:
            with self.subTest(fields=tuple(values)), self.assertRaisesRegex(RuntimeError, '^rollback attack$'):
                with transaction.atomic():
                    refresh = uuid.uuid4()
                    fields = dict(actor_id=original.actor_id, profile_id=original.profile_id,
                        installation_id=original.installation_id, portable_connection_id=original.portable_connection_id,
                        contract_version='enhance/v1', mode='portable', client_key=uuid.uuid4(), refresh_nonce=refresh,
                        portable_stage='authorized', portable_expires_at=original.portable_expires_at,
                        input_snapshot={}, portable_bindings=copy.deepcopy(original.portable_bindings),
                        input_digest=digest(canonical_bytes({'schema': 'enhance/v1',
                            'bindings': original.portable_bindings['bindings'], 'refresh_nonce': str(refresh)})),
                        evidence=dict(code='OK', outcomes=[], phase='catalogue', expected_publication_revision=0))
                    fields.update(values)
                    if 'input_digest' not in values:
                        fields['input_digest'] = digest(canonical_bytes({'schema': 'enhance/v1',
                            'bindings': fields['portable_bindings'].get('bindings', {}),
                            'refresh_nonce': str(refresh)}))
                    # The insert guard rejects nonempty input before any reader runs.
                    if 'input_snapshot' in values:
                        self.rejected_write(lambda: self._insert_broker(fields))
                    else:
                        self._insert_broker(fields)
                        self.fault('CAPABILITY_UNAVAILABLE', ga._history_revisions, self.state)
                        self.fault('CAPABILITY_UNAVAILABLE', profile_snapshot.capture, before)
                    raise RuntimeError('rollback attack')
        from importlib import import_module
        broker = import_module('shelf.migrations.0022_portable_enhance_state')
        for sql in broker.TRIGGERS:
            name = sql.split('CREATE TRIGGER ', 1)[1].split()[0]
            with self.subTest(missing_guard=name), self.assertRaisesRegex(RuntimeError, '^rollback guard$'):
                with transaction.atomic():
                    # Disposable transactional DDL; rollback restores the trusted
                    # guard. A label cannot hide a row if even one guard is absent.
                    with connection.cursor() as cursor:
                        cursor.execute('DROP TRIGGER ' + name)
                    self.fault('CAPABILITY_UNAVAILABLE', ga.synthesis_history, self.state)
                    self.fault('CAPABILITY_UNAVAILABLE', profile_snapshot.capture, before)
                    raise RuntimeError('rollback guard')
        legacy = SynthesisRun.objects.create(actor_id=self.state.actor_id, profile_id=self.state.profile_id,
            input_snapshot={'legacy': 'unadmitted'}, input_digest='e' * 64)
        current = self.view(self.profile_ref)
        self.assertNotEqual(current.profile_revision_digest, before.profile_revision_digest)
        self.assertEqual([row['id'] for row in ga.synthesis_history(self.state)], [legacy.pk])
        self.fault('CAPABILITY_UNAVAILABLE', profile_snapshot.capture, current)
        SynthesisRun.objects.filter(pk=legacy.pk).update(evidence={'changed': True})
        self.assertNotEqual(self.view(self.profile_ref).profile_revision_digest, current.profile_revision_digest)

    def _insert_broker(self, fields):
        from shelf.models import SynthesisRun
        with ga._write_window():
            return SynthesisRun.objects.create(**fields)


class SourceUseHistoryTests(BrokerHistoryCase):
    def source_row(self):
        row = self.store.admit(self.intent())
        outcomes = [{'category': key, 'state': 'fresh' if key == 'books' else 'unavailable',
            'candidate_count': int(key == 'books'), 'code': 'OK'} for key in self.registry.categories]
        summary = [{'category': key, 'collected_count': int(key == 'books'),
                    'model_eligible_count': 0} for key in self.registry.categories]
        return self.store.checkpoint(str(row.pk), stage='sealed', code='AUTHORITY_DENIED',
            outcomes=outcomes, metadata={'phase': 'source_model_context_unavailable', 'source_use': summary},
            collection_digest='a' * 64)

    def test_actual_history_preserves_sealed_cancelled_stale_old_and_redacted_rows(self):
        old = self.store.admit(self.intent())
        self.assertNotIn('source_use', old.evidence)
        row = self.source_row()
        self.assertEqual(ga.synthesis_history(self.state), [])
        self.store.cancel(str(row.pk), expected_revision=row.revision)
        self.assertEqual(ga.synthesis_history(self.state), [])
        stale = self.source_row()
        self.store.finish(str(stale.pk), stage='stale', code='PREVIEW_STALE')
        self.assertEqual(ga.synthesis_history(self.state), [])
        self.store.redact_profile()
        self.assertEqual(ga.synthesis_history(self.state), [])

    def test_mixed_publication_history_does_not_equate_collection_and_published_counts(self):
        from .test_portable_cabinet_store import StoreCase, candidate, result
        row = StoreCase.sealed(self, self.intent())
        outcomes = [{**value, **({'state': 'fresh', 'candidate_count': 1, 'code': 'OK'}
                    if value['category'] == 'films' else {})} for value in row.evidence['outcomes']]
        summary = [{'category': key, 'collected_count': int(key in ('books', 'films')),
                    'model_eligible_count': int(key == 'books')} for key in self.registry.categories]
        row = self.store.checkpoint(str(row.pk), stage='awaiting_preview', outcomes=outcomes,
            metadata={'source_use': summary}, collection_digest=row.collection_digest)
        row = self.store.reserve(str(row.pk), body_digest=row.body_digest, expected_revision=row.revision)
        row = self.store.record_result(str(row.pk), result())
        manifest = StoreCase.manifest(self)
        next(value for value in manifest['categories'] if value['category'] == 'films')['code'] = 'AUTHORITY_DENIED'
        self.store.publish(str(row.pk), manifest=manifest, expected_publication_revision=0,
                           allowed_candidates=[candidate()])
        self.assertEqual(ga.synthesis_history(self.state), [])

    def test_history_rejects_malformed_summaries_and_any_effect_in_unavailable_phase(self):
        import copy
        from shelf.models import SynthesisRun
        row = self.source_row()
        original = SynthesisRun.objects.filter(pk=row.pk).values().get()
        valid = original['evidence']['source_use']
        malformed = [None, valid[:-1], list(reversed(valid)), [valid[0], valid[0], *valid[2:]]]
        for changes in ({'extra': 'not-status'}, {'collected_count': True}, {'collected_count': -1},
                        {'collected_count': 101}, {'model_eligible_count': 2}, {'category': 'foreign'}):
            malformed.append([{**valid[0], **changes}, *valid[1:]])
        for summary in malformed:
            with self.subTest(summary=summary):
                attack = copy.deepcopy(original)
                attack['evidence']['source_use'] = summary
                self.fault('CAPABILITY_UNAVAILABLE', ga._broker_history_row, attack, self.state)
        for mutation in ({'attempts': 1, 'transport_request_id': uuid.uuid4()}, {'output': {'forged': True}},
                         {'body_digest': 'b' * 64}, {'sealed_run_digest': 'b' * 64},
                         {'portable_stage': 'awaiting_preview'}, {'portable_stage': 'published'}):
            with self.subTest(mutation=mutation):
                self.fault('CAPABILITY_UNAVAILABLE', ga._broker_history_row,
                           {**copy.deepcopy(original), **mutation}, self.state)
        for extra in ({'submitted_refs': {'signals': [], 'candidates': []}},
                      {'publication_revision': 1}, {'unknown_status': 1}, {'phase': 'arbitrary'}):
            attack = copy.deepcopy(original)
            attack['evidence'].update(extra)
            self.fault('CAPABILITY_UNAVAILABLE', ga._broker_history_row, attack, self.state)
        attack = copy.deepcopy(original)
        del attack['evidence']['source_use']
        self.fault('CAPABILITY_UNAVAILABLE', ga._broker_history_row, attack, self.state)


class IdleCurrentnessTests(RecoveryCase):
    def test_touch_retains_original_source_and_inference_context_bytes_and_expiry(self):
        with self.controlled_recovery_state('clear'):
            profile = self.bind()
            self.consent(profile)
            self.consent(profile, purpose='catalogue_lookup')
            views = [ga.current_view(self.authority, self.ref, profile_ref=profile,
                connection_ref=str(self.config.pk), purpose=purpose)
                for purpose in ('catalogue_lookup', 'inference')]
            self.clock.advance(6)
            session = self.authority.authorize(self.ref)
            touched = session.last_seen
            for view in views:
                self.assertNotEqual(self.view(profile).context['session_expires_at'],
                                    view.context['session_expires_at'])
                unchanged = ga.assert_current(view)
                self.assertEqual(unchanged, view)
                self.assertEqual((unchanged._context, unchanged._grants, unchanged.expires_at),
                                 (view._context, view._grants, view.expires_at))
            self.assertEqual(session.last_seen, touched)

    def test_original_caps_malformed_shortened_and_other_context_changes_refuse(self):
        from dataclasses import replace
        from shelf.disclosure import canonical_bytes
        with self.controlled_recovery_state('clear'):
            profile = self.bind()
            self.consent(profile)
            view = self.view(profile)
            def changed(**context):
                return replace(view, _context=canonical_bytes({**view.context, **context}))
            for altered in (replace(view, expires_at=self.clock.wall),
                    changed(session_expires_at=ga._stamp(self.clock.wall)),
                    changed(session_expires_at='malformed'),
                    changed(session_expires_at=ga._stamp(self.clock.wall+timedelta(hours=1))),
                    changed(adapter_version='changed-context'),
                    replace(view, profile_revision_digest='f'*64)):
                with self.subTest(context=altered is view):
                    self.fault('PREVIEW_STALE', ga.assert_current, altered)
            self.clock.advance(6)
            self.authority.authorize(self.ref)
            self.consent(profile, action='revoke', revision=1)
            self.fault('AUTHORITY_DENIED', ga.assert_current, view)

    def test_logout_still_refuses_original_authority(self):
        with self.controlled_recovery_state('clear'):
            profile = self.bind()
            self.consent(profile)
            view = self.view(profile)
            self.authority.logout(self.ref)
            self.fault('UNAUTHENTICATED', ga.assert_current, view)

    def test_actual_idle_expiry_still_refuses_original_authority(self):
        with self.controlled_recovery_state('clear'):
            profile = self.bind()
            self.consent(profile)
            view = self.view(profile)
            self.clock.advance(1800)
            self.fault('UNAUTHENTICATED', ga.assert_current, view)

    def test_actual_absolute_expiry_refuses_despite_regular_owner_activity(self):
        with self.controlled_recovery_state('clear'):
            profile = self.bind()
            self.consent(profile)
            view = self.view(profile)
            for _ in range(16):
                self.clock.advance(1700)
                self.authority.authorize(self.ref)
            self.clock.advance(1600)
            self.fault('UNAUTHENTICATED', ga.assert_current, view)
