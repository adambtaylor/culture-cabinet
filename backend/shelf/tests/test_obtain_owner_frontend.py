"""Template-consumer checks against the published07 form/projection contract.

These use only Django's template library and explicit fixture widgets. They do
not import product views/forms, create authority, or prove a native HTTP caller.
"""
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
import unittest

from django.template import Context, Engine
from django.utils.html import format_html, format_html_join


class Field:
    def __init__(self, name, value='', kind='text', label=None):
        self.html_name, self._value, self.kind = name, value, kind
        self.label = label or name
        self.errors = ''

    def value(self):
        return self._value

    @property
    def as_hidden(self):
        return format_html('<input type="hidden" name="{}" value="{}">', self.html_name, self._value)

    @property
    def label_tag(self):
        return format_html('<label for="id_{}">{}</label>', self.html_name, self.label)

    @property
    def widget(self):
        if self.kind == 'checkbox':
            return format_html('<input type="checkbox" id="id_{}" name="{}" required>', self.html_name, self.html_name)
        return format_html('<input type="{}" id="id_{}" name="{}" value="{}">', self.kind, self.html_name, self.html_name, self._value)

    def __str__(self):
        return self.widget

    def __html__(self):
        return self.widget


class Form:
    def __init__(self, values, hidden=(), *, checkboxes=(), labels=None):
        self.fields = {name: Field(name, value, 'hidden' if name in hidden else 'checkbox' if name in checkboxes else 'text', (labels or {}).get(name)) for name, value in values.items()}
        self.non_field_errors = ''

    def __getattr__(self, key):
        if key in self.fields:
            return self.fields[key]
        raise AttributeError(key)

    @property
    def as_p(self):
        return format_html_join('', '<p>{}{}</p>', ((field.label_tag if field.kind != 'hidden' else '', field.as_hidden if field.kind == 'hidden' else field.widget) for field in self.fields.values()))


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms, self.scripts, self.ids = [], [], []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        if tag == 'script':
            self.scripts.append(attrs)
        if tag == 'form':
            self.current = {'action': attrs.get('action'), 'method': attrs.get('method', 'get').lower(), 'fields': []}
            self.forms.append(self.current)
        elif tag in {'input', 'select', 'textarea'} and self.current is not None and attrs.get('name'):
            self.current['fields'].append(attrs)

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


class IndependentRelease(SimpleNamespace):
    def __getattr__(self, name):
        if name in {'snapshot', 'item_id', 'source_id', 'provider', 'medium'}:
            raise AssertionError('A request must not recover catalogue facts from release metadata')
        raise AttributeError(name)


class OwnerTVFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(dirs=[str(Path(__file__).resolve().parents[3] / 'templates')])
        cls.parent = cls.engine.from_string('<main>{% block content %}{% endblock %}</main>')

    def render(self, name, **values):
        context = {'enhancement_base': self.parent, 'csrf_token': 'x' * 32, 'profile_ref': '11111111-1111-4111-8111-111111111111', 'profile_label': 'UI fixture', 'selection_token': 'opaque-selection-fixture', 'cabinet_url': '/cabinet?profile_ref=fixture', 'obtain_tv_entry_url': '/cabinet/obtain/tv/?profile_ref=fixture'}
        context.update(values)
        html = self.engine.get_template('cabinet/' + name).render(Context(context, use_l10n=False, use_tz=False))
        return html, Page(html)

    def names(self, form):
        return {field['name'] for field in form['fields']}

    def test_manual_entry_has_only_current_form_fields_and_no_script(self):
        form = Form({'title': 'Fixture series', 'series_year': '2024', 'season': '2', 'episode': '1'})
        html, page = self.render('portable_obtain_tv.html', obtain_tv_form=form, obtain_tv_preview_url='/cabinet/obtain/tv/preview/')
        self.assertEqual(len(page.forms), 1)
        self.assertEqual(page.forms[0]['action'], '/cabinet/obtain/tv/preview/')
        self.assertEqual(self.names(page.forms[0]), {'csrfmiddlewaretoken', 'profile_ref', 'selection_token', 'title', 'series_year', 'season', 'episode'})
        self.assertEqual(page.scripts, [])
        self.assertNotIn('Dark Matter', html)

    def test_unselected_profile_requires_only_explicit_closed_navigation_post(self):
        _, page = self.render('portable_obtain_tv.html', obtain_tv_needs_selection=True, obtain_tv_select_profile_url='/cabinet/saved/select/', selection_token=None)
        self.assertEqual(len(page.forms), 1)
        self.assertEqual(page.forms[0]['action'], '/cabinet/saved/select/')
        self.assertEqual(self.names(page.forms[0]), {'csrfmiddlewaretoken', 'profile_ref', 'return_to'})
        self.assertEqual(next(field['value'] for field in page.forms[0]['fields'] if field['name'] == 'return_to'), 'owner_tv')

    def test_source_preview_keeps_exact_opaque_form_and_escapes_body(self):
        values = {'intent_ref': 'opaque-intent', 'source_preview_ref': 'opaque-preview', 'plan_digest': 'a' * 64, 'operation_digest': 'b' * 64, 'confirmed': ''}
        html, page = self.render('portable_obtain_tv.html', obtain_tv_preview={'body': '</textarea><script>bad()</script>'}, obtain_tv_lookup_form=Form(values, values.keys() - {'confirmed'}), obtain_tv_lookup_url='/cabinet/obtain/tv/lookup/')
        self.assertEqual(len(page.forms), 1)
        self.assertEqual(self.names(page.forms[0]), set(values) | {'csrfmiddlewaretoken', 'profile_ref', 'selection_token'})
        self.assertEqual(page.scripts, [])
        self.assertIn('&lt;/textarea&gt;', html)

    def test_match_forms_keep_refs_unique_labels_and_no_source_identity_field(self):
        matches = []
        for number in range(2):
            values = {'intent_ref': 'opaque-intent', 'match_ref': 'opaque-' + str(number), 'match_digest': 'c' * 64, 'confirmed': ''}
            matches.append({'title': 'Fixture series', 'series_year': 2024, 'scope': 'episode_s02e01', 'selectable': True, 'select_form': Form(values, values.keys() - {'confirmed'})})
        html, page = self.render('portable_obtain_tv.html', obtain_tv_matches=matches, obtain_tv_select_url='/cabinet/obtain/tv/select/')
        self.assertEqual(len(page.forms), 2)
        self.assertEqual(len(page.ids), len(set(page.ids)))
        for form in page.forms:
            self.assertEqual(self.names(form), {'csrfmiddlewaretoken', 'profile_ref', 'selection_token', 'intent_ref', 'match_ref', 'match_digest', 'confirmed'})
            confirmation = next(field for field in form['fields'] if field['name'] == 'confirmed')
            self.assertEqual(confirmation['type'], 'checkbox')
            self.assertNotIn('checked', confirmation)
        self.assertIn('S02E01', html)
        self.assertEqual(page.scripts, [])

    def test_empty_lookup_is_distinct_from_a_missing_lookup_or_unselectable_match(self):
        html, page = self.render('portable_obtain_tv.html', obtain_tv_matches=[])
        self.assertIn('No matching series was confirmed', html)
        self.assertEqual(page.forms, [])
        _, denied = self.render('portable_obtain_tv.html', obtain_tv_matches=[{'title': 'Unresolved version', 'scope': 'episode_s02e01', 'selectable': False}])
        self.assertEqual(denied.forms, [])

    def test_options_emit_exactly_one_selector_variant(self):
        common = {'csrfmiddlewaretoken', 'profile_ref', 'selection_token'}
        options = {'title': 'Fixture series', 'scope': 'episode_s02e01', 'search_enabled': True, 'search_url': '/cabinet/obtain/search/', 'review_url': '/cabinet/obtain/options/', 'query': 'Fixture series 2024 S02E01', 'destination': 'http://hydra.invalid:5076'}
        for owner in [True, False]:
            values = {'owner_selection_ref': 'opaque-owner'} if owner else {'publication_revision': '2', 'candidate_key': 'tv:fixture', 'season': '2', 'episode': '1'}
            values.update(search_review_ref='11111111-1111-4111-8111-111111111112', search_confirmed='')
            _, page = self.render('portable_obtain_options.html', obtain_options=options, obtain_options_form=Form(values, {'search_review_ref'}, checkboxes={'search_confirmed'}))
            self.assertEqual(len(page.forms), 1 if owner else 2)
            search = next(form for form in page.forms if form['method'] == 'post')
            self.assertEqual(self.names(search), common | set(values))
            self.assertEqual(search['action'], '/cabinet/obtain/search/')
            self.assertEqual(len(page.ids), len(set(page.ids)))
            if not owner:
                review = next(form for form in page.forms if form['method'] == 'get')
                self.assertEqual(review['action'], '/cabinet/obtain/options/')
                self.assertEqual(self.names(review), {'profile_ref', 'publication_revision', 'candidate_key', 'season', 'episode'})
                for name in ['season', 'episode']:
                    self.assertEqual(next(field for field in review['fields'] if field['name'] == name)['type'], 'text')
                    frozen = next(field for field in search['fields'] if field['name'] == name)
                    self.assertEqual(frozen['type'], 'hidden')
                    self.assertEqual(frozen['value'], values[name])

    def test_publication_default_episode_review_omits_secrets_and_freezes_only_present_pair(self):
        options = {'title': 'Fixture series', 'scope': 'series_s01e01', 'search_enabled': True, 'search_url': '/cabinet/obtain/search/', 'review_url': '/cabinet/obtain/options/', 'query': 'Fixture series 2024 S01E01', 'destination': 'http://hydra.invalid:5076'}
        values = {'publication_revision': '2', 'candidate_key': 'tv:fixture', 'season': None, 'episode': None, 'search_review_ref': '11111111-1111-4111-8111-111111111112', 'search_confirmed': ''}
        html, page = self.render('portable_obtain_options.html', obtain_options=options, obtain_options_form=Form(values, {'search_review_ref'}, checkboxes={'search_confirmed'}), profile_ref=None)
        self.assertIn('Reviewed episode: S01E01.', html)
        review = next(form for form in page.forms if form['method'] == 'get')
        search = next(form for form in page.forms if form['method'] == 'post')
        self.assertEqual(self.names(review), {'publication_revision', 'candidate_key', 'season', 'episode'})
        self.assertEqual(self.names(search), {'csrfmiddlewaretoken', 'profile_ref', 'selection_token', 'publication_revision', 'candidate_key', 'search_review_ref', 'search_confirmed'})

    def test_options_display_exact_escaped_review_before_unchecked_required_confirmation(self):
        query = 'Different canonical title 2024 S02E01 <script>unsafe</script>'
        destination = 'http://hydra.invalid:5076/?label=<unsafe>'
        label = 'I approve this exact query and Hydra destination.'
        form = Form({'owner_selection_ref': 'opaque-owner', 'search_review_ref': '11111111-1111-4111-8111-111111111112', 'search_confirmed': ''}, {'search_review_ref'}, checkboxes={'search_confirmed'}, labels={'search_confirmed': label})
        form.search_confirmed.errors = 'Approval is required.'
        html, page = self.render('portable_obtain_options.html', obtain_options={'title': 'Display title must not build the query', 'scope': 'episode_s02e01', 'search_enabled': True, 'search_url': '/cabinet/obtain/search/', 'query': query, 'destination': destination, 'expires_at': '2030-01-01T00:00:00Z'}, obtain_options_form=form)
        self.assertIn('Different canonical title 2024 S02E01 &lt;script&gt;unsafe&lt;/script&gt;', html)
        self.assertIn('http://hydra.invalid:5076/?label=&lt;unsafe&gt;', html)
        self.assertLess(html.index('Different canonical title'), html.index('data-obtain-search'))
        self.assertIn(label, html)
        self.assertIn('Approval is required.', html)
        self.assertIn('2030-01-01T00:00:00Z', html)
        confirmation = next(field for field in page.forms[0]['fields'] if field['name'] == 'search_confirmed')
        self.assertEqual(confirmation['type'], 'checkbox')
        self.assertIn('required', confirmation)
        self.assertNotIn('checked', confirmation)
        self.assertEqual(page.scripts, [])
        self.assertTrue({'query', 'destination'}.isdisjoint(self.names(page.forms[0])))

    def test_options_without_complete_issued_review_offer_no_search(self):
        complete = {'title': 'Fixture series', 'search_enabled': True, 'search_url': '/cabinet/obtain/search/', 'query': 'Fixture series 2024 S02E01', 'destination': 'http://hydra.invalid:5076'}
        for missing in ['query', 'destination', 'search_review_ref']:
            options = {key: value for key, value in complete.items() if key != missing}
            fields = {'owner_selection_ref': 'opaque-owner', 'search_confirmed': ''}
            if missing != 'search_review_ref':
                fields['search_review_ref'] = '11111111-1111-4111-8111-111111111112'
            _, page = self.render('portable_obtain_options.html', obtain_options=options, obtain_options_form=Form(fields))
            self.assertEqual(page.forms, [])

    def test_known_submission_tags_share_safe_release_fields_unknown_has_no_send(self):
        exact = IndependentRelease(result_id='hydra-result', guid='hydra-guid', size=123)
        selection = IndependentRelease(edition='Independent release edition', storage='TV storage')
        for schema in [None, 'obtain-publication-submission/v1', 'obtain-owner-selection-submission/v1', 'unexpected']:
            binding = {'exact': exact, 'selection': selection, 'max_bytes': 456, 'nzb_sha256': 'd' * 64}
            if schema is not None:
                binding['schema'] = schema
                binding['delivery_binding'] = {'destination': {'host': 'fixture-mac', 'root': '/Volumes/TV'}, 'relative_parent': 'Fixture series', 'create_parent': False}
                binding['complete_root'] = '/srv/sab/complete'
            html, page = self.render('portable_obtain_submission.html', obtain_submission=binding, acquisition_job={'title': 'Retained release label', 'scope': 'episode_s02e01', 'episode_code': 'S02E01'}, obtain_submit_form=Form({'token': 'opaque-submit'}, {'token'}), obtain_submit_url='/cabinet/saved/jobs/42/submit/')
            if schema == 'unexpected':
                self.assertEqual(page.forms, [])
            else:
                self.assertEqual(len(page.forms), 1)
                self.assertIn('Independent release edition', html)
                self.assertIn('TV storage', html)

    def test_native_submission_shows_escaped_delivery_separately_from_sab_before_confirmation(self):
        for schema in ['obtain-publication-submission/v1', 'obtain-owner-selection-submission/v1']:
            for create_parent in [False, True]:
                delivery = {'schema': 'obtain-tv-delivery-binding/v1', 'destination': {'host': 'Mac <host>', 'root': '/Volumes/TV & Film'}, 'relative_parent': 'Series <folder>', 'create_parent': create_parent, 'digest': 'DELIVERY-DIGEST-MUST-NOT-BE-DISPLAYED'}
                binding = {'schema': schema, 'delivery_binding': delivery, 'complete_root': '/srv/sab/complete & <download>'}
                html, page = self.render('portable_obtain_submission.html', obtain_submission=binding, obtain_submit_form=Form({'token': 'opaque-submit'}, {'token'}), obtain_submit_url='/cabinet/saved/jobs/42/submit/')
                self.assertEqual(len(page.forms), 1)
                self.assertEqual(self.names(page.forms[0]), {'csrfmiddlewaretoken', 'profile_ref', 'selection_token', 'token'})
                for literal in ['Mac &lt;host&gt;', '/Volumes/TV &amp; Film', 'Series &lt;folder&gt;', '/srv/sab/complete &amp; &lt;download&gt;']:
                    self.assertIn(literal, html)
                    self.assertLess(html.index(literal), html.index('<form'))
                self.assertIn('Delivery destination', html)
                self.assertIn('Downloader completed-download location', html)
                self.assertIn('final filename will come from the verified download', html)
                self.assertNotIn(delivery['digest'], html)
                self.assertEqual('confirmation includes creating the named folder' in html, create_parent)
                self.assertEqual('Use the existing folder shown above.' in html, not create_parent)
                self.assertEqual(page.scripts, [])

    def test_native_missing_delivery_needs_new_review_and_empty_parent_means_approved_root(self):
        for schema in ['obtain-publication-submission/v1', 'obtain-owner-selection-submission/v1']:
            kwargs = {'obtain_submit_form': Form({'token': 'opaque-submit'}, {'token'}), 'obtain_submit_url': '/cabinet/saved/jobs/42/submit/'}
            html, page = self.render('portable_obtain_submission.html', obtain_submission={'schema': schema}, **kwargs)
            self.assertEqual(page.forms, [])
            self.assertIn('delivery destination needs a fresh review', html)
            binding = {'schema': schema, 'delivery_binding': {'destination': {'host': 'fixture-mac', 'root': '/Volumes/TV'}, 'relative_parent': '', 'create_parent': False}, 'complete_root': '/srv/sab/complete'}
            html, page = self.render('portable_obtain_submission.html', obtain_submission=binding, **kwargs)
            self.assertEqual(len(page.forms), 1)
            self.assertIn('The library root itself', html)
            self.assertNotIn('confirmation includes creating', html)

    def test_owner_entry_link_is_visible_without_model_or_publication(self):
        html, page = self.render('portable_cabinet.html', published=False, publication_revision=0)
        self.assertIn('/cabinet/obtain/tv/?profile_ref=', html)
        self.assertIn('Obtain a TV episode', html)
        self.assertFalse(any(form['action'] == '/enhance/start' for form in page.forms))
