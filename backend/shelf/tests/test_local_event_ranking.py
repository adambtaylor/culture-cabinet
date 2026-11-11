"""Post-validation local soft ranking and attributable owner-only overlays."""
from copy import deepcopy
from datetime import datetime,timezone
from unittest import TestCase
from shelf.local_event_preferences import LocalEventContext
from shelf.local_event_ranking import display,rank_key
from shelf.postal_locality import LocalReference
from shelf.enhance_selection import GroundedSelector
from shelf.enhance_contract import CategoryOutcome
from .test_enhance_selection import GroundedSelectionTests


class LocalEventRankingTests(TestCase):
    def context(self):
        return LocalEventContext('11111111-1111-4111-8111-111111111111',1,'fixture-dataset',
            LocalReference(0,0,'fixture_coordinate',('fixture-reference',),1),10,'a'*64,'mapped')

    def material(self):
        submitted,output,_=GroundedSelectionTests().material()
        cards=submitted['candidates']
        for card in cards:card.update(category='event',medium='event')
        cards[0]['key']='far';cards[1]['key']='near';cards[2]['key']='unknown'
        for card,latitude in ((cards[0],'1'),(cards[1],'0.01')):
            card['facts']=[dict(id=card['key']+':'+key,property=key,value=value,observation_ref='fixture-reference')
                for key,value in (('venue.latitude',latitude),('venue.longitude','0'))]
        for card,assessment in zip(cards,output['candidate_assessments']):assessment['candidate_key']=card['key']
        return submitted,output,(CategoryOutcome('event','fresh',3),)

    def test_same_tier_prefers_near_unknown_far_without_dropping_any_candidate(self):
        submitted,output,outcomes=self.material();context=self.context()
        original=deepcopy(submitted)
        result=GroundedSelector().build(output,submitted,outcomes,local_context=context)
        self.assertEqual([card['key'] for card in result['categories'][0]['cards']],['near','unknown','far'])
        self.assertEqual(submitted,original)
        output['candidate_assessments'][1]['decision']='uncertain'
        separated=GroundedSelector().build(output,submitted,outcomes,local_context=context)
        self.assertEqual([card['key'] for card in separated['categories'][0]['cards']],['unknown','far','near'])
        excluded=GroundedSelector().build(output,submitted,outcomes,local_context=context,
            snapshot={'hard_exclusions':['near'],'seen_keys':[],'hard_constraints':[]})
        self.assertEqual({card['key'] for card in excluded['categories'][0]['cards']},{'unknown','far'})

    def test_other_categories_and_unset_location_keep_baseline_order(self):
        submitted,output,outcomes=GroundedSelectionTests().material()
        baseline=GroundedSelector().build(output,submitted,outcomes)
        self.assertEqual(GroundedSelector().build(output,submitted,outcomes,local_context=self.context()),baseline)
        unset=LocalEventContext(self.context().profile_ref,0,'fixture-dataset',None,None,'b'*64,'unset')
        submitted,output,outcomes=self.material()
        self.assertEqual(GroundedSelector().build(output,submitted,outcomes,local_context=unset),
            GroundedSelector().build(output,submitted,outcomes))
        self.assertEqual(rank_key(self.context(),submitted['candidates'][2]),(1,0))

    def test_display_binds_revision_unknown_distance_and_never_verifies_a_name_match(self):
        submitted,_,_=self.material();card=submitted['candidates'][1];card['category']='gallery'
        values={'gallery.artist_name.0':'Fictional Cafe\u0301','gallery.opening_starts_at':'2026-11-01T19:00:00+11:00',
            'gallery.exhibition_starts_on':'2026-11-01','gallery.exhibition_ends_on':'2026-11-30',
            'gallery.timezone':'Australia/Melbourne','gallery.date_precision':'date_only'}
        card['facts'] += [dict(id='fact:'+key,property=key,value=value,observation_ref='fixture-reference')
            for key,value in values.items()]
        arguments=dict(publication_ref='22222222-2222-4222-8222-222222222222',publication_revision=3,
            now=datetime(2026,11,11,10,tzinfo=timezone.utc))
        rendered=display(self.context(),card,**arguments,permitted_visual_names=('FICTIONAL Café',))
        self.assertEqual((rendered['publication_revision'],rendered['location_revision']),(3,1))
        self.assertEqual(rendered['radius_band'],'in_radius')
        self.assertEqual(rendered['opening_status'],'current_exhibition')
        self.assertEqual(rendered['artist_match_status'],'name_match_unconfirmed')
        self.assertIn('postcode reference',rendered['distance_label'])
        self.assertEqual(display(self.context(),card,**arguments)['artist_match_status'],'none')
        unknown=display(self.context(),submitted['candidates'][2],**arguments)
        self.assertEqual((unknown['distance_basis'],unknown['radius_band']),('unknown','unknown'))
        self.assertEqual(unknown['distance_label'],'Distance unknown.')
