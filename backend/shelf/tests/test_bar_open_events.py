"""Fictional official-shell fragments; recurrence never creates an occurrence."""
from unittest import TestCase
from unittest.mock import patch
from shelf import bar_open_events as source, event_scraping as engine, event_listing_transport as transport
from shelf.discovery_sources import SourceError
from shelf.tests.test_event_sources import NOW


def listing():
    return (b'<html><h1>Make It Up Club</h1><div class="entry-content">'
        b'<p>Held every Tuesday night at Bar Open</p><h3>Upcoming Events</h3>'
        b'<a href="https://www.instagram.com/makeitupclub/">Instagram</a>'
        b'</div><footer>Copyright 2026</footer></html>')


class BarOpenTests(TestCase):
    def test_recognized_recurrence_is_unavailable_not_an_empty_or_invented_next_event(self):
        with self.assertRaises(engine.ProgrammeUnavailable):
            source.parse_listing(listing(),retrieved_at=NOW.isoformat())
        with patch.object(transport,'bar_open',return_value=(200,listing())) as read:
            outcome=engine.collect('bar_open',now=NOW)
        self.assertEqual(outcome.status,'unavailable')
        self.assertEqual(outcome.candidates,())
        self.assertIn('recurrence is not a confirmed next event',outcome.message)
        read.assert_called_once_with()

    def test_unrecognized_challenge_and_oversize_are_invalid_not_programme_empty(self):
        for raw in (b'<html>challenge</html>',listing().replace(b'Make It Up Club',b'Unknown source'),
                    listing()+b' '*(source.MAX_BYTES+1), listing().replace(b'</div>',b'</span>')):
            with patch.object(transport,'bar_open',return_value=(200,raw)):
                outcome=engine.collect('bar_open',now=NOW)
            self.assertEqual(outcome.status,'invalid')
            self.assertEqual(outcome.candidates,())

    def test_unavailability_exception_text_never_becomes_publisher_or_caller_output(self):
        with patch.object(transport,'bar_open',return_value=(200,listing())), \
             patch.object(source,'parse_listing',side_effect=engine.ProgrammeUnavailable('CANARY')):
            outcome=engine.collect('bar_open',now=NOW)
        self.assertEqual(outcome.status,'unavailable')
        self.assertNotIn('CANARY',outcome.message)
