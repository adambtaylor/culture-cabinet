"""Synthetic native gallery composition; no real gallery request or owner taste."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

from shelf import gallery_transport as transport
from shelf.discovery_sources import SourceError
from shelf.enhance_contract import CollectionBudget, EnhanceFault
from shelf.gallery_sources import native_gallery_cards, parse_apw
from shelf.source_adapters import GalleryBackend, SourceAuthorityIssuer, SourcePermit
from shelf.source_collection import SourceCollection
from shelf.tests.test_source_collection import Clock, plan
from shelf.tests.test_source_evidence import policy

DAY = datetime(2026, 11, 11, tzinfo=timezone.utc)
APW = b'''<div class="sidebar__hero"><p class="sidebar__hero-text">Australian Print Workshop New Exhibition</p>
<a class="sidebar__hero-action">Opening 6pm. Friday 6 November 2026</a>
<p class="sidebar__hero-dates">7 November - 30 November 2026</p>
<span itemprop="artist">Fictional Participant</span>
<a class="exhibition-artist" href="https://www.australianprintworkshop.com/artists/fictional">Another Participant</a>
<p>General prose names must not be participants: Unattributed Name.</p>
<address><span itemprop="addressCountry">AU</span><span itemprop="postalCode">3065</span>
<span itemprop="addressLocality">Fitzroy</span><span itemprop="addressRegion">VIC</span>
<meta itemprop="latitude" content="-37.80"><meta itemprop="longitude" content="144.97"></address></div>'''


class GalleryClock(Clock):
    def now(self):
        return DAY + timedelta(seconds=self.elapsed)


class Issuer(SourceAuthorityIssuer):
    """Trusted synthetic issuer exercises the REAL backend/collector port only.

    This is not a native owner/session/SourceAuthority acceptance claim.
    """
    def __init__(self):
        self.rights = replace(policy(), checked_at=DAY-timedelta(seconds=1), expires_at=DAY+timedelta(seconds=900))
        self.revoked = False

    def operation_for(self, request, plan, *, now):
        return transport.GalleryOperation(request.source)

    def authorize(self, request, plan, operation, *, now):
        return SourcePermit(request.source, operation.material['origin'], 'catalogue_lookup',
            plan.plan_ref, plan.bindings, request.request_digest, 1, 1,
            self.rights.ref, self.rights.revision, plan.expires_at)

    def revalidate(self, *args, **kwargs):
        return not self.revoked

    def policy(self, *args, **kwargs):
        return self.rights

    def model_context_current(self, *args, **kwargs):
        return True


def query():
    registry, value = plan(('gallery',))
    return registry, replace(value, expires_at=DAY+timedelta(seconds=600))


class NativeGalleryMetadataTests(TestCase):
    def test_past_opening_current_run_and_same_page_participants_are_separate(self):
        card, = native_gallery_cards('apw', APW, observed_at=DAY, now=DAY)
        facts = dict(card.facts)
        self.assertEqual(card.medium, 'gallery')
        self.assertEqual(facts['gallery.exhibition_ends_on'], '2026-11-30')
        self.assertLess(datetime.fromisoformat(facts['gallery.opening_starts_at']), DAY)
        self.assertEqual(facts['gallery.artist_name.0'], 'Fictional Participant')
        self.assertNotIn('Unattributed Name', repr(facts))
        self.assertEqual(facts['venue.latitude'], '-37.8')
        self.assertEqual(facts['venue.address.postcode'], '3065')
        self.assertNotIn('price', repr(facts))
        self.assertFalse(any('verified_artist' in key for key in facts))
        outside = APW + b'<h1 class="print__title" itemprop="creator">Offer Artist Is Not Exhibition Participant</h1>'
        card, = native_gallery_cards('apw', outside, observed_at=DAY, now=DAY)
        self.assertNotIn('Offer Artist', repr(card.facts))

    def test_occurrence_identity_not_homepage_or_retrieval_and_unknown_opening_not_run_start(self):
        first = parse_apw(APW, retrieved_at=DAY.isoformat())
        self.assertEqual(first.key, parse_apw(APW, retrieved_at=(DAY+timedelta(days=1)).isoformat()).key)
        self.assertNotEqual(first.key, parse_apw(APW.replace(b'New Exhibition', b'Later Exhibition'),
                                                retrieved_at=DAY.isoformat()).key)
        raw = APW.replace(b'Opening 6pm. Friday 6 November 2026', b'View exhibition')
        card, = native_gallery_cards('apw', raw, observed_at=DAY, now=DAY)
        facts = dict(card.facts)
        self.assertNotIn('gallery.opening_starts_at', facts)
        self.assertEqual(facts['gallery.date_precision'], 'date')
        self.assertEqual(native_gallery_cards('apw', APW, observed_at=DAY,
                                              now=DAY+timedelta(days=30)), ())
        fixture = Path(__file__).resolve().parents[1]/'fixtures/gallery_gertrude.html'
        gertrude, = native_gallery_cards('gertrude', fixture.read_bytes(), observed_at=DAY, now=DAY)
        self.assertEqual(dict(gertrude.facts)['gallery.exhibition_ends_on'], '2026-12-12')

    def test_real_backend_issuer_collector_keeps_rights_freshness_and_other_source_failure(self):
        registry, value = query(); clock = GalleryClock(); issuer = Issuer()
        backend = GalleryBackend('apw', issuer, clock=clock)
        unavailable = GalleryBackend('gertrude', issuer, clock=clock)
        # No model permission for unlisted optional artist/venue facts: exact
        # per-field admission must omit them while retaining minimal identity.
        robots = (transport.time.monotonic(), 200, b'User-agent: *\nCrawl-delay: 10\nAllow: /')
        def read(url, **bounds):
            if url == transport.GERTRUDE_URL:
                raise SourceError('Other source unavailable')
            self.assertEqual(url, transport.APW_URL)
            self.assertTrue(bounds['revalidate']())
            return 200, APW, (('cache-control', 'max-age=900'), ('age', '800'))
        with patch.dict(transport._ROBOTS_CACHE,
            {transport.APW_URL+'robots.txt': robots, 'https://gertrude.org.au/robots.txt': robots}, clear=True), \
             patch.object(transport, 'read', side_effect=read) as wire, \
             patch.object(transport, 'prepare_robots', return_value=transport.RobotsPreparation(
                 'ready', DAY, DAY+timedelta(seconds=600), 'a'*64, 0, 0, DAY)), \
             patch.object(transport, 'preparation_current', return_value=True):
            collector = SourceCollection((('gallery', ('apw', 'gertrude')),),
                {'apw': backend, 'gertrude': unavailable}, clock=clock)
            seal = collector.collect(value, registry, CollectionBudget())
            self.assertEqual(len(seal.records), 1)
            self.assertEqual(seal.outcomes[0].state, 'fresh')
            self.assertEqual(seal.expires_at, DAY+timedelta(seconds=100))
            self.assertEqual(seal.records[0]['candidate']['facts'], [])
            self.assertEqual(collector.state.cache, {})
            self.assertEqual(wire.call_count, 2)
            self.assertTrue(collector.revalidate_collection(seal, value, registry))
            issuer.revoked = True
            with self.assertRaises(EnhanceFault):
                collector.revalidate_collection(seal, value, registry)

    def test_missing_robots_refusal_and_changed_authority_never_fetch_listing_or_offer(self):
        op = transport.GalleryOperation('apw')
        with patch.dict(transport._ROBOTS_CACHE, {}, clear=True), patch.object(transport, 'read') as read:
            with self.assertRaises(SourceError):
                transport.gallery_authorized(op, revalidate=lambda: True)
            read.assert_not_called()
        refused = (transport.time.monotonic(), 200, b'User-agent: *\nDisallow: /')
        with patch.dict(transport._ROBOTS_CACHE, {op.robots_url: refused}, clear=True), \
             patch.object(transport, 'read') as read:
            with self.assertRaises(SourceError):
                transport.gallery_authorized(op, revalidate=lambda: True)
            read.assert_not_called()
        for source in ['offers', transport.OFFER_URL, 'https://evil.invalid']:
            with self.assertRaises(SourceError):
                transport.GalleryOperation(source)

    def test_gallery_driver_bounds_revalidation_headers_and_child_cleanup(self):
        child = Mock(returncode=0); child.poll.return_value = None; child.stdout.fileno.return_value = 91
        ready = MagicMock(); ready.__enter__.return_value = ready
        ready.get_map.side_effect = [True, True, False]
        raw = b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nAge: 1\r\n\r\n<p>bounded</p>\n200'
        with patch.object(transport.subprocess, 'Popen', return_value=child) as spawn, \
             patch.object(transport.selectors, 'DefaultSelector', return_value=ready), \
             patch.object(transport.os, 'set_blocking'), \
             patch.object(transport.os, 'read', side_effect=[raw, b'']):
            result = transport._send(transport._base(262144)+['--include', '--request', 'GET',
                '--url', transport.APW_URL], max_bytes=262144, deadline=transport.time.monotonic()+6,
                revalidate=lambda: True, include_headers=True)
        self.assertEqual(result, (200, b'<p>bounded</p>', (('age', '1'), ('content-type', 'text/html'))))
        self.assertEqual(spawn.call_count, 1)
        args = spawn.call_args.args[0]
        self.assertLessEqual(float(args[args.index('--max-time')+1]), 5)
        self.assertEqual(args[args.index('--retry')+1], '0')
        child.kill.assert_called_once()
        child.stdout.close.assert_called_once()
        with patch.object(transport.subprocess, 'Popen') as spawn, self.assertRaises(SourceError):
            transport._send(transport._base(100), max_bytes=100,
                deadline=transport.time.monotonic()+6, revalidate=lambda: False)
        spawn.assert_not_called()
