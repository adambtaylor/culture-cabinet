"""A public retailer link must not silently become an inventory permission."""
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch

from shelf import halftix_events
from shelf.discovery_sources import Outcome, SourceError
from shelf.event_sources import SOURCES, parse_official_snapshot


class HalftixBoundaryTests(TestCase):
    def test_manual_registry_and_shared_outcome_never_fetch_or_claim_empty_stock(self):
        source = SOURCES['halftix']
        self.assertEqual(source.listing_url, 'https://halftixmelbourne.com/all-events/')
        self.assertEqual(source.connection, 'manual-link-only; permission-required')
        with patch('shelf.discovery_transport._send', side_effect=AssertionError('No site read authorized')) as send:
            for _ in range(2):
                result = halftix_events.fetch_listing(datetime.now(timezone.utc))
                self.assertIsInstance(result, Outcome)
                self.assertEqual(result.status, 'permission-required')
                self.assertEqual(result.candidates, ())
                self.assertIn('Discounts vary', result.message)
            send.assert_not_called()

    def test_registered_manual_link_cannot_bypass_policy_via_snapshot_parser(self):
        for provenance in ('fixture', 'supplied-snapshot', 'live-automated'):
            with self.subTest(provenance=provenance), self.assertRaises(SourceError):
                parse_official_snapshot(b'{"format":"event-metadata-v1","events":[]}',
                    source_id='halftix', retrieved_at='2026-10-30T00:00:00+00:00',
                    provenance=provenance)
