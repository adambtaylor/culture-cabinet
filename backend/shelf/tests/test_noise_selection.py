"""Bounded local variety on the already validated pool; no provider calls."""
from copy import deepcopy
from django.test import SimpleTestCase
from shelf.disclosure import canonical_bytes
from shelf.enhance_selection import GroundedSelector,RunSelectionPolicy,selection_seed
from .test_enhance_selection import GroundedSelectionTests


class NoiseSelectionTests(SimpleTestCase):
    def material(self,count=8):
        submitted,output,outcomes=GroundedSelectionTests().material()
        card=submitted['candidates'][0];assessment=output['candidate_assessments'][0]
        submitted['candidates']=[dict(card,key='source:'+str(index),provider='provider'+str(index%3)) for index in range(count)]
        output['candidate_assessments']=[dict(assessment,candidate_key=row['key']) for row in submitted['candidates']]
        return submitted,output,outcomes

    def select(self,noise,submitted,output,outcomes,**kwargs):
        policy=RunSelectionPolicy(noise);report=[]
        seed=selection_seed(policy,'fixed-run','a'*64,'b'*64)
        manifest=GroundedSelector().build(output,submitted,outcomes,selection_policy=policy,
            selection_seed=seed,selection_report=report,**kwargs)
        return manifest,report

    def test_precise_is_identical_experimental_actually_varies_deterministically_and_inputs_stay_exact(self):
        submitted,output,outcomes=self.material();before=canonical_bytes(submitted)
        baseline=GroundedSelector().build(output,submitted,outcomes)
        precise,report=self.select(0,submitted,output,outcomes)
        self.assertEqual(precise,baseline);self.assertEqual(report[0]['requested'],0)
        experimental,report=self.select(100,submitted,output,outcomes)
        self.assertNotEqual(experimental,baseline)
        self.assertEqual(experimental['categories'][0]['cards'][0],baseline['categories'][0]['cards'][0])
        self.assertEqual((report[0]['requested'],report[0]['used']),(4,4))
        submitted['candidates'].reverse();output['candidate_assessments'].reverse()
        self.assertEqual(self.select(100,submitted,output,outcomes),(experimental,report))
        submitted['candidates'].reverse()
        self.assertEqual(canonical_bytes(submitted),before)
        self.assertEqual(len({row['key'] for row in experimental['categories'][0]['cards']}),8)

    def test_uncertain_tail_rejects_dislikes_hard_exclusions_and_small_pool_never_gain_experimental_authority(self):
        submitted,output,outcomes=self.material()
        submitted['profile']['signals']=[dict(key='dislike',medium='books',basis='typed_feedback',
            value={'signal':'interest','scope':'item','value':{'value':'no','basis':'anticipated'}})]
        from .test_inference_contract import graph
        output['associations']=graph(['dislike'])
        output['candidate_assessments'][0]['signal_keys']=['dislike']
        output['candidate_assessments'][1]['decision']='uncertain'
        output['candidate_assessments'][2]['decision']='reject'
        snapshot={'hard_constraints':[],'seen_keys':['source:3'],'hard_exclusions':['source:4']}
        baseline=GroundedSelector().build(output,submitted,outcomes,snapshot=snapshot)
        varied,report=self.select(100,submitted,output,outcomes,snapshot=snapshot)
        self.assertNotIn('source:0',report[0]['exploratory_keys'])
        self.assertTrue(set(report[0]['exploratory_keys']).isdisjoint({'source:1','source:2','source:3','source:4'}))
        self.assertEqual(varied['categories'][0]['cards'][-1],baseline['categories'][0]['cards'][-1])
        for count in (0,1,2):
            values=self.material(count)
            result,report=self.select(100,*values)
            self.assertEqual(report[0]['used'],0)
            self.assertTrue(report[0]['reason'])
            self.assertEqual(result,GroundedSelector().build(values[1],values[0],values[2]))

    def test_capacity_and_uncertain_tail_stay_bounded_and_invalid_policy_refuses(self):
        from shelf.enhance_contract import EnhanceFault
        values=self.material(30);varied,report=self.select(100,*values)
        self.assertEqual(len(varied['categories'][0]['cards']),20)
        self.assertEqual(report[0]['requested'],8)
        self.assertEqual(report[0]['used'],8)
        for value in (True,-1,101,'25',25.0):
            with self.assertRaises(EnhanceFault):RunSelectionPolicy(value)
        with self.assertRaises(EnhanceFault):RunSelectionPolicy(25,version='legacy-ranked/v1')


    def test_experimental_replacements_keep_actual_local_radius_bands_and_prefer_weaker_positive_evidence(self):
        from .test_local_event_ranking import LocalEventRankingTests
        from .test_inference_contract import graph
        from shelf.local_event_ranking import rank_key
        from shelf.enhance_contract import CategoryOutcome
        submitted,output,_=self.material()
        context=LocalEventRankingTests().context()
        for index,card in enumerate(submitted['candidates']):
            card.update(category='event',medium='event')
            latitude='0.01' if index<4 else '1'
            card['facts']=[dict(id=card['key']+':'+key,property=key,value=value,observation_ref='fixture-reference')
                for key,value in (('venue.latitude',latitude),('venue.longitude','0'))]
        outcomes=(CategoryOutcome('event','fresh',8),)
        baseline=GroundedSelector().build(output,submitted,outcomes,local_context=context)
        varied,report=self.select(100,submitted,output,outcomes,local_context=context)
        self.assertGreater(report[0]['used'],0)
        self.assertEqual([rank_key(context,card)[0] for card in varied['categories'][0]['cards']],
                         [rank_key(context,card)[0] for card in baseline['categories'][0]['cards']])
        submitted['profile']['signals']=[dict(key='favourite',medium='event',basis='typed_feedback',
            value={'signal':'saved','scope':'item','value':{'value':True}})]
        output['associations']=graph(['favourite'])
        output['candidate_assessments'][0]['signal_keys']=['favourite']
        varied,report=self.select(100,submitted,output,outcomes,local_context=context)
        self.assertEqual(varied['categories'][0]['cards'][0]['key'],'source:0')
        self.assertTrue(all(key!='source:0' for key in report[0]['exploratory_keys']))


    def test_provider_diversity_does_not_penalize_a_neutral_pick_for_an_earlier_stronger_favourite(self):
        from .test_inference_contract import graph
        submitted,output,outcomes=self.material(5)
        for index,provider in enumerate(('alpha','beta','alpha','beta','gamma')):
            submitted['candidates'][index]['provider']=provider
        submitted['profile']['signals']=[
            dict(key='favourite',medium='books',basis='typed_feedback',
                 value={'signal':'saved','scope':'item','value':{'value':True}}),
            dict(key='like',medium='books',basis='typed_feedback',
                 value={'signal':'interest','scope':'item','value':{'value':'yes','basis':'anticipated'}})]
        output['associations']=graph(['favourite','like'])
        output['candidate_assessments'][0]['signal_keys']=['favourite']
        output['candidate_assessments'][1]['signal_keys']=['like']
        varied,report=self.select(100,submitted,output,outcomes)
        # These equal neutral candidates have no prior selection in their own
        # affinity/local-rank tier. The fixed seed chooses source:2; an earlier
        # favourite from alpha must not lend alpha a diversity penalty here.
        self.assertEqual(report[0]['exploratory_keys'][0],'source:2')
        self.assertEqual(varied['categories'][0]['cards'][0]['key'],'source:0')


from .test_portable_enhance_service import ServiceCase


class NoiseAdmissionTests(ServiceCase):
    def test_actual_admission_persists_only_successful_noise_and_changed_replay_conflicts_without_outbound_change(self):
        import uuid
        from shelf.models import PortableJourneyState,SynthesisRun
        key,refresh=str(uuid.uuid4()),str(uuid.uuid4())
        first=self.start_run(noise=0,client_key=key,refresh_nonce=refresh)
        self.assertEqual(first['selection_policy'],{'version':'selection-policy/v1','noise':0})
        self.assertEqual(PortableJourneyState.objects.get().next_noise,0)
        replay=self.start_run(noise=0,client_key=key,refresh_nonce=refresh)
        self.assertEqual(replay['run_ref'],first['run_ref'])
        self.fault('IDEMPOTENCY_CONFLICT',self.start_run,noise=100,client_key=key,refresh_nonce=refresh)
        self.assertEqual(PortableJourneyState.objects.get().next_noise,0)
        catalogue0=self.enhance.preview(self.ref,run_ref=first['run_ref'])['disclosure']['body']
        inferred=self.confirm_run(first)
        personal0=self.enhance.preview(self.ref,run_ref=inferred['run_ref'])['disclosure']['body']
        self.enhance.cancel(self.ref,run_ref=inferred['run_ref'],expected_revision=inferred['revision'])
        second=self.start_run(noise=100)
        self.assertEqual(PortableJourneyState.objects.get().next_noise,100)
        catalogue100=self.enhance.preview(self.ref,run_ref=second['run_ref'])['disclosure']['body']
        inferred=self.confirm_run(second)
        personal100=self.enhance.preview(self.ref,run_ref=inferred['run_ref'])['disclosure']['body']
        self.assertEqual(catalogue0,catalogue100);self.assertEqual(personal0,personal100)
        self.assertEqual(self.transport.calls,0)
        self.enhance.cancel(self.ref,run_ref=inferred['run_ref'],expected_revision=inferred['revision'])
        default=self.start_run()
        self.assertEqual(default['selection_policy']['noise'],100)
        self.assertEqual(SynthesisRun.objects.count(),3)

    def test_real_selector_consumes_frozen_policy_seed_report_and_old_runs_remain_legacy(self):
        from shelf.models import SynthesisRun
        from shelf import grant_authority as ga
        self.enhance.selector=GroundedSelector()
        started=self.start_run(noise=100)
        inferred=self.confirm_run(started);published=self.confirm_run(inferred)
        self.assertTrue(published['published'])
        row=SynthesisRun.objects.get(pk=published['run_ref'])
        policy=RunSelectionPolicy(100)
        self.assertEqual(row.evidence['selection_policy_digest'],policy.digest)
        self.assertEqual(row.evidence['selection_seed'],selection_seed(policy,str(row.pk),
            row.portable_bindings['bindings']['snapshot_revision'],row.collection_digest))
        self.assertEqual(row.evidence['selection_report']['policy_digest'],policy.digest)
        self.assertEqual(published['selection_policy']['noise'],100)
        self.assertEqual(row.evidence['selection_report']['categories'][0]['used'],0)
        self.assertTrue(row.evidence['selection_report']['categories'][0]['reason'])
        from shelf.models import ProfileCabinetState
        state=ProfileCabinetState.objects.get(profile_ref=self.profile_ref)
        ga._broker_history_row(SynthesisRun.objects.values().get(pk=row.pk),state)
        legacy=self.store.admit(self.intent())
        self.assertNotIn('selection_policy',legacy.evidence)
        self.assertEqual(self.enhance._public(legacy)['selection_policy'],{'version':'legacy-ranked/v1','noise':None})

    def test_redacted_policy_run_remains_valid_closed_broker_history_without_old_content_or_send_authority(self):
        from shelf.models import SynthesisRun,ProfileCabinetState
        from shelf import grant_authority as ga
        started=self.start_run(noise=75)
        row=SynthesisRun.objects.get(pk=started['run_ref'])
        state=ProfileCabinetState.objects.get(profile_ref=self.profile_ref)
        ga._broker_history_row(SynthesisRun.objects.values().get(pk=row.pk),state)
        self.store.redact_profile(run_refs={row.pk})
        row.refresh_from_db();state.refresh_from_db()
        self.assertEqual(row.output,{})
        self.assertNotIn('selection_report',row.evidence)
        ga._broker_history_row(SynthesisRun.objects.values().get(pk=row.pk),state)
        public=self.enhance.status(self.ref,run_ref=str(row.pk))
        self.assertNotIn('confirm',public['actions'])
        self.assertEqual(public['code'],'PREVIEW_STALE')


from .test_portable_feedback_views import NativeFeedbackCase


class NativeNoiseNoninterferenceTests(NativeFeedbackCase):
    def test_real_current_profile_and_sole_source_personal_codecs_ignore_noise_admission_metadata(self):
        import uuid
        from shelf import profile_snapshot,inference_body,grant_authority as ga,disclosure
        from shelf.enhance_contract import EnhanceIntent
        from shelf.portable_taste_inputs import PortableTasteInputs
        from shelf.models import SynthesisRun,PortableSourceCheckAttempt
        from .test_inference_body import InferenceBodyTests
        classes=['raw_notes','free_text_entry','explicit_favourites','unresolved_personal_text']
        self.consent(self.profile_ref,classes=classes)
        self.consent(self.profile_ref,purpose='catalogue_lookup',classes=classes)
        self.assertEqual(self.post('/cabinet/taste',self.note_fields(text='An actual native fictional preference')).status_code,303)
        PortableTasteInputs(self.authority).mutate_favourite(self.ref,self.profile_ref,
            entry_ref=str(uuid.uuid4()),mutation_id=str(uuid.uuid4()),base_revision=0,
            category='book',title='An actual native fictional favourite')
        record=InferenceBodyTests.record(self)
        collection=self.disclosure.fixture_collection([record],
            {'observation:1':{'revision':7,'digest':'c'*64}},{'policy:1':{'revision':3}},expires_at=self.expires)
        store=self.enhance._store(self.ref,self.profile_ref,str(self.config.pk))
        def outgoing():
            current=store.current();snapshot=profile_snapshot.capture(current.view)
            source_view=ga.current_view(self.authority,self.ref,profile_ref=self.profile_ref,
                connection_ref=str(self.config.pk),purpose='catalogue_lookup')
            source_body,_=disclosure.catalogue_input(snapshot,source_view.grants,source_view.context,
                                                    self.enhance.registry.categories,now=self.clock.wall)
            personal=inference_body.serialize(snapshot,current.view.grants,current.view.context,collection,
                                              now=self.clock.wall).body
            return snapshot,source_body,personal
        baseline=outgoing()
        for noise in (0,100):
            row=store.admit(EnhanceIntent(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4()),store.current().bindings),
                            local_context=store.current().local_context,selection_policy=RunSelectionPolicy(noise))
            self.assertEqual(row.evidence['selection_policy']['noise'],noise)
            self.assertEqual(outgoing(),baseline)
        self.assertFalse(SynthesisRun.objects.filter(attempts=1).exists())
        self.assertFalse(PortableSourceCheckAttempt.objects.exists())


class SelectionRetentionTests(ServiceCase):
    def published_policy(self):
        from shelf.enhance_contract import CategoryOutcome
        from .test_portable_cabinet_store import candidate
        from shelf.disclosure import canonical_bytes
        policy=RunSelectionPolicy(100)
        row=self.store.admit(self.intent(),selection_policy=policy)
        cards=[dict(candidate(),key='fixture:books:'+str(index)) for index in range(3)]
        outcomes=[dict(category=key,state='fresh' if key=='books' else 'unavailable',
            candidate_count=3 if key=='books' else 0,code='OK' if key=='books' else 'CAPABILITY_UNAVAILABLE')
            for key in self.registry.categories]
        row=self.store.checkpoint(str(row.pk),stage='awaiting_preview',outcomes=outcomes,
            metadata={'phase':'inference','submitted_refs':{'signals':[],
                'candidates':[dict(key=card['key'],evidence_refs=['evidence-1']) for card in cards]}},
            collection_digest='c'*64,body_digest='b'*64,sealed_run_digest='d'*64)
        row=self.store.reserve(str(row.pk),body_digest=row.body_digest,expected_revision=row.revision)
        output=dict(schema='cabinet-inference-result/v1',associations=None,candidate_assessments=[dict(
            candidate_key=card['key'],decision='retain',signal_keys=[],evidence_refs=['evidence-1'],
            hypothesis='Synthetic retained card.') for card in cards])
        row=self.store.record_result(str(row.pk),canonical_bytes(output))
        policy,seed=self.store.freeze_selection(str(row.pk));reports=[]
        manifest=GroundedSelector().build(output,{'profile':{'signals':[]},'candidates':cards},
            tuple(CategoryOutcome(**value) for value in outcomes),selection_policy=policy,
            selection_seed=seed,selection_report=reports)
        report=dict(schema='selection-report/v1',policy_digest=policy.digest,seed=seed,categories=reports)
        self.store.publish(str(row.pk),manifest=manifest,expected_publication_revision=0,
                           allowed_candidates=cards,selection_report=report)
        row.refresh_from_db();return row

    def test_published_policy_redaction_erases_real_report_keys_and_preserves_original_digests_once(self):
        import json
        from shelf import grant_authority as ga
        from shelf.models import ProfileCabinetState,PortableCabinetManifest,_content_digest
        from shelf.recovery import semantic_check
        from django.db import connection
        row=self.published_policy();original=row.input_digest
        self.assertTrue(row.evidence['selection_report']['categories'][0]['exploratory_keys'])
        expected=_content_digest(json.dumps(row.output),json.dumps(row.evidence))
        self.store.redact_profile(run_refs={row.pk});row.refresh_from_db()
        self.assertEqual(row.evidence,{'selection_policy':{'version':'selection-policy/v1','noise':100}})
        self.assertEqual((row.output,row.input_digest,row.content_digest),({},original,expected))
        self.assertEqual(PortableCabinetManifest.objects.get().manifest,{})
        state=ProfileCabinetState.objects.get();before=(row.revision,state.policy_epoch,state.restriction_watermark)
        self.store.redact_profile(run_refs={row.pk});row.refresh_from_db();state.refresh_from_db()
        self.assertEqual((row.revision,state.policy_epoch,state.restriction_watermark),before)
        ga._broker_history_row(type(row).objects.values().get(pk=row.pk),state)
        semantic_check(connection.connection)
        self.enhance._memory[str(row.pk)]={'phase':'inference','session_digest':'stale','expires_at':self.expires}
        public=self.enhance.status(self.ref,run_ref=str(row.pk))
        self.assertTrue(set(public['actions']).isdisjoint({'preview','confirm','current'}))
        self.assertNotIn('selection_report',public);self.assertNotIn('selection_policy',public)
        self.assertEqual((self.catalogue.calls,self.transport.calls),(0,0))

    def test_redacted_original_attempt_reconcile_never_restores_receipt_body_or_resets_identity(self):
        import uuid
        from types import SimpleNamespace
        from shelf import grant_authority as ga
        from shelf.models import ProfileCabinetState
        from .test_portable_cabinet_store import result
        for outcome in ('completed','proven_no_send'):
            row=self.store.admit(self.intent(),selection_policy=RunSelectionPolicy(75))
            row=self.store.checkpoint(str(row.pk),stage='awaiting_preview',
                outcomes=[dict(category=key,state='unavailable',candidate_count=0,code='CAPABILITY_UNAVAILABLE')
                          for key in self.registry.categories],
                metadata={'phase':'inference','submitted_refs':{'signals':[],'candidates':[]}},
                collection_digest='c'*64,body_digest='b'*64,sealed_run_digest='d'*64)
            row=self.store.reserve(str(row.pk),body_digest=row.body_digest,expected_revision=row.revision)
            self.store.mark_unknown(str(row.pk));self.store.redact_profile(run_refs={row.pk});row.refresh_from_db()
            original=(row.attempts,row.transport_request_id,row.input_digest,row.content_digest,row.evidence)
            wrong=SimpleNamespace(verify=lambda *args: (str(uuid.uuid4()),outcome,result()))
            self.fault('AUTHORITY_DENIED',self.store.reconcile,str(row.pk),receipt_ref=str(uuid.uuid4()),reconciler=wrong)
            exact=SimpleNamespace(verify=lambda *args: (str(row.transport_request_id),outcome,result()))
            settled=self.store.reconcile(str(row.pk),receipt_ref=str(uuid.uuid4()),reconciler=exact)
            self.assertEqual((settled.attempts,settled.transport_request_id,settled.input_digest,
                              settled.content_digest,settled.evidence),original)
            self.assertEqual(settled.output,{})
            ga._broker_history_row(type(row).objects.values().get(pk=row.pk),ProfileCabinetState.objects.get())
            self.assertTrue(set(self.enhance._public(settled)['actions']).isdisjoint({'preview','confirm','current'}))

    def test_authorized_orm_cannot_mutate_policy_frozen_metadata_or_retired_content(self):
        from copy import deepcopy
        from datetime import timedelta
        from django.db import DatabaseError,transaction,connection
        from shelf import grant_authority as ga
        from shelf.models import SynthesisRun
        row=self.published_policy()
        def refused(**changes):
            before=SynthesisRun.objects.values().get(pk=row.pk)
            with self.assertRaises(DatabaseError),transaction.atomic(),ga._write_window():
                SynthesisRun.objects.filter(pk=row.pk).update(revision=before['revision']+1,**changes)
            self.assertEqual(SynthesisRun.objects.values().get(pk=row.pk),before)
        for noise in (0,True,1.5,'100',None):
            evidence=deepcopy(row.evidence);evidence['selection_policy']['noise']=noise
            refused(evidence=evidence)
        for key in ('selection_policy','selection_seed','selection_policy_digest','selection_report'):
            evidence=deepcopy(row.evidence);evidence.pop(key);refused(evidence=evidence)
        evidence=deepcopy(row.evidence);evidence['selection_policy']['extra']='no';refused(evidence=evidence)
        evidence=deepcopy(row.evidence);evidence['selection_report']['categories'][0]['reason']='changed';refused(evidence=evidence)
        refused(portable_expires_at=row.portable_expires_at+timedelta(seconds=1));refused(attempts=0)
        self.store.redact_profile(run_refs={row.pk});row.refresh_from_db()
        refused(output={'retired':'content'});refused(content_digest='a'*64)
        refused(content_redacted_at=None)
        refused(evidence={**row.evidence,'selection_seed':'a'*64})
        refused(evidence={**row.evidence,'selection_report':{'candidate_keys':['retired']}})
        refused(evidence={})
        # A NULL validator result is denial, not SQL's unknown truth escape.
        connection.connection.create_function('cabinet_selection_valid',7,lambda *args:None)
        try:refused(updated_at=self.authority.now())
        finally:
            from shelf.models import register_closed_sql_guards
            register_closed_sql_guards(connection.connection)

    def test_legacy_retirement_and_late_policy_or_seed_insertion_are_refused(self):
        from django.db import DatabaseError,transaction
        from shelf import grant_authority as ga
        from shelf.models import SynthesisRun,ProfileCabinetState
        row=self.store.admit(self.intent());original=row.input_digest
        for fields in ({'selection_policy':{'version':'selection-policy/v1','noise':25}},
                       {'selection_seed':'a'*64,'selection_policy_digest':'b'*64}):
            with self.assertRaises(DatabaseError),transaction.atomic(),ga._write_window():
                SynthesisRun.objects.filter(pk=row.pk).update(revision=row.revision+1,evidence={**row.evidence,**fields})
        self.store.redact_profile(run_refs={row.pk});row.refresh_from_db()
        self.assertEqual((row.evidence,row.output,row.input_digest),({},{},original))
        ga._broker_history_row(SynthesisRun.objects.values().get(pk=row.pk),ProfileCabinetState.objects.get())


    def test_valid_seed_cannot_freeze_before_validation_or_retired_report_return(self):
        from copy import deepcopy
        from django.db import DatabaseError,transaction
        from shelf import grant_authority as ga
        from shelf.models import SynthesisRun
        policy=RunSelectionPolicy(100)
        queued=self.store.admit(self.intent(),selection_policy=policy)
        premature=dict(queued.evidence,selection_policy_digest=policy.digest,selection_seed=selection_seed(
            policy,str(queued.pk),queued.portable_bindings['bindings']['snapshot_revision'],'c'*64))
        with self.assertRaises(DatabaseError),transaction.atomic(),ga._write_window():
            SynthesisRun.objects.filter(pk=queued.pk).update(revision=queued.revision+1,
                evidence=premature,collection_digest='c'*64)
        self.assertEqual(SynthesisRun.objects.get(pk=queued.pk).evidence,queued.evidence)
        row=self.published_policy();original=deepcopy(row.evidence)
        stripped={key:value for key,value in original.items() if key not in
            ('selection_seed','selection_policy_digest','selection_report')}
        with self.assertRaises(DatabaseError),transaction.atomic(),ga._write_window():
            SynthesisRun.objects.filter(pk=row.pk).update(revision=row.revision+1,evidence=stripped)
        self.store.redact_profile(run_refs={row.pk});row.refresh_from_db()
        revived={**row.evidence,**{key:original[key] for key in
            ('selection_seed','selection_policy_digest','selection_report')}}
        with self.assertRaises(DatabaseError),transaction.atomic(),ga._write_window():
            SynthesisRun.objects.filter(pk=row.pk).update(revision=row.revision+1,evidence=revived)
        self.assertEqual(SynthesisRun.objects.get(pk=row.pk).evidence,row.evidence)


from .test_grant_authority import RecoveryCase
from .test_portable_cabinet_store import StoreCase,IsolatedStateProvider


class SelectionMigrationTests(RecoveryCase):
    intent=StoreCase.intent

    def setUp(self):
        super().setUp()
        from shelf.enhance_contract import RegistryView
        from shelf.portable_cabinet_store import CabinetStore
        self.profile_ref=self.bind()
        self.consent(self.profile_ref);self.consent(self.profile_ref,purpose='catalogue_lookup')
        self.registry=RegistryView('1',('books','films','tv','music','events','outings'))
        self.provider=IsolatedStateProvider(self)
        self.store=CabinetStore.isolated_test(self.authority,self.ref,profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk),state_provider=self.provider)

    def test_upgrade_inventory_legacy_compatibility_and_missing_trigger_refuse(self):
        import sqlite3
        from contextlib import closing
        from types import SimpleNamespace
        from importlib import import_module
        from django.db import connection
        from shelf.models import register_closed_sql_guards
        from shelf.recovery import _effective_guards,RecoveryBlocked
        migration=import_module('shelf.migrations.0030_run_selection_retention')
        legacy=self.store.admit(self.intent())
        with closing(sqlite3.connect(':memory:')) as raw:
            connection.connection.backup(raw);register_closed_sql_guards(raw)
            editor=SimpleNamespace(connection=SimpleNamespace(connection=raw,vendor='sqlite',cursor=lambda:closing(raw.cursor())),execute=raw.execute)
            original=raw.execute('SELECT input_digest,evidence FROM shelf_synthesisrun').fetchone()
            _effective_guards(raw)
            migration.uninstall(None,editor)
            raw.execute("DELETE FROM django_migrations WHERE app='shelf' AND name='0030_run_selection_retention'")
            _effective_guards(raw)
            migration.install(None,editor)
            raw.execute("INSERT INTO django_migrations(app,name,applied) VALUES ('shelf','0030_run_selection_retention','2026-11-11T00:00:00Z')")
            _effective_guards(raw)
            self.assertEqual(raw.execute('SELECT input_digest,evidence FROM shelf_synthesisrun').fetchone(),original)
            raw.execute('DROP TRIGGER enhance_selection_update')
            with self.assertRaises(RecoveryBlocked):_effective_guards(raw)

    def test_policy_history_downgrade_refuses_without_changing_any_guard(self):
        import sqlite3
        from contextlib import closing
        from types import SimpleNamespace
        from importlib import import_module
        from django.db import connection
        from shelf.models import register_closed_sql_guards
        from shelf.recovery import _effective_guards
        migration=import_module('shelf.migrations.0030_run_selection_retention')
        self.store.admit(self.intent(),selection_policy=RunSelectionPolicy(25))
        with closing(sqlite3.connect(':memory:')) as raw:
            connection.connection.backup(raw);register_closed_sql_guards(raw)
            editor=SimpleNamespace(connection=SimpleNamespace(connection=raw,vendor='sqlite',cursor=lambda:closing(raw.cursor())),execute=raw.execute)
            before=raw.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
            with self.assertRaisesRegex(RuntimeError,'SELECTION_HISTORY_REVERSAL_REFUSED'):migration.uninstall(None,editor)
            self.assertEqual(raw.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall(),before)
            _effective_guards(raw)
