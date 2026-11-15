"""Actual native cabinet/Enhance start callers and fixed guide asset policy."""
from urllib.parse import urlencode
from django.db import connection
from shelf.models import PortableJourneyState,SynthesisRun
from .test_portable_enhance_service import ServiceCase
from .test_portable_feedback_views import NativeFeedbackCase
from .test_portable_taste_history_http import Forms


class CabinetEnhanceStartTests(ServiceCase):
    claim_owner=NativeFeedbackCase.claim_owner
    csrf=NativeFeedbackCase.csrf
    post=NativeFeedbackCase.post

    def setUp(self):
        super().setUp()
        # Normal HTTP composition must expose this exact fixture's validation
        # service, just as the native runtime does; no fabricated choice list.
        self.enterContext(self.settings(PORTABLE_CONNECTIONS=self.service))

    def selection(self):return {'profile_ref':self.profile_ref,'connection_ref':str(self.config.pk)}

    def start_fields(self):
        page=self.http.get('/cabinet',self.selection(),**self.headers)
        self.assertEqual(page.status_code,200)
        fields=Forms(page.content).rows['/enhance/start'];fields.pop('csrfmiddlewaretoken',None)
        return fields,page

    def test_normal_cabinet_and_enhance_share_readonly_start_default_and_successful_admission_persists_noise(self):
        fields,page=self.start_fields()
        self.assertContains(page,'name="noise"');self.assertContains(page,'value="25"')
        enhance=self.http.get('/enhance',self.selection(),**self.headers)
        self.assertEqual(enhance.status_code,200)
        self.assertIn('/enhance/start',Forms(enhance.content).rows)
        self.assertFalse(SynthesisRun.objects.exists());self.assertFalse(PortableJourneyState.objects.exists())
        started=self.post('/enhance/start',{**fields,'noise':'75'})
        self.assertEqual(started.status_code,303)
        row=SynthesisRun.objects.get();self.assertEqual(row.evidence['selection_policy']['noise'],75)
        self.assertEqual(PortableJourneyState.objects.get().next_noise,75)
        pending=self.http.get('/cabinet',self.selection(),**self.headers)
        self.assertNotIn('/enhance/start',Forms(pending.content).rows)
        self.assertContains(pending,'Continue the recorded round')
        self.enhance.cancel(self.ref,run_ref=str(row.pk),expected_revision=row.revision)
        _,reloaded=self.start_fields();self.assertContains(reloaded,'value="75"')
        self.assertEqual((self.catalogue.calls,self.transport.calls),(0,0))

    def test_closed_noise_forms_reject_duplicates_signs_fractions_range_and_json_types_before_admission(self):
        fields,_=self.start_fields()
        for noise in ('-1','+1','01','1.0','101','０','true'):
            self.assertEqual(self.post('/enhance/start',{**fields,'noise':noise}).status_code,422)
        duplicate=list(fields.items())+[('noise','25'),('noise','50')]
        self.assertEqual(self.post('/enhance/start',duplicate).status_code,422)
        for noise in (True,25.0,-1,101):
            response=self.http.post('/enhance/start',{**fields,'noise':noise},content_type='application/json',
                HTTP_X_CSRFTOKEN=self.csrf(),HTTP_ACCEPT='application/json',**self.headers)
            self.assertEqual(response.status_code,422)
        self.assertFalse(SynthesisRun.objects.exists());self.assertFalse(PortableJourneyState.objects.exists())
        self.assertEqual(self.post('/enhance/start',{**fields,'noise':'0'}).status_code,303)
        self.assertEqual(PortableJourneyState.objects.get().next_noise,0)

    def test_missing_run_query_never_hides_actual_unknown_attempt(self):
        started=self.start_run(noise=25);inferred=self.confirm_run(started)
        row=self.store.status(inferred['run_ref'])
        reserved=self.store.reserve(str(row.pk),body_digest=row.body_digest,expected_revision=row.revision)
        self.store.mark_unknown(str(reserved.pk))
        for path in ('/cabinet','/enhance'):
            page=self.http.get(path,self.selection(),**self.headers)
            self.assertEqual(page.status_code,200)
            self.assertNotIn('/enhance/start',Forms(page.content).rows)
            self.assertContains(page,'/enhance?run_ref='+str(row.pk))
        self.assertEqual(SynthesisRun.objects.get().attempts,1)
        self.assertEqual(self.transport.calls,0)

    def test_exact_successful_guide_get_head_asset_csp_and_error_routes_stay_separate(self):
        query={'profile_ref':self.profile_ref}
        script=self.boundary.origin+'/cabinet/actions/script.js'
        for method in (self.http.get,self.http.head):
            page=method('/setup/journey',query,**self.headers)
            self.assertEqual(page.status_code,200)
            policy=page['Content-Security-Policy']
            self.assertIn('script-src '+script+';',policy)
            self.assertIn("script-src-attr 'none'",policy)
            self.assertNotIn('connect-src',policy)
            self.assertNotIn("script-src 'self'",policy)
        page=self.http.get('/setup/journey',{'profile_ref':'not-a-uuid'},**self.headers)
        self.assertEqual(page.status_code,422);self.assertNotIn('script-src',page['Content-Security-Policy'])
        other=self.http.get('/setup/journey/not-real',**self.headers)
        self.assertEqual(other.status_code,404);self.assertNotIn('script-src',other['Content-Security-Policy'])
        asset=self.http.get('/cabinet/actions/script.js',**self.headers)
        head=self.http.head('/cabinet/actions/script.js',**self.headers)
        self.assertEqual((asset.status_code,head.status_code),(200,200))
        self.assertTrue(asset.content);self.assertEqual(head.content,b'')
        self.assertEqual(asset['X-Content-Type-Options'],'nosniff')
        self.assertFalse(SynthesisRun.objects.exists());self.assertFalse(PortableJourneyState.objects.exists())
