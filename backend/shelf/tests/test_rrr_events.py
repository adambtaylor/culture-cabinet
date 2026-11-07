"""Synthetic HTML only; upstream pages are evidence, never test fixtures."""
from datetime import timedelta
from hashlib import sha256
from html import escape
import json
from unittest import TestCase
from unittest.mock import patch

from shelf import discovery_sources as sources, discovery_transport as transport
from shelf import rrr_events as rrr
from shelf.tests.test_discovery_transport import NOW


def card(event_id=1, *, name='SYNTHETIC event', path=None, date='Friday, 30 October',
         calendar='Gig Guide', venue='Synthetic venue'):
    marker = escape(json.dumps({'id': event_id, 'name': name, 'type': 'events'}), quote=True)
    path = path or f'/events/synthetic-{event_id}'
    return (f'<div class="grid-col grid-col--3"><div data-content="{marker}"></div>'
            '<div class="card clearfix"><div class="card__text">'
            f'<div class="card__meta mb-xsmall"><span>{escape(calendar)}</span></div>'
            f'<h1 class="card__title"><a class="card__anchor" href="{escape(path, quote=True)}">'
            f'{escape(name)}</a></h1><span class="card__meta"><div class="d-block">{escape(date)}</div>'
            + (f'<div class="d-block">{escape(venue)}</div>' if venue else '')
            + '</span></div></div></div>')


def listing(*rows):
    return ('<!doctype html><html><body>' + ''.join(rows or [card()]) + '</body></html>').encode()


class RRRParserTests(TestCase):
    def parse(self, raw):
        return rrr.parse_listing(raw, retrieved_at=NOW.isoformat())

    def test_source_identity_hash_and_unknowns_survive_normalization(self):
        raw = listing(card(81, name='SYNTHETIC A & B'))
        event, = self.parse(raw)
        self.assertEqual(event.title, 'SYNTHETIC A & B')
        self.assertEqual(event.key, 'official:event:' + sha256(b'rrr\0id\0' + b'81').hexdigest())
        self.assertEqual(event.url, 'https://www.rrr.org.au/events/synthetic-81')
        self.assertEqual(event.source_event_id, '81')
        self.assertEqual(event.listed_date, 'Friday, 30 October')
        self.assertEqual(event.category, 'music')
        self.assertEqual(event.venue, 'Synthetic venue')
        self.assertEqual(event.provenance, 'live-automated')
        self.assertEqual(event.evidence[0].snapshot_sha256, sha256(raw).hexdigest())
        self.assertEqual(event.evidence[0].retrieved_at, NOW.isoformat())
        self.assertEqual(event.evidence[0].position, 1)
        self.assertIsNone(event.starts_at)
        self.assertIsNone(event.ends_at)
        self.assertIsNone(event.event_timezone)
        self.assertIsNone(event.entry_fee)
        self.assertIsNone(event.lineup)
        self.assertIsNone(event.venue_key)
        self.assertEqual(event.roots, ())
        self.assertEqual(event.eligibility, 'unknown')
        self.assertEqual(event.status, 'unknown')
        self.assertIn('year and start time unverified', event.date_status(NOW))
        self.assertIn('availability unconfirmed', event.freshness(NOW))
        self.assertIn('Stale', event.freshness(NOW + timedelta(minutes=16)))

    def test_first_nine_source_positions_no_crawl_or_title_deduplication(self):
        events = self.parse(listing(*(card(i) for i in range(1, 16))))
        self.assertEqual([e.source_event_id for e in events], [str(i) for i in range(1, 10)])
        self.assertEqual([e.rank for e in events], list(range(1, 10)))
        self.assertEqual(len({e.key for e in events}), 9)

    def test_calendar_classification_does_not_invent_gallery_or_public_eligibility(self):
        for calendar, expected in [('Gig Guide', 'music'), ('Community Events', 'community'),
                                   ('Arts Diary', 'all'), ('RRR Presents', 'all')]:
            event, = self.parse(listing(card(calendar=calendar, venue='')))
            self.assertEqual(event.category, expected)
            self.assertEqual(event.venue, '')
            self.assertIsNone(event.searchable)
            self.assertEqual(event.eligibility, 'unknown')

    def test_cross_year_range_retained_without_inferred_year_or_precision(self):
        event, = self.parse(listing(card(date='30 December – 2 January')))
        self.assertEqual(event.listed_date, '30 December – 2 January')
        self.assertEqual(event.date_precision, 'unknown')
        self.assertIsNone(event.source_start)
        self.assertIsNone(event.source_end)

    def test_source_plain_date_span_without_venue_is_supported(self):
        raw = listing(card(venue='')).replace(b'<div class="d-block">Friday, 30 October</div>',
                                               b'Friday, 30 October')
        event, = self.parse(raw)
        self.assertEqual(event.listed_date, 'Friday, 30 October')
        self.assertEqual(event.venue, '')
        self.assertIsNone(event.starts_at)

    def test_unsafe_or_non_detail_links_fail_closed(self):
        for path in ('//evil.invalid/x', 'http://www.rrr.org.au/events/x',
                     'https://www.rrr.org.au/events/x', '/events', '/events/x?q=1',
                     '/events/x#fragment', '/events/../admin', '/events/%78',
                     '/events/x/redirect', '/events/x\\evil'):
            with self.subTest(path=path), self.assertRaises(sources.SourceError):
                self.parse(listing(card(path=path)))

    def test_invalid_identity_and_conflicting_fields_fail_closed(self):
        for event_id in (True, 0, -1, 2**63, '123'):
            with self.subTest(event_id=event_id), self.assertRaises(sources.SourceError):
                self.parse(listing(card(event_id)))
        changed = card().replace('>SYNTHETIC event</a>', '>Different title</a>')
        with self.assertRaises(sources.SourceError):
            self.parse(listing(changed))

    def test_duplicate_ids_or_urls_do_not_create_ambiguous_records(self):
        for rows in ((card(1), card(1)), (card(1), card(2, path='/events/synthetic-1'))):
            with self.assertRaises(sources.SourceError):
                self.parse(listing(*rows))

    def test_source_drift_truncation_and_challenge_are_not_empty_inventory(self):
        for raw in (b'<html>CANARY access challenge</html>', b'', listing().replace(b'card__title', b'new-title'),
                    listing()[:-30], listing(card(date='')), listing(card(name='x'*301)),
                    listing().replace(b'class="card__anchor"', b'class="card__anchor" class="other"')):
            with self.subTest(raw=raw[:30]), self.assertRaises(sources.SourceError):
                self.parse(raw)

    def test_response_depth_and_node_budgets(self):
        for raw in (b'x' * (sources.MAX_BYTES + 1),
                    b'<div class="grid-col">' + b'<div>'*49,
                    b'<div class="grid-col">' + b'<i></i>'*4001 + b'</div>'):
            with self.assertRaises(sources.SourceError):
                self.parse(raw)
        with self.assertRaises(sources.SourceError):
            rrr.parse_listing(listing(), retrieved_at='2026-01-01')


class RRRTransportTests(TestCase):
    def test_fixed_anonymous_request_uses_existing_transport_bounds(self):
        with patch.object(transport, '_send', return_value=(200, listing())) as send:
            result = sources.fetch_public('rrr', '', '', NOW)
        self.assertEqual(result.status, 'ready')
        send.assert_called_once()
        command = send.call_args.args[0]
        self.assertEqual(command[-1], 'https://www.rrr.org.au/events')
        self.assertEqual(command[command.index('--request')+1], 'GET')
        self.assertIn('Accept: text/html', command)
        for flag, value in [('--max-filesize', str(sources.MAX_BYTES)), ('--max-time', '5'),
                            ('--max-redirs', '0'), ('--retry', '0'), ('--proxy', '')]:
            self.assertEqual(command[command.index(flag)+1], value)
        self.assertIn('--no-netrc', command)
        self.assertNotIn('--location', command)
        self.assertNotIn('authorization', repr(command).lower())

    def test_http_errors_timeout_and_invalid_html_are_safe_no_retry(self):
        for status, raw, expected in [(403, b'CANARY', 'error'), (429, b'CANARY', 'rate-limited'),
                (302, b'CANARY', 'error'), (500, b'CANARY', 'error'),
                (200, b'<html>CANARY</html>', 'invalid'), (200, b'\xff', 'invalid'),
                (200, b'x'*(sources.MAX_BYTES+1), 'invalid')]:
            with self.subTest(expected=expected), patch.object(transport, 'rrr', return_value=(status, raw)) as send:
                result = sources.fetch_public('rrr', '', '', NOW)
                self.assertEqual(result.status, expected)
                self.assertNotIn('CANARY', repr(result))
                self.assertEqual(result.candidates, ())
                send.assert_called_once()
        with patch.object(transport, 'rrr', side_effect=TimeoutError('CANARY')) as send:
            self.assertEqual(sources.fetch_public('rrr', '', '', NOW).status, 'timeout')
            send.assert_called_once()

    def test_caller_cannot_supply_query_seed_mode_or_destination(self):
        with patch.object(transport, '_send') as send:
            for seed, mode in [('https://evil.invalid', ''), ('', 'medium')]:
                self.assertEqual(sources.fetch_public('rrr', seed, mode, NOW).status, 'invalid')
            send.assert_not_called()
