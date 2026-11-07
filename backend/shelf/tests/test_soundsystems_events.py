"""Synthetic server-rendered cards only; no source, calendar or social requests."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from unittest import TestCase
from unittest.mock import patch

from shelf import soundsystems_events as source, discovery_transport as transport
from shelf.discovery_sources import SourceError
from shelf.event_sources import reconcile_events

NOW = datetime(2026, 10, 30, 4, tzinfo=timezone.utc)


def card(*, title='SYNTHETIC sound event', when='NOV 1 2026', weekday='SUNDAY',
         start='13:00', end='23:30', location='SYNTHETIC Town Hall',
         post='https://www.instagram.com/p/Synthetic01/?igsh=tracking', caption='CANARY full article'):
    def div(name, body):
        return f'<div class="Calendar_{name}__testHash">{body}</div>'
    return div('eventCard', div('eventDate', '<div>' + escape(when) + '</div>'
        + div('eventWeekday', escape(weekday)))
        + f'<h3 class="Calendar_eventTitle__testHash">{escape(title)}</h3>'
        + div('eventTime', f'<span>{escape(start)}</span> — <span>{escape(end)}</span>')
        + div('eventLocation', escape(location))
        + f'<p class="Calendar_moreInfoCaption__testHash">{escape(caption)}</p>'
        + f'<a class="Calendar_moreInfoInstagramLink__testHash" href="{escape(post, quote=True)}">View on Instagram</a>')


def listing(rows=None):
    return ('<html><head><meta property="og:url" content="' + source.LISTING_URL.rstrip('/') + '" /></head>'
        '<body><h2 class="Calendar_calendarTitle__testHash">UPCOMING DANCES</h2>'
        + ''.join([card()] if rows is None else rows) + '</body></html>').encode()


class SoundSystemsTests(TestCase):
    def parse(self, raw):
        return source.parse_listing(raw, retrieved_at=NOW.isoformat())

    def test_observed_facts_and_hash_without_inventory_or_social_access_claim(self):
        raw = listing()
        event, = self.parse(raw)
        self.assertEqual(event.starts_at, '2026-11-01')
        self.assertEqual((event.start_clock, event.end_clock), ('13:00', '23:30'))
        self.assertEqual(event.date_precision, 'date')
        self.assertEqual(event.venue, 'SYNTHETIC Town Hall')
        self.assertEqual(event.location_status, 'source-stated')
        self.assertEqual(event.url, source.LISTING_URL)
        self.assertEqual(event.referenced_post, 'https://www.instagram.com/p/Synthetic01/')
        self.assertEqual(event.provenance, 'live-automated')
        self.assertEqual(event.evidence[0].snapshot_sha256, sha256(raw).hexdigest())
        self.assertEqual(event.evidence[0].retrieved_at, NOW.isoformat())
        self.assertIsNone(event.source_event_id)
        self.assertIsNone(event.official_url)
        self.assertIsNone(event.evidence[0].official_url)
        for value in (event.ends_at, event.event_timezone, event.entry_fee, event.lineup,
                      event.outdoor, event.day_party, event.community):
            self.assertIsNone(value)
        self.assertEqual(event.status, 'unknown')
        self.assertEqual(event.eligibility, 'unknown')
        self.assertNotIn('CANARY', repr(event))
        self.assertNotIn('tracking', repr(event))
        self.assertIn('Stale', event.freshness(NOW + timedelta(minutes=16)))

    def test_tba_and_embargoed_location_stay_unresolved(self):
        for value in ('TBA', 'TBC', 'Secret location', 'Embargoed until Friday', 'To be announced'):
            with self.subTest(value=value):
                event, = self.parse(listing([card(location=value)]))
                self.assertEqual(event.venue, '')
                self.assertEqual(event.source_location, value)
                self.assertEqual(event.location_status, 'undisclosed')

    def test_overnight_clock_does_not_invent_ending_day_or_timezone(self):
        event, = self.parse(listing([card(start='22:00', end='06:00')]))
        self.assertEqual(event.end_clock, '06:00')
        self.assertIsNone(event.ends_at)
        self.assertIsNone(event.event_timezone)
        self.assertIn('ending calendar day unverified', event.date_status(NOW))

    def test_unknown_year_and_clock_remain_unknown(self):
        event, = self.parse(listing([card(when='NOV 1', start='25:00')]))
        self.assertIsNone(event.starts_at)
        self.assertIsNone(event.start_clock)
        self.assertEqual(event.listed_date, 'NOV 1')
        self.assertEqual(event.listed_time, '25:00 — 23:30')

    def test_invalid_explicit_dates_are_rejected(self):
        for values in ({'weekday': 'MONDAY'}, {'when': 'FEB 31 2027'}, {'when': 'XYZ 1 2026'}):
            with self.subTest(values=values), self.assertRaises(SourceError):
                self.parse(listing([card(**values)]))

    def test_shared_listing_url_does_not_merge_distinct_observations(self):
        rows = self.parse(listing([card(title='SYNTHETIC one'),
            card(title='SYNTHETIC two', post='https://www.instagram.com/p/Synthetic02/')]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(reconcile_events(rows)), 2)
        self.assertEqual(len({row.key for row in rows}), 2)

    def test_same_reference_conflicts_fail_and_exact_repeats_coalesce(self):
        self.assertEqual(len(self.parse(listing([card(), card()]))), 1)
        for other in (card(title='Changed title'), card(start='14:00'), card(location='Different hall')):
            with self.assertRaises(SourceError):
                self.parse(listing([card(), other]))

    def test_past_future_window_and_source_order_are_bounded(self):
        self.assertEqual(self.parse(listing([card(when='OCT 29 2026', weekday='THURSDAY'),
            card(when='FEB 1 2027', weekday='MONDAY')])), ())
        rows = [card(title='SYNTHETIC ' + str(i), post=f'https://www.instagram.com/p/Item{i}/') for i in range(12)]
        self.assertEqual([e.rank for e in self.parse(listing(rows))], list(range(1, 10)))
        with self.assertRaises(SourceError):
            self.parse(listing(rows * 5))

    def test_only_explicit_community_clause_is_attributed(self):
        event, = self.parse(listing([card(caption='Music — Synthetic Festival is bringing the whole community together.')]))
        self.assertTrue(event.community)
        self.assertEqual(event.source_attributes, (('community', 'is bringing the whole community together'),))
        self.assertIsNone(event.outdoor)
        self.assertIsNone(event.day_party)
        for caption in ('Afternoon at the waterfront', 'A town hall event',
                        'It is not true that Synthetic Festival is bringing the whole community together.',
                        'Synthetic Festival is bringing the whole community together?',
                        'Synthetic Festival is bringing the whole community together, but that is false.'):
            event, = self.parse(listing([card(caption=caption)]))
            self.assertEqual(event.source_attributes, ())
            self.assertIsNone(event.community)

    def test_unsafe_social_reference_is_omitted_without_changing_fixed_source_link(self):
        for value in ('https://evil.invalid/p/One/', 'javascript:alert(1)',
                'http://www.instagram.com/p/One/', 'https://user@instagram.com/p/One/',
                'https://instagram.com:443/p/One/', 'https://instagram.com/p/One/?next=https://evil.invalid',
                'https://www.instagram.com/p/One/?igsh=a&igsh=b', 'https://instagram.com/direct/inbox/',
                'https://instagram.com/p/One/#redirect'):
            with self.subTest(value=value):
                event, = self.parse(listing([card(post=value)]))
                self.assertIsNone(event.referenced_post)
                self.assertEqual(event.url, source.LISTING_URL)

    def test_drift_challenge_truncation_and_empty_are_not_inventory(self):
        for raw in (b'', b'<html>CANARY challenge</html>', listing([]),
                listing().replace(b'UPCOMING DANCES', b'PAST DANCES'),
                listing().replace(b'Calendar_eventDate__', b'Changed_eventDate__'),
                listing().replace(b'https://soundsystemsofmelbourne.com.au/shows', b'https://evil.invalid/shows'),
                listing().replace(b'</div>', b'', 1),
                listing().replace(b'class="Calendar_eventTitle__testHash"', b'class="Calendar_eventTitle__testHash" class="other"')):
            with self.subTest(raw=raw[:30]), self.assertRaises(SourceError):
                self.parse(raw)

    def test_byte_structure_and_retrieval_time_limits(self):
        for raw in (b'x' * (source.MAX_BYTES + 1), b'\xff',
                listing().replace(b'CANARY full article', b'<i></i>' * 4001),
                listing().replace(b'CANARY full article', b'<div>' * 49)):
            with self.assertRaises((SourceError, UnicodeError)):
                self.parse(raw)
        with self.assertRaises(SourceError):
            source.parse_listing(listing(), retrieved_at='2026-10-30')


class SoundSystemsTransportTests(TestCase):
    def test_exact_public_html_endpoint_and_no_feed_or_social_follow(self):
        with patch.object(transport, '_send', return_value=(200, listing())) as send:
            self.assertEqual(source.fetch_listing(NOW).status, 'ready')
        send.assert_called_once()
        command = send.call_args.args[0]
        self.assertEqual(command[-1], source.LISTING_URL)
        for flag, value in (('--request', 'GET'), ('--max-time', '5'), ('--max-filesize', str(source.MAX_BYTES)),
                            ('--max-redirs', '0'), ('--retry', '0'), ('--proxy', '')):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn('--no-netrc', command)
        self.assertIn('Accept: text/html', command)
        for excluded in ('--location', 'basic.ics', 'instagram.com', 'supabase', 'authorization'):
            self.assertNotIn(excluded, repr(command).lower())

    def test_errors_and_rate_limits_do_not_fallback_or_leak_source(self):
        for code, raw, expected in ((403, b'CANARY', 'error'), (429, b'CANARY', 'rate-limited'),
                (302, b'CANARY', 'error'), (200, b'CANARY', 'invalid'), (200, b'\xff', 'invalid')):
            with patch.object(transport, 'soundsystems', return_value=(code, raw)) as send:
                result = source.fetch_listing(NOW)
                self.assertEqual(result.status, expected)
                self.assertEqual(result.candidates, ())
                self.assertNotIn('CANARY', repr(result))
                send.assert_called_once()
        with patch.object(transport, 'soundsystems', side_effect=TimeoutError('CANARY')):
            self.assertEqual(source.fetch_listing(NOW).status, 'timeout')

    def test_recognized_out_of_window_cards_report_bounded_empty(self):
        with patch.object(transport, 'soundsystems', return_value=(200, listing([card(when='FEB 1 2027', weekday='MONDAY')]))):
            result = source.fetch_listing(NOW)
        self.assertEqual(result.status, 'empty')
        self.assertIn('wider coverage and ticket stock remain unknown', result.message)
