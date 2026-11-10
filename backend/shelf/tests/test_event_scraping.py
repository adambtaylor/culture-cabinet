"""Shared acquisition contract over synthetic source markup; no network."""
from datetime import datetime
from hashlib import sha256
from unittest import TestCase
from unittest.mock import patch

from shelf import discovery_sources, discovery_transport, event_scraping
from shelf.discovery_sources import SourceError
from shelf.tests.test_discovery_transport import NOW
from shelf.tests.test_rrr_events import card, listing


class EventScrapingTests(TestCase):
    def test_fixed_source_and_aware_clock_required_before_io(self):
        with patch.object(discovery_transport, 'rrr') as send:
            for source in ('pbs', 'halftix', 'https://www.rrr.org.au/events', '', [], None):
                with self.subTest(source=source):
                    self.assertEqual(event_scraping.collect(source, now=NOW).status, 'invalid')
            for clock in (None, '2026-10-28', datetime(2026, 10, 28)):
                with self.subTest(clock=clock):
                    self.assertEqual(event_scraping.collect('rrr', now=clock).status, 'invalid')
            send.assert_not_called()

    def test_real_parser_preserves_source_order_provenance_and_unknown_dates(self):
        raw = listing(*(card(i, date='30 December – 2 January') for i in range(1, 12)))
        with patch.object(discovery_transport, 'rrr', return_value=(200, raw)) as send:
            result = event_scraping.collect('rrr', now=NOW)
        send.assert_called_once_with()
        self.assertEqual(result.status, 'ready')
        self.assertEqual([c.source_event_id for c in result.candidates], [str(i) for i in range(1, 10)])
        for position, candidate in enumerate(result.candidates, 1):
            self.assertEqual(candidate.retrieved_at, NOW.isoformat())
            self.assertEqual(candidate.evidence[0].snapshot_sha256, sha256(raw).hexdigest())
            self.assertEqual(candidate.evidence[0].position, position)
            self.assertIsNone(candidate.starts_at)
            self.assertIsNone(candidate.event_timezone)
            self.assertEqual(candidate.eligibility, 'unknown')
            self.assertEqual(candidate.listed_date, '30 December – 2 January')
        self.assertIn('Partial coverage', result.message)

    def test_refusals_redirects_and_failure_never_parse_or_retry(self):
        for code, expected in ((401, 'error'), (403, 'error'), (429, 'rate-limited'),
                               (302, 'error'), (500, 'error'), ('200', 'error')):
            with self.subTest(code=code), patch.object(discovery_transport, 'rrr',
                    return_value=(code, b'CANARY body')) as send, \
                    patch('shelf.rrr_events.parse_listing') as parse:
                result = event_scraping.collect('rrr', now=NOW)
                self.assertEqual(result.status, expected)
                self.assertEqual(result.candidates, ())
                self.assertNotIn('CANARY', repr(result))
                send.assert_called_once_with()
                parse.assert_not_called()

    def test_timeout_and_transport_errors_are_safe_and_single_attempt(self):
        for error, expected in ((TimeoutError('CANARY'), 'timeout'),
                                (OSError('CANARY'), 'invalid'),
                                (SourceError('CANARY'), 'invalid')):
            with self.subTest(error=type(error)), patch.object(discovery_transport, 'rrr',
                    side_effect=error) as send:
                result = event_scraping.collect('rrr', now=NOW)
                self.assertEqual(result.status, expected)
                self.assertEqual(result.candidates, ())
                self.assertNotIn('CANARY', repr(result))
                send.assert_called_once_with()

    def test_unrecognized_empty_challenge_or_changed_html_is_not_empty_inventory(self):
        for raw in (b'', b'<html>CANARY login challenge</html>', b'\xff',
                    listing().replace(b'card__title', b'changed-title'),
                    b'x' * (discovery_sources.MAX_BYTES + 1)):
            with self.subTest(raw=raw[:30]), patch.object(discovery_transport, 'rrr',
                    return_value=(200, raw)) as send:
                result = event_scraping.collect('rrr', now=NOW)
                self.assertEqual(result.status, 'invalid')
                self.assertEqual(result.candidates, ())
                self.assertNotIn('CANARY', repr(result))
                send.assert_called_once_with()
