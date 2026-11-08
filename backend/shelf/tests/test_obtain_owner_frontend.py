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
    def __init__(self, name, value='', kind='text'):
        self.html_name, self._value, self.kind = name, value, kind
        self.errors = ''

    def value(self):
        return self._value

    @property
    def as_hidden(self):
        return format_html('<input type="hidden" name="{}" value="{}">', self.html_name, self._value)

    @property
    def label_tag(self):
        return format_html('<label for="id_{}">{}</label>', self.html_name, self.html_name)

    @property
    def widget(self):
        return format_html('<input type="{}" id="id_{}" name="{}" value="{}">', self.kind, self.html_name, self.html_name, self._value)

    def __str__(self):
        return self.widget

    def __html__(self):
        return self.widget


class Form:
    def __init__(self, values, hidden=()):
        self.fields = {name: Field(name, value, 'hidden' if name in hidden else 'text') for name, value in values.items()}
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
            self.current = {'action': attrs.get('action'), 'fields': []}
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
        options = {'title': 'Fixture series', 'scope': 'episode_s02e01', 'search_enabled': True, 'search_url': '/cabinet/obtain/search/'}
        for owner in [True, False]:
            values = {'owner_selection_ref': 'opaque-owner'} if owner else {'publication_revision': '2', 'candidate_key': 'tv:fixture', 'season': '2', 'episode': '1'}
            _, page = self.render('portable_obtain_options.html', obtain_options=options, obtain_options_form=Form(values))
            self.assertEqual(len(page.forms), 1)
            self.assertEqual(self.names(page.forms[0]), common | set(values))

    def test_known_submission_tags_share_safe_release_fields_unknown_has_no_send(self):
        exact = IndependentRelease(result_id='hydra-result', guid='hydra-guid', size=123)
        selection = IndependentRelease(edition='Independent release edition', storage='TV storage')
        for schema in [None, 'obtain-publication-submission/v1', 'obtain-owner-selection-submission/v1', 'unexpected']:
            binding = {'exact': exact, 'selection': selection, 'max_bytes': 456, 'nzb_sha256': 'd' * 64}
            if schema is not None:
                binding['schema'] = schema
            html, page = self.render('portable_obtain_submission.html', obtain_submission=binding, acquisition_job={'title': 'Retained release label', 'scope': 'episode_s02e01', 'episode_code': 'S02E01'}, obtain_submit_form=Form({'token': 'opaque-submit'}, {'token'}), obtain_submit_url='/cabinet/saved/jobs/42/submit/')
            if schema == 'unexpected':
                self.assertEqual(page.forms, [])
            else:
                self.assertEqual(len(page.forms), 1)
                self.assertIn('Independent release edition', html)
                self.assertIn('TV storage', html)

    def test_owner_entry_link_is_visible_without_model_or_publication(self):
        html, page = self.render('portable_cabinet.html', published=False, publication_revision=0)
        self.assertIn('/cabinet/obtain/tv/?profile_ref=', html)
        self.assertIn('Obtain a TV episode', html)
        self.assertFalse(any(form['action'] == '/enhance/start' for form in page.forms))
