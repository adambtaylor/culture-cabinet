"""Synthetic RSS and mocked transport only; no account, calendar or source requests."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from unittest import TestCase
from unittest.mock import patch

from shelf import discovery_transport as transport, tribe_events as tribe
from shelf.discovery_sources import MAX_BYTES, SourceError
from shelf.event_sources import upcoming_events

NOW = datetime(2026, 10, 30, 5, tzinfo=timezone.utc)
PUBLISHED = 'Wed, 07 Oct 2026 04:05:01 +0000'
BUILT = 'Wed, 07 Oct 2026 04:10:12 +0000'


def item(slug='synthetic-event', *, title='SYNTHETIC Tribe event', guid='fixture:feed:event',
         permalink='false', published=PUBLISHED, body=None, url=None):
    body = body or ('<description><![CDATA[CANARY description. Sold out; cancelled; private location later. '
                    '<img src="https://unrelated.invalid/image"><a href="https://unrelated.invalid/tickets">Buy</a>'
                    'Starts 31 October 2026 at 8 pm.]]></description>')
    return ('<item><title>' + escape(title) + '</title><link>' + escape(url or tribe.LISTING_URL + '/' + slug)
        + '</link>' + (f'<guid isPermaLink="{permalink}">{escape(guid)}</guid>' if guid is not None else '')
        + ('<pubDate>' + escape(published) + '</pubDate>' if published is not None else '') + body + '</item>')


def listing(rows=None, *, build=BUILT, channel_url=tribe.LISTING_URL + '/'):
    rows = [item()] if rows is None else rows
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        '<title>Upcoming Events - SYNTHETIC Tribe</title><link>' + escape(channel_url) + '</link>'
        + ('<lastBuildDate>' + escape(build) + '</lastBuildDate>' if build is not None else '')
        + ''.join(rows) + '</channel></rss>').encode()


class TribeParserTests(TestCase):
    def parse(self, raw):
        return tribe.parse_listing(raw, retrieved_at=NOW.isoformat())

    def test_publication_and_feed_build_preserved_without_event_date_or_location(self):
        raw = listing()
        event, = self.parse(raw)
        self.assertEqual(event.title, 'SYNTHETIC Tribe event')
        self.assertEqual(event.url, tribe.LISTING_URL + '/synthetic-event')
        self.assertEqual(event.source_event_id, 'fixture:feed:event')
        self.assertEqual(event.key, 'official:event:' + sha256(b'tribe\0guid\0fixture:feed:event').hexdigest())
        self.assertEqual(event.published_at, '2026-10-07T04:05:01+00:00')
        self.assertEqual(event.publication_label, PUBLISHED)
        self.assertEqual(event.feed_built_at, '2026-10-07T04:10:12+00:00')
        self.assertEqual(event.feed_build_label, BUILT)
        self.assertEqual((event.starts_at, event.ends_at, event.event_timezone), (None, None, None))
        self.assertEqual((event.date_precision, event.end_precision), ('unknown', 'unknown'))
        self.assertEqual((event.venue, event.locality), ('', ''))
        self.assertEqual((event.status, event.eligibility), ('unknown', 'unknown'))
        self.assertIsNone(event.entry_fee)
        self.assertIsNone(event.lineup)
        self.assertEqual(upcoming_events([event], NOW), ())
        self.assertIn('publication is not an event start', event.date_status(NOW))
        self.assertNotIn('CANARY', repr(event))
        self.assertNotIn('unrelated.invalid', repr(event))
        evidence, = event.evidence
        self.assertEqual(evidence.snapshot_sha256, sha256(raw).hexdigest())
        self.assertEqual((evidence.source_id, evidence.event_id, evidence.official_url),
                         ('tribe', 'fixture:feed:event', event.url))
        self.assertEqual((evidence.position, evidence.retrieved_at), (1, NOW.isoformat()))

    def test_old_publication_is_retained_as_lead_and_never_classifies_event_as_past(self):
        event, = self.parse(listing([item(published='Wed, 26 Jun 2019 03:42:06 +0000')]))
        self.assertEqual(event.published_at, '2019-06-26T03:42:06+00:00')
        self.assertIsNone(event.starts_at)
        self.assertIn('older than seven days', event.freshness(NOW))
        self.assertIn('past announcements', event.freshness(NOW))
        self.assertIn('Stale', event.freshness(NOW + timedelta(minutes=16)))

    def test_unknown_and_future_source_publication_stay_separate_from_retrieval(self):
        for value in (None, '', 'not a date', '07 Oct 2026 04:05:01'):
            with self.subTest(value=value):
                event, = self.parse(listing([item(published=value)], build=value))
                self.assertIsNone(event.published_at)
                self.assertIsNone(event.feed_built_at)
                self.assertIsNone(event.starts_at)
                self.assertIn('Feed update time unknown', event.freshness(NOW))
        event, = self.parse(listing([item(published='Fri, 30 Oct 2027 05:00:00 +0000')],
                                   build='Fri, 30 Oct 2027 05:00:00 +0000'))
        self.assertIn('future dated', event.freshness(NOW))
        self.assertIsNone(event.starts_at)
        event, = self.parse(listing(build='Fri, 30 Oct 2026 04:00:00 +0000'))
        self.assertIn('does not establish an upcoming event', event.freshness(NOW))

    def test_cancellation_words_and_announced_later_locations_are_not_verified_fields(self):
        event, = self.parse(listing([item(title='SYNTHETIC cancelled show - location announced later')]))
        self.assertIn('cancelled', event.title)
        self.assertEqual(event.status, 'unknown')
        self.assertEqual(event.venue, '')
        self.assertIn('Cancellation and ticket availability unverified', event.date_status(NOW))

    def test_missing_guid_uses_exact_event_url_identity(self):
        event, = self.parse(listing([item(guid=None)]))
        self.assertIsNone(event.source_event_id)
        self.assertEqual(event.key, 'official:event:' + sha256(('tribe\0url\0' + event.url).encode()).hexdigest())
        url = tribe.LISTING_URL + '/synthetic-event'
        event, = self.parse(listing([item(guid=url, permalink='true')]))
        self.assertEqual(event.source_event_id, url)
        with self.assertRaises(SourceError):
            self.parse(listing([item(guid='https://other.invalid/event', permalink='true')]))

    def test_exact_duplicates_coalesce_and_titles_never_merge_distinct_events(self):
        events = self.parse(listing([item(), item(), item('another-event', guid='fixture:other')]))
        self.assertEqual(len(events), 2)
        self.assertEqual([e.rank for e in events], [1, 3])
        for other in (item(title='Changed title'), item(guid='fixture:changed'),
                      item(published='Thu, 08 Oct 2026 04:05:01 +0000'),
                      item('different-url-with-same-guid')):
            with self.subTest(other=other), self.assertRaises(SourceError):
                self.parse(listing([item(), other]))

    def test_nine_leads_with_bounded_full_feed_validation(self):
        rows = [item('event-' + str(i), guid='fixture:' + str(i)) for i in range(48)]
        events = self.parse(listing(rows))
        self.assertEqual(len(events), 9)
        self.assertEqual([e.rank for e in events], list(range(1, 10)))
        with self.assertRaises(SourceError):
            self.parse(listing(rows + [item('extra', guid='fixture:extra')]))
        with self.assertRaises(SourceError):
            self.parse(listing(rows[:9] + [item(url='https://other.invalid/trap')]))

    def test_only_canonical_event_paths_accepted(self):
        bad = ('http://www.tribesoundsystem.com/events/a', 'https://tribesoundsystem.com/events/a',
            tribe.LISTING_URL, tribe.LISTING_URL + '/a/', tribe.LISTING_URL + '/a?format=ical',
            tribe.LISTING_URL + '/a?format=rss', tribe.LISTING_URL + '/a#fragment',
            tribe.LISTING_URL + '/a/b', tribe.LISTING_URL + '/../account',
            tribe.LISTING_URL + '/%61', tribe.LISTING_URL + '/a\\b',
            tribe.LISTING_URL + '/a\n', tribe.LISTING_URL + '/a' * 256,
            'https://user@www.tribesoundsystem.com/events/a',
            'https://www.tribesoundsystem.com:443/events/a',
            'https://www.tribesoundsystem.com.evil.invalid/events/a')
        for url in bad:
            with self.subTest(url=url), self.assertRaises(SourceError):
                tribe.event_url(url)
        self.assertEqual(tribe.event_url(tribe.LISTING_URL + '/event--one-123'), tribe.LISTING_URL + '/event--one-123')

    def test_wrong_feed_and_changed_primary_metadata_fail_closed(self):
        raw = listing()
        cases = [listing(channel_url='https://other.invalid/events'), raw.replace(b'version="2.0"', b'version="1.0"'),
            raw.replace(b'<title>SYNTHETIC', b'<title><b>bad</b>SYNTHETIC'),
            raw.replace(b'<item>', b'<item><link>https://other.invalid/events/a</link>'),
            raw.replace(b'<item>', b'<item><pubDate>duplicate</pubDate>'),
            raw.replace(b'<item>', b'<item><guid isPermaLink="false">duplicate</guid>'),
            raw.replace(b'</rss>', b'<channel></channel></rss>'),
            raw.replace(b'isPermaLink="false"', b'isPermaLink="maybe"')]
        for content in cases:
            with self.subTest(content=content[:150]), self.assertRaises(SourceError):
                self.parse(content)

    def test_entities_doctype_depth_nodes_size_truncation_and_challenges_rejected(self):
        dtd = b'<!DOCTYPE rss [<!ENTITY leak SYSTEM "file:///etc/passwd">]>'
        for raw in (b'', b'\xff', b'<html>CANARY challenge</html>', b'x' * (MAX_BYTES + 1),
                    listing()[:-20], listing().replace(b'<rss', dtd + b'<rss', 1),
                    listing().replace(b'<channel>', b'<channel>' + b'<x>' * 17).replace(b'</channel>', b'</x>' * 17 + b'</channel>'),
                    listing().replace(b'</channel>', b'<x/>' * 2001 + b'</channel>')):
            with self.subTest(raw=raw[:80]), self.assertRaises((SourceError, UnicodeError)):
                self.parse(raw)
        with self.assertRaises(SourceError):
            tribe.parse_listing(listing(), retrieved_at='2026-10-30T05:00:00')

    def test_valid_empty_feed_is_distinct_from_unrecognizable_source(self):
        self.assertEqual(self.parse(listing([])), ())
        with patch.object(transport, 'tribe', return_value=(200, listing([]))):
            result = tribe.fetch_listing(NOW)
        self.assertEqual(result.status, 'empty')
        self.assertIn('does not establish no events or tickets', result.message)


class TribeTransportTests(TestCase):
    def test_one_fixed_anonymous_rss_request_no_calendar_or_linked_read(self):
        with patch.object(transport, '_send', return_value=(200, listing())) as send:
            result = tribe.fetch_listing(NOW)
        self.assertEqual(result.status, 'ready')
        self.assertEqual(len(result.candidates), 1)
        self.assertIn('not confirmed upcoming sessions', result.message)
        self.assertIn('disallows automated format=ical', result.message)
        send.assert_called_once()
        command = send.call_args.args[0]
        self.assertEqual(command[-1], tribe.RSS_URL)
        self.assertEqual(command[:2], [transport.CURL, '-q'])
        self.assertIn('Accept: application/rss+xml', command)
        for flag, value in (('--max-time', '5'), ('--max-filesize', str(MAX_BYTES)), ('--max-redirs', '0'),
                            ('--retry', '0'), ('--proxy', ''), ('--noproxy', '*'), ('--request', 'GET')):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn('--no-netrc', command)
        self.assertNotIn('--location', command)
        self.assertNotIn('CANARY', repr(command))
        self.assertNotIn('format=ical', repr(command))

    def test_source_errors_are_opaque_without_retry_or_alternate_route(self):
        for code, raw, status in ((403, b'CANARY block', 'error'), (429, b'CANARY rate limit', 'rate-limited'),
            (302, b'CANARY redirect', 'error'), (500, b'CANARY error', 'error'),
            (200, b'<html>CANARY challenge</html>', 'invalid'), (200, b'\xffCANARY', 'invalid')):
            with self.subTest(code=code), patch.object(transport, 'tribe', return_value=(code, raw)) as read:
                result = tribe.fetch_listing(NOW)
                self.assertEqual(result.status, status)
                self.assertEqual(result.candidates, ())
                self.assertNotIn('CANARY', repr(result))
                read.assert_called_once()
        for error, expected in ((TimeoutError('CANARY'), 'timeout'), (OSError('CANARY'), 'invalid')):
            with patch.object(transport, 'tribe', side_effect=error) as read:
                self.assertEqual(tribe.fetch_listing(NOW).status, expected)
                read.assert_called_once()

    def test_invalid_clock_and_arbitrary_destination_cannot_start_io(self):
        with patch.object(transport, '_send') as send:
            for now in (None, datetime(2026, 10, 30), '2026-10-30'):
                with self.assertRaises(SourceError):
                    tribe.fetch_listing(now)
            with self.assertRaises(TypeError):
                transport.tribe('https://other.invalid/events')
            send.assert_not_called()
