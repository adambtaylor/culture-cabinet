"""Synthetic detail pages test source ambiguity without network access."""
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import subprocess
import sys
from unittest import TestCase
from unittest.mock import patch

from shelf import discovery_sources as sources, discovery_transport as transport
from shelf import rrr_details as details, rrr_events as rrr
from shelf.tests.test_discovery_transport import NOW
from shelf.tests.test_rrr_events import card, listing

SINGLE = '<span class="d-block"> 6:00 pm-8:00 pm</span><span class="d-block">Friday, 30 October</span>'
RANGE = '<span class="event__details-starts d-block">Starts</span> 4:15 pm, 25 October 2026<span class="event__details-ends d-block">Ends</span> 6:00 pm, 8 November 2026'


def event():
    return rrr.parse_listing(listing(), retrieved_at=NOW.isoformat())[0]


def detail(*, when=SINGLE, venue='Synthetic venue', address='123 Synthetic Street',
           more='https://tickets.example.org/event/123', number=1, canonical=None,
           title='SYNTHETIC event'):
    marker = escape(json.dumps({'id': number, 'name': title, 'type': 'events'}), quote=True)
    canonical = canonical or event().url
    return (f'<html><head><link rel="canonical" href="{escape(canonical,quote=True)}" /></head>'
            f'<body><div data-content="{marker}"></div><section class="event event--simple">'
            f'<h1 class="event__title">{escape(title)}</h1><div class="event__details">'
            f'<h2 class="event__details-heading">When</h2><p class="event__details-copy">{when}</p></div>'
            + (f'<div class="event__details"><h2 class="event__details-heading">Where</h2>'
               f'<span class="event__details-copy"><p>{escape(venue)}</p><p>{escape(address)}</p></span></div>' if venue else '')
            + (f'<a class="action-button" ga-event-action="more-info-click" href="{escape(more,quote=True)}">More info '
               '<svg><title>Right arrow</title></svg></a>' if more else '')
            + '</section></body></html>').encode()


class DetailParserTests(TestCase):
    def parse(self, raw, **kwargs):
        return details.parse_detail(raw, event=kwargs.get('event', event()), retrieved_at=NOW.isoformat())

    def test_missing_year_retains_label_and_extracts_clock_address_and_source_link(self):
        raw = detail()
        result = self.parse(raw)
        self.assertEqual(result.when, '6:00 pm-8:00 pm Friday, 30 October')
        self.assertIsNone(result.start_date)
        self.assertIsNone(result.end_date)
        self.assertEqual((result.start_time, result.end_time), ('18:00', '20:00'))
        self.assertEqual(result.venue, 'Synthetic venue')
        self.assertEqual(result.address, '123 Synthetic Street')
        self.assertEqual(result.more_info_url, 'https://tickets.example.org/event/123')
        self.assertEqual(result.snapshot_sha256, sha256(raw).hexdigest())
        enriched = replace(event(), details=result)
        self.assertIsNone(enriched.starts_at)
        self.assertIsNone(enriched.event_timezone)
        self.assertIn('year unverified', enriched.date_status(NOW))
        self.assertNotIn('start time unverified', enriched.date_status(NOW))

    def test_explicit_range_preserves_both_years_without_timezone_or_upcoming_claim(self):
        result = self.parse(detail(when=RANGE))
        self.assertEqual((result.start_date, result.end_date), ('2026-10-25', '2026-11-08'))
        self.assertEqual((result.start_time, result.end_time), ('16:15', '18:00'))
        enriched = replace(event(), details=result)
        self.assertNotIn('year unverified', enriched.date_status(NOW))
        self.assertIn('timezone unverified', enriched.date_status(NOW))
        self.assertIsNone(enriched.starts_at)
        self.assertEqual(enriched.status, 'unknown')

    def test_explicit_cross_year_and_midnight_noon(self):
        raw = RANGE.replace('4:15 pm, 25 October 2026', '12:00 am, 31 December 2026').replace('6:00 pm, 8 November 2026', '12:00 pm, 1 January 2027')
        result = self.parse(detail(when=raw))
        self.assertEqual((result.start_date, result.end_date), ('2026-12-31', '2027-01-01'))
        self.assertEqual((result.start_time, result.end_time), ('00:00', '12:00'))

    def test_ambiguous_missing_time_and_multiple_times_remain_unknown(self):
        for when in ('Doors 6pm; show 8pm, late finish',
                     '<span class="d-block">Doors 6pm / show 8pm</span><span class="d-block">30 October</span>',
                     '<span class="d-block">25:77 pm</span><span class="d-block">Friday, 30 October</span>'):
            result = self.parse(detail(when=when))
            self.assertIsNone(result.start_time)
            self.assertIsNone(result.start_date)
        result = self.parse(detail(when=RANGE.replace('4:15 pm, ', '').replace('6:00 pm, ', '')))
        self.assertEqual(result.start_date, '2026-10-25')
        self.assertIsNone(result.start_time)

    def test_invalid_explicit_dates_weekday_and_backwards_range_rejected(self):
        for when in (RANGE.replace('25 October', '31 February'), RANGE.replace('8 November 2026', '8 November 2025'),
                     SINGLE.replace('Friday, 30 October', 'Monday, 30 October 2026')):
            with self.assertRaises(ValueError):
                self.parse(detail(when=when))

    def test_identity_title_venue_and_canonical_disagreements_rejected(self):
        for changes in ({'number': 2}, {'number': True}, {'title': 'Wrong title'},
                        {'venue': 'Different venue'}, {'canonical': 'https://www.rrr.org.au/events/another'}):
            with self.subTest(changes=changes), self.assertRaises(sources.SourceError):
                self.parse(detail(**changes))

    def test_missing_venue_and_link_are_unknown_not_zero_or_free(self):
        result = self.parse(detail(venue='', more=''))
        self.assertEqual((result.venue, result.address), ('', ''))
        self.assertIsNone(result.more_info_url)
        self.assertEqual(event().eligibility, 'unknown')
        self.assertIsNone(event().entry_fee)

    def test_unsafe_or_query_links_are_omitted_without_rewriting(self):
        for url in ('javascript:alert(1)', 'http://example.org/e', 'https://user:pass@example.org/e',
                    'https://example.org:8443/e', 'https://example.org/e?redirect=x',
                    'https://example.org/e#fragment', 'https://127.0.0.1/e', 'https://localhost/e',
                    'https://host.local/e', 'https://example.org/%250aheader', 'https://example.org/a\\b'):
            with self.subTest(url=url):
                self.assertIsNone(self.parse(detail(more=url)).more_info_url)

    def test_ambiguous_link_set_omitted_and_ambiguous_when_rejected(self):
        raw = detail()
        extra = b'<a class="action-button" ga-event-action="more-info-click" href="https://other.example.org/e">More info</a>'
        self.assertIsNone(self.parse(raw.replace(b'</section>', extra+b'</section>')).more_info_url)
        duplicate = b'<div class="event__details"><h2 class="event__details-heading">When</h2></div>'
        with self.assertRaises(sources.SourceError):
            self.parse(raw.replace(b'</section>', duplicate+b'</section>'))

    def test_challenge_truncation_and_budget_do_not_release_partial_details(self):
        for raw in (b'<html>Challenge CANARY</html>', detail()[:-40], b'x'*(sources.MAX_BYTES+1),
                    detail().replace(b'event__details-heading', b'changed-heading'),
                    detail().replace(b'<section class="event event--simple">', b'<section class="event">'+b'<div>'*49)):
            with self.assertRaises((sources.SourceError, KeyError)):
                self.parse(raw)

    def test_malformed_long_slug_rejects_promptly_in_both_source_parsers(self):
        # Regression for overlapping repetitions taking exponential time. A child
        # deadline keeps a future regression from hanging the entire suite.
        code = '''import sys
sys.path.insert(0,sys.argv[1])
from shelf.tests.test_rrr_details import detail,event
from shelf.tests.test_rrr_events import card,listing
from shelf.rrr_details import parse_detail,detail_url
from shelf.rrr_events import parse_listing
from shelf.discovery_sources import SourceError
path='/events/' + 'a-'*64 + 'a!'
for parse in (lambda:parse_listing(listing(card(path=path)),retrieved_at=event().retrieved_at),
              lambda:parse_detail(detail(canonical='https://www.rrr.org.au'+path),event=event(),retrieved_at=event().retrieved_at)):
    try: parse()
    except SourceError: pass
    else: raise AssertionError('Malformed slug accepted')
assert detail_url('https://www.rrr.org.au/events/synthetic--1').endswith('synthetic--1')
print('rejected')
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, str(Path(__file__).resolve().parents[2])],
                                capture_output=True, text=True, timeout=2, check=True)
        self.assertEqual(result.stdout.strip(), 'rejected')


class DetailTransportTests(TestCase):
    def test_exact_source_link_only_and_original_transport_budget(self):
        with patch.object(transport, '_send', return_value=(200, detail())) as send:
            result = details.fetch_detail(event(), NOW+timedelta(minutes=1))
        self.assertEqual(result.detail_status, 'ready')
        self.assertEqual(result.retrieved_at, event().retrieved_at)
        self.assertEqual(result.evidence, event().evidence)
        send.assert_called_once()
        command = send.call_args.args[0]
        self.assertEqual(command[-1], event().url)
        for flag, value in [('--max-time','5'), ('--max-filesize',str(sources.MAX_BYTES)), ('--max-redirs','0'), ('--retry','0')]:
            self.assertEqual(command[command.index(flag)+1],value)
        self.assertIn('--no-netrc',command)

    def test_invalid_destination_and_stale_or_future_listing_never_calls_transport(self):
        with patch.object(transport,'_send') as send:
            for url in ('https://evil.example.org/events/x', 'https://www.rrr.org.au/admin/x',
                        'https://www.rrr.org.au/events/x?next=1', 'https://www.rrr.org.au/events/../admin',
                        'https://www.rrr.org.au/events/x/redirect'):
                with self.assertRaises(ValueError):
                    transport.rrr_detail(url)
            for now in (NOW-timedelta(seconds=1),NOW+timedelta(minutes=15)):
                with self.assertRaises(ValueError):
                    details.fetch_detail(event(),now)
            send.assert_not_called()

    def test_failed_refresh_clears_old_enrichment_and_retains_listing_only(self):
        old=replace(event(), details=details.parse_detail(detail(),event=event(),retrieved_at=NOW.isoformat()))
        for code,raw,status in [(403,b'CANARY','error'),(429,b'CANARY','rate-limited'),(302,b'CANARY','error'),
                                (200,b'<html>CANARY</html>','invalid'),(200,b'\xff','invalid')]:
            with patch.object(transport,'rrr_detail',return_value=(code,raw)) as send:
                result=details.fetch_detail(old,NOW)
                self.assertEqual(result.detail_status,status)
                self.assertIsNone(result.details)
                self.assertEqual(result.listed_date,old.listed_date)
                self.assertNotIn('CANARY',repr(result))
                send.assert_called_once()
        with patch.object(transport,'rrr_detail',side_effect=TimeoutError('CANARY')):
            self.assertEqual(details.fetch_detail(old,NOW).detail_status,'timeout')
