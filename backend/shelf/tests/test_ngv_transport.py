"""Closed NGV event paths share one real pacing receipt; all HTTP mocked."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from shelf import event_listing_transport as transport
from shelf.discovery_sources import SourceError
from shelf.ngv_events import ROUTES


class NGVTransportTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.enterContext(patch.object(transport, '_RATE_DIRECTORY', directory.name))
        self.enterContext(patch.object(transport, '_DELAYS', dict(transport._DELAYS)))

    def test_both_routes_and_robots_share_one_receipt_with_stricter_delay(self):
        raw = b'User-agent: *\nAllow: /\nCrawl-delay: 20\n'
        with patch.object(transport, '_send', return_value=(200, raw)) as send:
            result = transport.observe_robots('ngv')
            self.assertEqual(result['routes_allowed'], {'programs': True, 'exhibitions': True})
            self.assertEqual(result['delay'], 20)
            for route in ROUTES:
                with self.assertRaises(transport.ListingIntervalPending), \
                        patch.object(transport, 'NGV_ACCESS', dict.fromkeys(ROUTES, True)):
                    transport.ngv(route)
            send.assert_called_once()
        receipts = list(self.directory.iterdir())
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0].name.endswith('-ngv.rate'))
        self.assertEqual(json.loads(receipts[0].read_text())['delay'], 20)

    def test_default_and_explicit_routes_use_only_fixed_targets(self):
        with patch.object(transport, 'NGV_ACCESS', dict.fromkeys(ROUTES, True)), \
                patch.object(transport, '_reserve') as reserve, \
                patch.object(transport, '_send', return_value=(200, b'HTML')) as send:
            for route in ('', *ROUTES):
                transport.ngv(route)
                self.assertEqual(send.call_args.args[0][-1], ROUTES[route or 'programs'])
            self.assertEqual(reserve.call_count, 3)
            self.assertEqual([call.args for call in reserve.call_args_list], [('ngv',)]*3)

    def test_unknown_route_and_unconfirmed_path_refuse_before_reservation(self):
        with patch.object(transport, '_reserve') as reserve,patch.object(transport, '_send') as send:
            for route in ('https://evil.invalid', 'archive', [], None):
                with self.subTest(route=route),self.assertRaises(SourceError):transport.ngv(route)
            with patch.object(transport,'NGV_ACCESS',dict.fromkeys(ROUTES,False)):
                for route in ROUTES:
                    with self.assertRaises(transport.ListingPolicyUnavailable):transport.ngv(route)
            with self.assertRaises(SourceError):transport._read('moons24', event_route='programs')
            reserve.assert_not_called();send.assert_not_called()

    def test_single_robots_response_can_allow_one_path_and_deny_the_other(self):
        raw = b'User-agent: *\nAllow: /\nDisallow: /whats-on/exhibitions/\n'
        with patch.object(transport, '_send', return_value=(200, raw)) as send:
            result = transport.observe_robots('ngv')
        self.assertEqual(result['routes_allowed'], {'programs': True, 'exhibitions': False})
        send.assert_called_once()
