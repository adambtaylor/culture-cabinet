"""Synthetic Arts House cards follow the one observed programme layout."""
from datetime import datetime, timezone
from html import escape
from unittest import TestCase
from unittest.mock import patch

from django.test import TestCase as DjangoCase, override_settings

from shelf import arts_house_events as arts, discovery_views as views
from shelf import event_listing_transport as transport
from shelf.discovery_reducer import replay
from shelf.event_scraping import ProgrammeUnavailable, collect
from shelf.models import DiscoveryState
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests
from shelf.discovery_sources import SourceError

NOW = datetime(2026, 11, 12, 2, tzinfo=timezone.utc)


def card(slug='fictional-show', dates='12 – 15 Nov 2026', title='FICTIONAL Arts House show'):
    return ('<a class="event_anchor" href="/whats-on/'+escape(slug, quote=True)+'">'
        '<div class="wrap"><h3>'+escape(title)+'</h3><div class="event-time"><time>'+
        escape(dates)+'</time></div></div></a>')


def listing(*rows):
    return ('<!doctype html><html><body><main>'+''.join(rows or [card()])+
        '</main></body></html>').encode()


class ArtsHouseEventTests(TestCase):
    def parse(self, raw):
        return arts.parse_listing(raw, now=NOW, retrieved_at=NOW.isoformat(), provenance='fixture')

    def test_explicit_ranges_and_days_keep_date_precision_without_sessions(self):
        run, day = self.parse(listing(card(), card('day', '5 Dec 2026')))
        self.assertEqual((run.source_start, run.source_end), ('2026-11-12', '2026-11-15'))
        self.assertEqual(run.occurrence_basis, 'explicit_range')
        self.assertEqual((run.starts_at, run.ends_at, run.event_timezone), (None, None, None))
        self.assertEqual((day.source_start, day.source_end), ('2026-12-05', None))
        self.assertEqual(day.occurrence_basis, 'explicit_date')
        self.assertEqual((day.starts_at, day.date_precision), (None, 'date'))
        self.assertIn('programme run', run.date_status(NOW))

    def test_cross_month_range_requires_one_explicit_year(self):
        event, = self.parse(listing(card(dates='29 Nov – 2 Dec 2026')))
        self.assertEqual((event.source_start, event.source_end), ('2026-11-29', '2026-12-02'))
        for value in ('12 – 15 Nov', '31 Feb 2026', '30 Dec – 2 Jan 2027 7:30pm'):
            with self.subTest(value=value), self.assertRaises(SourceError):
                self.parse(listing(card(dates=value)))

    def test_unknown_layout_challenge_and_unsafe_links_fail_closed(self):
        cases = (
            b'<html><body>Sign in</body></html>',
            listing(card()).replace(b'class="event_anchor"', b'class="changed"'),
            listing(card()).replace(b'class="event-time"', b'class="changed-time"'),
            listing(card()).replace(b'href="/whats-on/fictional-show"',
                                    b'href="https://example.invalid/show"'),
        )
        for raw in cases:
            with self.subTest(raw=raw[:70]), self.assertRaises(SourceError):
                self.parse(raw)

    def test_past_only_listing_is_unavailable_not_current_empty(self):
        with self.assertRaises(ProgrammeUnavailable):
            self.parse(listing(card(dates='1 – 3 Nov 2026')))

    def test_same_url_different_runs_remain_distinct_and_page_bound_is_kept(self):
        first, second = self.parse(listing(card(), card(dates='19 – 22 Nov 2026')))
        self.assertEqual(first.url, second.url)
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(len(self.parse(listing(*(card(str(i)) for i in range(48))))), 9)
        with self.assertRaises(SourceError):
            self.parse(listing(*(card(str(i)) for i in range(49))))

    def test_fixed_registry_normal_caller_uses_one_substituted_acquisition(self):
        with patch.object(transport, 'arts_house', return_value=(200, listing())) as read:
            outcome = collect('arts_house', now=NOW)
        self.assertEqual(outcome.status, 'ready')
        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(outcome.candidates[0].category, 'all')
        read.assert_called_once_with()


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class ArtsHouseHandlerTests(DjangoCase):
    form = PublicDiscoveryUITests.form

    def setUp(self):
        PublicDiscoveryUITests.setUp(self)
        self.enterContext(patch.object(views.timezone, 'now', return_value=NOW))

    def test_authenticated_source_action_replay_and_get_do_not_write_taste(self):
        with patch.object(transport, 'arts_house', return_value=(200, listing())) as read:
            data = self.form('event_source', source_id='arts_house')
            self.assertEqual(self.client.post(self.action, data).status_code, 302)
            self.assertEqual(self.client.post(self.action, data).status_code, 302)
            page = self.client.get(self.url)
            outcome = page.context['public_results'][0]['outcome']
            self.assertEqual(outcome.status, 'ready')
            self.assertEqual(len(outcome.candidates), 1)
            read.assert_called_once_with()
        state = DiscoveryState.objects.get()
        self.assertEqual(state.history, [])
        self.assertEqual(replay(state.history)['choices'], {})
