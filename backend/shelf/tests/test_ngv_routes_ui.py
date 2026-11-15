"""Closed route wiring on real handlers; fictional descriptor, never NGV parsing."""
from dataclasses import replace
from unittest.mock import patch

from django.test import TestCase, override_settings
from shelf import discovery_views as views, event_source_registry as registry
from shelf import discovery_sources, event_scraping
from shelf.event_sources import ListingEvent
from shelf.models import DiscoveryState
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests
from shelf.tests.test_discovery_transport import NOW


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class NGVRouteUITests(TestCase):
    setUp = PublicDiscoveryUITests.setUp
    form = PublicDiscoveryUITests.form

    def fixture(self, *, failure_route=None, revoke=False):
        calls = []
        def acquire(now, *, event_route):
            self.assertEqual(now, NOW);calls.append(event_route)
            if revoke:
                self.grant.can_select = False;self.grant.save()
            if event_route == failure_route:
                raise TimeoutError('CANARY upstream exception')
            return 200, b'fictional descriptor transport bytes'
        card = ListingEvent('official:event:ngv-fixture-occurrence', 'FICTIONAL NGV route wiring only',
            'event', 'NGV', 'live-automated', 1, NOW.isoformat(), category='gallery',
            source_id='ngv', url='https://www.ngv.vic.gov.au/program/fictional-session/',
            official_url='https://www.ngv.vic.gov.au/program/fictional-session/')
        def parse(raw, *, now, retrieved_at, event_route):
            self.assertEqual(raw,b'fictional descriptor transport bytes')
            self.assertEqual(retrieved_at,NOW.isoformat())
            self.assertIn(event_route,('programs','exhibitions'))
            return (card,)
        descriptors = dict(registry.SOURCES)
        descriptors['ngv'] = replace(descriptors['ngv'], acquire=acquire, parse=parse,
            coverage='FICTIONAL route plumbing only; no source-format proof', retention_seconds=900)
        self.enterContext(patch.object(registry,'SOURCES',descriptors))
        self.enterContext(patch.object(views,'EVENT_SOURCES',descriptors))
        return calls,card

    def test_default_route_replay_and_explicit_route_are_bound_to_whole_input(self):
        calls,_ = self.fixture()
        data = self.form('event_source',source_id='ngv')
        self.assertEqual(self.client.post(self.action,data).status_code,302)
        self.assertEqual(self.client.post(self.action,data).status_code,302)
        self.assertEqual(self.client.post(self.action,dict(data,event_route='programs')).status_code,409)
        self.assertEqual(self.client.post(self.action,dict(data,event_route='exhibitions')).status_code,409)
        self.client.get(self.url)
        self.assertEqual(calls,['programs'])
        absent = views.ActionForm(data);self.assertTrue(absent.is_valid())
        self.assertNotIn('event_route',absent.cleaned_data)

    def test_route_failure_keeps_other_route_and_replay_get_do_not_acquire(self):
        calls,_ = self.fixture(failure_route='exhibitions')
        self.client.post(self.action,self.form('event_source',source_id='ngv'))
        failure = self.form('event_source',source_id='ngv',event_route='exhibitions')
        self.client.post(self.action,failure);self.client.post(self.action,failure)
        page = self.client.get(self.url)
        outcomes = {row['event_route']:row['outcome'].status for row in page.context['public_results']}
        self.assertEqual(outcomes,{'programs':'ready','exhibitions':'timeout'})
        self.assertContains(page,'NGV · Programs &amp; events')
        self.assertContains(page,'NGV · Exhibition runs')
        self.assertNotContains(page,'CANARY')
        self.assertEqual(calls,['programs','exhibitions'])
        self.assertEqual(DiscoveryState.objects.get().history,[])

    def test_same_occurrence_from_both_indexes_has_one_response_identity(self):
        calls,card = self.fixture()
        for route in ('programs','exhibitions'):
            self.client.post(self.action,self.form('event_source',source_id='ngv',event_route=route))
        page = self.client.get(self.url)
        rows = page.context['events']+page.context['more_source_events']
        self.assertEqual(sum(row['card'].key==card.key for row in rows),1)
        self.assertEqual(calls,['programs','exhibitions'])

    def test_unsupported_routes_or_other_operations_refuse_before_acquisition(self):
        calls,_ = self.fixture()
        for changes in ({'event_route':'archive'}, {'event_route':'https://evil.invalid'},
            {'event_route':['programs','exhibitions']},
            {'source_id':'pbs','event_route':'programs'},
            {'operation':'undo','source_id':'','event_route':'programs'}):
            self.assertEqual(self.client.post(self.action,
                dict(self.form('event_source',source_id='ngv'),**changes)).status_code,400)
        self.assertEqual(calls,[])

    def test_live_descriptor_remains_unavailable_and_controls_are_metadata_only(self):
        with patch.object(discovery_sources,'fetch_public',wraps=discovery_sources.fetch_public) as fetch:
            for route in ('programs','exhibitions'):
                self.client.post(self.action,self.form('event_source',source_id='ngv',event_route=route))
            page = self.client.get(self.url)
        self.assertEqual(fetch.call_count,2)
        self.assertEqual([row['outcome'].status for row in page.context['public_results']],['unavailable']*2)
        control=next(row for row in page.context['event_sources'] if row['source_id']=='ngv')
        self.assertFalse(control['implemented'])
        self.assertEqual([row['event_route'] for row in control['event_routes']],['programs','exhibitions'])
        self.assertEqual(event_scraping.collect('pbs',now=NOW,event_route='programs').status,'invalid')

    def test_revocation_during_route_acquisition_cannot_publish_result(self):
        calls,_ = self.fixture(revoke=True)
        self.assertEqual(self.client.post(self.action,
            self.form('event_source',source_id='ngv',event_route='programs')).status_code,404)
        self.assertEqual(calls,['programs'])
        self.assertEqual(views.PUBLIC_CACHE._cache,{})

    def test_nonstring_route_in_internal_caller_cannot_become_default(self):
        calls,_ = self.fixture()
        for value in (None,[],False):
            result=discovery_sources.fetch_public('event_source','','',NOW,source_id='ngv',event_route=value)
            self.assertEqual(result.status,'invalid')
        self.assertEqual(calls,[])
