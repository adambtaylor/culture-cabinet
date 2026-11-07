from copy import deepcopy
from dataclasses import replace
import sqlite3
import uuid
from django.db import connection
from django.db.models import Count
from django.http import Http404
from shelf.feedback import (canonical, strict_json, FeedbackError, Conflict, Gone, mutate)
from shelf.portability import (import_legacy_fixture, import_portable, human_export, decode_package,
    encode_package, validate_package, legacy_to_package, document_counts, NATIVE_LABELS)
from shelf.models import FeedbackTarget, FeedbackEvent, FeedbackProjection, MutationReceipt, FeedbackSubject, RecoveryRestriction
from shelf.tests.synthetic_feedback import (FeedbackCase, legacy_fixture, binding_for_fixture,
    binding_for_package, fresh_destination)


class PortabilityTests(FeedbackCase):
    def import_fixture(self,fixture=None):
        fixture=fixture or legacy_fixture()
        binding=binding_for_fixture(self.profile,fixture)
        report=import_legacy_fixture(actor=self.actor,profile_id=self.profile.pk,mutation_id=str(uuid.uuid4()),fixture_bytes=canonical(fixture),binding=binding)
        return fixture,binding,report

    def test_F08_12_exact_native_states_labels_nulls_counts_and_provenance(self):
        fixture=legacy_fixture(); fixture['interactions'][0]['source_metadata']={'request_id':'fiction-request-1','undone':True,'before':None,'after':{'state':'rated','rating':1,'saved':False}}
        self.import_fixture(fixture)
        self.assertEqual(len(fixture['interactions']),12)
        self.assertEqual(FeedbackTarget.objects.count(),15)
        self.assertEqual(FeedbackEvent.objects.count(),38)
        self.assertEqual(FeedbackProjection.objects.count(),38)
        self.assertEqual(dict(FeedbackEvent.objects.values('signal').annotate(n=Count('id')).values_list('signal','n')),
            {'legacy_state':12,'saved':12,'legacy_rating':12,'seed_statement':2})
        ratings=list(FeedbackEvent.objects.filter(signal='legacy_rating'))
        self.assertEqual(sum(e.body is None for e in ratings),2)
        self.assertEqual([sum(e.body is not None and e.body['value']==n for e in ratings) for n in range(1,6)],[2,2,2,2,2])
        self.assertEqual(sum(p.value=={'value':True} for p in FeedbackProjection.objects.filter(signal='saved')),6)
        self.assertFalse(FeedbackEvent.objects.filter(signal='enjoyment').exists())
        for e in ratings:
            if e.body:
                self.assertEqual(e.body['basis'],'legacy_unknown'); self.assertEqual(e.body['labels'],NATIVE_LABELS)
        self.assertEqual(FeedbackEvent.objects.filter(source_metadata__undone=True).count(),3)
        self.assertFalse(FeedbackEvent.objects.filter(operation='reverse').exists())
        package=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        self.assertEqual(package['counts']['source_interaction_rows'],12)
        self.assertEqual(package['counts']['active_rating_projections'],10)

    def test_F08_10000_literal_manifest_and_all_formats_two_crossprofile_hops(self):
        fixture=legacy_fixture(10000); self.assertEqual(len(canonical(fixture))<64*1024*1024,True)
        template=sqlite3.connect(':memory:'); connection.connection.backup(template)
        self.addCleanup(template.close)
        self.import_fixture(fixture)
        self.assertEqual(FeedbackTarget.objects.count(),10003)
        self.assertEqual(FeedbackEvent.objects.count(),31666)
        self.assertEqual(FeedbackProjection.objects.count(),31666)
        ratings=list(FeedbackEvent.objects.filter(signal='legacy_rating').values_list('body',flat=True))
        self.assertEqual([sum(b is not None and b['value']==n for b in ratings) for n in range(1,6)],[1429,1429,1429,1429,1428])
        self.assertEqual(sum(b is None for b in ratings),2856)
        self.assertEqual(FeedbackEvent.objects.filter(signal='seed_statement').count(),1666)
        states=list(FeedbackEvent.objects.filter(signal='legacy_state').values_list('body',flat=True))
        self.assertEqual([sum(b['value']==s for b in states) for s in ('rated','unseen','skipped')],[7144,1428,1428])
        self.assertEqual(FeedbackEvent.objects.filter(signal='saved',body={'value':True}).count(),5000)
        original=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        self.assertLess(len(canonical(original)),64*1024*1024)
        for index,format in enumerate(('json','csv','md')):
            payload=encode_package(original,format)
            for hop in range(2):
                with fresh_destination(self,template,100+index*2+hop) as (profile,coordinator):
                    binding=binding_for_package(profile,original)
                    import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=str(uuid.uuid4()),package_bytes=payload,binding=binding)
                    self.assertEqual(FeedbackTarget.objects.count(),10003)
                    self.assertEqual(FeedbackEvent.objects.count(),31666)
                    self.assertEqual(FeedbackProjection.objects.count(),31666)
                    exported=decode_package(human_export(actor=self.actor,profile_id=profile.pk,format=format).content)
                    self.assertEqual(exported['counts']['source_interaction_rows'],10000)
                    self.assertEqual(exported['counts']['active_rating_projections'],7144)
                    stable=lambda p:{k:v for k,v in p.items() if k not in ('package_id','exported_at')}
                    # Array order is presentation only; identities and all typed
                    # fields/provenance/references/counts must survive exactly.
                    stable_original=stable(original); stable_exported=stable(exported)
                    for key in ('targets','records'): stable_original[key]=sorted(stable_original[key],key=lambda r:r['id']); stable_exported[key]=sorted(stable_exported[key],key=lambda r:r['id'])
                    self.assertEqual(stable_original,stable_exported)
                    payload=encode_package(exported,format)

    def test_F08_absent_rating_and_person_media_isolation(self):
        fixture=legacy_fixture(1); row=fixture['interactions'][0]; row['state']='unseen'; del row['rating']
        self.import_fixture(fixture)
        self.assertEqual(FeedbackEvent.objects.count(),2)
        self.assertEqual(FeedbackProjection.objects.count(),2)
        self.assertEqual(FeedbackTarget.objects.count(),4)
        foreign=deepcopy(fixture); foreign['interactions'][0]['person']='person-b'
        with self.assertRaises(FeedbackError): legacy_to_package(foreign,binding_for_fixture(self.profile,fixture))
        for change in ({'state':'unknown'},{'state':'rated','rating':None},{'state':'unseen','rating':1},{'saved':None}):
            bad=deepcopy(fixture); bad['interactions'][0].update(change)
            with self.assertRaises(FeedbackError): legacy_to_package(bad,binding_for_fixture(self.profile,bad))

    def test_F09_12_roundtrip_safe_codecs_explicit_null_and_metadata(self):
        template=sqlite3.connect(':memory:'); connection.connection.backup(template); self.addCleanup(template.close)
        fixture=legacy_fixture(); fixture['seeds'][0]['text']='=HYPERLINK("https://invalid")\nUnicode ☃ <script> [x](bad) ```'
        fixture['interactions'][0]['source_metadata']={'before':None,'after':{'state':'rated','rating':1},'undone':True,'request_id':'source-test-12'}
        self.import_fixture(fixture)
        original=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        for index,format in enumerate(('json','csv','md')):
            payload=encode_package(original,format)
            self.assertEqual(decode_package(payload),original)
            if format=='csv': self.assertFalse(any(line.startswith(b'=') for line in payload.splitlines()))
            if format=='md': self.assertIn(b'````json',payload)
            for hop in range(2):
                with fresh_destination(self,template,200+index*2+hop) as (profile,_):
                    import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=str(uuid.uuid4()),package_bytes=payload,binding=binding_for_package(profile,original))
                    out=decode_package(human_export(actor=self.actor,profile_id=profile.pk,format=format).content)
                    self.assertEqual(out['counts'],dict(targets=12,events=38,projections=38,source_interaction_rows=12,
                        rating_statements_by_basis={'legacy_unknown':10},active_rating_projections=10,action_statements=12,seeds=2,reversals=0,corrections=0,tombstones=0))
                    self.assertEqual({r['id']:r for r in original['records']},{r['id']:r for r in out['records']})
                    payload=encode_package(out,format)

    def test_F10_bad_last_record_and_authority_tombstones_atomic(self):
        fixture=legacy_fixture(); binding=binding_for_fixture(self.profile,fixture)
        package=legacy_to_package(fixture,binding); binding=binding_for_package(self.profile,package)
        baseline=(FeedbackTarget.objects.count(),FeedbackEvent.objects.count(),FeedbackProjection.objects.count(),MutationReceipt.objects.count(),RecoveryRestriction.objects.count())
        cases=[]
        bad=deepcopy(package); bad['records'][-1]['actor_id']=self.actor.pk; cases.append(bad)
        bad=deepcopy(package); bad['records'][-1]['value']['basis']='experienced'; cases.append(bad)
        bad=deepcopy(package); bad['records'].append(deepcopy(bad['records'][0])); cases.append(bad)
        bad=deepcopy(package); bad['subject_id']=str(uuid.uuid4()); cases.append(bad)
        bad=deepcopy(package); bad['restrictions']=[dict(kind='deny_view',profile_id=self.profile.pk)]; cases.append(bad)
        bad=deepcopy(package); bad['records'][-1]['reverses_id']=str(uuid.uuid4()); cases.append(bad)
        for bad in cases:
            with self.assertRaises(FeedbackError): import_portable(actor=self.actor,profile_id=self.profile.pk,mutation_id=str(uuid.uuid4()),package_bytes=canonical(bad),binding=binding)
            self.assertEqual(baseline,(FeedbackTarget.objects.count(),FeedbackEvent.objects.count(),FeedbackProjection.objects.count(),MutationReceipt.objects.count(),RecoveryRestriction.objects.count()))
            self.assertEqual((self.recovery.directory/'journal').read_bytes(),b'')
        with self.assertRaises(FeedbackError): import_portable(actor=self.actor,profile_id=self.profile.pk,mutation_id=str(uuid.uuid4()),package_bytes=b'{"schema_version":1,"schema_version":2}',binding=binding)

    def test_F02_import_discriminated_hash_binding_and_revoked_retry(self):
        fixture=legacy_fixture(); binding=binding_for_fixture(self.profile,fixture); mid=str(uuid.uuid4())
        kwargs=dict(actor=self.actor,profile_id=self.profile.pk,mutation_id=mid,fixture_bytes=canonical(fixture),binding=binding)
        import_legacy_fixture(**kwargs); import_legacy_fixture(**kwargs)
        self.assertEqual(FeedbackEvent.objects.count(),38)
        changed=deepcopy(fixture); changed['interactions'][0]['saved']=False
        with self.assertRaises(Conflict): import_legacy_fixture(**{**kwargs,'fixture_bytes':canonical(changed)})
        with self.assertRaises(Conflict): import_legacy_fixture(**{**kwargs,'binding':replace(binding,fictional_manifest_digest='0'*64)})
        package=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        with self.assertRaises(Conflict): import_portable(actor=self.actor,profile_id=self.profile.pk,mutation_id=mid,package_bytes=canonical(package),binding=binding_for_package(self.profile,package))
        self.grant.can_mutate=False; self.grant.save()
        with self.assertRaises(Http404): import_legacy_fixture(**kwargs)

    def test_F09_correction_reversal_and_erased_tombstone_five_event_roundtrip(self):
        template=sqlite3.connect(':memory:'); connection.connection.backup(template); self.addCleanup(template.close)
        fixture=legacy_fixture(1); package=legacy_to_package(fixture,binding_for_fixture(self.profile,fixture))
        a=next(r for r in package['records'] if r['signal']=='legacy_rating')
        b=deepcopy(a); b.update(id=str(uuid.uuid4()),revision=2,corrects_id=a['id']); b['value']['value']=2
        reverse=deepcopy(b); reverse.update(id=str(uuid.uuid4()),revision=3,operation='reverse',value=None,reverses_id=b['id'],corrects_id=None)
        saved=next(r for r in package['records'] if r['signal']=='saved')
        saved.update(erased=True,value=None,attribution=None,source_record_id=None,source_time=None,source_metadata=None,original_recorded_at=None)
        erased=deepcopy(saved); erased.update(id=str(uuid.uuid4()),revision=2,operation='erase')
        package['records']=[a,b,reverse,saved,erased]
        package['restrictions']=[dict(kind='feedback_erased',target_id=saved['target_id'],scope='item',signals=['saved'],through_revision=2)]
        package['counts']=dict(targets=1,events=5,projections=2,source_interaction_rows=1,
            rating_statements_by_basis={'legacy_unknown':2},active_rating_projections=1,action_statements=1,
            seeds=0,reversals=1,corrections=1,tombstones=1)
        validate_package(package)
        for index,format in enumerate(('json','csv','md')):
            payload=encode_package(package,format)
            for hop in range(2):
                with fresh_destination(self,template,300+index*2+hop) as (profile,coordinator):
                    mid=str(uuid.uuid4()); binding=binding_for_package(profile,package)
                    import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=mid,package_bytes=payload,binding=binding)
                    self.assertEqual(FeedbackEvent.objects.count(),5)
                    self.assertEqual(FeedbackProjection.objects.count(),2)
                    self.assertEqual(FeedbackProjection.objects.get(signal='legacy_rating').value['value'],1)
                    self.assertTrue(FeedbackProjection.objects.get(signal='saved').erased)
                    self.assertEqual(RecoveryRestriction.objects.count(),1)
                    self.assertFalse(MutationReceipt.objects.exclude(state='retired',payload_sha256=None).exists())
                    with self.assertRaises(Gone): import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=mid,package_bytes=payload,binding=binding)
                    out=decode_package(human_export(actor=self.actor,profile_id=profile.pk,format=format).content)
                    self.assertEqual(out['counts'],package['counts'])
                    self.assertEqual({r['id']:r for r in out['records']},{r['id']:r for r in package['records']})
                    payload=encode_package(out,format)

    def test_F08_same_media_ids_different_people_share_targets_not_statements(self):
        from shelf.models import Profile,Grant
        first=legacy_fixture(); self.import_fixture(first)
        second=legacy_fixture(person='person-b')
        partner=Profile.objects.create(kind='partner',label='Other fictional person',active=True)
        Grant.objects.create(user=self.actor,profile=partner,can_view=True,can_mutate=True,can_export=True)
        binding=binding_for_fixture(partner,second)
        import_legacy_fixture(actor=self.actor,profile_id=partner.pk,mutation_id=str(uuid.uuid4()),fixture_bytes=canonical(second),binding=binding)
        self.assertEqual(FeedbackTarget.objects.count(),15)
        self.assertEqual(FeedbackEvent.objects.filter(profile=self.profile).count(),38)
        self.assertEqual(FeedbackEvent.objects.filter(profile=partner).count(),38)
        self.assertNotEqual(FeedbackSubject.objects.get(profile=partner).subject_id,FeedbackSubject.objects.get(profile=self.profile).subject_id)

    def test_F10_parser_depth_utf8_duplicate_csv_and_truncated_markdown(self):
        fixture=legacy_fixture(1); package=legacy_to_package(fixture,binding_for_fixture(self.profile,fixture))
        binding=binding_for_package(self.profile,package)
        for data in (b'['*17+b'0'+b']'*17,b'\xff',b'{"a":NaN}',b'{"a":1,"a":2}'):
            with self.assertRaises(FeedbackError): import_portable(actor=self.actor,profile_id=self.profile.pk,mutation_id=str(uuid.uuid4()),package_bytes=data,binding=binding)
        csv=encode_package(package,'csv')
        duplicate=csv+csv.splitlines(keepends=True)[1]
        with self.assertRaises(FeedbackError): decode_package(duplicate)
        with self.assertRaises(FeedbackError): decode_package(encode_package(package,'md').rsplit(b'```',1)[0])
        with self.assertRaises(FeedbackError): strict_json(b' '*17,limit=16)
        self.assertEqual(FeedbackEvent.objects.count(),0)
        self.assertEqual(MutationReceipt.objects.count(),0)
        self.assertEqual((self.recovery.directory/'journal').read_bytes(),b'')


class SourcePolicyRepairTests(FeedbackCase):
    def ban(self,coordinator=None):
        from shelf.recovery import restrict_authority,trusted_operator
        coordinator=coordinator or self.recovery
        restrict_authority(actor=trusted_operator(coordinator,self.actor),recovery=coordinator,
            command=dict(mutation_id=str(uuid.uuid4()),base_sequence=coordinator.sequence,
                restrictions=[dict(kind='source_restrict',profile_id=None,target_id=None,
                    signal=None,scope=None,subject_id='legacy-fixture-v1',restriction_code='deny_source_content')]))

    def snapshot(self,coordinator=None):
        coordinator=coordinator or self.recovery
        return (tuple(connection.connection.iterdump()),(coordinator.directory/'journal').read_bytes(),
                deepcopy((coordinator.frames,coordinator.head_id,coordinator.sequence,
                          coordinator.length,coordinator.digest,coordinator.pending)))

    def denied(self,call,exception=FeedbackError,coordinator=None):
        before=self.snapshot(coordinator)
        with self.assertRaises(exception): call()
        self.assertEqual(before,self.snapshot(coordinator))

    def witness(self,result):
        # Separate approval captured from a witnessed local direct mutation before
        # any export/input parsing. Never attest imported rows or incoming origin.
        import hashlib
        from shelf.portability import iso
        e=FeedbackEvent.objects.get(pk=result.event_id)
        self.assertEqual((e.actor_id,e.origin,e.original_origin),(self.actor.pk,'direct','direct'))
        record=dict(id=str(e.pk),target_id=str(e.target_id),signal=e.signal,scope=e.scope,
            revision=e.revision,operation=e.operation,value=e.body,source_record_id=e.source_record_id,
            source_time=iso(e.source_time),source_metadata=e.source_metadata,original_origin=e.original_origin,
            attribution=e.attribution,original_recorded_at=iso(e.original_recorded_at),
            reverses_id=str(e.reverses_id) if e.reverses_id else None,
            corrects_id=str(e.corrects_id) if e.corrects_id else None,erased=e.erased)
        return dict(id=str(e.pk),sha256=hashlib.sha256(canonical(record)).hexdigest())

    def fixture_import(self,fixture=None,mid=None):
        fixture=fixture or legacy_fixture(1); mid=mid or str(uuid.uuid4())
        binding=binding_for_fixture(self.profile,fixture)
        import_legacy_fixture(actor=self.actor,profile_id=self.profile.pk,mutation_id=mid,
                              fixture_bytes=canonical(fixture),binding=binding)
        return fixture,binding,mid

    def test_source_policy_both_paths_fresh_profiles_old_receipts_and_stable_replay_atomic(self):
        from shelf.models import Profile,Grant
        fixture,binding,mid=self.fixture_import()
        original=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        portable_mid=str(uuid.uuid4()); portable_binding=binding_for_package(self.profile,original)
        import_portable(actor=self.actor,profile_id=self.profile.pk,mutation_id=portable_mid,
                        package_bytes=canonical(original),binding=portable_binding)
        self.ban()
        for retry in (mid,str(uuid.uuid4())):
            self.denied(lambda:import_legacy_fixture(actor=self.actor,profile_id=self.profile.pk,
                mutation_id=retry,fixture_bytes=canonical(fixture),binding=binding))
        for retry in (portable_mid,str(uuid.uuid4())):
            self.denied(lambda:import_portable(actor=self.actor,profile_id=self.profile.pk,
                mutation_id=retry,package_bytes=canonical(original),binding=portable_binding))
        partner=Profile.objects.create(kind='partner',label='Fictional partner',active=True)
        Grant.objects.create(user=self.actor,profile=partner,can_view=True,can_mutate=True,can_export=True)
        other=legacy_fixture(1,'person-b'); other_package=legacy_to_package(other,binding_for_fixture(partner,other))
        self.denied(lambda:import_legacy_fixture(actor=self.actor,profile_id=partner.pk,mutation_id=str(uuid.uuid4()),
            fixture_bytes=canonical(other),binding=binding_for_fixture(partner,other)))
        self.denied(lambda:import_portable(actor=self.actor,profile_id=partner.pk,mutation_id=str(uuid.uuid4()),
            package_bytes=canonical(other_package),binding=binding_for_package(partner,other_package)))
        # Authorization still precedes source policy, including old receipts.
        self.grant.can_mutate=False; self.grant.save()
        self.denied(lambda:import_legacy_fixture(actor=self.actor,profile_id=self.profile.pk,mutation_id=mid,
            fixture_bytes=canonical(fixture),binding=binding),Http404)

    def test_v2_three_codecs_two_stores_correction_reverse_clear_and_erase(self):
        from shelf.feedback import erase_feedback,EraseCommand,key_token
        fixture=legacy_fixture(1)
        source_package=legacy_to_package(fixture,binding_for_fixture(self.profile,fixture))
        for t in source_package['targets']: FeedbackTarget.objects.create(**t)
        template=sqlite3.connect(':memory:'); connection.connection.backup(template); self.addCleanup(template.close)
        self.fixture_import(fixture)
        saved=FeedbackEvent.objects.get(signal='saved')
        correction=mutate(actor=self.actor,profile_id=self.profile.pk,
            command=self.command('saved',{'value':False},target_id=str(saved.target_id),corrects_id=str(saved.pk)))
        approved=[self.witness(correction)]
        self.ban()
        stages=[]
        first=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        stages.append((first,tuple(approved)))
        self.assertEqual(first['counts'],dict(targets=1,events=4,projections=3,source_interaction_rows=0,
            rating_statements_by_basis={},active_rating_projections=0,action_statements=2,seeds=0,
            reversals=0,corrections=1,tombstones=0,source_redactions=3))
        self.assertEqual(next(r for r in first['records'] if r['id']==correction.event_id)['value'],{'value':False})
        reverse=mutate(actor=self.actor,profile_id=self.profile.pk,command=replace(self.command('saved',
            target_id=str(saved.target_id),operation='reverse',reverses_id=correction.event_id),value=None))
        approved.append(self.witness(reverse))
        second=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        stages.append((second,tuple(approved)))
        p=FeedbackProjection.objects.get(signal='saved')
        self.assertEqual((p.value,str(p.effective_event_id)),(None,str(saved.pk)))
        self.assertEqual((second['counts']['events'],second['counts']['reversals'],second['counts']['source_redactions']),(5,1,3))
        token=key_token(str(saved.target_id),'saved','item')
        erase_feedback(actor=self.actor,profile_id=self.profile.pk,recovery=self.recovery,
            command=EraseCommand(str(uuid.uuid4()),str(saved.target_id),'item',('saved',),
                {token:3},{token:reverse.event_id},'ERASE_FICTIONAL_FEEDBACK'))
        third=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        stages.append((third,tuple(approved)))
        self.assertEqual((third['counts']['events'],third['counts']['projections'],third['counts']['source_redactions'],third['counts']['tombstones']),(6,3,2,1))
        self.assertFalse(MutationReceipt.objects.exclude(state='retired',payload_sha256=None).exists())
        for stage,(package,approvals) in enumerate(stages):
            self.assertEqual(package['schema_version'],'cc.feedback.v2')
            for index,fmt in enumerate(('json','csv','md')):
                payload=encode_package(package,fmt)
                if fmt=='md': self.assertIn(b'Source content redacted',payload)
                for hop in range(2):
                    number=600+stage*10+index*2+hop
                    with fresh_destination(self,template,number) as (profile,coordinator):
                        self.ban(coordinator)
                        binding=replace(binding_for_package(profile,package),independent_record_digests=approvals)
                        mid=str(uuid.uuid4())
                        if stage==2:
                            from unittest.mock import patch
                            phases=[]; current_policy=coordinator.assert_import_source_policy
                            def policy(**kwargs):
                                phases.append(coordinator.pending is not None)
                                return current_policy(**kwargs)
                            with patch.object(coordinator,'assert_import_source_policy',side_effect=policy):
                                import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=mid,package_bytes=payload,binding=binding)
                            self.assertEqual(phases,[False,True])
                        else:
                            import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=mid,package_bytes=payload,binding=binding)
                        self.assertEqual((FeedbackEvent.objects.count(),FeedbackProjection.objects.count()),(4+stage,3))
                        out=human_export(actor=self.actor,profile_id=profile.pk,format=fmt)
                        self.assertEqual(out.filename,'culture-cabinet-fiction-v2.'+fmt)
                        exported=decode_package(out.content)
                        self.assertEqual(exported['counts'],package['counts'])
                        self.assertEqual({r['id']:r for r in exported['records']},{r['id']:r for r in package['records']})
                        if stage==0: self.assertEqual(FeedbackProjection.objects.get(signal='saved').value,{'value':False})
                        if stage==1:
                            p=FeedbackProjection.objects.get(signal='saved')
                            self.assertEqual((p.value,str(p.effective_event_id)),(None,str(saved.pk)))
                        if stage==2:
                            self.assertTrue(FeedbackProjection.objects.get(signal='saved').erased)
                            self.assertFalse(MutationReceipt.objects.exclude(state='retired',payload_sha256=None).exists())
                            self.denied(lambda:import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=mid,
                                package_bytes=payload,binding=binding),Gone,coordinator)
                            last=FeedbackEvent.objects.get(profile=profile,signal='saved',revision=4)
                            self.denied(lambda:mutate(actor=self.actor,profile_id=profile.pk,command=replace(self.command('saved',
                                target_id=str(saved.target_id)),base_revision=4,context_event_id=str(last.pk))),Gone,coordinator)
                        payload=out.content
        # With every source scope erased, flags disappear and v1 is sufficient.
        for signal in ('legacy_rating','legacy_state'):
            projection=FeedbackProjection.objects.get(profile=self.profile,signal=signal)
            last=FeedbackEvent.objects.get(profile=self.profile,signal=signal,revision=projection.revision)
            token=key_token(str(saved.target_id),signal,'item')
            erase_feedback(actor=self.actor,profile_id=self.profile.pk,recovery=self.recovery,
                command=EraseCommand(str(uuid.uuid4()),str(saved.target_id),'item',(signal,),
                    {token:projection.revision},{token:str(last.pk)},'ERASE_FICTIONAL_FEEDBACK'))
        fully_erased=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        self.assertEqual(fully_erased['schema_version'],'cc.feedback.v1')
        self.assertEqual((fully_erased['counts']['events'],fully_erased['counts']['tombstones']),(8,3))
        self.assertTrue(all(r['erased'] and 'source_redacted' not in r for r in fully_erased['records']))
        for fmt in ('json','csv','md'):
            self.assertEqual(decode_package(encode_package(fully_erased,fmt)),fully_erased)
            if fmt=='md':
                self.assertNotIn(b'Explicitly cleared',encode_package(fully_erased,fmt))
                self.assertNotIn(b'Source content redacted',encode_package(fully_erased,fmt))

    def test_masks_missing_ban_approval_forged_direct_bad_last_and_changed_human_atomic(self):
        fixture=legacy_fixture(1); initial=legacy_to_package(fixture,binding_for_fixture(self.profile,fixture))
        for t in initial['targets']: FeedbackTarget.objects.create(**t)
        template=sqlite3.connect(':memory:'); connection.connection.backup(template); self.addCleanup(template.close)
        self.fixture_import(fixture)
        saved=FeedbackEvent.objects.get(signal='saved')
        human=mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('saved',{'value':False},
            target_id=str(saved.target_id),corrects_id=str(saved.pk)))
        approval=(self.witness(human),)
        self.ban(); package=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        with fresh_destination(self,template,701) as (profile,coordinator):
            binding=replace(binding_for_package(profile,package),independent_record_digests=approval)
            call=lambda p,b:import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=str(uuid.uuid4()),package_bytes=canonical(p),binding=b)
            self.denied(lambda:call(package,binding),coordinator=coordinator)
            self.ban(coordinator)
            self.denied(lambda:call(package,replace(binding,independent_record_digests=())),coordinator=coordinator)
            for field,value in (('value',{'value':True}),('original_recorded_at','2026-01-01T00:00:00+00:00'),('corrects_id',None)):
                bad=deepcopy(package); next(r for r in bad['records'] if r['id']==human.event_id)[field]=value
                bad['counts']=document_counts(bad)
                self.denied(lambda:call(bad,binding),coordinator=coordinator)
            for change in ({'source_redacted':False},{'original_origin':'direct','source_redacted':False},
                           {'value':{'value':True}},{'source_metadata':{'request_id':'fiction-provenance'}}):
                bad=deepcopy(package); r=next(r for r in bad['records'] if r['signal']=='saved' and r['revision']==1); r.update(change)
                bad['counts']=document_counts(bad)
                self.denied(lambda:call(bad,binding),coordinator=coordinator)
            bad=deepcopy(package)
            last=next(r for r in bad['records'] if r['signal']=='saved' and r['revision']==1)
            bad['records'].remove(last); last.update(source_redacted=False,source_metadata={'request_id':'fiction-last-bad'})
            bad['records'].append(last); bad['counts']=document_counts(bad)
            validate_package(bad)
            self.denied(lambda:call(bad,binding),coordinator=coordinator)
            bad=deepcopy(package); bad['targets'][-1]['title']='Unapproved source payload'
            self.denied(lambda:call(bad,binding),coordinator=coordinator)
            for entries in ((approval[0],approval[0]),(dict(id=human.event_id,sha256='forged'),)):
                self.denied(lambda:call(package,replace(binding,independent_record_digests=entries)),coordinator=coordinator)
            # An approval manifest is canonicalized independently of entry order.
            extra=dict(id=str(uuid.uuid4()),sha256='0'*64)
            a=replace(binding,independent_record_digests=approval+(extra,))
            b=replace(binding,independent_record_digests=(extra,)+approval)
            self.assertEqual(a.canonical_value(),b.canonical_value())
            import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=str(uuid.uuid4()),package_bytes=canonical(package),binding=binding)
            # Continuation after the ban needs a new witnessed local approval.
            continuation=mutate(actor=self.actor,profile_id=profile.pk,
                command=replace(self.command('saved',{'value':True},target_id=str(saved.target_id)),
                    base_revision=2,context_event_id=human.event_id,corrects_id=human.event_id))
            continuation_approval=self.witness(continuation)
            continued=decode_package(human_export(actor=self.actor,profile_id=profile.pk,format='json').content)
        with fresh_destination(self,template,702) as (profile,coordinator):
            self.ban(coordinator)
            binding=replace(binding_for_package(profile,continued),independent_record_digests=approval)
            self.denied(lambda:import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=str(uuid.uuid4()),
                package_bytes=canonical(continued),binding=binding),coordinator=coordinator)
            import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=str(uuid.uuid4()),package_bytes=canonical(continued),
                binding=replace(binding,independent_record_digests=approval+(continuation_approval,)))

    def test_strict_v2_fields_counts_history_and_explicit_clear_v1(self):
        self.fixture_import(); saved=FeedbackEvent.objects.get(signal='saved')
        clear=mutate(actor=self.actor,profile_id=self.profile.pk,
            command=replace(self.command('saved',target_id=str(saved.target_id)),value=None))
        before=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        self.assertEqual(before['schema_version'],'cc.feedback.v1')
        self.assertIn(b'Explicitly cleared',encode_package(before,'md'))
        self.ban(); package=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        human=next(r for r in package['records'] if r['id']==clear.event_id)
        self.assertEqual((human['value'],human['erased'],human['source_redacted']),(None,False,False))
        md=encode_package(package,'md'); self.assertIn(b'Explicitly cleared',md); self.assertIn(b'Source content redacted',md)
        for mutate_package in (
            lambda p:p.update(schema_version='cc.feedback.v3'),
            lambda p:p['records'][0].pop('source_redacted'),
            lambda p:p['records'][0].update(source_redacted=1),
            lambda p:p['records'][0].update(revision=7),
            lambda p:p['records'].append(deepcopy(p['records'][0])),
            lambda p:p['counts'].update(source_redactions=True),
            lambda p:p['records'][0].update(original_recorded_at='2026-01-01T00:00:00+00:00'),
            lambda p:p['records'][0].update(attribution={'subject_id':p['subject_id']}),
            lambda p:p['records'][0].update(source_record_id='hidden-source'),
            lambda p:p['records'][0].update(source_time='2026-01-01T00:00:00+00:00'),
            lambda p:p['records'][0].update(original_origin='direct'),
            lambda p:p['records'][0].update(erased=True),
        ):
            bad=deepcopy(package); mutate_package(bad)
            with self.assertRaises(FeedbackError): validate_package(bad)
        bad=deepcopy(before); bad['records'][0]['source_redacted']=False
        with self.assertRaises(FeedbackError): validate_package(bad)
        bad=deepcopy(package)
        for r in bad['records']: r['source_redacted']=False
        bad['counts']=document_counts(bad)
        with self.assertRaises(FeedbackError): validate_package(bad)

    def test_pending_source_denies_both_imports_without_journal_or_row_change(self):
        from shelf.recovery import trusted_operator,restrict_authority
        from unittest.mock import patch
        fixture,binding,mid=self.fixture_import()
        package=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        cap=trusted_operator(self.recovery,self.actor)
        with patch.object(self.recovery,'apply_pending',side_effect=RuntimeError('fictional interruption')):
            with self.assertRaises(RuntimeError): restrict_authority(actor=cap,recovery=self.recovery,
                command=dict(mutation_id=str(uuid.uuid4()),base_sequence=0,restrictions=[dict(kind='source_restrict',
                    profile_id=None,target_id=None,signal=None,scope=None,subject_id='legacy-fixture-v1',restriction_code='deny_source_content')]))
        self.denied(lambda:import_legacy_fixture(actor=self.actor,profile_id=self.profile.pk,mutation_id=mid,
            fixture_bytes=canonical(fixture),binding=binding),Http404)
        self.denied(lambda:import_portable(actor=self.actor,profile_id=self.profile.pk,mutation_id=str(uuid.uuid4()),
            package_bytes=canonical(package),binding=binding_for_package(self.profile,package)),Http404)

    def test_banned_v1_null_source_and_mixed_bad_last_precede_any_erase_prepare(self):
        from unittest.mock import patch
        fixture=legacy_fixture(1); package=legacy_to_package(fixture,binding_for_fixture(self.profile,fixture))
        for t in package['targets']: FeedbackTarget.objects.create(**t)
        self.ban()
        cases=[deepcopy(package)]
        null_source=deepcopy(package)
        for record in null_source['records']:
            for field in ('value','source_record_id','source_time','source_metadata','attribution','original_recorded_at'): record[field]=None
        null_source['counts']=document_counts(null_source); cases.append(null_source)
        mixed=deepcopy(package); saved=next(r for r in mixed['records'] if r['signal']=='saved')
        for field in ('value','source_record_id','source_time','source_metadata','attribution','original_recorded_at'): saved[field]=None
        saved['erased']=True
        erased=deepcopy(saved); erased.update(id=str(uuid.uuid4()),revision=2,operation='erase')
        mixed['records'].append(erased)
        mixed['restrictions']=[dict(kind='feedback_erased',target_id=saved['target_id'],scope='item',signals=['saved'],through_revision=2)]
        mixed['counts']=document_counts(mixed); cases.append(mixed)
        for bad in cases:
            validate_package(bad)
            with patch.object(self.recovery,'prepare',wraps=self.recovery.prepare) as prepare:
                self.denied(lambda:import_portable(actor=self.actor,profile_id=self.profile.pk,mutation_id=str(uuid.uuid4()),
                    package_bytes=canonical(bad),binding=binding_for_package(self.profile,bad)))
                prepare.assert_not_called()
        self.denied(lambda:import_legacy_fixture(actor=self.actor,profile_id=self.profile.pk,mutation_id=str(uuid.uuid4()),
            fixture_bytes=canonical(fixture),binding=binding_for_fixture(self.profile,fixture)))

    def test_independent_v1_human_clear_on_banned_namespace_and_manifest_receipt_hash(self):
        fixture=legacy_fixture(1); fixture_package=legacy_to_package(fixture,binding_for_fixture(self.profile,fixture))
        for t in fixture_package['targets']: FeedbackTarget.objects.create(**t)
        target=fixture_package['targets'][0]['id']
        template=sqlite3.connect(':memory:'); connection.connection.backup(template); self.addCleanup(template.close)
        human=mutate(actor=self.actor,profile_id=self.profile.pk,command=replace(self.command('saved',target_id=target),value=None))
        approved=(self.witness(human),)
        package=decode_package(human_export(actor=self.actor,profile_id=self.profile.pk,format='json').content)
        self.assertEqual(package['schema_version'],'cc.feedback.v1')
        for index,fmt in enumerate(('json','csv','md')):
            payload=encode_package(package,fmt)
            for hop in range(2):
                with fresh_destination(self,template,730+index*2+hop) as (profile,coordinator):
                    self.ban(coordinator)
                    binding=replace(binding_for_package(profile,package),independent_record_digests=approved)
                    self.denied(lambda:import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=str(uuid.uuid4()),
                        package_bytes=payload,binding=replace(binding,independent_record_digests=())),coordinator=coordinator)
                    mid=str(uuid.uuid4())
                    import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=mid,package_bytes=payload,binding=binding)
                    self.assertIsNone(FeedbackProjection.objects.get(signal='saved').value)
                    self.assertFalse(FeedbackProjection.objects.get(signal='saved').erased)
                    extra=dict(id=str(uuid.uuid4()),sha256='f'*64)
                    self.denied(lambda:import_portable(actor=self.actor,profile_id=profile.pk,mutation_id=mid,
                        package_bytes=payload,binding=replace(binding,independent_record_digests=approved+(extra,))),Conflict,coordinator)
                    out=human_export(actor=self.actor,profile_id=profile.pk,format=fmt)
                    if fmt=='md':
                        self.assertIn(b'Explicitly cleared',out.content); self.assertNotIn(b'Source content redacted',out.content)
                    self.assertEqual(decode_package(out.content)['schema_version'],'cc.feedback.v1')
                    self.assertEqual(decode_package(out.content)['records'],package['records'])
                    payload=out.content
