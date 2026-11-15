"""Fictional fields in the observed rendered programme shape; no HTTP."""
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from unittest import TestCase
from unittest.mock import patch

from shelf import west_space_events as west, event_scraping, event_listing_transport as transport
from shelf.discovery_sources import SourceError

NOW = datetime(2026, 11, 11, 6, tzinfo=timezone.utc)


def card(slug='fictional-run', title='FICTIONAL programme', artist='Fictional artist',
         dates='31 Oct → 6 Dec 2026'):
    return (f'<a class="css-rgn3jt" href="/program/{escape(slug, quote=True)}">'
        '<h2 class="type--50">“<!-- -->'+escape(title)+'<!-- -->”<br/><!-- -->'
        +escape(artist)+'<!-- --><br/><!-- -->'+escape(dates)+'</h2></a>')


def listing(*cards):
    return ('<!doctype html><html><body><main><div class="type--50">'
        +''.join(cards or [card()])+'</div></main></body></html>').encode()


class WestSpaceParserTests(TestCase):
    def parse(self, raw):
        return west.parse_listing(raw, retrieved_at=NOW.isoformat())

    def test_programme_range_is_not_an_opening_performance_or_invented_lineup(self):
        raw=listing();event,=self.parse(raw)
        self.assertEqual(event.title,'FICTIONAL programme')
        self.assertEqual((event.exhibition_starts_on,event.exhibition_ends_on),('2026-10-31','2026-12-06'))
        self.assertEqual((event.starts_at,event.ends_at,event.opening_date),(None,None,None))
        self.assertIsNone(event.event_timezone);self.assertIsNone(event.lineup)
        self.assertIsNone(event.entry_fee)
        self.assertEqual(event.official_url,'https://westspace.org.au/program/fictional-run')
        self.assertEqual(event.occurrence_basis,'explicit_range')
        self.assertEqual(event.evidence[0].snapshot_sha256,sha256(raw).hexdigest())
        self.assertEqual(event.evidence[0].retrieved_at,NOW.isoformat())
        self.assertIn('Current programme run',event.date_status(NOW))
        self.assertIn('opening, hours and individual sessions unverified',event.date_status(NOW))
        self.assertIn('Fictional artist',event.source_notice)
        self.assertFalse(hasattr(event,'gallery_data'))

    def test_abbreviated_year_is_scoped_to_publisher_range_not_retrieval_clock(self):
        self.assertEqual(west._range('7 Nov → 30 Jan 2027'),('2026-11-07','2027-01-30'))
        self.assertEqual(west._range('31 Oct → 6 Dec 2025'),('2025-10-31','2025-12-06'))
        self.assertEqual(west._range('8 May 2025 → 17 July 2027'),('2025-05-08','2027-07-17'))
        for text in ('7 Nov → 30 Jan','31 Feb → 6 Dec 2026','8 May 2027 → 17 July 2026'):
            with self.subTest(text=text),self.assertRaises(SourceError):west._range(text)

    def test_overlap_window_preserves_current_run_and_excludes_far_future_and_past(self):
        raw=listing(card('ongoing'),card('cross-year',dates='7 Nov → 30 Jan 2027'),
            card('far',dates='8 May → 17 July 2027'),card('past',dates='1 Oct → 30 Oct 2026'))
        self.assertEqual([event.official_url.rsplit('/',1)[-1] for event in self.parse(raw)],
                         ['ongoing','cross-year'])
        with patch.object(transport,'west_space',return_value=(200,listing(card(dates='1 Oct → 30 Oct 2026')))):
            outcome=event_scraping.collect('west_space',now=NOW)
        self.assertEqual(outcome.status,'unavailable');self.assertEqual(outcome.candidates,())
        self.assertIn('not proof of no events',outcome.message)

    def test_same_url_new_range_is_distinct_and_duplicates_keep_evidence(self):
        first,second=self.parse(listing(card(),card(dates='1 Dec → 31 Dec 2026')))
        self.assertNotEqual(first.key,second.key)
        self.assertEqual(first.url,second.url)
        merged,=self.parse(listing(card(),card()))
        self.assertEqual(merged.key,first.key);self.assertEqual(len(merged.evidence),2)

    def test_changed_fields_and_unsafe_or_truncated_programmes_fail_closed(self):
        for raw in (b'<html>challenge</html>',b'<main></main>',listing().replace(b'type--50',b'changed'),
            listing().replace(b'fictional-run',b'../private'),listing().replace(b'</main>',b''),
            listing().replace('“'.encode(),b''),listing().replace(b'31 Oct',b'31 February')):
            with self.subTest(raw=raw[:50]),patch.object(transport,'west_space',return_value=(200,raw)) as read:
                outcome=event_scraping.collect('west_space',now=NOW)
                self.assertEqual(outcome.status,'invalid');self.assertEqual(outcome.candidates,())
                read.assert_called_once_with()

    def test_card_and_output_limits_are_shared_bounds(self):
        self.assertEqual(len(self.parse(listing(*(card(str(i)) for i in range(48))))),9)
        with self.assertRaises(SourceError):self.parse(listing(*(card(str(i)) for i in range(49))))
        with self.assertRaises(SourceError):self.parse(b'x'*(transport.MAX_BYTES+1))
