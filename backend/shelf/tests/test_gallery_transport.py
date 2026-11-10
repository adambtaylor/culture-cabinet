"""A5 cold/warm phase composition under synthetic clocks and fake HTTP only."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shelf import gallery_transport as transport
from shelf.disclosure import read_json
from shelf.enhance_contract import CollectionBudget, EnhanceFault
from shelf.source_adapters import GallerySourceBackend, initial_source_request
from shelf.source_collection import SourceCollection
from shelf.tests.test_native_gallery_metadata import APW, DAY, GalleryClock, Issuer, query
from shelf.tests.test_source_collection import Backend, plan

ROBOTS = b'User-agent: *\nCrawl-delay: 10\nAllow: /'


@contextmanager
def driver(clock, *, robots=ROBOTS, page_status=200, warm=False):
    """Use actual host reservation/cache/robots logic; fake only subprocess I/O."""
    calls = []
    original_open = transport.os.open
    def send(command, **limits):
        url = command[command.index('--url')+1]
        status, body = (200, robots) if url.endswith('/robots.txt') else (page_status, APW)
        calls.append((url, clock.elapsed, limits))
        if limits.get('observation') is not None:
            limits['observation'].update(actual_sends=1, actual_response_bytes=len(body),
                observed_at=clock.now(), body_sha256=sha256(body).hexdigest())
        return (status, body, ()) if limits.get('include_headers') else (status, body)
    with TemporaryDirectory() as directory:
        def private_open(path, *args):
            actual = str(Path(directory)/Path(path).name) if str(path).startswith('/tmp/culture-cabinet-gallery-') else path
            return original_open(actual, *args)
        with patch.object(transport.os, 'open', side_effect=private_open), \
             patch.object(transport.time, 'time', side_effect=lambda: clock.now().timestamp()), \
             patch.object(transport.time, 'monotonic', side_effect=clock.monotonic), \
             patch.object(transport.time, 'sleep', side_effect=AssertionError('Backend cannot sleep ten seconds')), \
             patch.object(transport, 'datetime') as dt, \
             patch.object(transport, '_send', side_effect=send), \
             patch.dict(transport._ROBOTS_CACHE, {}, clear=True):
            dt.now.side_effect = lambda *args: clock.now()
            dt.fromtimestamp.side_effect = datetime.fromtimestamp
            if warm:
                transport.prepare_robots(transport.GalleryOperation('apw'), revalidate=lambda: True)
                clock.sleep(10)
                calls.clear()
            yield calls


class GalleryPhaseTests(TestCase):
    def collect(self, clock, issuer=None, **budget):
        registry, value = query(); issuer = issuer or Issuer()
        backend = GallerySourceBackend('apw', issuer, clock=clock)
        progress = []
        collector = SourceCollection((('gallery', ('apw',)),), {'apw': backend}, clock=clock,
                                     progress=progress.append)
        return collector, collector.collect(value, registry, CollectionBudget(**budget)), progress

    def test_cold_two_distinct_phases_with_collector_wait_and_exact_accounting(self):
        clock = GalleryClock()
        with driver(clock) as calls:
            collector, seal, progress = self.collect(clock)
            self.assertEqual(len(seal.records), 1)
            self.assertEqual([(url, at) for url, at, _ in calls],
                [(transport.APW_URL+'robots.txt', 0), (transport.APW_URL, 10)])
            meta = read_json(seal.source_revisions)
            self.assertEqual(meta['_collection']['initial_sends_reserved'], 2)
            self.assertEqual(meta['_collection']['sends'], 2)
            self.assertEqual(meta['_collection']['response_bytes'], len(ROBOTS)+len(APW))
            receipt = meta['gallery:apw']['gallery_preparation']
            self.assertEqual(receipt['actual_sends'], 1)
            self.assertEqual(receipt['body_sha256'], sha256(ROBOTS).hexdigest())
            self.assertEqual([p['phase'] for p in progress],
                ['preparing_gallery_access', 'waiting_for_source_interval', 'reading_gallery_listings'])
            self.assertTrue(collector.revalidate_collection(seal, *reversed(query())))

    def test_warm_reuse_keeps_original_observation_expiry_and_only_one_page_send(self):
        clock = GalleryClock()
        with driver(clock, warm=True) as calls:
            _, seal, _ = self.collect(clock)
            meta = read_json(seal.source_revisions)
            receipt = meta['gallery:apw']['gallery_preparation']
            self.assertEqual(receipt['actual_sends'], 0)
            self.assertEqual(receipt['actual_response_bytes'], 0)
            self.assertEqual(receipt['observed_at'], DAY.isoformat())
            self.assertEqual(meta['_collection']['initial_sends_reserved'], 2)
            self.assertEqual(meta['_collection']['sends'], 1)
            self.assertEqual([url for url, _, _ in calls], [transport.APW_URL])

    def test_robots_refusal_or_unsupported_delay_is_not_retried_or_a_listing(self):
        for robots in [b'User-agent: *\nDisallow: /', b'User-agent: *\nCrawl-delay: 20\nAllow: /', b'<html>challenge</html>']:
            clock = GalleryClock()
            with driver(clock, robots=robots) as calls:
                _, seal, _ = self.collect(clock)
                self.assertEqual(seal.records, [])
                self.assertEqual(len(calls), 1)
                self.assertTrue(calls[0][0].endswith('/robots.txt'))
                self.assertEqual(read_json(seal.source_revisions)['_collection']['sends'], 1)

    def test_non_gallery_reservation_survives_omitted_gallery_and_small_time_budget(self):
        clock = GalleryClock()
        registry, value = plan(('gallery', 'movie'))
        value = replace(value, expires_at=DAY.replace(hour=1))
        movie = Backend('tmdb_movie', clock, [(200, {'results':[{'id':1,'title':'Fictional film'}]}, ())])
        movie.rights = Issuer().rights
        gallery = GallerySourceBackend('apw', Issuer(), clock=clock)
        with driver(clock) as calls:
            result = SourceCollection((('gallery', ('apw',)), ('movie', ('tmdb_movie',))),
                {'apw':gallery,'tmdb_movie':movie}, clock=clock, isolated_fixture=True).collect(
                    value, registry, CollectionBudget(initial_sends=2))
            self.assertEqual(len(result.records), 1)
            self.assertEqual(calls, [])
            self.assertEqual(next(o for o in result.outcomes if o.category=='gallery').code, 'LIMIT_EXCEEDED')
        clock = GalleryClock()
        with driver(clock) as calls:
            _, result, _ = self.collect(clock, collection_seconds=12)
            self.assertEqual(result.records, [])
            self.assertEqual(len(calls), 1)
            self.assertEqual(clock.elapsed, 0)
        clock = GalleryClock()
        with driver(clock) as calls:
            _, result, _ = self.collect(clock, aggregate_bytes=len(ROBOTS))
            self.assertEqual(result.records, [])
            self.assertEqual(len(calls), 1)
            self.assertEqual(clock.elapsed, 0)
            self.assertEqual(result.outcomes[0].code, 'LIMIT_EXCEEDED')

    def test_preparation_receipt_forgery_or_second_phase_attempt_refuses(self):
        clock=GalleryClock(); issuer=Issuer(); registry,value=query()
        backend=GallerySourceBackend('apw',issuer,clock=clock)
        request=initial_source_request('apw','gallery',())
        with driver(clock):
            permit=backend.authorize(request,value,now=clock.now())
            receipt=backend.prepare_robots(request,permit,response_bytes=262144,request_seconds=5,parent_seconds=6)
            with self.assertRaises(EnhanceFault):
                backend.revalidate_preparation(replace(receipt),request,permit,value,now=clock.now())
            with self.assertRaises(EnhanceFault):
                backend.prepare_robots(request,permit,response_bytes=262144,request_seconds=5,parent_seconds=6)
            clock.sleep(10)
            backend.request(request,permit,response_bytes=262144,request_seconds=5,parent_seconds=6)
            with self.assertRaises(EnhanceFault):
                backend.request(request,permit,response_bytes=262144,request_seconds=5,parent_seconds=6)
            issuer.revoked=True
            with self.assertRaises(EnhanceFault):
                backend.revalidate_preparation(receipt,request,permit,value,now=clock.now())

    def test_competing_same_host_consumer_and_failed_page_cannot_retry(self):
        for failure in (False, True):
            clock=GalleryClock(); registry,value=query(); issuer=Issuer()
            backend=GallerySourceBackend('apw',issuer,clock=clock)
            request=initial_source_request('apw','gallery',())
            with driver(clock, page_status=503 if failure else 200) as calls:
                permit=backend.authorize(request,value,now=clock.now())
                receipt=backend.prepare_robots(request,permit,response_bytes=262144,request_seconds=5,parent_seconds=6)
                clock.sleep(10)
                if not failure:
                    # Another legitimate fixed caller reserves this host slot.
                    transport.read(transport.APW_URL, revalidate=lambda: True)
                    with self.assertRaises(EnhanceFault) as raised:
                        backend.request(request,permit,response_bytes=262144,request_seconds=5,parent_seconds=6)
                    self.assertEqual(raised.exception.code,'RATE_LIMITED')
                else:
                    reply=backend.request(request,permit,response_bytes=262144,request_seconds=5,parent_seconds=6)
                    self.assertEqual(reply.status,503)
                with self.assertRaises(EnhanceFault):
                    backend.request(request,permit,response_bytes=262144,request_seconds=5,parent_seconds=6)
                self.assertEqual(len(calls),2)

    def test_policy_or_robots_expiry_and_revocation_between_phases_deny_page(self):
        for kind in ['expiry','revocation','changed_policy']:
            clock=GalleryClock(); registry,value=query(); issuer=Issuer()
            backend=GallerySourceBackend('apw',issuer,clock=clock)
            request=initial_source_request('apw','gallery',())
            with driver(clock) as calls:
                permit=backend.authorize(request,value,now=clock.now())
                receipt=backend.prepare_robots(request,permit,response_bytes=262144,request_seconds=5,parent_seconds=6)
                if kind=='expiry':
                    clock.sleep(600)
                elif kind=='revocation':
                    issuer.revoked=True
                else:
                    issuer.rights=replace(issuer.rights,revision=issuer.rights.revision+1)
                with self.assertRaises(EnhanceFault):
                    backend.request(request,permit,response_bytes=262144,request_seconds=5,parent_seconds=6)
                self.assertEqual(len(calls),1)

    def test_failed_page_still_charges_received_bytes_after_the_currentness_guard(self):
        clock=GalleryClock()
        with driver(clock,page_status=403) as calls:
            _,result,_=self.collect(clock)
            self.assertEqual(result.records,[])
            meta=read_json(result.source_revisions)['_collection']
            self.assertEqual(meta['sends'],2)
            self.assertEqual(meta['response_bytes'],len(ROBOTS)+len(APW))
            self.assertEqual(len(calls),2)
