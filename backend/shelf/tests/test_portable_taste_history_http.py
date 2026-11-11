"""Normal keyless history forms and closed native command boundaries."""
from html.parser import HTMLParser
import uuid
from shelf.models import FeedbackEvent,PortableJourneyCommand
from shelf.portable_taste_history import TasteHistory
from .test_portable_feedback_views import NativeFeedbackCase


class Forms(HTMLParser):
    def __init__(self,content):
        super().__init__();self.rows={};self.current=None;self.feed(content.decode())
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='form':
            self.current={};self.rows[attrs.get('action')]=self.current
        if tag=='input' and self.current is not None and attrs.get('name') and attrs.get('type')=='hidden':
            self.current[attrs['name']]=attrs.get('value','')
    def handle_endtag(self,tag):
        if tag=='form':self.current=None


class TasteHistoryHTTPTests(NativeFeedbackCase):
    def test_native_keyless_compare_preview_confirm_restore_and_undo_forms_use_current_references(self):
        history=TasteHistory(self.authority);old=self.note_fields(text='  Original note\n雪  ')
        self.assertEqual(self.post('/cabinet/taste',old).status_code,303)
        point=history.capture(self.ref,profile_ref=self.profile_ref,origin_command_ref=old['mutation_id'])
        later=self.note_fields(text='Later note',entry_ref=old['entry_ref'],base_revision=1)
        self.assertEqual(self.post('/cabinet/taste',later).status_code,303)
        self.vault.remove(self.ref,self.config.pk,self.config.config_revision,disable=True)
        page=self.http.get('/cabinet/taste/history',{'profile_ref':self.profile_ref},**self.headers)
        self.assertEqual(page.status_code,200);self.assertContains(page,'Taste history')
        path='/cabinet/taste/history/'+point['checkpoint_ref']
        compared=self.http.get(path,{'profile_ref':self.profile_ref},**self.headers)
        self.assertEqual(compared.status_code,200);self.assertContains(compared,'Original note')
        self.assertContains(compared,'Later note')
        before=FeedbackEvent.objects.count();commands=PortableJourneyCommand.objects.count()
        self.assertEqual(self.http.get(path,{'profile_ref':self.profile_ref},**self.headers).status_code,200)
        self.assertEqual((FeedbackEvent.objects.count(),PortableJourneyCommand.objects.count()),(before,commands))
        fields=Forms(compared.content).rows['/cabinet/taste/history/preview'];fields.pop('csrfmiddlewaretoken',None)
        response=self.post('/cabinet/taste/history/preview',fields)
        self.assertEqual(response.status_code,303)
        preview=self.http.get(response['Location'],**self.headers)
        self.assertEqual(preview.status_code,200)
        fields=Forms(preview.content).rows['/cabinet/taste/history/restore'];fields.pop('csrfmiddlewaretoken',None)
        self.assertNotIn('confirmed',fields)
        self.assertEqual(self.post('/cabinet/taste/history/restore',fields).status_code,422)
        fields['confirmed']='restore_taste_choices'
        restored=self.post('/cabinet/taste/history/restore',fields);self.assertEqual(restored.status_code,303)
        receipt=self.http.get(restored['Location'],**self.headers);self.assertEqual(receipt.status_code,200)
        self.assertContains(receipt,'Taste restored')
        fields=Forms(receipt.content).rows['/cabinet/taste/history/undo'];fields.pop('csrfmiddlewaretoken',None)
        fields['confirmed']='undo_taste_restore'
        undone=self.post('/cabinet/taste/history/undo',fields);self.assertEqual(undone.status_code,303)
        self.assertEqual(self.http.get(undone['Location'],**self.headers).status_code,200)
        self.assertEqual(FeedbackEvent.objects.count(),before+2)
        from shelf.models import SynthesisRun,PortableSourceCheckAttempt
        self.assertFalse(SynthesisRun.objects.exists())
        self.assertFalse(PortableSourceCheckAttempt.objects.exists())

    def test_csrf_duplicates_foreign_references_posted_values_and_oversize_do_not_create_metadata(self):
        old=self.note_fields();self.assertEqual(self.post('/cabinet/taste',old).status_code,303)
        history=TasteHistory(self.authority)
        point=history.capture(self.ref,profile_ref=self.profile_ref,origin_command_ref=old['mutation_id'])
        comparison=history.compare(self.ref,profile_ref=self.profile_ref,checkpoint_ref=point['checkpoint_ref'])
        values=dict(profile_ref=self.profile_ref,checkpoint_ref=point['checkpoint_ref'],
            expected_state_digest=comparison['current_state_digest'],command_id=str(uuid.uuid4()))
        count=PortableJourneyCommand.objects.count()
        self.assertEqual(self.post('/cabinet/taste/history/preview',values,csrf=False).status_code,403)
        self.assertEqual(self.post('/cabinet/taste/history/preview',{**values,'changes':'[]'}).status_code,422)
        duplicate=list(values.items())+[('profile_ref',self.profile_ref)]
        self.assertEqual(self.post('/cabinet/taste/history/preview',duplicate).status_code,422)
        self.assertEqual(self.post('/cabinet/taste/history/preview',{**values,'checkpoint_ref':str(uuid.uuid4())}).status_code,403)
        self.assertEqual(self.post('/cabinet/taste/history/preview',{**values,'expected_state_digest':'0'*9000}).status_code,422)
        self.assertEqual(PortableJourneyCommand.objects.count(),count)
        self.assertEqual(FeedbackEvent.objects.count(),1)

    def test_original_native_maybe_interest_remains_displayable_comparable_and_restorable(self):
        from shelf.models import FeedbackProjection
        history=TasteHistory(self.authority)
        first=self.fields(value='maybe')
        self.assertEqual(self.post('/cabinet/taste',first).status_code,303)
        point=PortableJourneyCommand.objects.get(kind='taste.checkpoint',
            result__origin_command_ref=first['mutation_id']).result
        newer={**first,'value':'yes','base_revision':1,'mutation_id':str(uuid.uuid4())}
        self.assertEqual(self.post('/cabinet/taste',newer).status_code,303)
        path='/cabinet/taste/history/'+point['checkpoint_ref']
        compared=self.http.get(path,{'profile_ref':self.profile_ref},**self.headers)
        self.assertEqual(compared.status_code,200)
        self.assertContains(compared,'Maybe')
        fields=Forms(compared.content).rows['/cabinet/taste/history/preview'];fields.pop('csrfmiddlewaretoken',None)
        response=self.post('/cabinet/taste/history/preview',fields);self.assertEqual(response.status_code,303)
        preview=self.http.get(response['Location'],**self.headers)
        self.assertEqual(preview.status_code,200);self.assertContains(preview,'Maybe')
        fields=Forms(preview.content).rows['/cabinet/taste/history/restore'];fields.pop('csrfmiddlewaretoken',None)
        fields['confirmed']='restore_taste_choices'
        self.assertEqual(self.post('/cabinet/taste/history/restore',fields).status_code,303)
        self.assertEqual(FeedbackProjection.objects.get(signal='interest').value,{'value':'maybe','basis':'anticipated'})
