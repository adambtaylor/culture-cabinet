"""Fictional current Oncord cards; no downloaded page or image fixtures."""
from datetime import datetime,timezone
from html import escape
from unittest import TestCase
from unittest.mock import patch
from shelf import linden_events as linden,event_scraping,event_listing_transport as transport
from shelf.discovery_sources import SourceError

NOW=datetime(2026,11,11,6,tzinfo=timezone.utc)


def card(slug='fictional-workshop',when='Saturday 28 November, 2026',
         clock='12:00pm - 1:00pm',notice='Ages 2+ FREE',title='FICTIONAL workshop'):
    url='/event-rsvp/'+slug+'/'
    return ('<div class="col-sm-12 col-md-6 col-lg-4"><div class="boxed">'
        f'<a href="{escape(url,quote=True)}"><img width height src="ignored-placeholder" /></a>'
        f'<h3>{escape(title)}</h3><p>{escape(when)}<br/>{escape(clock)}<br/>'
        f'{escape(notice)}</p><a class="blackBorderButton" href="{escape(url,quote=True)}">'
        '+ Read more</a></div></div>')


def listing(*rows,archive=''):
    return ('<html><body><h1>Our events</h1><div id="s8-datarepeater1_ajaxcontainer">'
        +''.join(rows or [card()])+'</div><h3>Our Past Events</h3>'
        '<div id="posts_repeater_ajaxcontainer">'+archive+'</div></body></html>').encode()


class LindenParserTests(TestCase):
    def parse(self,raw):return linden.parse_listing(raw,retrieved_at=NOW.isoformat())

    def test_current_fields_clocks_audience_and_image_omission(self):
        event,=self.parse(listing())
        self.assertEqual(event.title,'FICTIONAL workshop')
        self.assertEqual(event.source_start,'2026-11-28T12:00:00')
        self.assertEqual(event.source_end,'2026-11-28T13:00:00')
        self.assertEqual((event.starts_at,event.ends_at,event.event_timezone),(None,None,None))
        self.assertEqual(event.occurrence_basis,'explicit_range')
        self.assertIn('Saturday 28 November 2026 12:00pm - 1:00pm',event.listed_date)
        self.assertEqual(event.source_notice,'Ages 2+ FREE')
        self.assertIsNone(event.entry_fee)
        self.assertEqual(event.eligibility,'unknown')
        self.assertNotIn('ignored-placeholder',repr(event))
        self.assertIn('Ages 2+',event.date_status(NOW))

    def test_archive_ignored_and_old_or_far_future_current_cards_unavailable(self):
        event,=self.parse(listing(archive=card(title='ARCHIVE CANARY')))
        self.assertNotIn('CANARY',repr(event))
        for when in ('Saturday 31 October 2026','Saturday 13 February 2027'):
            with patch.object(transport,'linden',return_value=(200,listing(card(when=when)))):
                result=event_scraping.collect('linden',now=NOW)
            self.assertEqual(result.status,'unavailable');self.assertEqual(result.candidates,())

    def test_yearless_source_does_not_get_retrieval_year_or_stable_session(self):
        event,=self.parse(listing(card(when='Saturday 28 November')))
        self.assertIsNone(event.source_start)
        self.assertEqual(event.occurrence_basis,'unresolved_observation')
        self.assertNotIn('2026',event.listed_date)

    def test_same_url_different_clocks_and_session_scoped_cancellation(self):
        rows=(card(clock='11:00am - 12:00pm'),card(clock='1:00pm - 2:00pm'))
        first,second=self.parse(listing(*rows))
        self.assertEqual(first.url,second.url);self.assertNotEqual(first.key,second.key)
        survivor,=self.parse(listing(*rows,card(clock='11:00am - 12:00pm',notice='Cancelled')))
        self.assertEqual(survivor.key,second.key)

    def test_overnight_end_day_not_invented(self):
        event,=self.parse(listing(card(clock='10:00pm - 2:00am')))
        self.assertEqual(event.source_start,'2026-11-28T22:00:00')
        self.assertIsNone(event.source_end)
        self.assertIn('2:00am',event.listed_date)

    def test_recognizer_dates_urls_and_scan_bounds_fail_closed(self):
        for raw in (b'<html>challenge</html>',listing().replace(b's8-datarepeater1_ajaxcontainer',b'changed'),
            listing().replace(b'</p>',b''),listing(card(when='Sunday 28 November 2026')),
            listing().replace(b'/event-rsvp/fictional-workshop/',b'https://evil.invalid/'),
            listing().replace(b'<p>',b'<p ambiguous>'),listing(*(card(str(i)) for i in range(49)))):
            with self.subTest(raw=raw[:50]),self.assertRaises(SourceError):self.parse(raw)
        self.assertEqual(len(self.parse(listing(*(card(str(i)) for i in range(48))))),9)
