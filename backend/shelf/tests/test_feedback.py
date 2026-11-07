from dataclasses import asdict, replace
from unittest.mock import patch
import json
import sqlite3
import uuid
from django.contrib.auth import get_user_model
from django.db import connection
from django.http import Http404
from django.test import Client, override_settings
from django.urls import reverse
from shelf.feedback import (mutate, feedback_context, FeedbackError, Conflict, Gone, Busy,
    EraseCommand, erase_feedback, key_token, immediate, strict_json)
from shelf.models import FeedbackEvent, FeedbackProjection, MutationReceipt, FeedbackSubject, Grant
from shelf.recovery import (RecoveryBlocked, restrict_authority, trusted_operator)
from shelf.tests.synthetic_feedback import FeedbackCase


class FeedbackTests(FeedbackCase):
    def test_F01_integer_scale_signal_separation_and_explicit_clear(self):
        for number in range(1,6):
            mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command(value=dict(value=number,basis='experienced',scale='integer-1-5')))
            self.assertEqual(FeedbackProjection.objects.get(signal='enjoyment').value['value'],number)
        for bad in (True,2.0,'3',0,6):
            with self.assertRaises(FeedbackError):
                mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command(value=dict(value=bad,basis='experienced',scale='integer-1-5')))
        for signal,value in (('interest',dict(value='yes',basis='anticipated')),('saved',dict(value=True)),
            ('familiarity',dict(value='aware')),('completion',dict(value='completed')),('constraint',dict(text='Quiet seats'))):
            mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command(signal,value))
        self.assertEqual(FeedbackEvent.objects.filter(signal='enjoyment').count(),5)
        clear=replace(self.command('constraint'),value=None)
        mutate(actor=self.actor,profile_id=self.profile.pk,command=clear)
        self.assertIsNone(FeedbackProjection.objects.get(signal='constraint').value)
        self.assertFalse(FeedbackProjection.objects.filter(signal='skipped').exists())
        with self.assertRaises(FeedbackError): strict_json(b'{"value":1,"value":2}')

    def test_F02_replay_payload_crossoperation_and_actor_namespace(self):
        command=self.command(); first=mutate(actor=self.actor,profile_id=self.profile.pk,command=command)
        self.assertEqual(first,mutate(actor=self.actor,profile_id=self.profile.pk,command=command))
        self.assertEqual(FeedbackEvent.objects.count(),1)
        with self.assertRaises(Conflict): mutate(actor=self.actor,profile_id=self.profile.pk,command=replace(command,value=dict(value=2,basis='experienced',scale='integer-1-5')))
        reverse=replace(self.command(),mutation_id=command.mutation_id,operation='reverse',value=None,reverses_id=first.event_id)
        with self.assertRaises(Conflict): mutate(actor=self.actor,profile_id=self.profile.pk,command=reverse)
        other=get_user_model().objects.create(username='other-fiction')
        Grant.objects.create(user=other,profile=self.profile,can_view=True,can_mutate=True)
        second=replace(self.command('saved'),mutation_id=command.mutation_id)
        mutate(actor=other,profile_id=self.profile.pk,command=second)
        self.assertEqual(MutationReceipt.objects.count(),2)

    def test_F03_fresh_actor_grant_profile_before_duplicate(self):
        command=self.command(); mutate(actor=self.actor,profile_id=self.profile.pk,command=command)
        for field,model,pk in [('can_view',Grant,self.grant.pk),('can_mutate',Grant,self.grant.pk),('is_active',get_user_model(),self.actor.pk),('active',type(self.profile),self.profile.pk)]:
            model.objects.filter(pk=pk).update(**{field:False})
            with self.assertRaises(Http404): mutate(actor=self.actor,profile_id=self.profile.pk,command=command)
            model.objects.filter(pk=pk).update(**{field:True})
        self.actor.refresh_from_db()
        self.grant.can_mutate=False; self.grant.save()
        self.actor.is_superuser=True; self.actor.save()
        with self.assertRaises(Http404): mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command())

    def test_F04_forged_fields_scope_and_unauthorized_erase_no_prepare(self):
        with self.assertRaises(TypeError): self.command().__class__(**asdict(self.command()),actor_id=100)
        with self.assertRaises(FeedbackError): mutate(actor=self.actor,profile_id=self.profile.pk,command=replace(self.command(),scope='session'))
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command())
        erase=self.erase_command()
        before=(self.recovery.directory/'journal').read_bytes()
        with self.assertRaises(Conflict): erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=replace(erase,base_revisions={}),recovery=self.recovery)
        self.assertEqual(before,(self.recovery.directory/'journal').read_bytes())
        self.grant.can_mutate=False; self.grant.save()
        with self.assertRaises(Http404): erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=erase,recovery=self.recovery)
        self.assertEqual(before,(self.recovery.directory/'journal').read_bytes())

    def test_F05_same_base_conflict_independent_signals_and_outer_state(self):
        same=self.command(); independent=self.command('saved')
        mutate(actor=self.actor,profile_id=self.profile.pk,command=same)
        with self.assertRaises(Conflict): mutate(actor=self.actor,profile_id=self.profile.pk,command=replace(same,mutation_id=str(uuid.uuid4())))
        mutate(actor=self.actor,profile_id=self.profile.pk,command=independent)
        self.assertEqual(FeedbackProjection.objects.count(),2)
        timeout=connection.connection.execute('PRAGMA busy_timeout').fetchone()[0]
        with self.assertRaises(RuntimeError):
            with immediate():
                FeedbackSubject.objects.all().delete()
                raise RuntimeError('synthetic interruption')
        self.assertTrue(FeedbackSubject.objects.exists())
        self.assertFalse(connection.connection.in_transaction)
        self.assertEqual(connection.connection.execute('PRAGMA busy_timeout').fetchone()[0],timeout)
        with immediate():
            with self.assertRaises(FeedbackError):
                with immediate(): pass

    def test_F06_intervening_undo_and_correction_reduce_history(self):
        a=mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command(value=dict(value=1,basis='experienced',scale='integer-1-5')))
        b=mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command(value=dict(value=2,basis='experienced',scale='integer-1-5')))
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('saved'))
        undo_a=replace(self.command(),operation='reverse',value=None,reverses_id=a.event_id)
        mutate(actor=self.actor,profile_id=self.profile.pk,command=undo_a)
        self.assertEqual(FeedbackProjection.objects.get(signal='enjoyment').value['value'],2)
        c=mutate(actor=self.actor,profile_id=self.profile.pk,command=replace(self.command(),corrects_id=b.event_id))
        undo_c=replace(self.command(),operation='reverse',value=None,reverses_id=c.event_id)
        result=mutate(actor=self.actor,profile_id=self.profile.pk,command=undo_c)
        self.assertEqual(result,mutate(actor=self.actor,profile_id=self.profile.pk,command=undo_c))
        self.assertEqual(FeedbackProjection.objects.get(signal='enjoyment').value['value'],2)
        self.assertEqual(FeedbackProjection.objects.get(signal='saved').value,{'value':True})
        with self.assertRaises(Conflict): mutate(actor=self.actor,profile_id=self.profile.pk,command=replace(self.command(),operation='reverse',value=None,reverses_id=c.event_id))

    def erase_command(self,signals='all'):
        rows=FeedbackProjection.objects.filter(profile=self.profile)
        if signals!='all': rows=rows.filter(signal__in=signals)
        return EraseCommand(str(uuid.uuid4()),None,'profile',signals,
            {key_token(str(p.target_id),p.signal,p.scope):p.revision for p in rows},
            {key_token(str(p.target_id),p.signal,p.scope):str(FeedbackEvent.objects.get(profile=self.profile,target_id=p.target_id,signal=p.signal,scope=p.scope,revision=p.revision).pk) for p in rows},
            'ERASE_FICTIONAL_FEEDBACK')

    def test_F07_erase_retires_all_profile_hashes_and_closed_scope(self):
        first=self.command(); mutate(actor=self.actor,profile_id=self.profile.pk,command=first)
        saved=self.command('saved'); mutate(actor=self.actor,profile_id=self.profile.pk,command=saved)
        other=get_user_model().objects.create(username='other-receipt')
        Grant.objects.create(user=other,profile=self.profile,can_view=True,can_mutate=True)
        mutate(actor=other,profile_id=self.profile.pk,command=self.command('constraint',{'text':'fictional prose'}))
        erase=self.erase_command(('enjoyment',))
        report=erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=erase,recovery=self.recovery)
        self.assertEqual(report.status,'accepted')
        self.assertFalse(MutationReceipt.objects.exclude(state='retired',payload_sha256=None).exists())
        self.assertEqual(MutationReceipt.objects.count(),4)
        self.assertEqual(FeedbackProjection.objects.get(signal='saved').value,{'value':True})
        self.assertEqual(FeedbackProjection.objects.get(signal='constraint').value,{'text':'fictional prose'})
        self.assertFalse(FeedbackEvent.objects.filter(signal='enjoyment').exclude(body=None,attribution=None,source_metadata=None,original_recorded_at=None).exists())
        with self.assertRaises(Gone): mutate(actor=self.actor,profile_id=self.profile.pk,command=first)
        with self.assertRaises(Gone): erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=erase,recovery=self.recovery)
        with self.assertRaises(Gone): mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command())
        self.grant.can_view=False; self.grant.save()
        with self.assertRaises(Http404): mutate(actor=self.actor,profile_id=self.profile.pk,command=first)

    @override_settings(ALLOWED_HOSTS=['testserver'],TEMPLATES=[{'BACKEND':'django.template.backends.django.DjangoTemplates','APP_DIRS':False,'OPTIONS':{'loaders':[('django.template.loaders.locmem.Loader',{
        'cabinet/feedback.html':'<form method="post">{% csrf_token %}{% for form in forms %}{{ form.as_p }}{% endfor %}</form>{{ target.title }}',
        'cabinet/feedback_export.html':'<form method="post">{% csrf_token %}{{ form.as_p }}</form>'})]}}])
    def test_F15_typed_forms_csrf_methods_headers_and_escaping_fixture_only(self):
        client=Client(enforce_csrf_checks=True); client.force_login(self.actor)
        target=self.command().target_id
        url=reverse('feedback_detail',args=[self.profile.pk,target]); post=url+'mutate/'
        response=client.get(url)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response['Cache-Control'],'no-store')
        self.assertEqual(response['Referrer-Policy'],'same-origin')
        self.assertIn("default-src 'none'",response['Content-Security-Policy'])
        self.assertEqual(client.get(post).status_code,405)
        self.assertEqual(client.post(post,{}).status_code,403)
        token=client.cookies['cc_synthetic_csrf'].value
        data=dict(mutation_id=str(uuid.uuid4()),target_type='item',scope='item',signal='enjoyment',operation='set',base_revision='0',context_event_id='',corrects_id='',reverses_id='',rating='4',csrfmiddlewaretoken=token)
        self.assertEqual(client.post(post,data).status_code,303)
        self.assertEqual(client.post(post,data).status_code,303)
        self.assertEqual(FeedbackEvent.objects.count(),1)
        export=reverse('feedback_export',args=[self.profile.pk])
        self.assertEqual(client.post(export,{'format':'json'}).status_code,403)
        response=client.post(export,{'format':'json','csrfmiddlewaretoken':token})
        self.assertEqual(response.status_code,200)
        self.assertIn('attachment',response['Content-Disposition'])
        self.assertEqual(client.post(post,{**data,'actor_id':self.actor.pk}).status_code,400)
        self.assertEqual(client.post(post,{**data,'rating':['1','2']}).status_code,400)
        self.assertEqual(FeedbackEvent.objects.count(),1)

    def test_F07_item_hide_sessions_and_erase_descendants_preserves_other_item(self):
        from shelf.models import FeedbackTarget
        item=self.command().target_id
        session=FeedbackTarget.objects.create(id=uuid.uuid4(),kind='session',parent_id=item,title='Fictional session',category='Fictional session',source_namespace='synthetic-test',source_key='session-1')
        other='00000000-0000-4000-8000-000000000102'
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('saved'))
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('saved',target_id=str(session.pk),scope='session'))
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('saved',target_id=other))
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('visibility',{'value':'hidden'}))
        with self.assertRaises(Http404): feedback_context(actor=self.actor,profile_id=self.profile.pk,target_id=session.pk)
        self.assertTrue(feedback_context(actor=self.actor,profile_id=self.profile.pk,target_id=session.pk,include_hidden=True).is_hidden)
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('visibility',{'value':'visible'}))
        self.assertFalse(feedback_context(actor=self.actor,profile_id=self.profile.pk,target_id=session.pk).is_hidden)
        rows=FeedbackProjection.objects.filter(profile=self.profile,target_id__in=[item,session.pk])
        erase=EraseCommand(str(uuid.uuid4()),item,'item','all',
            {key_token(str(p.target_id),p.signal,p.scope):p.revision for p in rows},
            {key_token(str(p.target_id),p.signal,p.scope):str(FeedbackEvent.objects.get(profile=self.profile,target_id=p.target_id,signal=p.signal,scope=p.scope,revision=p.revision).pk) for p in rows},'ERASE_FICTIONAL_FEEDBACK')
        erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=erase,recovery=self.recovery)
        self.assertTrue(FeedbackProjection.objects.get(target=session,signal='saved').erased)
        self.assertFalse(FeedbackProjection.objects.get(target_id=other,signal='saved').erased)
        with self.assertRaises(Gone): mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('opened'))

    def test_F03_new_grant_flags_default_false_inactive_slots_denied(self):
        from shelf.models import Profile
        other=get_user_model().objects.create(username='default-denied')
        grant=Grant.objects.create(user=other,profile=self.profile,can_view=True,can_select=True)
        self.assertFalse(grant.can_mutate); self.assertFalse(grant.can_export)
        with self.assertRaises(Http404): mutate(actor=other,profile_id=self.profile.pk,command=self.command())
        for kind in ('partner','family'):
            slot=Profile.objects.create(kind=kind,label='Inactive fictional slot',active=False)
            Grant.objects.create(user=self.actor,profile=slot,can_view=True,can_select=True,can_mutate=True,can_export=True)
            with self.assertRaises(Http404): mutate(actor=self.actor,profile_id=slot.pk,command=self.command())
