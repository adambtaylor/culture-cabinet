"""Native HTML boundaries; backend authority and real browser proof are separate.

Use the real portable parent and Django forms, without a database, session
forgery or a view that dispatches acquisition. Assertions cover what the user
can submit; PA1/PS1 integration must independently prove those commands safe.
"""
import ast
from pathlib import Path

from django.template import Context, Engine
from django.test import RequestFactory, SimpleTestCase

from shelf.tests.test_enhance_presentation import Document
from shelf.obtain_preview import SelectionForm
from shelf.obtain_views import SubmissionForm
from shelf.enhancement_views import (
    _cabinet_projection, _SAVED_ATTRIBUTION_UNAVAILABLE, render_portable_saved)

ROOT = Path(__file__).resolve().parents[3]


class PortableSavedPresentationTests(SimpleTestCase):
    def render_page(self, name, **changes):
        module = ast.parse((ROOT / 'backend/shelf/enhancement_views.py').read_text())
        literal = next(n.value for n in module.body if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == '_PORTABLE_ENHANCE_BASE'
                               for t in n.targets))
        engine = Engine(dirs=[ROOT / 'templates'])
        values = {'enhancement_base': engine.from_string(ast.literal_eval(literal)),
                  'csrf_token': 's' * 64, 'profile_ref': 'fictional-profile',
                  'profile_label': 'Fictional <img> cabinet', 'selection_token': 'fresh-context',
                  'cabinet_url': '/cabinet?profile_ref=fictional-profile', **changes}
        html = engine.get_template('cabinet/' + name + '.html').render(
            Context(values, use_l10n=False))
        return html, Document(html)

    def form_fields(self, form):
        return {n.attrs.get('name'): n.attrs.get('value')
                for n in form.nodes() if n.tag in ('input', 'select')}

    def cabinet(self, category='book', **changes):
        current = {'schema': 'cabinet-current/v1', 'published': True, 'code': 'OK',
                   'publication_revision': 7, 'manifest': {'schema': 'cabinet-manifest/v1',
                   'for_you': [], 'categories': [{'category': category, 'state': 'fresh',
                   'code': 'OK', 'cards': [{'key': 'opaque-<img>-key', 'category': category,
                   'title': 'Fiction <script> book', 'medium': category, 'provider': 'Fixture',
                   'facts': []}]}]}}
        projection = _cabinet_projection(current)
        for row in projection['categories']:
            row.update(visible=True, url='/cabinet?category=' + category)
        return self.render_page('portable_cabinet', **projection, **changes)

    def test_selection_is_explicit_post_and_navigation_does_not_select(self):
        _, doc = self.cabinet(selection_token='')
        forms = doc.tagged('form')
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0].attrs['action'], '/cabinet/saved/select/')
        self.assertEqual(forms[0].attrs['method'], 'post')
        self.assertEqual(self.form_fields(forms[0]), {
            'profile_ref': 'fictional-profile', 'csrfmiddlewaretoken': 's' * 64})
        self.assertIn('Selection does not start acquisition', doc.root.text())

    def test_save_posts_only_current_candidate_context_and_preserves_escaping(self):
        # Form component only: current-policy native Save must still deny under
        # C4. This context does not stand in for a successful native Save flow.
        html, doc = self.cabinet()
        form = next(f for f in doc.tagged('form') if f.attrs['action'] == '/cabinet/saved/save/')
        self.assertEqual(set(self.form_fields(form)), {'csrfmiddlewaretoken', 'profile_ref',
                         'selection_token', 'publication_revision', 'candidate_key', 'list_kind'})
        self.assertEqual(self.form_fields(form)['candidate_key'], 'opaque-<img>-key')
        self.assertEqual(self.form_fields(form)['publication_revision'], '7')
        self.assertEqual([n.attrs['value'] for n in form.nodes() if n.tag == 'option'],
                         ['read', 'favorites'])
        self.assertEqual(doc.tagged('img'), [])
        self.assertNotIn('<script> book', html)
        self.assertIn('never starts Obtain', doc.root.text())

    def test_missing_durable_permission_shows_reason_without_save_action(self):
        _, doc = self.cabinet(save_unavailable_reason=_SAVED_ATTRIBUTION_UNAVAILABLE)
        self.assertEqual(doc.tagged('form'), [])
        self.assertIn(_SAVED_ATTRIBUTION_UNAVAILABLE, doc.root.text())
        self.assertIn('Fiction <script> book', doc.root.text())

    def test_unsupported_category_stays_visible_without_save_command(self):
        _, doc = self.cabinet(category='events')
        self.assertEqual(doc.tagged('form'), [])
        self.assertIn('Fiction <script> book', doc.root.text())
        self.assertIn('Save and Obtain are unavailable for this category', doc.root.text())

    def test_saved_search_and_remove_are_distinct_bound_posts(self):
        _, doc = self.render_page('portable_saved_items', saved_lists=[{'label': 'To watch',
            'items': [{'title': 'Fiction <img> series', 'is_tv': True,
                       'obtain_preview_url': '/cabinet/saved/8/search/',
                       'remove_url': '/cabinet/saved/8/remove/'}]}])
        forms = doc.tagged('form')
        self.assertEqual([f.attrs['action'] for f in forms],
                         ['/cabinet/saved/8/search/', '/cabinet/saved/8/remove/'])
        for form in forms:
            self.assertEqual(form.attrs['method'], 'post')
            values = self.form_fields(form)
            self.assertEqual(values['selection_token'], 'fresh-context')
            self.assertEqual(values['csrfmiddlewaretoken'], 's' * 64)
            self.assertEqual(values['profile_ref'], 'fictional-profile')
        numbers = {n.attrs['name']: n.attrs for n in doc.tagged('input')
                   if n.attrs.get('type') == 'number'}
        self.assertEqual((numbers['season']['min'], numbers['season']['max']), ('0', '999'))
        self.assertEqual((numbers['episode']['min'], numbers['episode']['max']), ('1', '999'))
        self.assertEqual(doc.tagged('img'), [])

    def test_unknown_and_queued_receipts_never_offer_retrieval_or_resend(self):
        for state, copy in [('outcome_unknown', 'Do not resubmit'),
                            ('queued', 'Completion and local availability remain unknown'),
                            ('local_verified', 'does not establish playable content')]:
            with self.subTest(state=state):
                _, doc = self.render_page('portable_saved_items', acquisition_job={
                    'title': 'Fiction Film', 'status': state,
                    'submission_review_url': '/cabinet/saved/jobs/9/submit/review/'})
                self.assertEqual(doc.tagged('form'), [])
                self.assertIn(copy, doc.root.text())

    def test_failed_search_offers_only_deliberate_return_not_enqueue_retry(self):
        _, doc = self.render_page('portable_obtain_preview', obtain_preview={
            'title': 'Fiction Book', 'scope': 'audiobook', 'state': 'failed'})
        self.assertEqual(doc.tagged('form'), [])
        self.assertIn('Search failed before submission', doc.root.text())
        self.assertIn('no enqueue retry is implied', doc.root.text())
        self.assertIn('Full audiobook, never ebook', doc.root.text())

    def test_exact_selection_preserves_native_token_scope_and_required_acknowledgments(self):
        form = SelectionForm(initial={'review_token': 'native-review-token',
                                     'scope': 'audiobook', 'edition': 'Fiction <img> edition',
                                     'storage': 'local_library'})
        _, doc = self.render_page('portable_obtain_preview', obtain_preview={
            'title': 'Fiction Book', 'scope': 'audiobook', 'state': 'results'},
            obtain_selected_result={'title': 'Fiction <img> edition', 'size': 100},
            obtain_result_form=form,
            obtain_result_confirm_url='/cabinet/saved/previews/p/r/review/confirm/')
        rendered = doc.tagged('form')[0]
        self.assertEqual(self.form_fields(rendered)['review_token'], 'native-review-token')
        checks = [n for n in rendered.nodes() if n.attrs.get('type') == 'checkbox']
        self.assertTrue(checks)
        self.assertTrue(all('required' in n.attrs and 'checked' not in n.attrs for n in checks))
        self.assertEqual(doc.tagged('img'), [])
        self.assertIn('cannot enqueue', doc.root.text())

    def test_submission_requires_separate_native_five_unchecked_confirmations(self):
        _, doc = self.render_page('portable_obtain_submission',
            obtain_submission={'selection': {'edition': 'Fiction edition', 'storage': 'local_library'},
                'exact': {'result_id': 'exact-result', 'guid': 'fictional-guid', 'size': 100},
                'max_bytes': 20000, 'nzb_sha256': 'a' * 64},
            acquisition_job={'title': 'Fiction Film', 'scope': 'film'},
            obtain_submit_form=SubmissionForm(initial={'token': 'fresh-native-token'}),
            obtain_submit_url='/cabinet/saved/jobs/9/submit/')
        form = doc.tagged('form')[0]
        self.assertEqual(form.attrs['method'], 'post')
        self.assertEqual(self.form_fields(form)['token'], 'fresh-native-token')
        self.assertEqual(self.form_fields(form)['selection_token'], 'fresh-context')
        checks = [n for n in form.nodes() if n.attrs.get('type') == 'checkbox']
        self.assertEqual({n.attrs['name'] for n in checks},
                         {'exact_selection', 'rights', 'scope', 'size', 'duplicates'})
        self.assertTrue(all('required' in n.attrs and 'checked' not in n.attrs for n in checks))
        self.assertIn('do not resubmit', doc.root.text())
        _, denied = self.render_page('portable_obtain_submission', selection_token='',
                                    obtain_submission={'present': True})
        self.assertEqual(denied.tagged('form'), [])

    def test_renderer_uses_explicit_csrf_without_exposing_request_context(self):
        request = RequestFactory().get('/cabinet/saved/')
        request.private_marker = 'DO-NOT-EXPOSE'
        response = render_portable_saved(request, template='cabinet/portable_saved_items.html',
            context={'profile_label': 'Fictional cabinet', 'profile_ref': 'fixture',
                     'selection_token': 'context', 'saved_lists': []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertNotIn(b'DO-NOT-EXPOSE', response.content)
        with self.assertRaises(ValueError):
            render_portable_saved(request, template='cabinet/saved_items.html', context={})

    def test_composition_preserves_artist_forms_and_source_consent_without_saved_selection(self):
        # Actual combined templates, scalar component fixtures only. Browser
        # acceptance must still follow N1's real routes and live PA1 projection.
        _, artist = self.render_page('portable_cabinet', artist_input=True,
            entry_ref='entry', newmutation_id='mutation', base_revision=0)
        self.assertEqual([f.attrs['action'] for f in artist.tagged('form')],
                         ['/cabinet/taste/artist', '/cabinet/taste/artist'])
        self.assertEqual([self.form_fields(f)['signal'] for f in artist.tagged('form')],
                         ['interest', 'enjoyment'])
        self.assertNotIn('/cabinet/saved/select/', [f.attrs['action'] for f in artist.tagged('form')])
        _, source = self.render_page('portable_cabinet', source_setup=True,
            source_configured=True, source_consent_action='grant', source_consent_revision=2,
            source_connection_ref='source', source_choices=[
                {'code': 'anticipated_reactions', 'label': 'Artist interests'}])
        self.assertEqual([f.attrs['action'] for f in source.tagged('form')],
                         ['/cabinet/source/musicbrainz/consent'])
        fields = self.form_fields(source.tagged('form')[0])
        self.assertEqual(fields['source_connection_ref'], 'source')
        self.assertEqual(fields['expected_revision'], '2')
        self.assertIn('allow_anticipated_reactions', fields)
        self.assertNotIn('selection_token', fields)

    def test_unavailable_save_keeps_enhance_and_source_navigation_without_success_control(self):
        _, doc = self.cabinet(save_unavailable_reason=_SAVED_ATTRIBUTION_UNAVAILABLE,
            enhance_url='/enhance?profile_ref=fictional-profile&connection_ref=model')
        links = [n.attrs.get('href') for n in doc.tagged('a')]
        self.assertIn('/enhance?profile_ref=fictional-profile&connection_ref=model', links)
        self.assertIn('/cabinet/source/musicbrainz?profile_ref=fictional-profile', links)
        self.assertIn('/cabinet/taste/artist?profile_ref=fictional-profile', links)
        self.assertIn('/cabinet/saved/', links)
        self.assertEqual(doc.tagged('form'), [])
        self.assertIn(_SAVED_ATTRIBUTION_UNAVAILABLE, doc.root.text())
