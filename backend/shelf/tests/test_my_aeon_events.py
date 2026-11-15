"""Fictional observed Webflow cards, including archive and identity cases."""
from datetime import datetime,timezone
from html import escape
from unittest import TestCase
from shelf import my_aeon_events as source,event_scraping as engine,event_listing_transport as transport
from shelf.discovery_sources import SourceError
from unittest.mock import patch

NOW=datetime(2026,11,11,4,tzinfo=timezone.utc)


def card(name='SYNTHETIC event',when='November 13, 2026',url='/events/synthetic-event'):
    return ('<div class="collection-item-11 w-dyn-item"><a class="homepage-event-v2 w-inline-block" href="'
        +escape(url,quote=True)+'"><div class="event-listing-name">'+escape(name)
        +'</div><div class="event-listing-date-center">'+escape(when)
        +'</div><div class="home-page-event-desc-copy-all">CANARY description</div></a></div>')


def listing(rows=None):
    return ('<html><div class="event-header"><h1>EVENTS</h1></div>'
        '<div class="event-listing-collection-list w-dyn-items">'
        +''.join([card()] if rows is None else rows)+'</div></html>').encode()


class MyAeonTests(TestCase):
    def parse(self,raw):
        return source.parse_listing(raw,retrieved_at=NOW.isoformat())

    def test_dated_lead_has_exact_name_url_date_and_unknown_time_zone_stock(self):
        event,=self.parse(listing())
        self.assertEqual(event.title,'SYNTHETIC event')
        self.assertEqual(event.starts_at,'2026-11-13')
        self.assertEqual(event.date_precision,'date')
        self.assertIsNone(event.event_timezone)
        self.assertEqual(event.occurrence_basis,'explicit_date')
        self.assertEqual(event.url,'https://www.myaeon.com.au/events/synthetic-event')
        self.assertEqual(event.status,'unknown')
        self.assertIsNone(event.lineup)
        self.assertFalse(hasattr(event,'description'))

    def test_all_stale_or_empty_wrapper_unavailable_not_empty_and_no_year_rollover(self):
        for raw in (listing([card(when='January 17, 2026'),card(when='April 11, 2026')]),listing([])):
            with self.assertRaises(engine.ProgrammeUnavailable):self.parse(raw)
            with patch.object(transport,'my_aeon',return_value=(200,raw)):
                outcome=engine.collect('my_aeon',now=NOW)
            self.assertEqual(outcome.status,'unavailable')
            self.assertEqual(outcome.candidates,())
            self.assertIn('Old years are not rolled forward',outcome.message)

    def test_date_window_filters_past_and_far_future_but_today_is_not_assumed_past(self):
        events=self.parse(listing([card('PAST','January 17, 2026'),
            card('TODAY','November 11, 2026','/events/today'),
            card('FAR FUTURE','January 8, 2027','/events/far-future')]))
        self.assertEqual([event.title for event in events],['TODAY'])
        unknown,=self.parse(listing([card(when='November 13')]))
        self.assertIsNone(unknown.starts_at)
        self.assertEqual(unknown.occurrence_basis,'unresolved_observation')

    def test_exact_duplicates_reconcile_but_shared_title_or_url_does_not_bridge_dates(self):
        one,=self.parse(listing([card(),card()]))
        self.assertEqual(len(one.evidence),2)
        events=self.parse(listing([card(),card(when='November 14, 2026'),
                                  card(url='/events/synthetic-event-other')]))
        self.assertEqual(len(events),3)
        self.assertEqual(len({event.key for event in events}),3)
        with self.assertRaises(SourceError):self.parse(listing([card(),card('CONFLICT')]))

    def test_unsafe_links_invalid_dates_layout_and_budget_are_not_accepted(self):
        for raw in (listing([card(when='February 30, 2026')]),
                    listing([card(when='2026-01-17')]),
                    listing([card(url='https://evil.invalid/events/a')]),
                    listing([card(url='/events/')]),
                    listing().replace(b'</a>',b'</span>'),b'<html>challenge</html>',listing([card()]*49)):
            with self.assertRaises(SourceError):self.parse(raw)

    def test_nine_lead_bound_and_fixture_source_mode_are_preserved(self):
        events=self.parse(listing([card(url='/events/synthetic-'+str(i)) for i in range(10)]))
        self.assertEqual(len(events),9)
        fixture,=source.parse_listing(listing(),retrieved_at=NOW.isoformat(),provenance='fixture')
        live,=self.parse(listing())
        self.assertNotEqual(fixture.key,live.key)
        self.assertIsNone(fixture.url)
