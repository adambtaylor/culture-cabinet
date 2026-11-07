"""Synthetic Beat markup only; real pages stay outside distributed test fixtures."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch

from shelf import beat_events as beat, discovery_transport as transport
from shelf.discovery_sources import SourceError, fetch_public, MAX_BYTES

NOW = datetime(2026, 10, 30, 0, 10, tzinfo=timezone.utc)
URL = 'https://beat.com.au/synthetic-event/'


def card(*, slug='synthetic-event', title='Synthetic event', date='Fri 30 Oct 2026', extra=''):
    return f'<div class="gig-card"><div class="gig-date">{date}</div>{extra}<h3 class="gig-title"><a href="https://beat.com.au/{slug}/">{title}</a></h3><div class="gig-location">Synthetic Hall</div><div class="gig-price">$25.00</div></div>'


def listing(**kwargs):
    return card(**kwargs).encode()


def detail(*, canonical=URL, title='Synthetic event', date='Fri 30 Oct 2026', wording='DOORS 7PM // SHOW STARTS 8:30PM', address='123 Example Street'):
    def field(name, value):
        return f'<div class="article-meta-item"><div class="article-meta-headline">{name}</div><div class="article-meta-content">{value}</div></div>'
    location = f'<div><a>Synthetic Hall</a></div><div>{address}</div>'
    return (f'<link rel="canonical" href="{canonical}"><h1>{title}</h1><div class="article-meta">' +
            field('Date', date) + field('Location', location) + field('Time', '7pm') + field('Price', '$25.00') +
            f'</div><div class="article-content__primary"><p>{wording}</p><p>Private synthetic prose not to retain.</p></div>').encode()


def event(with_details=False):
    item = beat.parse_listing(listing(), retrieved_at=NOW.isoformat())[0]
    return beat.parse_detail(detail(), event=item, retrieved_at=NOW.isoformat()) if with_details else item


class BeatTests(TestCase):
    def test_real_date_facts_and_provenance_without_inventory_claim(self):
        item = event()
        self.assertEqual((item.starts_at, item.source_start, item.venue), ('2026-10-30', 'Fri 30 Oct 2026', 'Synthetic Hall'))
        self.assertEqual(item.evidence[0].snapshot_sha256, __import__('hashlib').sha256(listing()).hexdigest())
        self.assertIsNone(item.event_timezone)
        self.assertIn('unverified', item.date_status(NOW))
        self.assertEqual(item.price_label, '$25.00')

    def test_date_ambiguity_ranges_past_and_horizon_are_not_sessions(self):
        for date in ('Fri 30 Oct', 'Thu 30 Oct 2026', 'Fri 30 Oct 2026 – Sat 31 Oct 2026', 'Thu 29 Oct 2026', 'Sat 14 Nov 2026'):
            with self.subTest(date=date):
                self.assertEqual(beat.parse_listing(listing(date=date), retrieved_at=NOW.isoformat()), ())
        self.assertEqual(beat.parse_listing(listing(extra='<div class="gig-end-date">Sat 31 Oct 2026</div>'), retrieved_at=NOW.isoformat()), ())

    def test_bounded_cards_and_results_and_duplicate_conflicts(self):
        raw = ''.join(card(slug=f'event-{i}') for i in range(60)).encode()
        self.assertEqual(len(beat.parse_listing(raw, retrieved_at=NOW.isoformat())), 9)
        raw = (card(date='Thu 29 Oct 2026') * 48 + card()).encode()
        self.assertEqual(beat.parse_listing(raw, retrieved_at=NOW.isoformat()), ())
        self.assertEqual(len(beat.parse_listing(listing() * 2, retrieved_at=NOW.isoformat())), 1)
        with self.assertRaises(SourceError):
            beat.parse_listing(listing() + listing(title='Changed'), retrieved_at=NOW.isoformat())

    def test_unrecognized_truncated_excessive_and_external_are_rejected(self):
        for raw in (b'<h1>Challenge</h1>', listing()[:-6], b'x' * (beat.MAX_BYTES + 1), listing().replace(b'https://beat.com.au/', b'https://evil.invalid/')):
            with self.subTest(raw=raw[:30]), self.assertRaises((SourceError, ValueError)):
                beat.parse_listing(raw, retrieved_at=NOW.isoformat())
        with patch.object(transport, '_send') as send:
            for url in ('http://beat.com.au/a/', 'https://beat.com.au/a/?key=secret', 'https://beat.com.au/wp-admin/', 'https://evil.invalid/a/'):
                with self.assertRaises(SourceError): transport.beat_detail(url)
            send.assert_not_called()

    def test_details_keep_summary_doors_and_show_separate_and_discard_prose(self):
        item = event(True)
        self.assertEqual((item.details.time_label, item.details.doors_time, item.details.show_time), ('7pm', '19:00', '20:30'))
        self.assertEqual(item.details.address, '123 Example Street')
        self.assertNotIn('Private synthetic prose', repr(asdict(item)))
        self.assertNotIn('end_time', asdict(item.details))

    def test_conflicting_clocks_remain_unknown_and_summary_is_not_show(self):
        for wording, doors, show in [('Music at 7pm', None, None), ('DOORS 7PM SHOW STARTS 8PM SHOW STARTS 9PM', '19:00', None), ('DOORS 13PM SHOW STARTS 8:30PM', None, '20:30')]:
            result = beat.parse_detail(detail(wording=wording), event=event(), retrieved_at=NOW.isoformat())
            self.assertEqual((result.details.doors_time, result.details.show_time), (doors, show))

    def test_detail_identity_and_duplicate_canonical(self):
        raw = detail() + f'<link rel="canonical" href="{URL}">'.encode()
        self.assertEqual(beat.parse_detail(raw, event=event(), retrieved_at=NOW.isoformat()).detail_status, 'ready')
        for raw in (detail(canonical='https://beat.com.au/changed/'), detail(title='Changed'), detail(date='Sat 31 Oct 2026'), detail().replace(b'Synthetic Hall', b'Other Hall'), raw + b'<link rel="canonical" href="https://beat.com.au/other/">'):
            with self.assertRaises(SourceError): beat.parse_detail(raw, event=event(), retrieved_at=NOW.isoformat())

    def test_failure_clears_detail_and_never_leaks_source_error(self):
        for response in ((403, b'CANARY'), (200, b'<h1>Challenge</h1>')):
            with patch.object(transport, 'beat_detail', return_value=response) as send:
                result = beat.fetch_detail(event(True), NOW)
                self.assertIsNone(result.details)
                self.assertEqual(result.detail_status, 'unavailable')
                self.assertNotIn('CANARY', repr(result))
                send.assert_called_once_with(URL)

    def test_fixed_anonymous_transport_budget_does_not_change_other_sources(self):
        self.assertEqual(transport._base()[transport._base().index('--max-filesize') + 1], str(MAX_BYTES))
        with patch.object(transport, '_send', return_value=(200, listing())) as send:
            outcome = fetch_public('beat', '', '', NOW)
            self.assertEqual(outcome.status, 'ready')
            args = send.call_args.args[0]
            self.assertEqual(args[-1], beat.LISTING_URL)
            self.assertEqual(send.call_args.kwargs, {'max_bytes': beat.MAX_BYTES})
            for flag, value in (('--max-filesize', str(beat.MAX_BYTES)), ('--max-redirs', '0'), ('--retry', '0'), ('--max-time', '5')):
                self.assertEqual(args[args.index(flag) + 1], value)
            self.assertIn('--no-netrc', args)
            self.assertNotIn('--location', args)
