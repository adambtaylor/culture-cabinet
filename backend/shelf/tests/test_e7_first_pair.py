"""Fictional season cards and source-policy replies, never downloaded images."""
from datetime import datetime,timezone
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from django.test import Client,TestCase as DjangoCase,override_settings
from shelf import impro_melbourne_events as impro,event_scraping,discovery_views as views
from shelf import event_listing_transport as transport
from shelf.discovery_sources import SourceError
from shelf.models import DiscoveryState
from shelf.discovery_reducer import replay
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests

NOW=datetime(2026,11,11,7,tzinfo=timezone.utc)


def card(slug='fictional-show',name='FICTIONAL improv show',dates='Oct 31 – Nov 28',css='cards__card--show'):
    return f'<li class="{css}"><a href="shows/2026-season/{escape(slug,quote=True)}">'+\
        f'<img src="ignored-image" alt="{escape(name,quote=True)}">'+\
        f'<div class="cards__card-title">{escape(dates)}</div></a></li>'


def listing(*rows,past='',season='2026 Season'):
    return ('<html><body><h2 class="hero__title">'+season+'</h2><div class="cards">'
        '<h3 class="cards__title">Now Playing</h3><ul class="cards__content">'+
        ''.join(rows or [card()])+'</ul></div><div class="cards">'
        '<h3 class="cards__title">Past Shows</h3><ul class="cards__content">'+past+
        '</ul></div></body></html>').encode()


class ImproSeasonTests(TestCase):
    def parse(self,raw):return impro.parse_listing(raw,retrieved_at=NOW.isoformat())

    def test_title_from_source_alt_and_season_range_not_invented_sessions(self):
        event,=self.parse(listing())
        self.assertEqual(event.title,'FICTIONAL improv show')
        self.assertEqual((event.programme_starts_on,event.programme_ends_on),('2026-10-31','2026-11-28'))
        self.assertEqual((event.starts_at,event.ends_at,event.event_timezone),(None,None,None))
        self.assertEqual(event.occurrence_basis,'explicit_range')
        self.assertEqual(event.official_url,'https://www.impromelbourne.com.au/shows/2026-season/fictional-show')
        self.assertIn('does not instantiate recurring sessions',event.date_status(NOW))
        self.assertNotIn('ignored-image',repr(event))
        self.assertIsNone(event.entry_fee)

    def test_past_section_and_workshop_cards_never_become_current_shows(self):
        event,=self.parse(listing(card(),card('workshop',name='WORKSHOP CANARY',css='cards__card--workshop'),
            past=card('past-show',name='PAST CANARY',dates='Nov 12 – Nov 29')))
        self.assertNotIn('CANARY',repr(event))

    def test_original_season_year_does_not_roll_forward(self):
        with self.assertRaises(event_scraping.ProgrammeUnavailable):
            impro.parse_listing(listing(),retrieved_at='2027-11-11T07:00:00+00:00')
        for raw in (listing(season='2027 Season'),listing(season='Season'),
                    listing(card(dates='Dec 20 – Jan 5'))):
            with self.subTest(raw=raw[:70]),self.assertRaises(SourceError):self.parse(raw)

    def test_overlap_identity_is_independent_of_title(self):
        first,=self.parse(listing(card(dates='May 31 – November 29')))
        renamed,=self.parse(listing(card(dates='May 31 – November 29',name='FICTIONAL new label')))
        self.assertEqual(first.key,renamed.key)
        two=self.parse(listing(card(),card(dates='Nov 1 – Nov 29')))
        self.assertEqual(two[0].url,two[1].url);self.assertNotEqual(two[0].key,two[1].key)

    def test_changed_fields_unsafe_url_and_scan_bound_fail_closed(self):
        for raw in (b'<html>challenge</html>',listing().replace(b'Now Playing',b'Archive'),
            listing().replace(b' alt=',b' changed='),listing(card(slug='../outside')),
            listing(card(dates='Feb 31 – Nov 28')),listing(*(card(str(i)) for i in range(49)))):
            with self.subTest(raw=raw[:50]),self.assertRaises(SourceError):self.parse(raw)
        self.assertEqual(len(self.parse(listing(*(card(str(i)) for i in range(48))))),9)


class E7TransportTests(TestCase):
    def test_impro_robots_and_season_use_one_origin_and_fixed_path(self):
        with TemporaryDirectory() as directory,patch.object(transport,'_RATE_DIRECTORY',directory),\
             patch.object(transport,'_send',return_value=(404,b'Not found')) as send:
            self.assertTrue(transport.observe_robots('impro_melbourne')['allowed'])
            with self.assertRaises(transport.ListingIntervalPending):transport.impro_melbourne()
            self.assertEqual(len(list(Path(directory).iterdir())),1);send.assert_called_once()
        with patch.object(transport,'_reserve'),patch.object(transport,'_send',return_value=(200,b'HTML')) as send:
            transport.impro_melbourne()
            self.assertEqual(send.call_args.args[0][-1],impro.LISTING_URL)

    def test_butterfly_denial_keeps_original_user_agent_and_listing_closed(self):
        with TemporaryDirectory() as directory,patch.object(transport,'_RATE_DIRECTORY',directory),\
             patch.object(transport,'_send',return_value=(200,b'User-agent: *\nDisallow: /\n')) as send:
            self.assertFalse(transport.observe_robots('butterfly_club')['allowed'])
            command=send.call_args.args[0]
            self.assertEqual(command[command.index('--user-agent')+1],transport.USER_AGENT)
            with self.assertRaises(transport.ListingPolicyUnavailable):transport.butterfly_club()
            send.assert_called_once()


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class E7PairUITests(DjangoCase):
    form=PublicDiscoveryUITests.form

    def setUp(self):
        PublicDiscoveryUITests.setUp(self)
        self.enterContext(patch.object(views.timezone,'now',return_value=NOW))

    def test_impro_run_action_replay_response_save_undo_and_fresh_session(self):
        with patch.object(transport,'impro_melbourne',return_value=(200,listing())) as read:
            data=self.form('event_source',source_id='impro_melbourne')
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertContains(page,'FICTIONAL improv show');self.assertContains(page,'Source show run')
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

    def test_butterfly_normal_action_has_no_homepage_io_or_invented_show(self):
        with patch.object(transport,'_send') as send:
            data=self.form('event_source',source_id='butterfly_club')
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertContains(page,'no homepage was fetched')
            self.assertEqual(page.context['public_results'][0]['outcome'].status,'permission-required')
            self.assertEqual(DiscoveryState.objects.get().history,[]);send.assert_not_called()
