"""Visual artist statements remain distinct, unresolved and purpose-filtered."""
import uuid
from shelf import grant_authority as ga,profile_snapshot,disclosure
from shelf.category_registry import KEYS,CATEGORIES
from shelf.models import FeedbackEvent,FeedbackTarget,PortableJourneyCommand
from shelf.portable_journey import PortableJourney
from shelf.portable_taste_inputs import PortableTasteInputs,VISUAL_ARTIST_NAMESPACE
from .test_portable_feedback_views import NativeFeedbackCase


class VisualArtistIntakeTests(NativeFeedbackCase):
    def test_native_save_reload_clear_keeps_original_name_identity_and_separate_music(self):
        journey=PortableJourney(self.authority)
        initial=journey.project(self.ref,profile_ref=self.profile_ref)
        visual=next(row for row in initial['favourite_categories'] if row['category']=='visual_artist')
        self.assertEqual(len(visual['slots']),5)
        self.assertEqual(len(initial['categories']),13)
        fields={**visual['slots'][0]['actions']['add']['fields'],'title':'  Fictional Artist 雪  '}
        self.assertEqual(self.post('/cabinet/journey/favourites/add',fields).status_code,303)
        self.assertEqual(self.post('/cabinet/journey/favourites/add',fields).status_code,303)
        projected=journey.project(self.ref,profile_ref=self.profile_ref)
        visual=next(row for row in projected['favourite_categories'] if row['category']=='visual_artist')
        self.assertEqual(visual['slots'][0]['title'],'  Fictional Artist 雪  ')
        journey.step(self.ref,profile_ref=self.profile_ref,journey_ref=projected['journey_ref'],
            expected_revision=projected['revision'],step='providers',action='skip',command_id=str(uuid.uuid4()))
        page=self.http.get('/setup/journey',{'profile_ref':self.profile_ref},**self.headers)
        self.assertEqual(page.status_code,200)
        self.assertContains(page,'Fictional Artist 雪')
        self.assertContains(page,'Music artists')
        self.assertContains(page,'Visual artists')
        projected=journey.project(self.ref,profile_ref=self.profile_ref)
        visual=next(row for row in projected['favourite_categories'] if row['category']=='visual_artist')
        target=FeedbackTarget.objects.get(source_namespace=VISUAL_ARTIST_NAMESPACE)
        self.assertEqual(target.category,'visual_artist')
        music=next(row for row in projected['favourite_categories'] if row['category']=='artist')
        self.assertTrue(all(slot['title']=='' for slot in music['slots']))
        remove=visual['slots'][0]['actions']['remove']['fields']
        self.assertEqual(self.post('/cabinet/journey/favourites/remove',remove).status_code,303)
        self.assertEqual(list(FeedbackEvent.objects.order_by('revision').values_list('body',flat=True)),
            [{'value':True},None])
        self.assertEqual(len(KEYS),13)
        self.assertEqual({row.group for row in CATEGORIES if row.key in ('gallery','art_fair')},{'art'})

    def test_five_statement_bound_and_normalized_duplicate_do_not_create_false_person_identity(self):
        inputs=PortableTasteInputs(self.authority)
        base=dict(mutation_id=str(uuid.uuid4()),base_revision=0,category='visual_artist',entry_ref=str(uuid.uuid4()))
        inputs.mutate_favourite(self.ref,self.profile_ref,**base,title='  Fictional  Café  ')
        with self.assertRaises(ga.AuthorityFault):inputs.mutate_favourite(self.ref,self.profile_ref,
            **{**base,'entry_ref':str(uuid.uuid4()),'mutation_id':str(uuid.uuid4())},title='FICTIONAL Cafe\u0301')
        for number in range(4):
            inputs.mutate_favourite(self.ref,self.profile_ref,**{**base,'entry_ref':str(uuid.uuid4()),
                'mutation_id':str(uuid.uuid4())},title='Other fictional artist '+str(number))
        with self.assertRaises(ga.AuthorityFault):inputs.mutate_favourite(self.ref,self.profile_ref,
            **{**base,'entry_ref':str(uuid.uuid4()),'mutation_id':str(uuid.uuid4())},title='Sixth fictional artist')
        self.assertEqual(FeedbackEvent.objects.count(),5)
        self.assertTrue(all(target.source_namespace==VISUAL_ARTIST_NAMESPACE for target in FeedbackTarget.objects.all()))

    def test_permitted_visual_name_enters_personal_evidence_but_not_music_or_gallery_queries(self):
        inputs=PortableTasteInputs(self.authority)
        inputs.mutate_favourite(self.ref,self.profile_ref,entry_ref=str(uuid.uuid4()),mutation_id=str(uuid.uuid4()),
            base_revision=0,category='visual_artist',title='A fictional visual artist')
        self.consent(self.profile_ref,purpose='catalogue_lookup',classes=['explicit_favourites','unresolved_personal_text'])
        view=ga.current_view(self.authority,self.ref,profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk),purpose='catalogue_lookup')
        snapshot=profile_snapshot.capture(view)
        self.assertEqual(snapshot['evidence'][0]['identity_status'],'unresolved_personal')
        self.assertEqual(snapshot['evidence'][0]['medium'],'visual_artist')
        body,_=disclosure.catalogue_input(snapshot,view.grants,view.context,KEYS,now=self.authority.now())
        projected=disclosure.read_json(body)
        self.assertEqual(projected['profile']['signals'][0]['title'],'A fictional visual artist')
        self.assertTrue(all(row['terms']==[] for row in projected['queries']))
        from shelf.source_authority import configure_source,current_binding
        source=configure_source(self.authority,self.ref,confirmed=True,adapter='musicbrainz_artist')
        self.consent(self.profile_ref,purpose='catalogue_lookup',connection_ref=source,
            classes=['explicit_favourites','unresolved_personal_text'])
        music_view=current_binding(self.authority,self.ref,profile_ref=self.profile_ref,connection_ref=source)
        body,_=disclosure.catalogue_input(profile_snapshot.capture(music_view),music_view.grants,music_view.context,
            KEYS,now=self.authority.now())
        self.assertTrue(all(row['terms']==[] for row in disclosure.read_json(body)['queries']))


class GalleryNativeAuthorityTests(NativeFeedbackCase):
    def test_native_gallery_config_grant_cold_preview_confirm_and_collection_keep_fixed_phases(self):
        from dataclasses import replace
        from datetime import timedelta
        from hashlib import sha256
        from types import SimpleNamespace
        from unittest.mock import patch
        from shelf.source_authority import SourceAuthority,SourcePolicyProvider,SourceRights,current_binding
        from shelf.source_adapters import GallerySourceBackend,initial_source_request
        from shelf.source_collection import SourceCollection
        from shelf.enhance_coordinator import EnhanceCoordinator
        from shelf.enhance_contract import CollectionBudget,EnhanceFault
        from shelf.gallery_transport import RobotsPreparation
        from shelf.models import PortableConnection
        from .test_source_evidence import policy
        from .test_native_gallery_metadata import APW
        page='/cabinet/source/gallery/apw'
        configured=self.post(page+'/configure',dict(profile_ref=self.profile_ref,expected_revision=0,confirmed='yes'))
        self.assertEqual(configured.status_code,303)
        self.assertEqual(self.http.get(configured['Location'],**self.headers).status_code,200)
        row=PortableConnection.objects.get(adapter='apw')
        self.assertEqual((row.credential_generation,row.validation_generation),(0,0))
        consent=self.post(page+'/consent',dict(profile_ref=self.profile_ref,source_connection_ref=str(row.pk),
            expected_revision=0,action='grant',confirmed='yes',allow_canonical_seed='yes'))
        self.assertEqual(consent.status_code,303)
        properties=('gallery.artist_name.0','venue.address.country','venue.address.postcode','venue.latitude','venue.longitude')
        rights=SourceRights(policy(facts=properties),1,'fixture-gallery-terms','fixture-gallery-privacy',
            'fixture-gallery-locale','CultureCabinet/fixture (example.invalid)','fixture-gallery-processor',source='apw')
        class Provider(SourcePolicyProvider):
            def current(inner,*args,**kwargs):return rights
        issuer=SourceAuthority(self.authority,self.ref,profile_ref=self.profile_ref,connection_ref=str(row.pk),
                               policy_provider=Provider())
        view=current_binding(self.authority,self.ref,profile_ref=self.profile_ref,connection_ref=str(row.pk))
        plan=EnhanceCoordinator(None,now=self.authority.now).plan(view,self.enhance.registry,run_ref=str(uuid.uuid4()),
            client_key=str(uuid.uuid4()),refresh_nonce=str(uuid.uuid4())).plan
        preview=issuer.preview(plan)
        self.assertEqual(preview['body'],'');self.assertEqual(len(preview['phases']),2)
        self.assertEqual(preview['phases'][0]['url'],'https://www.australianprintworkshop.com/robots.txt')
        self.assertEqual(preview['phases'][1]['url'],'https://www.australianprintworkshop.com/')
        request=initial_source_request('apw','gallery',())
        with self.assertRaises(EnhanceFault):issuer.operation_for(request,plan,now=self.authority.now())
        issuer.confirm(preview['preview_ref'],plan_digest=preview['plan_digest'],
            operation_digest=preview['operation_digest'],confirmed=True)
        clock=SimpleNamespace(now=self.authority.now,monotonic=self.authority.monotonic,sleep=self.clock.advance)
        backend=GallerySourceBackend('apw',issuer,clock=clock)
        collection=SourceCollection((('gallery',('apw',)),),{'apw':backend},clock=clock)
        phases=[];robots=b'User-agent: *\nAllow: /\n'
        def prepare(operation,**kwargs):
            self.assertTrue(kwargs['revalidate']());phases.append(operation.robots_url)
            now=self.authority.now()
            return RobotsPreparation('ready',now,now+timedelta(seconds=300),sha256(robots).hexdigest(),
                1,len(robots),now+timedelta(seconds=10))
        def fetch(operation,**kwargs):
            self.assertTrue(kwargs['revalidate']());phases.append(operation.url)
            return 200,APW.replace(b'November',b'January').replace(b'Friday 6 January',b'Tuesday 6 January'),(('cache-control','max-age=60'),)
        read_failures=[];original_read=collection._read
        def observed_read(*args,**kwargs):
            try:return original_read(*args,**kwargs)
            except Exception as error:
                import traceback
                read_failures.append((type(error).__name__,getattr(error,'code',''),
                    [(frame.name,frame.lineno) for frame in traceback.extract_tb(error.__traceback__)]))
                raise
        with patch.object(collection,'_read',side_effect=observed_read), \
             patch('shelf.gallery_transport.prepare_robots',side_effect=prepare), \
             patch('shelf.gallery_transport.preparation_current',return_value=True), \
             patch('shelf.gallery_transport.gallery_authorized',side_effect=fetch):
            seal=collection.collect(plan,self.enhance.registry,CollectionBudget())
        outcome=next(row for row in seal.outcomes if row.category=='gallery')
        self.assertEqual((outcome.state,outcome.candidate_count),('fresh',1),str((outcome,read_failures,phases)))
        self.assertEqual(phases,[preview['phases'][0]['url'],preview['phases'][1]['url']])
        self.assertEqual(len(seal.records),1)
        self.assertEqual(seal.records[0]['candidate']['category'],'gallery')
        self.assertNotIn('goompi',' '.join(phases))
        normal_provider,normal_collection=self.enhance._normal_context(self.ref,self.profile_ref)
        self.assertIn('apw',normal_collection.backends)
        self.assertIsInstance(normal_collection.backends['apw'],GallerySourceBackend)
