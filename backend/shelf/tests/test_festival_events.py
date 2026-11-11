"""Fictional festival cards; no programme dates inferred from news or editions."""
from datetime import datetime,timezone
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from django.test import Client,TestCase as DjangoCase,override_settings
from shelf import fringe_events as fringe,event_scraping,event_listing_transport as transport,discovery_views as views
from shelf.discovery_sources import SourceError
from shelf.discovery_reducer import replay
from shelf.models import DiscoveryState
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests

NOW=datetime(2026,11,11,8,tzinfo=timezone.utc)


def card(slug='fictional-show',dates='12 - 18 Nov 2026',title='FICTIONAL Fringe show'):
    return ('<article class="whats-on-event-tile component"><h3 class="event-card-heading">'+
        escape(title)+'</h3><div class="event-card-subtitle">'+escape(dates)+'</div>'+
        '<div class="event-card-subtitle d-flex flex-column gap-2"><span>ARTIST CANARY</span></div>'+
        '<img width height src="ignored-image"><a href="/whats-on/events/'+escape(slug,quote=True)+
        '">Event Info</a><a href="/whats-on/events?eventfilters=genre">Genre</a></article>')


def listing(*rows):return ('<html><body>'+''.join(rows or [card()])+'</body></html>').encode()


class FringeEventTests(TestCase):
    def parse(self,raw):return fringe.parse_listing(raw,retrieved_at=NOW.isoformat())

    def test_run_and_point_keep_explicit_year_without_invented_performance_instants(self):
        run,point=self.parse(listing(card(),card('single',dates='12 Nov 2026 7:30pm')))
        self.assertEqual((run.source_start,run.source_end),('2026-11-12','2026-11-18'))
        self.assertEqual(run.occurrence_basis,'explicit_range')
        self.assertEqual(point.source_start,'2026-11-12T19:30:00')
        self.assertEqual(point.occurrence_basis,'explicit_datetime')
        self.assertEqual((point.starts_at,point.event_timezone),(None,None))
        self.assertNotIn('ARTIST CANARY',repr(run));self.assertIsNone(run.lineup)
        self.assertNotIn('ignored-image',repr(run));self.assertIsNone(run.entry_fee)

    def test_cross_month_ranges_are_source_dates_not_retrieval_year(self):
        event,=self.parse(listing(card(dates='30 Oct - 16 Nov 2026')))
        self.assertEqual((event.source_start,event.source_end),('2026-10-30','2026-11-16'))
        with self.assertRaises(event_scraping.ProgrammeUnavailable):
            fringe.parse_listing(listing(),retrieved_at='2027-11-11T08:00:00+00:00')
        for text in ('12 - 18 Nov','31 Feb 2026','30 Dec - 6 Jan 2026'):
            with self.subTest(text=text),self.assertRaises(SourceError):self.parse(listing(card(dates=text)))

    def test_repeated_date_fields_must_agree(self):
        raw=listing().replace(b'<img',b'<div class="event-card-subtitle">12 - 18 Nov 2026</div><img')
        self.assertEqual(len(self.parse(raw)),1)
        with self.assertRaises(SourceError):
            self.parse(raw.replace(b'12 - 18 Nov 2026</div><img',b'13 - 18 Nov 2026</div><img'))

    def test_past_only_listing_is_unavailable_not_current_empty(self):
        with self.assertRaises(event_scraping.ProgrammeUnavailable):
            self.parse(listing(card(dates='12 - 18 Oct 2026')))

    def test_shared_url_different_runs_have_different_occurrence_identities(self):
        first,second=self.parse(listing(card(),card(dates='13 - 19 Nov 2026')))
        self.assertEqual(first.url,second.url);self.assertNotEqual(first.key,second.key)
        renamed,=self.parse(listing(card(title='FICTIONAL renamed show')))
        self.assertEqual(renamed.key,first.key)

    def test_changed_layout_unsafe_link_and_limits_fail_closed(self):
        for raw in (b'<html>challenge</html>',listing().replace(b'whats-on-event-tile',b'changed'),
                    listing().replace(b'/whats-on/events/fictional-show',b'https://evil.invalid/'),
                    listing(*(card(str(i)) for i in range(49)))):
            with self.subTest(raw=raw[:50]),self.assertRaises(SourceError):self.parse(raw)
        self.assertEqual(len(self.parse(listing(*(card(str(i)) for i in range(48))))),9)


class FestivalTransportTests(TestCase):
    def test_programme_targets_replace_homepages_without_fanout(self):
        self.assertEqual(transport.TARGETS['miff'][0],'https://miff.com.au/program')
        self.assertEqual(transport.TARGETS['comedy_festival'][0],'https://www.comedyfestival.com.au/browse-shows/')
        with patch.object(transport,'_reserve') as reserve,patch.object(transport,'_send',return_value=(200,b'fixture')) as send:
            transport.miff();transport.comedy_festival()
            self.assertEqual([call.args[0][-1] for call in send.call_args_list],
                [transport.TARGETS[source][0] for source in ('miff','comedy_festival')])
            self.assertEqual([call.args for call in reserve.call_args_list],[('miff',),('comedy_festival',)])

    def test_each_fixed_origin_has_one_robots_listing_reservation(self):
        with TemporaryDirectory() as directory,patch.object(transport,'_RATE_DIRECTORY',directory),\
             patch.object(transport,'_send',return_value=(200,b'User-agent: *\nDisallow:\n')) as send:
            for source in ('miff','comedy_festival','fringe'):
                self.assertTrue(transport.observe_robots(source)['allowed'])
                self.assertEqual(send.call_args.args[0][-1],transport.TARGETS[source][1])
                with self.assertRaises(transport.ListingIntervalPending):transport._read(source)
            self.assertEqual(send.call_count,3);self.assertEqual(len(list(Path(directory).iterdir())),3)


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class FestivalEventUITests(DjangoCase):
    form=PublicDiscoveryUITests.form

    def setUp(self):
        PublicDiscoveryUITests.setUp(self)
        self.enterContext(patch.object(views.timezone,'now',return_value=NOW))

    def test_fringe_normal_action_replay_response_save_undo_fresh_session(self):
        with patch.object(transport,'fringe',return_value=(200,listing())) as read:
            data=self.form('event_source',source_id='fringe')
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url);self.assertContains(page,'Source show run')
            event=page.context['public_results'][0]['outcome'].candidates[0]
            self.client.post(self.action,self.form('save',key=event.key))
            self.client.post(self.action,self.form('respond',key=event.key,choice='intrigued'))
            self.client.post(self.action,self.form('undo'))
            views.PUBLIC_CACHE.clear()
            fresh=Client();fresh.force_login(self.actor)
            session=fresh.session;session['selected_profile_id']=1;session.save()
            row=next(row for row in fresh.get(self.url).context['responses'] if row['card'].key==event.key)
            self.assertTrue(row['saved']);self.assertIsNone(row['card'].url)
            self.assertNotIn(event.key,replay(DiscoveryState.objects.get().history)['choices'])
            read.assert_called_once_with()

    def test_past_fringe_rows_and_homepage_promos_are_not_current_screenings_or_shows(self):
        with patch.object(transport,'fringe',return_value=(200,listing(card(dates='12 - 18 Oct 2026')))):
            self.client.post(self.action,self.form('event_source',source_id='fringe'))
        with patch.object(transport,'_send') as send:
            for source in ('miff','comedy_festival'):
                data=self.form('event_source',source_id=source)
                self.client.post(self.action,data);self.client.post(self.action,data)
            page=self.client.get(self.url)
            self.assertEqual([row['outcome'].status for row in page.context['public_results']],['unavailable']*3)
            self.assertEqual(DiscoveryState.objects.get().history,[]);send.assert_not_called()
