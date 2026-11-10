"""Changed native saved-key callers with dummy bytes and a bounded store double."""
from copy import deepcopy
from unittest.mock import patch
import uuid

from shelf.models import SavedCredentialBinding
from shelf.saved_credentials import SavedCredentials, VERSION
from .test_portable_feedback_views import NativeFeedbackCase
from .test_portable_authority import PASSWORD


class StoreDouble:
    def __init__(self):
        self.record = self.revision = None
        self.operations, self.calls = {}, []
        self.locked = self.lose_ack = False

    def request(self, value):
        self.calls.append(deepcopy(value))
        reply = dict(version=VERSION, operation_id=value['operation_id'], status='AVAILABLE',
                     revision=None, record=None)
        if self.locked:
            return {**reply, 'status': 'LOCKED'}
        operation = value['operation']
        if operation == 'availability':
            return {**reply, 'status': 'AVAILABLE' if self.record else 'MISSING'}
        if operation == 'reconcile':
            return deepcopy(self.operations.get(value['operation_id'], {**reply, 'status': 'UNKNOWN'}))
        if value['expected_revision'] != self.revision:
            return {**reply, 'status': 'CONFLICT'}
        if operation == 'read':
            return {**reply, 'revision': self.revision, 'record': deepcopy(self.record)}
        self.revision = str(uuid.uuid4())
        if operation == 'put':
            self.record = deepcopy(value['record_if_put'])
        elif operation == 'delete':
            self.record = None
            reply['status'] = 'REMOVED'
        result = {**reply, 'revision': self.revision}
        self.operations[value['operation_id']] = result
        if self.lose_ack:
            self.lose_ack = False
            return {**reply, 'status': 'UNKNOWN'}
        return deepcopy(result)


class SavedCredentialTests(NativeFeedbackCase):
    def setUp(self):
        super().setUp()
        self.enterContext(self.settings(PORTABLE_CONNECTIONS=self.service))
        self.store = StoreDouble()
        self.saved = SavedCredentials(self.authority, self.service.vault, request=self.store.request)
        self.dummy = 'DUMMY-NOT-A-PROVIDER-KEY-persistent-01'

    def save(self):
        return self.service.vault.replace(self.ref, self.config.pk, 1,
                                           self.dummy.encode(), storage='persistent')

    def test_actual_native_persistent_form_and_safe_status(self):
        response = self.post('/setup/form/credential', dict(connection_ref=str(self.config.pk),
            revision='1', secret=self.dummy, storage='persistent'))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.http.session['_portable_notice'], 'KEY_SAVED')
        status = self.service.status(self.ref, self.config.pk, 1)
        self.assertTrue(status['credential_saved'])
        self.assertTrue(status['credential_available'])
        self.assertTrue(status['credential_configured'])
        self.assertFalse(status['connection_ready'])
        self.assertNotIn(self.dummy, str(status))
        self.assertNotIn(self.dummy, str(list(SavedCredentialBinding.objects.values())))
        self.assertEqual(self.store.record['schema'], 'cabinet-credential/v1')

    def test_passive_status_never_decrypts_but_explicit_consumer_hydrates(self):
        self.save()
        self.service.vault._leases.clear()
        self.store.calls.clear()
        status = self.service.status(self.ref, self.config.pk, 1)
        self.assertTrue(status['credential_available'])
        self.assertFalse(status['credential_configured'])
        self.assertFalse(any(c['operation'] == 'read' for c in self.store.calls))
        self.assertEqual(self.service.vault.resolve(self.ref, self.config.pk, 1), self.dummy.encode())
        self.assertEqual(self.store.calls[-1]['operation'], 'read')

    def test_new_login_resolves_saved_key_without_second_entry(self):
        self.save()
        self.authority.logout(self.ref)
        ref = self.authority.login(PASSWORD)
        self.saved.hydrate_owner(ref)
        self.assertEqual(self.service.vault.resolve(ref, self.config.pk, 1), self.dummy.encode())
        self.assertEqual(sum(c['operation'] == 'put' for c in self.store.calls), 1)

    def test_locked_collection_revokes_ram_and_cannot_dispatch(self):
        self.save()
        self.store.locked = True
        self.assertEqual(self.saved.describe(self.ref, self.config.pk, 1)['credential_store_state'], 'locked')
        self.assertFalse(self.service.vault._leases)
        self.fault('CREDENTIAL_REQUIRED', self.service.vault.resolve, self.ref, self.config.pk, 1)

    def test_lost_put_ack_reconciles_exact_operation_without_replacement(self):
        self.store.lose_ack = True
        self.fault('CREDENTIAL_STORE_UNKNOWN', self.save)
        pending = SavedCredentialBinding.objects.get()
        self.assertEqual(pending.state, 'PENDING_WRITE')
        self.assertFalse(self.service.vault._leases)
        self.assertTrue(self.saved.reconcile(self.ref, self.config.pk, 1))
        self.assertEqual(self.store.calls[-1]['operation_id'], str(pending.operation_id))
        self.assertIsNone(self.store.calls[-1]['expected_revision'])
        self.assertEqual(sum(c['operation'] == 'put' for c in self.store.calls), 1)

    def test_lost_delete_ack_disables_key_then_retains_cas_tombstone(self):
        self.save()
        self.store.lose_ack = True
        removed = self.service.vault.remove(self.ref, self.config.pk, 1)
        self.assertTrue(removed['removal_pending'])
        self.fault('CREDENTIAL_REQUIRED', self.service.vault.resolve, self.ref, self.config.pk, 1)
        self.assertTrue(self.saved.reconcile(self.ref, self.config.pk, 1))
        tombstone = SavedCredentialBinding.objects.get()
        self.assertTrue(tombstone.removed)
        self.assertFalse(self.saved.describe(self.ref, self.config.pk, 1)['credential_saved'])
        self.save()
        put = next(c for c in reversed(self.store.calls) if c['operation'] == 'put')
        self.assertEqual(put['expected_revision'], tombstone.store_revision)

    def test_wrong_owner_or_authenticated_record_cannot_supply_bytes(self):
        self.save()
        self.store.record['binding']['owner_revision'] += 1
        self.fault('CREDENTIAL_REQUIRED', self.service.vault.resolve, self.ref, self.config.pk, 1)
        self.assertFalse(self.service.vault._leases)

    def test_ram_only_replacement_never_resurrects_previous_saved_bytes(self):
        self.save()
        self.service.vault.replace(self.ref, self.config.pk, 1, b'DUMMY-RAM-ONLY')
        self.assertEqual(self.service.vault.resolve(self.ref, self.config.pk, 1), b'DUMMY-RAM-ONLY')
        self.authority.logout(self.ref)
        ref = self.authority.login(PASSWORD)
        self.fault('CREDENTIAL_REQUIRED', self.service.vault.resolve, ref, self.config.pk, 1)

    def test_remove_resolves_unknown_put_before_issuing_delete(self):
        self.store.lose_ack = True
        self.fault('CREDENTIAL_STORE_UNKNOWN', self.save)
        result = self.service.vault.remove(self.ref, self.config.pk, 1)
        self.assertTrue(result['removed'])
        self.assertEqual([c['operation'] for c in self.store.calls], ['put', 'reconcile', 'delete'])
        self.assertTrue(SavedCredentialBinding.objects.get().removed)

    def test_pending_disable_removal_can_reconcile_current_revision(self):
        self.save()
        self.store.lose_ack = True
        result = self.service.vault.remove(self.ref, self.config.pk, 1, disable=True)
        self.assertTrue(result['removal_pending'])
        self.assertEqual(result['revision'], 2)
        self.assertTrue(self.saved.reconcile(self.ref, self.config.pk, 2))
        self.assertTrue(SavedCredentialBinding.objects.get().removed)
