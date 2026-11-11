"""Actual local setting writers and pinned local index; zero provider work."""
import uuid
from django.db import DatabaseError,transaction
from shelf import grant_authority as ga,profile_snapshot,disclosure
from shelf.models import PortableJourneyState,FeedbackEvent,PortableJourneyCommand
from shelf.portable_journey import PortableJourney
from shelf.local_event_preferences import current_context,location_projection,validate_setting
from .test_portable_feedback_views import NativeFeedbackCase


class LocalEventPreferenceTests(NativeFeedbackCase):
    def initial(self):
        journey=PortableJourney(self.authority)
        projection=journey.project(self.ref,profile_ref=self.profile_ref)
        return journey,dict(profile_ref=self.profile_ref,journey_ref=projection['journey_ref'],
            expected_revision=0,command_id=str(uuid.uuid4()))

    def test_leading_zero_default_explicit_save_skip_remove_cas_and_native_post(self):
        journey,common=self.initial()
        before=current_context(self.state)
        saved=journey.location(self.ref,**common,action='save',country='AU',postcode=' 0800 ')
        row=PortableJourneyState.objects.get()
        self.assertEqual((row.home_country,row.home_postcode,row.radius_km,row.location_revision),('AU','0800',25,1))
        self.assertEqual(journey.location(self.ref,**common,action='save',country='AU',postcode=' 0800 '),saved)
        mapped=current_context(self.state)
        self.assertEqual(mapped.state,'mapped');self.assertIsNotNone(mapped.reference)
        self.assertNotEqual(mapped.digest,before.digest)
        skipped=journey.location(self.ref,**{**common,'expected_revision':1,'command_id':str(uuid.uuid4())},action='skip')
        row.refresh_from_db();self.assertEqual((row.home_postcode,row.location_revision),('0800',1))
        self.assertEqual(current_context(self.state).digest,mapped.digest)
        fields={**common,'expected_revision':skipped['revision'],'command_id':str(uuid.uuid4()),'action':'remove'}
        self.assertEqual(self.post('/cabinet/journey/location',fields).status_code,303)
        row.refresh_from_db();self.assertEqual((row.home_postcode,row.radius_km,row.location_revision),(None,None,2))
        self.assertFalse(FeedbackEvent.objects.exists())
        with self.assertRaises(ga.AuthorityFault):journey.location(self.ref,**common,action='remove')

    def test_invalid_shapes_preserve_setting_and_unmapped_is_a_valid_local_choice(self):
        journey,common=self.initial()
        for changes in (dict(country='NZ',postcode='0800'),dict(country='AU',postcode='０８００'),
            dict(country='AU',postcode='800'),dict(country='AU',postcode='0800',radius_km=0),
            dict(country='AU',postcode='0800',radius_km=501),dict(country='AU',postcode='0800',radius_km=True)):
            with self.assertRaises(ga.AuthorityFault):journey.location(self.ref,**common,action='save',**changes)
        self.assertFalse(PortableJourneyState.objects.exists())
        journey.location(self.ref,**common,action='save',country='AU',postcode='0000',radius_km=500)
        self.assertEqual(current_context(self.state).state,'unmapped')
        with self.assertRaises(DatabaseError),transaction.atomic(),ga._write_window():
            PortableJourneyState.objects.update(home_postcode='0800')
        self.assertEqual(PortableJourneyState.objects.get().home_postcode,'0000')

    def test_local_settings_do_not_change_snapshot_or_catalogue_bytes_and_change_publish_binding(self):
        from shelf.current_profile import CurrentProfile
        journey,common=self.initial()
        self.consent(self.profile_ref)
        self.consent(self.profile_ref,purpose='catalogue_lookup')
        from .test_inference_body import InferenceBodyTests
        from shelf import inference_body
        record=InferenceBodyTests.record(self)
        collection=self.disclosure.fixture_collection([record],
            {'observation:1':{'revision':7,'digest':'c'*64}},{'policy:1':{'revision':3}},expires_at=self.expires)
        def read():
            view=ga.current_view(self.authority,self.ref,profile_ref=self.profile_ref,
                connection_ref=str(self.config.pk),purpose='catalogue_lookup')
            snapshot=profile_snapshot.capture(view)
            body,_=disclosure.catalogue_input(snapshot,view.grants,view.context,self.enhance.registry.categories,
                                              now=self.authority.now())
            record=CurrentProfile(self.authority).current(self.authority,self.ref,profile_ref=self.profile_ref,
                connection_ref=str(self.config.pk)).record()
            personal_view=ga.current_view(self.authority,self.ref,profile_ref=self.profile_ref,
                connection_ref=str(self.config.pk),purpose='inference')
            personal=inference_body.serialize(profile_snapshot.capture(personal_view),personal_view.grants,
                personal_view.context,collection,now=self.authority.now()).body
            return snapshot,body,record,personal
        first=read()
        saved=journey.location(self.ref,**common,action='save',country='AU',postcode='0800')
        second=read()
        self.assertEqual(first[:2],second[:2]);self.assertEqual(first[3],second[3])
        self.assertEqual(first[2]['bindings'],second[2]['bindings'])
        self.assertNotEqual(first[2]['local_selection_digest'],second[2]['local_selection_digest'])
        journey.location(self.ref,**{**common,'expected_revision':saved['revision'],'command_id':str(uuid.uuid4())},
            action='save',country='AU',postcode='3000',radius_km=100)
        third=read();self.assertEqual(first[:2],third[:2]);self.assertEqual(first[3],third[3])
        projection=journey.project(self.ref,profile_ref=self.profile_ref)['location']
        self.assertEqual((projection['postcode'],projection['radius_km'],projection['state']),('3000',100,'mapped'))
        self.assertIn('GeoNames',projection['attribution'])

    def test_normal_six_step_location_form_save_reload_error_and_optional_prepare(self):
        from shelf.models import PortableSourceCheckAttempt
        journey,common=self.initial()
        first=journey.step(self.ref,**common,step='providers',action='skip')
        second=journey.step(self.ref,**{**common,'expected_revision':first['revision'],
            'command_id':str(uuid.uuid4())},step='favourites',action='skip')
        page=self.http.get('/setup/journey',{'profile_ref':self.profile_ref},**self.headers)
        self.assertEqual(page.status_code,200)
        self.assertContains(page,'data-journey-step="events_location"')
        self.assertContains(page,'/cabinet/journey/location')
        result=self.post('/cabinet/journey/location',{**common,'expected_revision':second['revision'],
            'command_id':str(uuid.uuid4()),'action':'save','country':'AU','postcode':'0800'})
        self.assertEqual(result.status_code,303)
        reloaded=self.http.get(result['Location'],**self.headers)
        self.assertEqual(reloaded.status_code,200)
        self.assertContains(reloaded,'data-journey-step="prepare"')
        self.assertContains(reloaded,'0800')
        self.assertContains(reloaded,'GeoNames')
        row=PortableJourneyState.objects.get()
        refused=self.post('/cabinet/journey/location',{**common,'expected_revision':row.revision,
            'command_id':str(uuid.uuid4()),'action':'save','country':'AU','postcode':'０８００'})
        self.assertEqual(refused.status_code,422)
        row.refresh_from_db();self.assertEqual((row.home_postcode,row.radius_km),('0800',25))
        self.assertFalse(FeedbackEvent.objects.exists())
        self.assertFalse(PortableSourceCheckAttempt.objects.exists())


from .test_portable_feedback_views import NativeFeedbackOutcomeTests


class LocalPublicationTests(NativeFeedbackOutcomeTests):
    def test_actual_local_publication_native_overlay_and_location_drift_refuse_old_publish_and_display(self):
        from shelf.disclosure import canonical_bytes
        from shelf.enhance_contract import EnhanceIntent
        from shelf.models import PortableCabinetManifest
        self.consent(self.profile_ref)
        journey=PortableJourney(self.authority);initial=journey.project(self.ref,profile_ref=self.profile_ref)
        common=dict(profile_ref=self.profile_ref,journey_ref=initial['journey_ref'],expected_revision=0,
                    command_id=str(uuid.uuid4()))
        saved=journey.location(self.ref,**common,action='save',country='AU',postcode='3000')
        store=self.enhance._store(self.ref,self.profile_ref,str(self.config.pk))
        current=store.current()
        row=store.admit(EnhanceIntent(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4()),current.bindings),
                        local_context=current.local_context)
        facts={'venue.address.country':'AU','venue.address.postcode':'3000','gallery.opening_starts_at':'2026-01-05T19:00:00+11:00',
               'gallery.exhibition_starts_on':'2026-01-01','gallery.exhibition_ends_on':'2026-01-31',
               'gallery.timezone':'Australia/Melbourne','gallery.date_precision':'date_only'}
        card=dict(key='fixture:gallery:1',category='gallery',title='Fictional permitted exhibition',medium='gallery',
            provider='fixture',source_id='1',source_url='https://example.invalid/gallery/1',
            identity_status='fixture_identity_only',evidence_type='fixture_not_live',
            observed_at=self.clock.wall.isoformat(),valid_until=self.expires.isoformat(),
            rights_ref='fixture-no-durable-rights',source_roots=['fixture-local-evidence'],facts=[
                dict(id='fact:'+key,property=key,value=value,observation_ref='fixture-local-evidence')
                for key,value in facts.items()])
        outcomes=[dict(category=k,state='fresh' if k=='gallery' else 'unavailable',candidate_count=int(k=='gallery'),
            code='OK' if k=='gallery' else 'CAPABILITY_UNAVAILABLE') for k in self.enhance.registry.categories]
        row=store.checkpoint(str(row.pk),stage='awaiting_preview',outcomes=outcomes,
            metadata={'phase':'inference','submitted_refs':{'signals':[],'candidates':[
                {'key':card['key'],'evidence_refs':card['source_roots']+[fact['id'] for fact in card['facts']]}]}},
            collection_digest='c'*64,body_digest='b'*64,sealed_run_digest='d'*64)
        row=store.reserve(str(row.pk),body_digest=row.body_digest,expected_revision=row.revision)
        store.record_result(str(row.pk),canonical_bytes(dict(schema='cabinet-inference-result/v1',associations=None,
            candidate_assessments=[dict(candidate_key=card['key'],decision='uncertain',signal_keys=[],
                evidence_refs=card['source_roots'],hypothesis='Synthetic local publication only.')])))
        manifest=dict(schema='cabinet-manifest/v1',categories=[dict(category=k,state=value['state'],
            code=value['code'],cards=[card] if k=='gallery' else []) for k,value in
            zip(self.enhance.registry.categories,outcomes)],for_you=[])
        publication=store.publish(str(row.pk),manifest=manifest,expected_publication_revision=0,allowed_candidates=[card])
        current=self.enhance.current_for_interaction(self.ref,profile_ref=self.profile_ref)
        overlay=current['local_event_display'][card['key']]
        self.assertEqual((overlay['publication_ref'],overlay['publication_revision']), (str(publication.pk),1))
        self.assertEqual((overlay['distance_basis'],overlay['radius_band']),('postcode','in_radius'))
        page,context=self.landing(profile_ref=self.profile_ref,category='gallery')
        self.assertEqual(page.status_code,200)
        rendered=next(group for group in context['categories'] if group['category']=='gallery')['cards'][0]
        self.assertEqual(rendered['local_event_display']['presentation_schema'],'local-event-display-presentation/v1')
        self.assertContains(page,overlay['distance_label']);self.assertContains(page,'2026-01-31')
        self.assertNotIn(b'Artist identity verified',page.content)
        journey.location(self.ref,**{**common,'expected_revision':saved['revision'],'command_id':str(uuid.uuid4())},
                         action='save',country='AU',postcode='0800',radius_km=100)
        with self.assertRaises(ga.AuthorityFault):
            store.publish(str(row.pk),manifest=manifest,expected_publication_revision=0,allowed_candidates=[card])
        with self.assertRaises(ga.AuthorityFault):
            self.enhance.current_for_interaction(self.ref,profile_ref=self.profile_ref)
        self.assertIsNotNone(PortableCabinetManifest.objects.get().content_redacted_at)
