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
