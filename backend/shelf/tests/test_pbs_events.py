"""Synthetic PBS-shaped markup and isolated limiter files; no provider calls."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch

from shelf import pbs_events as pbs, discovery_transport as transport
from shelf.discovery_sources import SourceError

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def card(slug='synthetic-event', *, title='SYNTHETIC music event', when='Sun 15 Nov 2026',
         machine='2026-11-15T00:00:00+11:00', end=None, notice='Free Event'):
    def span(css, text, content):
        return f'<span class="{css}" content="{escape(content, quote=True)}">{escape(text)}</span>'
    dates = span('date-display-single', when, machine) if end is None else (
        '<span class="date-display-range">' + span('date-display-start', when, machine)
        + ' to ' + span('date-display-end', *end) + '</span>')
    url = '/news/' + slug
    return (f'<div class="node node-news view-mode-card" about="{escape(url, quote=True)}">'
        + (f'<div class="field-name-field-news-alert">{escape(notice)}</div>' if notice else '')
        + f'<a class="group-more-info" href="{escape(url, quote=True)}">'
        '<div class="field-name-field-news-category">PBS Events</div>'
        f'<div class="field-title"><h3>{escape(title)}</h3></div>'
        f'<div class="field-name-field-news-date">{dates}</div>'
        '<div class="field-body">CANARY article prose at Fictional Venue, 9pm.</div>'
        '</a></div>')


def listing(rows=None, *, archive=''):
    rows = [card()] if rows is None else rows
    return ('<html><head><link rel="canonical" href="' + pbs.LISTING_URL + '" /></head>'
        '<body><h1 class="title">PBS Events</h1>'
        '<div class="view view-taxonomy-news view-display-id-attachment_1"><div class="view-content">'
        + ''.join(rows) + '</div></div><h2>Past News</h2>' + archive + '</body></html>').encode()


class PBSParserTests(TestCase):
    def parse(self, raw):
        return pbs.parse_listing(raw, retrieved_at=NOW.isoformat())

    def test_minimal_facts_provenance_and_midnight_is_not_show_time(self):
        raw = listing([card(title='SYNTHETIC A & B')])
        event, = self.parse(raw)
        self.assertEqual(event.title, 'SYNTHETIC A & B')
        self.assertEqual(event.key, 'official:event:' + sha256(
            b'pbs\0url\0https://www.pbsfm.org.au/news/synthetic-event').hexdigest())
        self.assertEqual(event.url, 'https://www.pbsfm.org.au/news/synthetic-event')
        self.assertEqual(event.starts_at, '2026-11-15')
        self.assertEqual(event.date_precision, 'date')
        self.assertEqual(event.listed_date, 'Sun 15 Nov 2026')
        self.assertEqual(event.source_notice, 'Free Event')
        self.assertEqual(event.venue, '')
        self.assertEqual(event.source_id, 'pbs')
        self.assertEqual(event.provenance, 'live-automated')
        self.assertEqual(event.evidence[0].snapshot_sha256, sha256(raw).hexdigest())
        self.assertEqual(event.evidence[0].retrieved_at, NOW.isoformat())
        self.assertIsNone(event.event_timezone)
        self.assertIsNone(event.entry_fee)
        self.assertIsNone(event.entry_currency)
        self.assertIsNone(event.lineup)
        self.assertEqual(event.status, 'unknown')
        self.assertEqual(event.eligibility, 'unknown')
        self.assertNotIn('CANARY', repr(event))
        self.assertIn('timezone unverified', event.date_status(NOW))
        self.assertIn('Stale', event.freshness(NOW + timedelta(minutes=16)))

    def test_past_archive_never_leaks_into_upcoming_cards(self):
        event, = self.parse(listing(archive=card('archive-trap', title='SYNTHETIC archive')))
        self.assertNotIn('archive', event.url)

    def test_yearless_date_keeps_unknown_despite_machine_timestamp(self):
        event, = self.parse(listing([card(when='15 November')]))
        self.assertIsNone(event.starts_at)
        self.assertEqual(event.date_precision, 'unknown')
        self.assertIn('Year or date unverified', event.date_status(NOW))

    def test_explicit_ranges_stay_calendar_ranges_not_sessions(self):
        event, = self.parse(listing([card(when='Fri 30 Oct 2026', machine='2026-10-30T00:00:00+11:00',
            end=('Sun 1 Nov 2026', '2026-11-01T00:00:00+11:00'))]))
        self.assertEqual((event.starts_at, event.ends_at), ('2026-10-30', '2026-11-01'))
        self.assertEqual((event.date_precision, event.end_precision), ('date', 'date'))
        self.assertIn(' to ', event.listed_date)
        self.assertIsNone(event.event_timezone)

    def test_past_and_beyond_window_cards_produce_truthful_bounded_empty(self):
        raw = listing([card(when='Thu 29 Oct 2026', machine='2026-10-29T00:00:00+11:00'),
            card('far-future', when='Mon 1 Feb 2027', machine='2027-02-01T00:00:00+11:00')])
        self.assertEqual(self.parse(raw), ())
        with patch.object(transport, 'pbs', return_value=(200, raw)):
            result = pbs.fetch_listing(NOW)
        self.assertEqual(result.status, 'empty')
        self.assertIn('does not establish no events', result.message)

    def test_explicit_date_conflicts_are_rejected(self):
        for options in (
            {'when': 'Sat 15 Nov 2026'}, {'when': 'Tue 31 Feb 2026'},
            {'machine': '2026-11-16T00:00:00+11:00'}, {'machine': '2026-11-15'},
            {'end': ('Sat 14 Nov 2026', '2026-11-14T00:00:00+11:00')},
        ):
            with self.subTest(options=options), self.assertRaises(SourceError):
                self.parse(listing([card(**options)]))

    def test_exact_duplicates_coalesce_without_title_based_merge(self):
        same = card()
        events = self.parse(listing([same, same, card('different-event')]))
        self.assertEqual(len(events), 2)
        self.assertEqual([e.rank for e in events], [1, 3])
        for other in (card(title='Conflicting title'), card(notice='Sold Out!')):
            with self.assertRaises(SourceError):
                self.parse(listing([same, other]))

    def test_limit_source_positions_and_keep_notice_unverified(self):
        events = self.parse(listing([card('synthetic-' + str(i), notice='Sold Out!') for i in range(20)]))
        self.assertEqual(len(events), 9)
        self.assertEqual([e.rank for e in events], list(range(1, 10)))
        self.assertTrue(all(e.status == 'unknown' and e.source_notice == 'Sold Out!' for e in events))
        with self.assertRaises(SourceError):
            self.parse(listing([card('synthetic-' + str(i)) for i in range(49)]))

    def test_safe_source_urls_and_card_identity_binding(self):
        for value in ('https://evil.invalid/news/x', 'http://www.pbsfm.org.au/news/x',
                '//www.pbsfm.org.au/news/x', '/news/x?q=1', '/news/../login', '/news/%78',
                '/news/x/next', '/news/pbs-events', 'https://user@www.pbsfm.org.au/news/x',
                'https://www.pbsfm.org.au:443/news/x'):
            with self.subTest(value=value), self.assertRaises(SourceError):
                pbs.event_url(value)
        raw = listing().replace(b'about="/news/synthetic-event"', b'about="/news/other"')
        with self.assertRaises(SourceError):
            self.parse(raw)

    def test_missing_or_changed_page_is_not_zero_inventory(self):
        for raw in (b'', b'<html>CANARY challenge</html>', listing([]),
                listing().replace(b'PBS Events', b'Changed Source'),
                listing().replace(b'field-name-field-news-date', b'new-date-field'),
                listing().replace(b'class="group-more-info"', b'class="group-more-info" class="other"'),
                listing().replace(b'</a></div>', b'</a>'),
                listing().replace(b'</head>', b'<link rel="canonical" href="' + pbs.LISTING_URL.encode() + b'" /></head>')):
            with self.subTest(raw=raw[:30]), self.assertRaises(SourceError):
                self.parse(raw)

    def test_response_encoding_structure_and_time_bounds(self):
        for raw in (b'x' * (pbs.MAX_BYTES + 1), b'\xff',
                listing().replace(b'<div class="field-body">', b'<div>' * 49),
                listing().replace(b'CANARY article prose', b'<i></i>' * 4001)):
            with self.subTest(raw=raw[:30]), self.assertRaises((SourceError, UnicodeError)):
                self.parse(raw)
        with self.assertRaises(SourceError):
            pbs.parse_listing(listing(), retrieved_at='2026-10-30')


class PBSTransportTests(TestCase):
    def test_one_fixed_anonymous_read_after_interval_reservation(self):
        with patch.object(transport, '_reserve_pbs_slot') as reserve, \
                patch.object(transport, '_send', return_value=(200, listing())) as send:
            self.assertEqual(pbs.fetch_listing(NOW).status, 'ready')
        reserve.assert_called_once()
        send.assert_called_once()
        command = send.call_args.args[0]
        self.assertEqual(command[-1], pbs.LISTING_URL)
        for flag, value in (('--request', 'GET'), ('--max-time', '5'), ('--max-filesize', str(pbs.MAX_BYTES)),
                            ('--max-redirs', '0'), ('--retry', '0'), ('--proxy', '')):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn('Accept: text/html', command)
        self.assertIn('--no-netrc', command)
        self.assertNotIn('--location', command)
        self.assertNotIn('authorization', repr(command).lower())

    def test_errors_are_safe_and_never_retry(self):
        for code, raw, expected in ((403, b'CANARY', 'error'), (429, b'CANARY', 'rate-limited'),
                (302, b'CANARY', 'error'), (200, b'CANARY', 'invalid'), (200, b'\xff', 'invalid')):
            with self.subTest(code=code), patch.object(transport, 'pbs', return_value=(code, raw)) as send:
                result = pbs.fetch_listing(NOW)
                self.assertEqual(result.status, expected)
                self.assertEqual(result.candidates, ())
                self.assertNotIn('CANARY', repr(result))
                send.assert_called_once()
        for exception, status in ((TimeoutError('CANARY'), 'timeout'),
                (transport.PBSIntervalPending('CANARY'), 'rate-limited'), (OSError('CANARY'), 'invalid')):
            with patch.object(transport, 'pbs', side_effect=exception):
                result = pbs.fetch_listing(NOW)
                self.assertEqual(result.status, status)
                self.assertNotIn('CANARY', repr(result))


class PBSSharedIntervalTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'pbs.rate'
        setting = patch.object(transport, '_PBS_RATE_FILE', str(self.path))
        setting.start()
        self.addCleanup(setting.stop)

    def test_shared_interval_survives_new_calls_and_clock_rollback(self):
        with patch.object(transport.time, 'monotonic', return_value=100):
            transport._reserve_pbs_slot()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        for clock in (100, 109.999, 99):
            with patch.object(transport.time, 'monotonic', return_value=clock), self.assertRaises(transport.PBSIntervalPending):
                transport._reserve_pbs_slot()
        with patch.object(transport.time, 'monotonic', return_value=110):
            transport._reserve_pbs_slot()

    def test_new_process_cannot_bypass_same_host_reservation(self):
        transport._reserve_pbs_slot()
        code = ('import sys\nsys.path.insert(0,sys.argv[2])\nfrom shelf import discovery_transport as t\n'
            't._PBS_RATE_FILE=sys.argv[1]\ntry:\n t._reserve_pbs_slot()\n'
            'except t.PBSIntervalPending:\n sys.exit(0)\nelse:\n sys.exit(9)\n')
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.path), str(Path(transport.__file__).resolve().parents[1])],
            capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_prior_boot_does_not_block_current_monotonic_clock(self):
        self.path.write_text(json.dumps({'boot': '00000000-0000-0000-0000-000000000000', 'at': 999999999}))
        self.path.chmod(0o600)
        transport._reserve_pbs_slot()
        self.assertNotEqual(json.loads(self.path.read_text())['boot'], '00000000-0000-0000-0000-000000000000')

    def test_locked_or_unsafe_limiter_never_starts_network(self):
        import fcntl
        self.path.write_bytes(b'')
        self.path.chmod(0o600)
        with self.path.open('rb') as held, patch.object(transport, '_send') as send:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(pbs.fetch_listing(NOW).status, 'rate-limited')
            send.assert_not_called()
        for raw in (b'CANARY', b'{}', b'x' * 201,
                    b'{"boot":"x","at":0,"at":1}',
                    b'{"boot":"00000000-0000-0000-0000-000000000000","at":NaN}'):
            self.path.write_bytes(raw)
            with patch.object(transport, '_send') as send:
                self.assertEqual(pbs.fetch_listing(NOW).status, 'invalid')
                send.assert_not_called()
        self.path.chmod(0o644)
        with patch.object(transport, '_send') as send:
            self.assertEqual(pbs.fetch_listing(NOW).status, 'invalid')
            send.assert_not_called()

    def test_symlink_or_hardlink_receipt_refuses_without_touching_target(self):
        target = Path(self.directory.name) / 'target'
        target.write_bytes(b'CANARY')
        target.chmod(0o600)
        self.path.symlink_to(target)
        with patch.object(transport, '_send') as send:
            self.assertEqual(pbs.fetch_listing(NOW).status, 'invalid')
            send.assert_not_called()
        self.assertEqual(target.read_bytes(), b'CANARY')
        self.path.unlink()
        os.link(target, self.path)
        with patch.object(transport, '_send') as send:
            self.assertEqual(pbs.fetch_listing(NOW).status, 'invalid')
            send.assert_not_called()
        self.assertEqual(target.read_bytes(), b'CANARY')
