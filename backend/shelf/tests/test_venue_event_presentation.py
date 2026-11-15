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


class SourcePicker(Forms):
    def __init__(self, html):
        self.selects = []; self.active_select = None
        super().__init__(html)

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == 'select' and self.current is not None:
            self.active_select = dict(attrs=dict(attrs), options=[], form=self.current)
            self.selects.append(self.active_select)
        elif tag == 'option' and self.active_select is not None:
            self.active_select['options'].append(dict(attrs))

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == 'select': self.active_select = None


@override_settings(ROOT_URLCONF='cabinet.provider_urls')
class EventRegistryPresentationTests(SimpleTestCase):
    def page(self, **extra):
        from shelf.event_source_registry import source_controls
        facts = dict(selected_profile={'pk': 1, 'label': 'Fictional cabinet'}, available=True,
            mode='household', revision=7, nonce='FICTIONAL-NONCE', queue='all', csrf_token='s'*64,
            action_url='/profiles/1/discovery/action/', events=[], public_results=[],
            event_sources=source_controls())
        facts.update(extra)
        return render_to_string('cabinet/discovery.html', facts)

    def test_actual_closed_registry_uses_one_native_picker_without_duplicate_alias_reads(self):
        from shelf.event_source_registry import source_controls
        sources = source_controls(); html = self.page(); doc = SourcePicker(html)
        picker = next(s for s in doc.selects if s['attrs'].get('name') == 'source_id')
        self.assertIn('required', picker['attrs'])
        self.assertEqual(picker['options'][0]['value'], '')
        self.assertIn('selected', picker['options'][0])
        options = {o['value']: o for o in picker['options'][1:]}
        self.assertEqual(set(options), {s['source_id'] for s in sources})
        self.assertEqual(len(options), 35)
        for source in sources:
            self.assertEqual('disabled' not in options[source['source_id']], source['implemented'])
        form = picker['form']
        self.assertEqual(form['attrs']['action'], '/profiles/1/discovery/action/')
        self.assertEqual(form['attrs']['method'], 'post')
        self.assertEqual({i['name']: i.get('value') for i in form['inputs']},
            dict(csrfmiddlewaretoken='s'*64, mode='household', revision='7', nonce='FICTIONAL-NONCE', queue='all'))
        self.assertEqual(form['buttons'], [dict(type='submit', name='operation', value='event_source')])
        # The registry replaces the five alias buttons only when its actual
        # metadata is present; it must not offer duplicate source refresh paths.
        alias_values = {'rrr', 'pbs', 'venue303', 'forum', 'events'}
        self.assertFalse(any(b.get('value') in alias_values for f in doc.forms for b in f['buttons']))
        self.assertIn('reader available', html)
        self.assertIn('access check required', html)
        self.assertIn('permission needed', html)
        self.assertIn('owner action needed', html)
        self.assertIn('not connected', html)
        self.assertIn('does not confirm current access or a successful read', html)
        for extra in ({'mode': 'sample'}, {'available': False}, {'queue': 'artist'}):
            self.assertNotIn('data-event-source-read', self.page(**extra))

    def test_source_local_results_distinguish_failure_empty_partial_and_ready_without_claiming_working(self):
        results = [dict(source=source, at=FETCHED, outcome=dict(status=status, message='Recorded source note.'))
            for source, status in [('PBS Events', 'ready'), ('303 gigs and events', 'timeout'),
                ('Forum Melbourne shows', 'permission-required'), ('NGV', 'unavailable'),
                ('Triple R events', 'invalid'), ('Bounded listing', 'empty'),
                ('Partial listing', 'partial'), ('Deferred listing', 'rate-limited'),
                ('Failed listing', 'error'), ('Unknown listing', 'unrecognized')]]
        html = self.page(public_results=results)
        for phrase in ('Source leads received', 'Read timed out', 'Source access not confirmed',
                       'Reader unavailable', 'Source unavailable', 'No eligible leads in this bounded read',
                       'Partial source results', 'Read deferred', 'Read failed', 'Read outcome not confirmed',
                       'not a successful empty listing', 'does not establish that no events or tickets exist',
                       'Last recorded read:', 'choosing another explicit source read'):
            self.assertIn(phrase, html)
        self.assertEqual(html.count('class="paper-panel" role="status"'), len(results))
        self.assertNotIn('working source', html.lower())
        self.assertIn('PBS Events', html)  # An independent failure cannot hide this result.

    def test_fixed_reference_metadata_and_result_notes_escape_and_remain_distinct_from_acquired_inventory(self):
        from shelf.event_source_registry import source_controls
        sources = list(source_controls())
        sources[0] = {**sources[0], 'label': '<Reader & source>', 'coverage': '<Bounded & unverified>'}
        html = self.page(event_sources=sources, public_results=[dict(source='<Result & source>', at='',
            outcome=dict(status='invalid', message='<Untrusted & notice>'))])
        for escaped in ('&lt;Reader &amp; source&gt;', '&lt;Bounded &amp; unverified&gt;',
                        '&lt;Result &amp; source&gt;', '&lt;Untrusted &amp; notice&gt;'):
            self.assertIn(escaped, html)
        self.assertNotIn('<Untrusted', html)
        self.assertIn('time not recorded', html)
        self.assertIn('opening a reference manually does not acquire listings into Cabinet', html)
        self.assertNotIn('observation_ref', html)  # No invented visual/saved-reference port.


@override_settings(ROOT_URLCONF='cabinet.provider_urls')
class SavedReferencePresentationTests(SimpleTestCase):
    """Closed producer-port rows, not a source-retention or native-caller proof."""
    def rows(self):
        from shelf.discovery_sources import Candidate
        events = parse_listing(listing(card(clock='7:30 PM'), card(clock='9:30 PM')),
                               retrieved_at=FETCHED, provenance='fixture')
        rows = []
        for event in events:
            identity = Candidate(event.key, 'CURRENT-TITLE-MUST-NOT-RETURN', 'event', '303',
                'supplied-snapshot', 1, FETCHED, url=None)
            reference = dict(schema='event-saved-reference/v1', source_id='venue303',
                official_url='https://303.net.au/gigs-events/fictional-interface-test',
                occurrence_ref=event.occurrence_ref, occurrence_basis=event.occurrence_basis,
                retention_policy_ref='fixture-event-private-bookmark-reference/v1')
            rows.append(dict(card=identity, reference_only=True, saved_reference=reference,
                freshness='Stale — source details expired; current details unverified',
                for_you={'text': 'CURRENT-RECOMMENDATION-MUST-NOT-RETURN', 'version': 'old'},
                why=['OLD-EXPLANATION-MUST-NOT-RETURN'], choice='interested', saved=True,
                note='My <own> note'))
        return rows

    def render_rows(self, rows, **extra):
        facts = dict(selected_profile={'pk': 1, 'label': 'Fictional cabinet'}, available=True,
            mode='household', revision=7, nonce='FICTIONAL-NONCE', queue='all', csrf_token='s'*64,
            action_url='/profiles/1/discovery/action/', events=[], responses=rows, public_results=[],
            choice_options=[('interested', 'Interested')])
        facts.update(extra)
        return render_to_string('cabinet/discovery.html', facts)

    def test_checked_shared_url_references_keep_distinct_occurrences_and_explicit_stale_meaning(self):
        rows = self.rows(); html = self.render_rows(rows); doc = Forms(html)
        self.assertNotEqual(rows[0]['card'].key, rows[1]['card'].key)
        self.assertNotEqual(rows[0]['saved_reference']['occurrence_ref'], rows[1]['saved_reference']['occurrence_ref'])
        self.assertEqual([a['data-card-key'] for a in doc.articles], [r['card'].key for r in rows])
        self.assertEqual(html.count('Original source page; current details unverified'), 2)
        self.assertEqual(html.count('https://303.net.au/gigs-events/fictional-interface-test'), 2)
        for row in rows:
            self.assertIn(row['saved_reference']['occurrence_ref'], html)
            self.assertIn(row['saved_reference']['occurrence_basis'], html)
        self.assertIn('Different sessions may share this page', html)
        self.assertIn('not a new recommendation or a ticket check', html)
        for absent in ('CURRENT-TITLE-MUST-NOT-RETURN', 'CURRENT-RECOMMENDATION-MUST-NOT-RETURN',
                       'OLD-EXPLANATION-MUST-NOT-RETURN', '<strong>For you</strong>',
                       'Source-listed date or range:', 'Source rank', 'Retrieval:',
                       'fixture-event-private-bookmark-reference/v1'):
            self.assertNotIn(absent, html)
        for form in doc.forms:
            if any(i.get('name') == 'key' for i in form['inputs']):
                fields = {i['name']: i.get('value') for i in form['inputs']}
                self.assertEqual(fields['csrfmiddlewaretoken'], 's'*64)
                self.assertEqual(fields['nonce'], 'FICTIONAL-NONCE')
                self.assertEqual(fields['revision'], '7')
                self.assertFalse(set(fields) & {'official_url', 'saved_reference', 'occurrence_ref'})
        self.assertIn('Remove local discovery save', html)
        self.assertIn('My &lt;own&gt; note', html)

    def test_absent_checked_reference_preserves_human_state_without_backfilling_from_card_url(self):
        row = self.rows()[0]
        row['saved_reference'] = None
        row['card'] = replace(row['card'], url='https://example.invalid/do-not-backfill')
        html = self.render_rows([row])
        self.assertIn('No checked source link is available', html)
        self.assertIn('Only your saved response identity is shown', html)
        self.assertNotIn('https://example.invalid/do-not-backfill', html)
        self.assertNotIn('Original source page;', html)
        self.assertNotIn('<strong>For you</strong>', html)
        self.assertIn(row['card'].key, html)
        self.assertIn('Your current response: interested', html)
        self.assertIn('Remove local discovery save', html)

    def test_link_needs_reference_only_mode_and_checked_schema_not_arbitrary_candidate_metadata(self):
        for change in ('ordinary', 'sample', 'schema'):
            row = self.rows()[0]; extra = {}
            if change == 'ordinary': row['reference_only'] = False
            elif change == 'sample': extra['mode'] = 'sample'
            else: row['saved_reference'] = {**row['saved_reference'], 'schema': 'untrusted/v0'}
            html = self.render_rows([row], **extra)
            self.assertNotIn('https://303.net.au/gigs-events/fictional-interface-test', html)

    def test_saved_only_history_uses_full_native_response_and_current_cards_keep_partial_markers(self):
        # A saved-only expired key is not in the current recommendation deck.
        # The partial JSON path intentionally refuses its first response;
        # preserve that guard and select the existing full native form path.
        for checked in (True, False):
            for choice in ('', 'interested'):
                row = self.rows()[0]; row['choice'] = choice
                if not checked: row['saved_reference'] = None
                with self.subTest(checked=checked, choice=choice):
                    doc = Forms(self.render_rows([row]))
                    response = next(f for f in doc.forms if
                        any(i.get('name') == 'operation' and i.get('value') == 'respond' for i in f['inputs']) or
                        any(b.get('name') == 'operation' and b.get('value') == 'respond' for b in f['buttons']))
                    self.assertNotIn('data-feedback-form', response['attrs'])
                    self.assertNotIn('data-feedback-correction', response['attrs'])
                    self.assertEqual(response['attrs']['method'], 'post')
                    self.assertEqual(response['attrs']['action'], '/profiles/1/discovery/action/')
                    fields = {i['name']: i.get('value') for i in response['inputs']}
                    self.assertEqual(fields['key'], row['card'].key)
                    self.assertEqual(fields['csrfmiddlewaretoken'], 's'*64)
                    self.assertEqual(fields['mode'], 'household')
                    self.assertEqual(fields['revision'], '7')
                    self.assertEqual(fields['nonce'], 'FICTIONAL-NONCE')
                    self.assertFalse(set(fields) & {'saved_reference', 'official_url', 'occurrence_ref'})
                    if not choice: self.assertEqual(fields['operation'], 'respond')
        for choice in ('', 'interested'):
            row = self.rows()[0]; row.update(reference_only=False, saved_reference=None, choice=choice)
            response = next(f for f in Forms(self.render_rows([row])).forms
                            if 'data-feedback-form' in f['attrs'])
            self.assertEqual(response['attrs'].get('data-feedback-correction'), 'respond' if choice else None)
