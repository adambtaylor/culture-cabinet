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
