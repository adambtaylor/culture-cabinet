"""Synthetic credential stores + fixed transport + existing fixture checkpoints."""
import json
import pickle
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from shelf import credential_store as store
from shelf.acquisition_backend import Backend, PrivateSearch, _queue_id
from shelf.acquisition_contract import Blocked, ContractError, PublicRequest, parse_caps
from shelf.acquisition_pipeline import Pipeline, Snapshot, AdapterUnavailable, QueueRejected
from shelf.acquisition_transport import LoopbackTransport
from shelf.tests.fixtures_credentials import FakeTree
from shelf.tests.test_acquisition_contract import CAPS, INERT_NZB
from shelf.tests.test_acquisition_pipeline import FixtureCheckpoints, saved_job, evidence_for
from shelf.tests.test_acquisition_transport import FakeHTTP, KEY


def grant(provider, **changes):
    context=store._provider_context(provider)
    now=store.utc_now()
    values=dict(binding=store.Binding('SYNTHETIC','11','22',provider,context.purpose),
                entered_at=now,expires_at=now+timedelta(days=1),
                declared_provider_expires_at=now+timedelta(days=1),scopes=context.scopes)
    values.update(changes)
    return store.EntryMetadata(**values)


class BackendTests(TestCase):
    def setUp(self):
        self.http=FakeHTTP()
        self.grants={p:grant(p) for p in ('hydra','sab')}
        self.paths={}
        for provider in self.grants:
            tree=FakeTree(store,provider=provider)
            self.addCleanup(tree.temporary.cleanup)
            self.paths[provider]=tree.path
            receipt=store.commit_entry(tree.store,self.grants[provider],bytearray(KEY if provider=='hydra' else 'SYNTHETIC-SAB-ONLY-KEY','utf-8'))
            self.assertEqual(receipt.outcome,'stored')
            tree.store.close()
        self.opened=[]
        def opener(*,provider):
            self.opened.append(provider)
            return store._fixture_store(self.paths[provider],provider=provider)
        self.backend=Backend(hydra_grant=self.grants['hydra'],sab_grant=self.grants['sab'],
            _open_store=opener,_transport=LoopbackTransport(_connection_factory=self.http.factory))

    def test_namespaces_purposes_and_scope_separation(self):
        for provider in ('hardcover','tmdb','librarything','hydra','sab'):
            context=store._provider_context(provider)
            for other in ('hardcover','tmdb','librarything','hydra','sab'):
                if other != provider:
                    self.assertNotEqual(context.directory,store._provider_context(other).directory)
                    self.assertNotEqual(context.purpose,store._provider_context(other).purpose)
                    self.assertNotEqual(context.scopes,store._provider_context(other).scopes)
        self.backend.caps(actor_id=11,profile_id=22)
        self.assertEqual(self.opened,['hydra'])

    def test_wrong_actor_profile_scope_provider_expired_no_release_no_transport(self):
        correct=self.grants['hydra']
        for wrong in (replace(correct,scopes=store.SAB_SCOPES), self.grants['sab'],
                      replace(correct,expires_at=store.utc_now()-timedelta(seconds=1)),
                      replace(correct,expires_at=correct.entered_at+timedelta(days=91),
                              declared_provider_expires_at=correct.entered_at+timedelta(days=91)),None):
            self.backend._grants['hydra']=wrong
            with self.assertRaises(Blocked):
                self.backend.caps(actor_id=11,profile_id=22)
        self.backend._grants['hydra']=correct
        for actor,profile in ((12,22),(11,23),('11',22),(True,22)):
            with self.assertRaises(Blocked):
                self.backend.caps(actor_id=actor,profile_id=profile)
        self.assertEqual(self.opened,[])
        self.assertEqual(self.http.calls,[])

    def test_current_record_binding_and_expiry_rechecked_each_operation(self):
        self.backend.caps(actor_id=11,profile_id=22)
        self.http.calls.clear()
        with patch.object(store,'utc_now',return_value=self.grants['hydra'].expires_at):
            with self.assertRaises(Blocked):
                self.backend.caps(actor_id=11,profile_id=22)
        self.backend._grants['hydra']=replace(self.grants['hydra'],binding=replace(self.grants['hydra'].binding,account_id='OTHER'))
        with self.assertRaises(Blocked):
            self.backend.caps(actor_id=11,profile_id=22)
        self.assertEqual(self.http.calls,[])

    def test_record_wrong_provider_and_scope_rejected(self):
        path=self.paths['hydra']/'credential.json'
        original=path.read_bytes()
        for change in ('provider','scope','expiry'):
            body=json.loads(original)
            if change=='provider': body['binding']['provider']='sab'
            if change=='scope': body['scopes']=list(store.SAB_SCOPES)
            if change=='expiry': body['expires_at']=body['entered_at']
            path.write_text(json.dumps(body))
            with self.assertRaises(Blocked): self.backend.caps(actor_id=11,profile_id=22)
        self.assertEqual(self.http.calls,[])

    def test_secret_is_transient_zeroed_and_search_is_private(self):
        records=[]
        real=store.read_record
        def capture(handle):
            record=real(handle); records.append(record); return record
        self.http.payload=b'{"link":"http://synthetic.invalid/?apikey=SECRET"}'
        with patch.object(store,'read_record',side_effect=capture):
            result=self.backend.search(parse_caps(CAPS),'fiction','film',actor_id=11,profile_id=22)
        self.assertIsInstance(result,PrivateSearch)
        self.assertNotIn('SECRET',repr(result))
        self.assertNotIn(KEY,repr(self.backend))
        with self.assertRaises(TypeError): pickle.dumps(result)
        self.assertFalse(hasattr(result,'__dict__'))
        self.assertEqual(set(records[0].secret),{0})
        result.close()
        self.assertEqual(set(result._payload),{0})

    def pipeline(self, folder):
        job=saved_job(); snapshot=Snapshot.from_saved_job(job); evidence=evidence_for(snapshot)
        provider=self.backend.provider(actor_id=11,profile_id=22,snapshot=snapshot,
                                       selection=evidence.selection,nzb=INERT_NZB)
        return Pipeline(job,FixtureCheckpoints(Path(folder)/'checkpoints'),provider,Mock(),Mock()),evidence

    def test_pipeline_success_repeated_dispatch_uses_checkpoint_not_memory(self):
        self.http.payload=b'{"status":true,"nzo_ids":["SABnzbd_nzo_fixture"]}'
        with tempfile.TemporaryDirectory() as folder:
            pipeline,evidence=self.pipeline(folder)
            self.assertEqual(pipeline.dispatch(evidence).job.job_id,'SABnzbd_nzo_fixture')
            recreated,again=self.pipeline(folder)
            self.assertEqual(recreated.dispatch(again).state,'queued')
        self.assertEqual(len(self.http.calls),1)
        self.assertEqual(self.opened,['sab'])
        self.assertIn(b'SYNTHETIC-SAB-ONLY-KEY',self.http.calls[0][2])
        self.assertNotIn(KEY.encode(),self.http.calls[0][2])

    def test_pipeline_lost_reply_repeat_and_advance_never_resend(self):
        self.http.failure=TimeoutError('SECRET')
        with tempfile.TemporaryDirectory() as folder:
            pipeline,evidence=self.pipeline(folder)
            self.assertEqual(pipeline.dispatch(evidence).state,'outcome_unknown')
            recreated,again=self.pipeline(folder)
            self.assertEqual(recreated.dispatch(again).state,'outcome_unknown')
            self.assertEqual(recreated.advance().state,'outcome_unknown')
            self.assertEqual(recreated.cancel().state,'cancel_pending')
        self.assertEqual(len(self.http.calls),1)

    def test_saved_only_invalid_identity_and_unconfirmed_never_enqueue(self):
        with tempfile.TemporaryDirectory() as folder:
            pipeline,evidence=self.pipeline(folder)
            self.assertEqual(pipeline.checkpoint.state,'blocked')
            with self.assertRaises(ContractError): pipeline.dispatch(None)
            request=PublicRequest('addfile',selection=evidence.selection,request_id=pipeline.snapshot.request_id)
            with self.assertRaises(Blocked):
                self.backend.enqueue(pipeline.snapshot,request,INERT_NZB,actor_id=12,profile_id=22)
        self.assertEqual(self.opened,[])
        self.assertEqual(self.http.calls,[])

    def test_queue_receipt_requires_exact_truthful_single_job(self):
        self.assertEqual(_queue_id(b'{"status":true,"nzo_ids":["SABnzbd_nzo_fixture"]}'),'SABnzbd_nzo_fixture')
        with self.assertRaises(QueueRejected): _queue_id(b'{"status":false,"error":"SECRET"}')
        for body in (b'{"status":true,"nzb_ids":["SABnzbd_nzo_fixture"]}',b'{}',b'not-json',b'{"status":true}',b'{"status":true,"nzo_ids":[]}',
                     b'{"status":true,"nzo_ids":["invented"]}',
                     b'{"status":true,"nzo_ids":["SABnzbd_nzo_a","SABnzbd_nzo_b"]}',
                     b'{"status":false,"nzo_ids":["SABnzbd_nzo_a"]}',
                     b'{"status":true,"status":false,"error":"SECRET"}'):
            with self.assertRaises(AdapterUnavailable) as caught: _queue_id(body)
            self.assertNotIn('SECRET',str(caught.exception))

    def test_enqueue_denied_grant_never_opens_store(self):
        self.backend._grants['sab']=self.grants['hydra']
        with tempfile.TemporaryDirectory() as folder:
            pipeline,evidence=self.pipeline(folder)
            self.assertEqual(pipeline.dispatch(evidence).state,'outcome_unknown')
        self.assertEqual(self.opened,[])
        self.assertEqual(self.http.calls,[])

    def test_provider_methods_have_no_status_cancel_or_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            pipeline,evidence=self.pipeline(folder)
            for method,arg in ((pipeline.provider.lookup,pipeline.snapshot),
                               (pipeline.provider.poll,None),(pipeline.provider.cancel,None)):
                with self.assertRaises(AdapterUnavailable): method(arg)
        self.assertEqual(self.opened,[])
        self.assertEqual(self.http.connections,[])
