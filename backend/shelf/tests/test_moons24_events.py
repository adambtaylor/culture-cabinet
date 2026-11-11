"""Fictional observed-layout fragments, not current programme content."""
from datetime import datetime, timezone
from html import escape
from unittest import TestCase

from shelf import moons24_events as source
from shelf.discovery_sources import SourceError

NOW = datetime(2026,11,11,4,tzinfo=timezone.utc)


def card(title='SYNTHETIC overnight event', when='Fri 13 Nov 2026 · 10:00pm — Sat 5:00am AEDT'):
    return ('<div class="this-week-event"><h3 class="this-week-title">Friday</h3>'
        '<a class="content-card events" href="https://ra.co/events/999999999">'
        '<div class="card-copy"><h3>'+escape(title)+'</h3><time class="meta">'+escape(when)+'</time></div>'
        '<div class="event-tags"><span>18+</span><span>Synthetic source tag</span></div></a></div>')


def listing(rows=None, *, week='9 Nov – 15 Nov 2026 · Melbourne time', later='', empty_week=False):
    rows=[card()] if rows is None else rows
    return ('<html><main><section class="listing-section"><div class="weekly-programme" id="this-week">'
        '<p class="meta this-week-meta">'+escape(week)+'</p>'+''.join(rows)
        + ('<p class="programme-empty">No events listed this week.</p>' if empty_week else '')
        + '</div><div class="later-programme" id="further-upcoming">'
        + (later or '<h3>Upcoming Events</h3><p class="programme-empty">No upcoming events are listed yet. Follow RA.</p>')
        + '</div></section></main></html>').encode()


class Moons24ParserTests(TestCase):
    def parse(self, raw):
        return source.parse_listing(raw,retrieved_at=NOW.isoformat())

    def test_current_week_is_not_erased_by_empty_later_section_and_overnight_is_exact(self):
        event,=self.parse(listing())
        self.assertEqual(event.title,'SYNTHETIC overnight event')
        self.assertEqual(event.starts_at,'2026-11-13T22:00:00+11:00')
        self.assertEqual(event.ends_at,'2026-11-14T05:00:00+11:00')
        self.assertEqual(event.occurrence_basis,'explicit_range')
        self.assertEqual(event.url,source.LISTING_URL)
        self.assertEqual(event.evidence[0].source_id,'moons24')
        self.assertIn('18+',event.source_notice)
        self.assertEqual(event.source_notice,'18+ · Synthetic source tag')
        self.assertEqual(event.status,'unknown')
        self.assertIsNone(event.lineup)
        self.assertIsNone(event.entry_fee)

    def test_same_programme_url_distinct_ranges_and_ambiguous_duplicate_observations(self):
        a,b=self.parse(listing([card(),card(when='Sat 14 Nov 2026 · 9:00pm — Sun 3:00am AEDT')]))
        self.assertEqual(a.url,b.url);self.assertNotEqual(a.key,b.key)
        self.assertNotEqual(a.occurrence_ref,b.occurrence_ref)
        duplicate=self.parse(listing([card('SYNTHETIC A'),card('SYNTHETIC B')]))
        self.assertEqual(len(duplicate),2)
        self.assertTrue(all(event.occurrence_basis=='unresolved_observation' for event in duplicate))

    def test_missing_year_or_offset_is_unknown_and_no_week_or_retrieval_backfill(self):
        for when in ('Fri 13 Nov · 10:00pm — Sat 5:00am AEDT',
                     'Fri 13 Nov 2026 · 10:00pm — Sat 5:00am'):
            event,=self.parse(listing([card(when=when)]))
            self.assertIsNone(event.starts_at)
            self.assertEqual(event.occurrence_basis,'unresolved_observation')
            self.assertIn('unverified',event.date_status(NOW))

    def test_stale_only_and_shell_are_not_successful_empty(self):
        for raw in (b'<html>challenge</html>',listing([]),
                    listing([card(when='Fri 6 Nov 2026 · 10:00pm — Sat 5:00am AEDT')],
                            week='2 Nov – 8 Nov 2026 · Melbourne time')):
            with self.assertRaises(SourceError):self.parse(raw)
        self.assertEqual(self.parse(listing([],empty_week=True)),())

    def test_changed_calendar_week_timezone_and_structure_fail_closed(self):
        for raw in (listing([card(when='Thu 13 Nov 2026 · 10:00pm — Fri 5:00am AEDT')]),
                    listing([card(when='Fri 13 Nov 2026 · 10:00pm — Sat 5:00am AEST')]),
                    listing(week='9 Nov – 16 Nov 2026 · Melbourne time'),
                    listing().replace(b'</time>',b'</span>'),listing([card()]*49)):
            with self.assertRaises(SourceError):self.parse(raw)

    def test_fixture_identity_cannot_bridge_into_household_and_body_is_not_retained(self):
        live,=self.parse(listing())
        fixture,=source.parse_listing(listing(),retrieved_at=NOW.isoformat(),provenance='fixture')
        self.assertNotEqual(live.key,fixture.key)
        self.assertIsNone(fixture.url)
        self.assertFalse(hasattr(live,'body'))

    def test_observation_budget_keeps_only_nine_bounded_leads(self):
        events=self.parse(listing([card('SYNTHETIC '+str(i)) for i in range(10)]))
        self.assertEqual(len(events),9)
        self.assertEqual(len({event.key for event in events}),9)
