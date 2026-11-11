"""Real templates over the released occurrence port; no transport or live proof."""
from dataclasses import replace
from html.parser import HTMLParser

from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings

from shelf.venue303_events import parse_listing
from .test_venue303_events import card, listing
from .test_event_sources import FETCHED


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.forms = []; self.current = None; self.articles = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        row = dict(attrs)
        if tag == 'form':
            if self.current is not None: raise AssertionError('Nested native forms')
            self.current = dict(attrs=row, inputs=[], buttons=[]); self.forms.append(self.current)
        elif tag in ('input', 'button') and self.current is not None:
            self.current['inputs' if tag == 'input' else 'buttons'].append(row)
        elif tag == 'article': self.articles.append(row)

    def handle_endtag(self, tag):
        if tag == 'form': self.current = None


@override_settings(ROOT_URLCONF='cabinet.provider_urls')
class VenueEventPresentationTests(SimpleTestCase):
    def page(self, **extra):
        facts = dict(selected_profile={'pk': 1, 'label': 'Fictional cabinet'}, available=True,
            mode='household', revision=7, nonce='FICTIONAL-NONCE', queue='all', csrf_token='s'*64,
            action_url='/profiles/1/discovery/action/', events=[], public_results=[])
        facts.update(extra)
        return render_to_string('cabinet/discovery.html', facts)

    def row(self, event):
        return dict(card=event, date_status=event.date_status(None), freshness='Fixture observation',
                    for_you={'version': 'fixture'}, why=[])

    def test_two_fixed_native_reads_keep_current_fields_and_require_explicit_buttons(self):
        html = self.page(); doc = Forms(html)
        forms = [f for f in doc.forms if f['attrs'].get('data-event-read') in ('venue303', 'forum')]
        self.assertEqual(len(forms), 2)
        for form in forms:
            operation = form['attrs']['data-event-read']
            self.assertEqual(form['attrs']['method'], 'post')
            self.assertEqual(form['attrs']['action'], '/profiles/1/discovery/action/')
            fields = {i.get('name'): i.get('value') for i in form['inputs']}
            self.assertEqual(fields, dict(csrfmiddlewaretoken='s'*64, mode='household', revision='7',
                                         nonce='FICTIONAL-NONCE', queue='all'))
            self.assertEqual(form['buttons'], [dict(type='submit', name='operation', value=operation)])
            self.assertNotIn('key', fields)
            self.assertNotIn('radio_mode', fields)
        self.assertIn('Read 303 gigs and events', html)
        self.assertIn('Read Forum Melbourne shows', html)
        self.assertIn('No automatic retry, ticket check or model request', html)

    def test_read_controls_are_absent_in_sample_and_unavailable_modes(self):
        for extra in ({'mode': 'sample'}, {'available': False}, {'queue': 'artist'}):
            html = self.page(**extra)
            self.assertNotIn('data-event-read=', html)

    def test_distinct_same_url_sessions_retain_source_keys_and_date_precision(self):
        events = parse_listing(listing(card(clock='7:30 PM'), card(clock='9:30 PM')),
                               retrieved_at=FETCHED, provenance='fixture')
        # Fixture parsers deliberately omit outgoing links. Supply one shared
        # fictional display link here while retaining fixture provenance/keys;
        # this proves template identity behavior, never a live listing read.
        events = tuple(replace(e, url='https://303.net.au/gigs-events/fictional-interface-test') for e in events)
        self.assertEqual(events[0].url, events[1].url)
        html = self.page(events=[self.row(e) for e in events]); doc = Forms(html)
        keys = [a['data-card-key'] for a in doc.articles if 'data-card-key' in a]
        self.assertEqual(keys, [e.key for e in events])
        self.assertEqual(len(set(keys)), 2)
        self.assertIn('Source-listed date or range:', html)
        self.assertIn('date/time unverified', html)
        for event in events:
            saved_keys = [i.get('value') for f in doc.forms for i in f['inputs'] if i.get('name') == 'key']
            self.assertIn(event.key, saved_keys)

    def test_unknown_range_and_source_notice_are_attributed_escaped_not_ticket_inventory(self):
        events = parse_listing(listing(card(start=''), card(end='2026-11-03', url='/gigs-events/range')),
                               retrieved_at=FETCHED, provenance='fixture')
        event = replace(events[1], source_notice='<Reported & unverified notice>')
        html = self.page(events=[self.row(events[0]), self.row(event)])
        self.assertIn('unresolved observation; no stable session identity', html)
        self.assertIn('explicit_range', html)
        self.assertIn('&lt;Reported &amp; unverified notice&gt;', html)
        self.assertNotIn('<Reported', html)
        self.assertIn('not a seller or current ticket-availability check', html)

    def test_actual_failure_categories_do_not_become_empty_or_publisher_denial(self):
        html = self.page(public_results=[
            dict(source='303 gigs and events', at=FETCHED, outcome=dict(status='invalid',
                message='Source response or transport invalid; no candidates accepted.')),
            dict(source='Forum Melbourne shows', at=FETCHED, outcome=dict(status='permission-required',
                message='Source listing access is not confirmed.'))])
        self.assertIn('Source unavailable', html)
        self.assertIn('No usable listing was accepted', html)
        self.assertIn('not a successful empty listing', html)
        self.assertIn('Source access not confirmed', html)
        self.assertIn('not evidence of a publisher denial or an empty venue', html)
        partial = self.page(public_results=[dict(source='303 gigs and events', at=FETCHED,
            outcome=dict(status='partial', message='Only the source-attributed portion is available.'))])
        self.assertIn('Partial source results', partial)
