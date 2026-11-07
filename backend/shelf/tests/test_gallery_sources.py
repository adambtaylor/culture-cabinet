"""Primary-page metadata regression tests; no live requests or image fixtures."""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from shelf.gallery_sources import (APW_URL, GERTRUDE_URL, OFFER_URL, MAX_BYTES,
    fetch, parse_apw, parse_gertrude, parse_offer, _event)
from shelf.discovery_sources import SourceError

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def raw(name):
    return (FIXTURES / ('gallery_' + name + '.html')).read_bytes()


class GallerySourceTests(unittest.TestCase):
    def test_opening_session_and_exhibition_run_are_distinct(self):
        event = parse_gertrude(raw('gertrude'), retrieved_at=NOW.isoformat())
        self.assertEqual(event.starts_at, '2026-11-06T18:00:00+11:00')
        self.assertEqual(event.opening_ends_at, '2026-11-06T20:00:00+11:00')
        self.assertEqual(event.exhibition_starts_on, '2026-11-07')
        self.assertEqual(event.exhibition_ends_on, '2026-12-12')
        self.assertEqual(event.gallery_data['opening_starts_at'], event.starts_at)
        self.assertIsNone(event.artwork_price)

    def test_apw_missing_end_and_run_are_unknown(self):
        event = parse_apw(raw('apw'), retrieved_at=NOW.isoformat())
        self.assertEqual(event.opening_date, '2026-11-06')
        self.assertIsNone(event.opening_ends_at)
        self.assertIsNone(event.exhibition_starts_on)
        self.assertIsNone(event.exhibition_ends_on)

    def test_ambiguous_or_conflicting_dates_are_rejected(self):
        for before, after in ((b'2026', b'2027'), (b'6-8pm', b'6-8'),
                              (b'Friday 6', b'Thursday 6')):
            with self.subTest(after=after), self.assertRaises(SourceError):
                parse_gertrude(raw('gertrude').replace(before, after), retrieved_at=NOW.isoformat())
        with self.assertRaises(SourceError):
            parse_apw(raw('apw').replace(b'6pm', b'6'), retrieved_at=NOW.isoformat())

    def test_offer_is_separate_and_unknown_costs_are_not_zero(self):
        offer = parse_offer(raw('goompi'), retrieved_at=NOW.isoformat())
        self.assertEqual(offer['components']['artwork']['amount'], '1100')
        self.assertEqual(offer['components']['tax']['status'], 'included')
        self.assertIn('$33 per tube', offer['components']['shipping']['source_basis'])
        self.assertIn('exact delivery charge unverified', offer['components']['shipping']['source_basis'])
        for component in ('shipping', 'framing', 'installation', 'fees', 'fx'):
            self.assertEqual(offer['components'][component]['status'], 'unknown')
            self.assertNotIn('amount', offer['components'][component])
        self.assertEqual(offer['availability'], 'unknown')
        self.assertFalse(offer['purchase_authorized'])

    def transport(self, overrides=None):
        responses = {'https://gertrude.org.au/robots.txt': (404, b''),
            APW_URL + 'robots.txt': (200, b'User-agent: *\nCrawl-delay: 10\nDisallow: /admin/'),
            GERTRUDE_URL: (200, raw('gertrude')), APW_URL: (200, raw('apw')),
            OFFER_URL: (200, raw('goompi'))}
        responses.update(overrides or {})
        seen = []
        def read(url):
            seen.append(url)
            return responses[url]
        return read, seen

    def test_five_fixed_reads_no_remote_contexts_or_images(self):
        read, seen = self.transport()
        result = fetch(NOW, transport=read)
        self.assertEqual(result.status, 'ready')
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(len(result.offers), 1)
        self.assertEqual(len(seen), 5)
        self.assertTrue(all(e.kind == 'event' and e.category == 'gallery' for e in result.candidates))

    def test_robots_denial_and_refusal_do_not_trigger_workarounds(self):
        for code, body, status in ((200, b'User-agent: *\nDisallow: /', 'robots-refused'),
                                    (403, b'', 'refused'), (429, b'', 'rate-limited')):
            with self.subTest(code=code):
                read, seen = self.transport({APW_URL + 'robots.txt': (code, body)})
                result = fetch(NOW, transport=read)
                self.assertEqual(len(result.candidates), 1)
                self.assertNotIn(APW_URL, seen)
                self.assertNotIn(OFFER_URL, seen)
                self.assertEqual(result.source_checks[-1]['status'], status)

    def test_past_year_never_rolls_to_next_year(self):
        read, _ = self.transport()
        result = fetch(datetime(2027, 10, 31, tzinfo=timezone.utc), transport=read)
        self.assertEqual(result.candidates, ())
        self.assertTrue(any(c['status'] == 'stale' for c in result.source_checks))

    def test_parser_change_is_visible_and_other_source_survives(self):
        read, _ = self.transport({GERTRUDE_URL: (200, b'<html>Challenge</html>')})
        result = fetch(NOW, transport=read)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.source_checks[0]['status'], 'parser-changed')

    def test_total_budget_and_missing_robot_policy_fail_closed(self):
        for response in ((200, b'x' * (MAX_BYTES + 1)), (503, b''), (200, b'<html>Error</html>')):
            read, seen = self.transport({'https://gertrude.org.au/robots.txt': response})
            result = fetch(NOW, transport=read)
            self.assertNotIn(GERTRUDE_URL, seen)
            self.assertNotEqual(result.source_checks[0]['status'], 'ready')

    def test_transport_rejects_arbitrary_urls_before_io(self):
        from shelf.gallery_transport import read
        for url in ('http://gertrude.org.au/', 'https://gertrude.org.au/private',
                    'https://evil.test/', APW_URL + '?redirect=1'):
            with self.assertRaises(SourceError):
                read(url)

    def test_shared_host_interval_and_refusal_backoff(self):
        from shelf import gallery_transport as transport
        with TemporaryDirectory() as temporary:
            original_open = transport.os.open
            def local_open(path, *args):
                return original_open(temporary + '/' + Path(path).name, *args)
            clock = [1000.0]
            def sleep(seconds):
                clock[0] += seconds
            with patch.object(transport.os, 'open', side_effect=local_open), \
                    patch.object(transport.time, 'time', side_effect=lambda: clock[0]), \
                    patch.object(transport.time, 'sleep', side_effect=sleep) as pause, \
                    patch.object(transport, '_send', return_value=(200, b'ok')) as send:
                transport.read(APW_URL)
                transport.read(OFFER_URL)
                pause.assert_called_once_with(10.0)
                send.return_value = (429, b'')
                transport.read(APW_URL)
                with self.assertRaisesRegex(SourceError, 'rate-limited'):
                    transport.read(OFFER_URL)
                self.assertEqual(send.call_count, 3)
                # Host refusal does not affect the second source.
                transport.read(GERTRUDE_URL)
                self.assertEqual(send.call_count, 4)

    def test_refresh_deadline_preserves_partial_results_without_wait(self):
        from shelf import gallery_transport as transport
        with TemporaryDirectory() as temporary:
            original_open = transport.os.open
            def local_open(path, *args):
                return original_open(temporary + '/' + Path(path).name, *args)
            with patch.object(transport.os, 'open', side_effect=local_open), \
                    patch.object(transport.time, 'monotonic', return_value=100), \
                    patch.object(transport, '_send') as send:
                with self.assertRaisesRegex(SourceError, 'rate-limited'):
                    transport.read(APW_URL, deadline=105)
                send.assert_not_called()

    def test_robots_policy_cache_is_bounded_and_does_not_refetch(self):
        from shelf import gallery_transport as transport
        with TemporaryDirectory() as temporary:
            original_open = transport.os.open
            def local_open(path, *args):
                return original_open(temporary + '/' + Path(path).name, *args)
            url = APW_URL + 'robots.txt'
            with patch.object(transport.os, 'open', side_effect=local_open), \
                    patch.dict(transport._ROBOTS_CACHE, {}, clear=True), \
                    patch.object(transport, '_send', return_value=(200, b'User-agent: *')) as send:
                self.assertEqual(transport.read(url), transport.read(url))
                self.assertEqual(send.call_count, 1)
                with self.assertRaises(SourceError):
                    transport.read(url, max_bytes=1)


    def test_offer_robots_denial_keeps_permitted_exhibitions(self):
        for agent in ('*', 'CultureCabinet'):
            with self.subTest(agent=agent):
                policy = f'User-agent: {agent}\nDisallow: /megan-cope/\n'.encode()
                read, seen = self.transport({APW_URL + 'robots.txt': (200, policy)})
                result = fetch(NOW, transport=read)
                self.assertEqual(len(result.candidates), 2)
                self.assertIn(APW_URL, seen)
                self.assertNotIn(OFFER_URL, seen)
                self.assertEqual(result.offers, ())
                self.assertEqual(result.source_checks[-1]['status'], 'robots-refused')
                self.assertEqual(result.source_checks[-1]['stage'], 'offer')
                self.assertEqual(result.source_checks[-1]['source_url'], OFFER_URL)

    def test_explicit_missing_robots_permits_fixed_offer(self):
        read, seen = self.transport({APW_URL + 'robots.txt': (404, b'')})
        result = fetch(NOW, transport=read)
        self.assertEqual(len(result.offers), 1)
        self.assertIn(OFFER_URL, seen)

    def test_homepage_event_identity_is_stable_and_distinguishes_events(self):
        event = parse_apw(raw('apw'), retrieved_at=NOW.isoformat())
        repeated = parse_apw(raw('apw'), retrieved_at='2026-10-31T12:10:00+00:00')
        self.assertEqual(event.key, repeated.key)
        def homepage_event(title, start):
            return _event(title, 'Australian Print Workshop', APW_URL,
                          NOW.isoformat(), 'Explicit source opening', start)
        renamed = homepage_event('Another exhibition', event.starts_at)
        next_year = homepage_event(event.title, '2027-11-06T18:00:00+11:00')
        another_date = homepage_event(event.title, '2026-11-07T18:00:00+11:00')
        self.assertEqual(len({event.key, renamed.key, next_year.key, another_date.key}), 4)


    def test_primary_page_events_use_existing_correction_provenance(self):
        for parser, fixture in ((parse_gertrude, 'gertrude'), (parse_apw, 'apw')):
            with self.subTest(fixture=fixture):
                event = parser(raw(fixture), retrieved_at=NOW.isoformat())
                self.assertEqual(event.provenance, 'live-automated')
                self.assertEqual(event.source_url, event.url)
                self.assertEqual(event.checked_at, NOW.isoformat())


    def test_process_paragraph_does_not_replace_medium_or_break_offer(self):
        process = b'<p class="print__description">Synthetic process notes about preparing and printing this example.</p>'
        offer = parse_offer(raw('goompi') + process, retrieved_at=NOW.isoformat())
        self.assertEqual(offer['medium'], 'Lithograph (hand-drawn plate lithograph, photolithograph)')
        self.assertEqual(offer['components']['artwork']['amount'], '1100')
        self.assertEqual(offer['components']['shipping']['status'], 'unknown')
        self.assertNotIn('Synthetic process notes', str(offer))

    def test_ambiguous_medium_remains_unknown_without_rejecting_offer(self):
        other = b'<p class="print__description">Lithograph (photolithograph)</p>'
        offer = parse_offer(raw('goompi') + other, retrieved_at=NOW.isoformat())
        self.assertEqual(offer['medium'], 'Medium unverified')
        self.assertEqual(offer['components']['artwork']['amount'], '1100')
