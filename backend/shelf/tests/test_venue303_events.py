"""Fictional listing fragments, never live programme seeds."""
from html import escape
from unittest import TestCase
from shelf import venue303_events as venue
from shelf.discovery_sources import SourceError
from shelf.tests.test_event_sources import FETCHED


def card(start='2026-11-01', *, title='SYNTHETIC 303', url='/gigs-events/synthetic-show',
         clock='', notice='', end=None):
    return (f'<article class="eventlist-event"><h1><a class="eventlist-title-link" href="{escape(url)}">'
        f'{escape(title)}</a></h1><li class="eventlist-meta-date"><time class="event-date" datetime="{start}">'
        + escape(start or 'November, year unverified') + '</time></li>'
        + (f'<time class="event-end-date" datetime="{end}">{end}</time>' if end else '')
        + (f'<li class="eventlist-meta-time"><span class="event-time-localized-start">{clock}</span></li>' if clock else '')
        + (f'<span class="eventlist-meta-status">{escape(notice)}</span>' if notice else '')
        + '</article>')


def listing(*rows):
    return ('<html>' + ''.join(rows or [card()]) + '</html>').encode()


class Venue303Tests(TestCase):
    def parse(self, raw, provenance='fixture'):
        return venue.parse_listing(raw, retrieved_at=FETCHED, provenance=provenance)

    def test_same_url_distinct_local_clocks_and_scoped_cancellation(self):
        events = self.parse(listing(card(clock='7:30 PM', notice='cancelled'), card(clock='9:30 PM')))
        self.assertEqual(len(events), 1)
        live = self.parse(listing(card(clock='7:30 PM'), card(clock='9:30 PM')), 'live-automated')
        self.assertEqual(len(live), 2)
        self.assertEqual(live[0].url, live[1].url)
        self.assertNotEqual(live[0].key, live[1].key)
        self.assertIsNone(live[0].starts_at)
        self.assertIsNone(live[0].event_timezone)
        self.assertEqual(live[0].occurrence_basis, 'explicit_datetime')

    def test_unknown_dates_and_ranges_remain_honest_fixture_and_live_distinct(self):
        raw = listing(card(start=''), card(start='2026-11-01', end='2026-11-03', url='/gigs-events/range'))
        unknown, ranged = self.parse(raw)
        self.assertEqual(unknown.occurrence_basis, 'unresolved_observation')
        self.assertIsNone(unknown.starts_at)
        self.assertEqual(ranged.occurrence_basis, 'explicit_range')
        self.assertIsNone(ranged.url)
        self.assertNotEqual(ranged.key, self.parse(raw, 'live-automated')[1].key)
        self.assertIn('no stable session', unknown.date_status(None))

    def test_source_notice_is_attributed_not_stock_or_artist_identity(self):
        event, = self.parse(listing(card(notice='Sold Out')))
        self.assertEqual(event.source_notice, 'Sold Out')
        self.assertEqual(event.status, 'unknown')
        self.assertEqual(event.eligibility, 'unknown')
        self.assertIsNone(event.lineup)
        self.assertIn('not a seller check', event.date_status(None))

    def test_window_source_order_limits_and_challenge_do_not_fake_empty(self):
        rows = [card(start='2026-10-01', url='/gigs-events/past')]
        rows += [card(url=f'/gigs-events/{i}') for i in range(12)]
        self.assertEqual([e.rank for e in self.parse(listing(*rows))], list(range(2, 11)))
        self.assertEqual(self.parse(listing(card(start='2026-10-01'))), ())
        for raw in (b'', b'<html>login challenge</html>', listing()[:-15],
                    listing(*[card() for _ in range(49)]), b'x'*(venue.MAX_BYTES+1),
                    listing(card(url='https://evil.invalid/event'))):
            with self.subTest(raw=raw[:25]), self.assertRaises((SourceError, UnicodeError)):
                self.parse(raw)
