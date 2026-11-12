"""Synthetic proposed Forum card shape; live shape acceptance is separate."""
from unittest import TestCase
from shelf import forum_events as forum
from shelf.discovery_sources import SourceError
from shelf.tests.test_event_sources import FETCHED


def session(start='2026-11-01T19:00:00+11:00', notice=''):
    return (f'<li class="show-session"><time class="show-date" datetime="{start}">{start}</time>'
            + (f'<span class="session-status">{notice}</span>' if notice else '') + '</li>')


def listing(*sessions, notice=''):
    return ('<html><div class="banner">cancelled</div><article class="show-card">'
            '<a class="show-title" href="/shows/synthetic">SYNTHETIC show</a>'
            + (f'<span class="show-status">{notice}</span>' if notice else '')
            + ''.join(sessions or [session()]) + '</article></html>').encode()


class ForumTests(TestCase):
    def parse(self, raw):
        return forum.parse_listing(raw, retrieved_at=FETCHED, provenance='fixture')

    def test_explicit_sessions_shared_url_with_scoped_and_unscoped_notice(self):
        first, second = self.parse(listing(session(), session('2026-11-01T21:00:00+11:00')))
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(first.official_url, second.official_url)
        self.assertEqual(first.status, 'unknown')
        remaining = self.parse(listing(session(notice='cancelled'),session('2026-11-01T21:00:00+11:00')))
        self.assertEqual([e.key for e in remaining], [second.key])
        self.assertEqual(self.parse(listing(session(), session('2026-11-01T21:00:00+11:00'), notice='postponed')), ())

    def test_yearless_dates_and_same_day_duplicates_are_not_invented_sessions(self):
        raw = listing().replace(b'<time class="show-date" datetime="2026-11-01T19:00:00+11:00">2026-11-01T19:00:00+11:00</time>',
                                b'<span class="show-date">Nov 14</span>')
        event, = self.parse(raw)
        self.assertIsNone(event.starts_at)
        self.assertEqual(event.occurrence_basis, 'unresolved_observation')
        first, second = self.parse(listing(session('2026-11-01'),session('2026-11-01')))
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(first.occurrence_basis, 'unresolved_observation')

    def test_sold_out_notice_not_seller_stock_and_invalid_pages_not_empty(self):
        event, = self.parse(listing(notice='sold out'))
        self.assertEqual(event.source_notice, 'sold out')
        self.assertEqual(event.status, 'unknown')
        self.assertIsNone(event.entry_fee)
        for raw in (b'<html>challenge</html>', listing()[:-15], b'x'*(forum.MAX_BYTES+1),
                    listing().replace(b'/shows/synthetic', b'https://evil.invalid/event')):
            with self.subTest(raw=raw[:20]), self.assertRaises(SourceError): self.parse(raw)

    def test_range_is_one_observation_and_outside_window_is_bounded_empty(self):
        raw = listing(session('2026-11-01')).replace(b'</li>',
            b'<time class="show-end-date" datetime="2026-11-03">Nov 3</time></li>')
        event, = self.parse(raw)
        self.assertEqual(event.occurrence_basis, 'explicit_range')
        self.assertEqual(event.ends_at, '2026-11-03')
        self.assertEqual(self.parse(listing(session('2026-10-01'))), ())
