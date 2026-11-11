"""Fictional homepage metadata; existing APW gallery transport remains owned."""
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from django.test import Client,TestCase as DjangoCase,override_settings
from shelf import apw_events as apw,event_scraping,gallery_transport,discovery_views as views
from shelf.gallery_sources import APW_URL
from shelf.discovery_reducer import replay
from shelf.models import DiscoveryState
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests

NOW=datetime(2026,11,11,6,tzinfo=timezone.utc)


def listing(opening='Opening 6pm. Friday 20 November 2026',run='20 November - 6 December 2026',title='FICTIONAL print programme'):
    return ('<html><div class="sidebar__hero"><p class="sidebar__hero-text">'
        'Australian Print Workshop <br> '+title+'</p>'+
        (f'<a class="sidebar__hero-action">{opening}</a>' if opening else '')+
        (f'<p class="sidebar__hero-dates">{run}</p>' if run else '')+'</div></html>').encode()


class APWEventTests(TestCase):
    def parse(self,raw):return apw.parse_listing(raw,retrieved_at=NOW.isoformat())

    def test_opening_and_run_are_separate_occurrences_without_offer_fields(self):
        opening,run=self.parse(listing())
        self.assertEqual(opening.starts_at,'2026-11-20T18:00:00+11:00')
        self.assertEqual(opening.occurrence_basis,'explicit_datetime')
        self.assertEqual(run.occurrence_basis,'explicit_range')
        self.assertNotEqual(opening.key,run.key)
        self.assertEqual(opening.url,run.url)
        self.assertIsNone(run.starts_at);self.assertIsNone(run.ends_at)
        self.assertEqual(run.exhibition_starts_on,'2026-11-20')
        self.assertIsNone(opening.artwork_price);self.assertIsNone(opening.entry_fee)
        self.assertIn('opening sessions',run.date_status(NOW))

    def test_past_opening_unknown_run_cannot_be_promoted_to_current(self):
        with self.assertRaises(event_scraping.ProgrammeUnavailable):
            self.parse(listing(opening='Opening 6pm. Friday 6 November 2026',run=''))
        run,=self.parse(listing(opening='Opening 6pm. Friday 6 November 2026',
            run='7 November - 6 December 2026'))
        self.assertIsNone(run.starts_at)
        self.assertEqual(run.source_start,'2026-11-07')

    def test_occurrence_identity_does_not_change_with_title_or_observation_clock(self):
        first=self.parse(listing());changed=self.parse(listing(title='FICTIONAL revised title'))
        self.assertEqual([row.key for row in first],[row.key for row in changed])
        later=apw.parse_listing(listing(),retrieved_at='2026-11-12T06:00:00+00:00')
        self.assertEqual([row.key for row in first],[row.key for row in later])

    def test_event_delegation_shares_existing_lock_backoff_and_never_waits(self):
        with TemporaryDirectory() as directory:
            original=gallery_transport.os.open
            def local_open(path,*args):return original(str(Path(directory)/Path(path).name),*args)
            with patch.object(gallery_transport.os,'open',side_effect=local_open),\
                 patch.object(gallery_transport.time,'time',return_value=1000),\
                 patch.object(gallery_transport.time,'sleep',side_effect=AssertionError('No waiting')) ,\
                 patch.object(gallery_transport,'_send',return_value=(403,b'CANARY')) as send:
                first=event_scraping.collect('apw',now=NOW)
                second=event_scraping.collect('apw',now=NOW)
                self.assertEqual((first.status,second.status),('error','rate-limited'))
                send.assert_called_once()
                self.assertEqual(send.call_args.args[0][-1],APW_URL)
                self.assertEqual(len(list(Path(directory).iterdir())),1)


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class APWEventUITests(DjangoCase):
    setUp=PublicDiscoveryUITests.setUp
    form=PublicDiscoveryUITests.form

    def test_normal_event_action_replay_run_and_opening_response_save_undo(self):
        with patch.object(views.timezone,'now',return_value=NOW),\
             patch.object(gallery_transport,'read',return_value=(200,listing())) as read,\
             patch('shelf.gallery_sources.fetch',side_effect=AssertionError('No aggregate/offers')):
            data=self.form('event_source',source_id='apw')
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertEqual(len(page.context['public_results'][0]['outcome'].candidates),2)
            self.assertContains(page,'Source exhibition run')
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
            read.assert_called_once()
            self.assertEqual(read.call_args.args,(APW_URL,))

    def test_recognized_past_opening_is_unavailable_not_verified_empty(self):
        with patch.object(gallery_transport,'read',return_value=(200,listing(
                opening='Opening 6pm. Friday 6 November 2026',run=''))):
            self.client.post(self.action,self.form('event_source',source_id='apw'))
        page=self.client.get(self.url)
        self.assertEqual(page.context['public_results'][0]['outcome'].status,'unavailable')
        self.assertEqual(DiscoveryState.objects.get().history,[])
