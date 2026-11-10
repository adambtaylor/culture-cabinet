"""Normal protected native form/receipt endpoints; fictional publication only."""
from dataclasses import asdict
from urllib.parse import parse_qs,urlsplit
import uuid
from shelf.card_actions import CardAction
from shelf.models import FeedbackEvent,PortableJourneyCommand
from .test_portable_feedback_views import NativeFeedbackOutcomeTests


class CardActionHTTPTests(NativeFeedbackOutcomeTests):
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
        self.assertEqual(self.post('/cabinet/actions/',fields,csrf=False).status_code,403)
        self.assertEqual(self.post('/cabinet/actions/',{**fields,'title':'INJECTED'}).status_code,422)
        self.assertEqual(self.post('/cabinet/actions/',[*fields.items(),('action','favourite')]).status_code,422)
        self.assertEqual(self.post('/cabinet/actions/',{**fields,'action':'1'}).status_code,422)
        self.assertEqual(self.post('/cabinet/actions/',{**fields,'command_id':'x'*9000}).status_code,422)
        self.assertEqual(FeedbackEvent.objects.count(),0)
        self.assertEqual(PortableJourneyCommand.objects.count(),0)
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
