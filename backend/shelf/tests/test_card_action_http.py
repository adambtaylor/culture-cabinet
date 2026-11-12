"""Normal protected native form/receipt endpoints; fictional publication only."""
from dataclasses import asdict
from urllib.parse import parse_qs,urlsplit
import uuid
from shelf.card_actions import CardAction
from shelf.models import FeedbackEvent,PortableJourneyCommand
from .test_portable_feedback_views import NativeFeedbackOutcomeTests


class CardActionHTTPTests(NativeFeedbackOutcomeTests):
    def test_refresh_context_uses_owned_receipt_and_fresh_cabinet_read_without_an_extra_post(self):
        from unittest.mock import patch
        from shelf import enhancement_views
        fields=self.prepare();response=self.post('/cabinet/actions/',fields)
        self.assertEqual(urlsplit(response['Location']).path,'/cabinet')
        captured=[];original=enhancement_views._native_cabinet_html
        def render(request,context,**kwargs):
            captured.append(context['card_action_refresh'])
            return original(request,context,**kwargs)
        with patch.object(enhancement_views,'_native_cabinet_html',side_effect=render):
            self.assertEqual(self.http.get(response['Location'],**self.headers).status_code,200)
        marker=captured[0]
        self.assertEqual(marker['profile_ref'],self.profile_ref)
        self.assertEqual(marker['publication_ref'],fields['publication_ref'])
        self.assertEqual(marker['publication_revision'],fields['publication_revision'])
        self.assertEqual(marker['read_state'],'current')
        self.assertEqual(marker['receipt']['command_id'],fields['command_id'])
        self.assertEqual(marker['receipt']['target_ref'],fields['target_ref'])
        self.assertEqual(marker['receipt']['preference_outcome'],'committed')
        self.assertEqual(marker['receipt']['interest_revision'],1)
        self.assertEqual(FeedbackEvent.objects.count(),1)

    def test_refresh_foreign_or_mismatched_receipt_and_refused_read_cannot_render_a_current_deck(self):
        from unittest.mock import patch
        from shelf.card_actions import CardActions
        from shelf import enhancement_views
        from shelf.portable_authority import AuthorityFault
        fields=self.prepare();response=self.post('/cabinet/actions/',fields)
        actual=CardActions(self.authority,service=self.enhance).receipt(self.ref,self.profile_ref,fields['command_id'])
        with patch.object(CardActions,'receipt',return_value={**actual,'command_id':str(uuid.uuid4())}), \
             patch.object(enhancement_views,'_native_cabinet_html') as render:
            refused=self.http.get(response['Location'],**self.headers)
        self.assertEqual(refused.status_code,422);render.assert_not_called()
        unknown=self.http.get('/cabinet',{'profile_ref':self.profile_ref,'action_receipt':str(uuid.uuid4())},**self.headers)
        self.assertEqual(unknown.status_code,403)
        captured=[];original=enhancement_views._native_cabinet_html
        def render(request,context,**kwargs):
            captured.append(context)
            return original(request,context,**kwargs)
        with patch.object(self.enhance,'current_for_interaction',side_effect=AuthorityFault('AUTHORITY_DENIED',403)), \
             patch.object(enhancement_views,'_native_cabinet_html',side_effect=render):
            refused=self.http.get(response['Location'],**self.headers)
        self.assertEqual(refused.status_code,403)
        self.assertEqual(captured[0]['categories'],[])
        self.assertEqual(captured[0]['card_action_refresh']['read_state'],'unavailable')
        self.assertEqual(captured[0]['card_action_refresh']['receipt']['command_id'],fields['command_id'])
        self.assertNotIn(b'PRIVATE fixture card title',refused.content)
        self.assertNotIn('connect-src',refused['Content-Security-Policy'])
        self.assertEqual(FeedbackEvent.objects.count(),1)

    def test_refresh_mixed_profile_or_publication_projection_refuses_the_whole_deck(self):
        from unittest.mock import patch
        from shelf.card_actions import CardActions
        from shelf import enhancement_views
        self.prepare();original=CardActions.project
        for field,bad in (('profile_ref',str(uuid.uuid4())),('publication_ref',str(uuid.uuid4())),
                          ('publication_revision',2)):
            def replaced(instance,*args,**kwargs):
                value=original(instance,*args,**kwargs)
                value[field]=bad
                for row in value['actions']:
                    row['fields'][field]=bad
                return value
            with self.subTest(field=field),patch.object(CardActions,'project',replaced), \
                 patch.object(enhancement_views,'_native_cabinet_html') as render:
                refused=self.http.get('/cabinet',{'profile_ref':self.profile_ref},**self.headers)
            self.assertEqual(refused.status_code,422);render.assert_not_called()
            self.assertNotIn(b'PRIVATE fixture card title',refused.content)
        self.assertEqual(FeedbackEvent.objects.count(),0)

    def prepare(self):
        self.consent(self.profile_ref);target=self.publish_card()
        current=self.enhance.current_for_interaction(self.ref,profile_ref=self.profile_ref)
        return asdict(CardAction(self.profile_ref,current['publication_ref'],current['publication_revision'],
            target,0,0,str(uuid.uuid4()),'like'))

    def test_normal_post_redirect_owned_receipt_and_exact_replay_without_source_title(self):
        fields=self.prepare()
        result=self.post('/cabinet/actions/',fields)
        self.assertEqual(result.status_code,303)
        query=parse_qs(urlsplit(result['Location']).query)
        self.assertEqual(query,{'profile_ref':[self.profile_ref],'action_receipt':[fields['command_id']]})
        self.assertEqual(self.post('/cabinet/actions/',fields).status_code,303)
        self.assertEqual(FeedbackEvent.objects.count(),1)
        page=self.http.get('/cabinet/actions/'+fields['command_id']+'/',**self.headers)
        self.assertEqual(page.status_code,200)
        self.assertContains(page,'like')
        self.assertNotIn(b'PRIVATE fixture card title',page.content)
        self.assertNotIn('script-src',page['Content-Security-Policy'])
        self.clock.advance(601)
        historical=self.http.get('/cabinet/actions/'+fields['command_id']+'/',**self.headers)
        self.assertEqual(historical.status_code,200)
        self.assertNotIn(b'PRIVATE fixture card title',historical.content)

    def test_csrf_duplicate_raw_identity_numeric_and_oversized_forms_have_no_effect(self):
        fields=self.prepare()
        # Preparing the publication can record taste-history metadata. Rejected
        # forms must leave that baseline intact, not assume an empty shared log.
        before=PortableJourneyCommand.objects.count()
        self.assertEqual(self.post('/cabinet/actions/',fields,csrf=False).status_code,403)
        self.assertEqual(self.post('/cabinet/actions/',{**fields,'title':'INJECTED'}).status_code,422)
        self.assertEqual(self.post('/cabinet/actions/',[*fields.items(),('action','favourite')]).status_code,422)
        self.assertEqual(self.post('/cabinet/actions/',{**fields,'action':'1'}).status_code,422)
        self.assertEqual(self.post('/cabinet/actions/',{**fields,'command_id':'x'*9000}).status_code,422)
        self.assertEqual(FeedbackEvent.objects.count(),0)
        self.assertEqual(PortableJourneyCommand.objects.count(),before)
        self.assertEqual(self.http.get('/cabinet/actions/'+str(uuid.uuid4())+'/',**self.headers).status_code,403)

    def test_actual_cabinet_four_choices_feedback_pending_receipt_and_fixed_assets_csp(self):
        values=self.prepare()
        page=self.http.get('/cabinet',{'profile_ref':self.profile_ref},**self.headers)
        self.assertEqual(page.status_code,200)
        for action in ('like','dislike','favourite','obtain'):
            self.assertContains(page,'name="action" value="'+action+'"')
        self.assertContains(page,'/cabinet/actions/script.js')
        csp=page['Content-Security-Policy']
        self.assertIn(self.boundary.origin+'/cabinet/actions/script.js',csp)
        self.assertIn(self.boundary.origin+'/cabinet/obtain/status.js',csp)
        self.assertNotIn("'unsafe-inline'",csp)
        self.assertNotIn("'unsafe-eval'",csp)
        for path,mime in (('/cabinet/actions/script.js','text/javascript'),
                          ('/cabinet/actions/style.css','text/css'),('/setup/journey/style.css','text/css')):
            asset=self.http.get(path,**self.headers)
            self.assertEqual(asset.status_code,200)
            self.assertTrue(asset['Content-Type'].startswith(mime))
            self.assertEqual(asset['X-Content-Type-Options'],'nosniff')
            self.assertNotIn('script-src',asset['Content-Security-Policy'])
        result=self.post('/cabinet/actions/',values)
        current=self.http.get(result['Location'],**self.headers)
        self.assertEqual(current.status_code,200)
        self.assertContains(current,'Feedback saved; Enhance to update recommendations.')
        self.assertContains(current,'Undo this preference')
        self.assertNotIn(b'feedback-current-reason',current.content)
        historical=self.http.get('/cabinet/actions/'+values['command_id']+'/',**self.headers)
        self.assertContains(historical,'Feedback receipt')
        self.assertNotIn('script-src',historical['Content-Security-Policy'])


    def assert_card_refusal(self,response,status,*,undo=False):
        self.assertEqual(response.status_code,status)
        self.assertContains(response,'Undo is not confirmed' if undo else 'Your card response is not confirmed',status_code=status)
        self.assertContains(response,'href="/setup"',status_code=status)
        self.assertContains(response,'href="/setup/cabinet"',status_code=status)
        self.assertNotIn(b'<form',response.content)
        self.assertNotIn(b'enhance/v1',response.content)
        self.assertNotIn(b'Enhance progress',response.content)
        self.assertNotIn(b'PRIVATE fixture card title',response.content)
        self.assertNotIn(self.profile_ref.encode(),response.content)
        self.assertEqual(response['Cache-Control'],'no-store')
        self.assertEqual(response['X-Content-Type-Options'],'nosniff')
        self.assertNotIn('Location',response)

    def test_native_stale_confirmation_and_absent_session_refuse_action_and_undo_without_effects(self):
        fields=self.prepare();self.clock.advance(301)
        self.assert_card_refusal(self.post('/cabinet/actions/',fields),401)
        undo=dict(profile_ref=self.profile_ref,original_command_id=fields['command_id'],
            command_id=str(uuid.uuid4()),interest_revision=0,saved_revision=0)
        self.assert_card_refusal(self.post('/cabinet/actions/undo/',undo),401,undo=True)
        session=self.http.session;session.pop('portable_session');session.save()
        self.assert_card_refusal(self.post('/cabinet/actions/',fields),401)
        self.assert_card_refusal(self.post('/cabinet/actions/undo/',undo),401,undo=True)
        self.assertEqual(FeedbackEvent.objects.count(),0)
        self.assertFalse(PortableJourneyCommand.objects.filter(kind__in=['card.action','card.undo']).exists())

    def test_native_json_refusal_keeps_original_closed_shape_status_and_no_redirect(self):
        from urllib.parse import urlencode
        fields=self.prepare();self.clock.advance(301)
        response=self.http.post('/cabinet/actions/',urlencode(fields),
            content_type='application/x-www-form-urlencoded',HTTP_ACCEPT='application/json',
            HTTP_X_CSRFTOKEN=self.csrf(),**self.headers)
        self.assertEqual(response.status_code,401)
        self.assertEqual(response.json(),{'ok':False,'code':'UNAUTHENTICATED'})
        self.assertEqual(response['Cache-Control'],'no-store');self.assertNotIn('Location',response)
        self.assertEqual(FeedbackEvent.objects.count(),0)
        self.assertFalse(PortableJourneyCommand.objects.filter(kind__in=['card.action','card.undo']).exists())

    def test_native_successful_action_and_undo_keep_303_and_exact_command_replay(self):
        fields=self.prepare();response=self.post('/cabinet/actions/',fields)
        self.assertEqual(response.status_code,303)
        undo=dict(profile_ref=self.profile_ref,original_command_id=fields['command_id'],
            command_id=str(uuid.uuid4()),interest_revision=1,saved_revision=0)
        response=self.post('/cabinet/actions/undo/',undo);self.assertEqual(response.status_code,303)
        self.assertEqual(self.post('/cabinet/actions/undo/',undo).status_code,303)
        self.assertEqual(FeedbackEvent.objects.count(),2)
        self.assertEqual(PortableJourneyCommand.objects.filter(kind__in=['card.action','card.undo']).count(),2)

    def test_native_post_effect_refusal_never_claims_not_saved_or_automatically_replays(self):
        from unittest.mock import patch
        from shelf.card_actions import CardActions
        from shelf.portable_authority import AuthorityFault
        fields=self.prepare();original=CardActions.apply
        def after_commit(instance,*args,**kwargs):
            original(instance,*args,**kwargs)
            raise AuthorityFault('UNKNOWN_RECONCILIATION_REQUIRED',409)
        with patch.object(CardActions,'apply',after_commit):
            response=self.post('/cabinet/actions/',fields)
        self.assert_card_refusal(response,409)
        self.assertNotIn(b'not saved',response.content.lower())
        self.assertEqual(FeedbackEvent.objects.count(),1)
        self.assertEqual(PortableJourneyCommand.objects.filter(kind='card.action',state='committed').count(),1)
        self.assertEqual(self.post('/cabinet/actions/',fields).status_code,303)
        self.assertEqual(FeedbackEvent.objects.count(),1)
