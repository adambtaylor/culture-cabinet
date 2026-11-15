from datetime import datetime
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from shelf import event_listing_transport, event_source_registry, event_scraping
from shelf.discovery_sources import SourceError
from shelf.gertrude_events import parse_listing

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'gertrude_exhibitions.html'
RAW = FIXTURE.read_bytes()
NOW = datetime(2026, 11, 12, 12, tzinfo=ZoneInfo('Australia/Melbourne'))
RETRIEVED = '2026-11-12T12:00:00+11:00'


class GertrudeEventsTests(TestCase):
    def test_parse_only_current_and_upcoming_explicit_exhibition_runs(self):
        events = parse_listing(RAW, now=NOW, retrieved_at=RETRIEVED, provenance='fixture')
        self.assertEqual([event.title for event in events], ['Et cetera', 'Erin Hallyburton rot'])
        self.assertEqual([event.source_id for event in events], ['gertrude', 'gertrude'])
        self.assertEqual([event.source_start for event in events], ['2026-11-07', '2026-11-13'])
        self.assertEqual([event.source_end for event in events], ['2026-12-12', '2026-12-12'])
        self.assertEqual([event.date_precision for event in events], ['date', 'date'])
        self.assertEqual([event.event_timezone for event in events], [None, None])
        self.assertEqual([event.occurrence_basis for event in events], ['explicit_range', 'explicit_range'])
        self.assertEqual(events[0].venue, 'Gertrude Contemporary')
        self.assertEqual(events[1].venue, 'Gertrude Glasshouse')
        self.assertNotIn('Past show', [event.title for event in events])
        self.assertTrue(all(event.starts_at == event.source_start and 'T' not in event.starts_at
                            for event in events))
        self.assertTrue(all(event.date_status(NOW).startswith('Source exhibition run:') for event in events))

    def test_collect_uses_one_fixed_normal_source_action(self):
        descriptor = event_source_registry.get_source('gertrude')
        self.assertTrue(descriptor.implemented)
        self.assertEqual(descriptor.targets, ('https://gertrude.org.au/exhibitions/',))
        with patch.object(event_listing_transport, 'gertrude', return_value=(200, RAW)) as acquire:
            outcome = event_scraping.collect('gertrude', now=NOW)
        acquire.assert_called_once_with()
        self.assertEqual(outcome.status, 'ready')
        self.assertEqual([event.title for event in outcome.candidates], ['Et cetera', 'Erin Hallyburton rot'])
        self.assertEqual(outcome.candidates[0].provenance, 'live-automated')
        self.assertIn('opening sessions', outcome.message)

    def test_missing_archive_boundary_or_ambiguous_fields_fails_closed(self):
        without_boundary = RAW.replace(b'<div class="section-title">Past exhibitions</div>', b'')
        with self.assertRaises(SourceError):
            parse_listing(without_boundary, now=NOW, retrieved_at=RETRIEVED, provenance='fixture')
        bad_run = RAW.replace(b'Nov 7, 2026 - Dec 12, 2026', b'Nov 7 - Dec 12, 2026')
        with self.assertRaises(SourceError):
            parse_listing(bad_run, now=NOW, retrieved_at=RETRIEVED, provenance='fixture')

    def test_fixed_target_and_closed_registry_count_do_not_expand(self):
        self.assertEqual(event_listing_transport.TARGETS['gertrude'][0],
                         'https://gertrude.org.au/exhibitions/')
        self.assertTrue(event_listing_transport.LISTING_ACCESS['gertrude'])
        self.assertEqual(len(event_source_registry.SOURCES), 35)
