"""Actual durable source-check intent/custody with a no-network transport port."""
from types import SimpleNamespace
import uuid
from shelf.source_connection_validation import SourceConnectionValidation
from shelf.source_authority import configure_source
from shelf.models import PortableConnection,PortableSourceCheckAttempt,PortableJourneyCommand
from shelf.portable_authority import AuthorityFault
from .test_saved_credentials import SavedCredentialTests


class SourceCheckTests(SavedCredentialTests):
    def setUp(self):
        super().setUp()
        identifier=configure_source(self.authority,self.ref,confirmed=True,adapter='hardcover')
        self.source=PortableConnection.objects.get(pk=identifier)
        self.service.vault.replace(self.ref,self.source.pk,1,self.dummy.encode(),storage='persistent')
        self.sends=[];self.fail=False
        def send(operation,**kwargs):
            self.assertTrue(kwargs['revalidate']())
            self.assertEqual(kwargs['secret'],bytearray(self.dummy.encode()))
            self.assertEqual((kwargs['request_seconds'],kwargs['parent_seconds'],kwargs['response_bytes']),(5,6,262144))
            self.sends.append(operation.operation_digest)
            if self.fail:raise TimeoutError
            return 200,b'{"data":{"books":[]}}',()
        self.checks=SourceConnectionValidation(self.authority,transport=send)

    def check(self,**changes):
        values=dict(profile_ref=self.profile_ref,provider='hardcover',command_id=str(uuid.uuid4()),confirmed=True)
        values.update(changes)
        return self.checks.check(self.ref,**values)

    def test_explicit_intent_replay_and_valid_pass_reuse_send_once_without_inference_readiness(self):
        identifier=str(uuid.uuid4())
        first=self.check(command_id=identifier)
        self.assertEqual((first['state'],first['test_scope'],first['test_current']),('passed','minimal_public_read_only',True))
        self.assertEqual(self.check(command_id=identifier)['attempt_ref'],first['attempt_ref'])
        reused=self.check()
        self.assertEqual(reused['attempt_ref'],first['attempt_ref'])
        self.assertEqual(len(self.sends),1)
        self.source.refresh_from_db();self.assertEqual(self.source.validation_generation,0)
        self.assertEqual(PortableSourceCheckAttempt.objects.count(),1)
        self.assertEqual(PortableJourneyCommand.objects.filter(kind='source.check').count(),2)

    def test_unknown_never_resends_even_with_a_new_intent_and_key_is_retained(self):
        self.fail=True;identifier=str(uuid.uuid4())
        self.assertEqual(self.check(command_id=identifier)['state'],'unknown')
        self.assertEqual(self.check(command_id=identifier)['state'],'unknown')
        with self.assertRaises(AuthorityFault):self.check()
        self.assertEqual(len(self.sends),1)
        self.assertEqual(self.service.vault.resolve(self.ref,self.source.pk,1),self.dummy.encode())

    def test_replacement_session_expiry_and_process_object_loss_make_pass_historical(self):
        self.check()
        self.assertTrue(self.checks.current(self.ref,profile_ref=self.profile_ref,provider='hardcover')['test_current'])
        other=SourceConnectionValidation(self.authority,transport=lambda *a,**k:(_ for _ in ()).throw(AssertionError()))
        self.assertFalse(other.current(self.ref,profile_ref=self.profile_ref,provider='hardcover')['test_current'])
        self.service.vault.replace(self.ref,self.source.pk,1,(self.dummy+'2').encode(),storage='persistent')
        self.assertFalse(self.checks.current(self.ref,profile_ref=self.profile_ref,provider='hardcover')['test_current'])
        self.assertEqual(len(self.sends),1)

    def test_no_confirmation_or_wrong_provider_never_creates_intent_or_send(self):
        with self.assertRaises(AuthorityFault):self.check(confirmed=False)
        with self.assertRaises(AuthorityFault):self.check(provider='openai')
        self.assertEqual(self.sends,[])
        self.assertFalse(PortableSourceCheckAttempt.objects.exists())

    def test_native_check_form_reuses_current_receipt_and_never_claims_inference_readiness(self):
        from shelf.portable_journey import PortableJourney
        self.authority._guided_source_checks=self.checks
        journey=PortableJourney(self.authority)
        initial=journey.project(self.ref,profile_ref=self.profile_ref)
        configured=journey.configure(self.ref,profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],
            expected_revision=0,command_id=str(uuid.uuid4()),provider='hardcover',confirmed=True)
        fields=dict(profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],
            expected_revision=configured['revision'],command_id=str(uuid.uuid4()),confirmed='yes')
        first=self.post('/cabinet/journey/providers/hardcover/test',fields)
        self.assertEqual(first.status_code,303)
        self.assertEqual(self.post('/cabinet/journey/providers/hardcover/test',fields).status_code,303)
        self.assertEqual(self.post('/cabinet/journey/providers/hardcover/test',
            {**fields,'command_id':str(uuid.uuid4())}).status_code,303)
        self.assertEqual(len(self.sends),1)
        page=self.http.get(first['Location'],**self.headers)
        self.assertEqual(page.status_code,200)
        self.assertContains(page,'Connection check passed')
        self.source.refresh_from_db();self.assertEqual(self.source.validation_generation,0)
        self.assertEqual(self.post('/cabinet/journey/providers/hardcover/test',
            {**fields,'expected_revision':999,'command_id':str(uuid.uuid4())}).status_code,409)
        self.assertEqual(len(self.sends),1)

    def test_tmdb_partial_persistent_save_keeps_acknowledged_child_and_never_resends_unknown(self):
        from shelf.portable_journey import PortableJourney
        from shelf.models import SavedCredentialBinding
        from .test_saved_credentials import StoreDouble
        stores={}
        def request(value):
            key=value['binding']['connection_id_or_null']
            if key not in stores:stores[key]=StoreDouble()
            return stores[key].request(value)
        self.saved.request=request
        journey=PortableJourney(self.authority)
        initial=journey.project(self.ref,profile_ref=self.profile_ref)
        configured=journey.configure(self.ref,profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],
            expected_revision=0,command_id=str(uuid.uuid4()),provider='tmdb',confirmed=True)
        tv=PortableConnection.objects.get(pk=configured['connection_refs'][1])
        stores[str(tv.pk)]=StoreDouble();stores[str(tv.pk)].lose_ack=True
        fields=dict(profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],
            expected_revision=configured['revision'],command_id=str(uuid.uuid4()),provider='tmdb',
            secret=self.dummy,storage='persistent')
        result=journey.save_provider(self.ref,**fields)
        self.assertEqual(result['code'],'SAVE_UNKNOWN')
        self.assertEqual(len(result['bindings']),1)
        self.assertEqual(SavedCredentialBinding.objects.get(pk=result['bindings'][0]['connection_ref']).state,'ACTIVE')
        self.assertEqual(SavedCredentialBinding.objects.get(pk=tv.pk).state,'PENDING_WRITE')
        before={key:len(store.calls) for key,store in stores.items()}
        replay=journey.save_provider(self.ref,**{**fields,'secret':'DIFFERENT-IGNORED-ON-REPLAY'})
        self.assertEqual(replay,result)
        self.assertEqual({key:len(store.calls) for key,store in stores.items()},before)
        self.assertNotIn(self.dummy,str(list(PortableJourneyCommand.objects.values())))
        self.assertTrue(all(row.validation_generation==0 for row in PortableConnection.objects.filter(
            pk__in=configured['connection_refs'])))
        self.assertEqual(self.sends,[])
