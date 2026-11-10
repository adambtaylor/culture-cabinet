"""Changed native saved-key callers with dummy bytes and a bounded store double."""
from copy import deepcopy
import json
from unittest.mock import patch
from types import SimpleNamespace
import uuid

from shelf.models import SavedCredentialBinding
from shelf.saved_credentials import SavedCredentials, VERSION
from shelf.saved_credentials import secret_service_request
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

    def native_tombstone_with_ram_replacement(self):
        fields = dict(connection_ref=str(self.config.pk), revision='1', secret=self.dummy)
        self.assertEqual(self.post('/setup/form/credential', {**fields, 'storage':'persistent'}).status_code,303)
        removal = dict(connection_ref=str(self.config.pk), revision='1')
        self.assertEqual(self.post('/setup/form/remove',removal).status_code,303)
        tombstone = SavedCredentialBinding.objects.get()
        self.assertTrue(tombstone.removed)
        self.assertEqual(self.post('/setup/form/credential',
            {**fields,'secret':'DUMMY-RAM-AFTER-DURABLE-REMOVAL'}).status_code,303)
        self.assertEqual(self.service.vault.resolve(self.ref,self.config.pk,1),
                         b'DUMMY-RAM-AFTER-DURABLE-REMOVAL')
        return removal,tombstone.store_revision

    def test_native_remove_tombstone_still_revokes_later_ram_replacement(self):
        fields,revision = self.native_tombstone_with_ram_replacement()
        self.assertEqual(self.post('/setup/form/remove',fields).status_code,303)
        self.fault('CREDENTIAL_REQUIRED',self.service.vault.resolve,self.ref,self.config.pk,1)
        self.assertFalse(self.service.vault._leases)
        self.assertEqual(SavedCredentialBinding.objects.get().store_revision,revision)
        self.assertEqual(sum(c['operation']=='delete' for c in self.store.calls),1)

    def test_json_disable_tombstone_revokes_ram_and_disables_connection(self):
        _,revision = self.native_tombstone_with_ram_replacement()
        response=self.http.post('/setup/connections/remove',json.dumps({
            'connection_ref':str(self.config.pk),'revision':1,'disable':True}),
            content_type='application/json',HTTP_X_CSRFTOKEN=self.csrf(),**self.headers)
        self.assertEqual(response.status_code,200)
        self.config.refresh_from_db()
        self.assertTrue(self.config.removed)
        self.assertEqual(self.config.config_revision,2)
        self.assertFalse(self.service.vault._leases)
        self.fault('REVISION_CONFLICT',self.service.vault.resolve,self.ref,self.config.pk,2)
        self.assertEqual(SavedCredentialBinding.objects.get().store_revision,revision)
        self.assertEqual(sum(c['operation']=='delete' for c in self.store.calls),1)

    def enable_delete_guarantee(self):
        self.saved.delete_guarantee = lambda:'direct-delete-locked/v1'

    def locked_delete(self):
        self.enable_delete_guarantee()
        self.save()
        self.store.locked = True
        fields=dict(connection_ref=str(self.config.pk),revision='1')
        self.assertEqual(self.post('/setup/form/remove',fields).status_code,303)
        saved=SavedCredentialBinding.objects.get()
        self.assertEqual(saved.delete_no_effect_proof,self.saved._delete_proof(saved))
        return fields,saved

    def test_explicit_remove_consumes_durable_locked_proof_after_new_login(self):
        fields,old=self.locked_delete()
        self.assertEqual(self.post('/setup/form/login',{'passphrase':PASSWORD}).status_code,303)
        self.ref=self.http.session['portable_session']
        # A new facade has no private retry state; the proof is in SQLite.
        self.saved=SavedCredentials(self.authority,self.service.vault,request=self.store.request,
            delete_guarantee=lambda:'direct-delete-locked/v1')
        self.store.locked=False
        self.assertEqual(self.post('/setup/form/remove',fields).status_code,303)
        target=SavedCredentialBinding.objects.get()
        self.assertTrue(target.removed)
        self.assertIsNone(target.delete_no_effect_proof)
        direct=[c for c in self.store.calls if c['operation']=='delete']
        self.assertEqual(len(direct),2)
        self.assertEqual(direct[0],direct[1])
        self.assertEqual(direct[1]['operation_id'],str(old.operation_id))
        self.assertEqual(direct[1]['expected_revision'],old.store_revision)

    def test_old_pending_without_proof_never_uses_later_locked_guarantee(self):
        self.save()
        self.store.locked=True
        self.service.vault.remove(self.ref,self.config.pk,1)
        self.assertIsNone(SavedCredentialBinding.objects.get().delete_no_effect_proof)
        self.enable_delete_guarantee()
        self.service.vault.remove(self.ref,self.config.pk,1)
        self.assertEqual(self.store.calls[-1]['operation'],'reconcile')
        self.assertIsNone(SavedCredentialBinding.objects.get().delete_no_effect_proof)

    def test_ambiguous_delete_reply_never_creates_no_effect_proof(self):
        self.enable_delete_guarantee()
        self.save()
        self.store.lose_ack=True
        self.service.vault.remove(self.ref,self.config.pk,1)
        saved=SavedCredentialBinding.objects.get()
        self.assertIsNone(saved.delete_no_effect_proof)
        self.assertFalse(saved.removed)
        self.service.vault.remove(self.ref,self.config.pk,1)
        self.assertTrue(SavedCredentialBinding.objects.get().removed)
        self.assertEqual(sum(c['operation']=='delete' for c in self.store.calls),1)

    def test_lost_caller_after_proof_consumption_cannot_reissue_mutation(self):
        _,old=self.locked_delete()
        self.store.locked=False
        original=self.saved._call
        def crash(operation,saved,**kwargs):
            if operation=='delete':
                self.assertIsNone(SavedCredentialBinding.objects.get().delete_no_effect_proof)
                raise RuntimeError('Owned fixture stops before IPC')
            return original(operation,saved,**kwargs)
        with patch.object(self.saved,'_call',side_effect=crash):
            with self.assertRaises(RuntimeError):self.service.vault.remove(self.ref,self.config.pk,1)
        saved=SavedCredentialBinding.objects.get()
        self.assertIsNone(saved.delete_no_effect_proof)
        self.assertEqual(saved.operation_id,old.operation_id)
        self.service.vault.remove(self.ref,self.config.pk,1)
        self.assertEqual(self.store.calls[-1]['operation'],'reconcile')
        self.assertIsNotNone(self.store.record)

    def test_changed_predecessor_invalidates_no_effect_proof(self):
        _,old=self.locked_delete()
        self.store.locked=False
        SavedCredentialBinding.objects.filter(pk=old.pk).update(store_revision=str(uuid.uuid4()))
        self.service.vault.remove(self.ref,self.config.pk,1)
        self.assertEqual(self.store.calls[-1]['operation'],'reconcile')
        self.assertEqual(sum(c['operation']=='delete' for c in self.store.calls),1)
        self.assertIsNotNone(self.store.record)

    def test_competing_remove_observes_spent_proof_before_first_ipc(self):
        _,old=self.locked_delete()
        self.store.locked=False
        original=self.saved._call
        competed=[]
        def interleave(operation,saved,**kwargs):
            if operation=='delete' and not competed:
                self.assertIsNone(SavedCredentialBinding.objects.get().delete_no_effect_proof)
                competed.append(self.service.vault.remove(self.ref,self.config.pk,1))
            return original(operation,saved,**kwargs)
        with patch.object(self.saved,'_call',side_effect=interleave):
            self.service.vault.remove(self.ref,self.config.pk,1)
        self.assertTrue(competed[0]['removal_pending'])
        self.assertEqual(sum(c['operation']=='delete' for c in self.store.calls),2)
        self.assertTrue(SavedCredentialBinding.objects.get().removed)

    def test_repeated_direct_locked_restores_same_no_effect_proof(self):
        fields,old=self.locked_delete()
        self.assertEqual(self.post('/setup/form/remove',fields).status_code,303)
        saved=SavedCredentialBinding.objects.get()
        self.assertEqual(saved.operation_id,old.operation_id)
        self.assertEqual(saved.delete_no_effect_proof,old.delete_no_effect_proof)
        self.assertIsNotNone(self.store.record)

    def test_malformed_proof_never_permits_direct_delete(self):
        _,old=self.locked_delete()
        SavedCredentialBinding.objects.filter(pk=old.pk).update(
            delete_no_effect_proof={**old.delete_no_effect_proof,'invented':True})
        self.store.locked=False
        self.service.vault.remove(self.ref,self.config.pk,1)
        self.assertEqual(self.store.calls[-1]['operation'],'reconcile')
        self.assertIsNotNone(self.store.record)

    def test_delete_guarantee_comes_only_from_current_trusted_packaged_adapter(self):
        self.saved.request=secret_service_request
        with patch('shelf.saved_credentials.packaged_adapter',return_value=SimpleNamespace(VERSION=VERSION)):
            self.assertFalse(self.saved._supports_delete_proof())
        with patch('shelf.saved_credentials.packaged_adapter',return_value=SimpleNamespace(
                VERSION=VERSION,DELETE_NO_EFFECT_GUARANTEE='direct-delete-locked/v1')):
            self.assertTrue(self.saved._supports_delete_proof())
        with patch('shelf.saved_credentials.packaged_adapter',return_value=SimpleNamespace(
                VERSION='unsupported',DELETE_NO_EFFECT_GUARANTEE='direct-delete-locked/v1')):
            self.assertFalse(self.saved._supports_delete_proof())
        self.saved.request=self.store.request
        self.assertFalse(self.saved._supports_delete_proof())

    def test_http_cannot_supply_a_delete_guarantee_or_proof(self):
        self.save()
        before=len(self.store.calls)
        response=self.http.post('/setup/connections/remove',json.dumps({
            'connection_ref':str(self.config.pk),'revision':1,'disable':False,
            'helper_guarantee':'direct-delete-locked/v1'}),content_type='application/json',
            HTTP_X_CSRFTOKEN=self.csrf(),**self.headers)
        self.assertEqual(response.status_code,422)
        self.assertEqual(len(self.store.calls),before)

    def test_normal_persistent_source_save_replace_remove_retains_zero_validation_generation(self):
        from shelf.source_authority import configure_source,_connection_metadata
        from shelf.models import PortableConnection
        connection=configure_source(self.authority,self.ref,confirmed=True,adapter='tmdb_movie')
        row=PortableConnection.objects.get(pk=connection)
        fields=dict(profile_ref=self.profile_ref,connection_ref=connection,revision='1',
                    secret=self.dummy,storage='persistent')
        self.assertEqual(self.post('/cabinet/source/tmdb/movie/credential',fields).status_code,303)
        row.refresh_from_db();self.assertEqual((row.credential_generation,row.validation_generation),(1,0))
        _connection_metadata(self.authority,self.ref,connection)
        self.assertEqual(self.post('/cabinet/source/tmdb/movie/credential',{**fields,'secret':self.dummy+'2'}).status_code,303)
        row.refresh_from_db();self.assertEqual((row.credential_generation,row.validation_generation),(2,0))
        self.assertEqual(self.service.vault.resolve(self.ref,row.pk,1),(self.dummy+'2').encode())
        self.service.vault.remove(self.ref,row.pk,1)
        row.refresh_from_db();self.assertEqual((row.credential_generation,row.validation_generation),(3,0))
        self.assertTrue(SavedCredentialBinding.objects.get(pk=row.pk).removed)
        self.assertEqual(self.fake.calls,1)  # Only preexisting neutral fixture.

    def test_model_saved_replacement_still_invalidates_model_validation_generation(self):
        self.config.refresh_from_db()
        before=self.config.validation_generation
        self.save();self.config.refresh_from_db()
        self.assertEqual(self.config.validation_generation,before+1)
        self.service.vault.remove(self.ref,self.config.pk,1);self.config.refresh_from_db()
        self.assertEqual(self.config.validation_generation,before+2)
