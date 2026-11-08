"""Isolated HTTP contract fixtures, not source/inference or owner-runtime proof.

Service and authorized choice/navigation seams use isolated doubles for
projection tests. Real adapters, closed parsers, CSRF decorators, canonical refs
and native templates run only when separately admitted. Real owner fixtures
cover creation/selection in test_portable_setup_http. No double is registered
as a normal product capability.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID
from urllib.parse import urlencode

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve

from shelf import enhancement_views as views, portable_setup_views
from shelf.portable_authority import AuthorityFault
from shelf.tests.test_enhance_presentation import Document


PROFILE = '11111111-1111-4111-8111-111111111111'
CONNECTION = '22222222-2222-4222-8222-222222222222'
RUN = '33333333-3333-4333-8333-333333333333'
PREVIEW = '44444444-4444-4444-8444-444444444444'
CLIENT = '55555555-5555-4555-8555-555555555555'
NONCE = '66666666-6666-4666-8666-666666666666'


class PortableEnhanceHTTPTests(SimpleTestCase):
    def setUp(self):
        templates = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
            'DIRS': [Path(__file__).resolve().parents[3] / 'templates'],
            'OPTIONS': {'context_processors': []}}]
        self.enterContext(override_settings(TEMPLATES=templates))
        self.factory = RequestFactory()
        self.authority = SimpleNamespace(authorize=Mock())
        self.status = {'schema': 'enhance/v1', 'run_ref': RUN, 'state': 'awaiting_preview',
            'stage': 'awaiting_preview', 'code': 'OK', 'terminal': False, 'run_ready': False,
            'published': False, 'publication_revision': None, 'revision': 2,
            'category_outcomes': [], 'actions': ['status', 'preview', 'confirm', 'cancel']}
        self.disclosure = {'kind': 'catalogue', 'preview_ref': PREVIEW,
            'body': '{"query":"</textarea><img src=x onerror=alert(1)>"}',
            'body_digest': 'a' * 64, 'destination': 'https://source.invalid',
            'destinations': ['https://source.invalid'], 'expires_at': '2099-01-01T00:00:00Z',
            'omitted_groups': ['notes']}
        self.service = SimpleNamespace(authority=self.authority,
            readiness=Mock(return_value={'available': True, 'run_ready': False, 'code': 'OK', 'actions': ['start']}),
            start=Mock(return_value=self.status), status=Mock(return_value=self.status),
            preview=Mock(return_value={**self.status, 'disclosure': self.disclosure}),
            confirm=Mock(return_value=self.status), cancel=Mock(return_value={**self.status, 'stage': 'cancelled'}),
            current=Mock(return_value={'schema': 'cabinet-current/v1', 'published': False,
                                      'publication_revision': None, 'manifest': None, 'code': 'OK'}))
        self.lookup = self.enterContext(patch('shelf.portable_enhance_service.get_service', return_value=self.service))
        # Existing HTTP tests isolate the display read, not backend authority.
        # Real handler/PA1/event proofs stay in Taste's native fixture suite.
        self.feedback_read = self.enterContext(patch('shelf.portable_feedback_views.card_feedback_context',
            side_effect=lambda authority, session_ref, profile_ref, *, target_ref: {
                'profile_ref': profile_ref, 'target_ref': target_ref, 'signals': [],
                'actions': [], 'feedback_action': '/cabinet/feedback'}))

    def request(self, method='GET', payload=None, *, native=False, csrf=True, query=None):
        if method == 'GET':
            request = self.factory.get('/enhance', query or {}, HTTP_ACCEPT='text/html' if native else 'application/json')
        elif native:
            request = self.factory.post('/enhance', urlencode({'csrfmiddlewaretoken': 's' * 64, **(payload or {})}),
                                        content_type='application/x-www-form-urlencoded', HTTP_ACCEPT='text/html')
        else:
            request = self.factory.post('/enhance', json.dumps(payload or {}),
                                        content_type='application/json', HTTP_ACCEPT='application/json')
        request.session = {'portable_session': 'isolated-session'}
        request.portable_authority = self.authority
        request._dont_enforce_csrf_checks = csrf
        return request

    def start_fields(self):
        return dict(profile_ref=PROFILE, connection_ref=CONNECTION, client_key=CLIENT, refresh_nonce=NONCE)

    def test_csrf_recent_owner_foreign_scope_and_closed_authority_inputs(self):
        response = views.portable_start(self.request('POST', self.start_fields(), csrf=False))
        self.assertEqual(response.status_code, 403)
        self.service.start.assert_not_called()
        self.authority.authorize.side_effect = AuthorityFault('UNAUTHENTICATED', 401)
        response = views.portable_start(self.request('POST', self.start_fields()))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(json.loads(response.content), {'ok': False, 'code': 'UNAUTHENTICATED'})
        self.service.start.assert_not_called()
        self.authority.authorize.side_effect = None
        for extra in ('actor_ref', 'body', 'source', 'callback', 'stage', 'confirmed'):
            with self.subTest(extra=extra):
                response = views.portable_start(self.request('POST', {**self.start_fields(), extra: 'untrusted'}))
                self.assertEqual(response.status_code, 422)
                self.service.start.assert_not_called()
        self.service.status.side_effect = AuthorityFault('AUTHORITY_DENIED', 403)
        response = views.portable_status(self.request(), UUID(RUN))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content), {'ok': False, 'code': 'AUTHORITY_DENIED'})
        self.authority.authorize.assert_called_with('isolated-session', sensitive=True)
        self.lookup.assert_called_with(self.authority)

    def test_typed_dispatch_explicit_preview_confirmation_cancel_and_filtered_read(self):
        response = views.portable_start(self.request('POST', self.start_fields()))
        self.assertEqual(response.status_code, 200)
        self.service.start.assert_called_once_with('isolated-session', **self.start_fields())
        views.portable_preview(self.request('POST'), UUID(RUN))
        self.service.preview.assert_called_once_with('isolated-session', run_ref=RUN)
        self.service.confirm.assert_not_called()
        fields = dict(preview_ref=PREVIEW, body_digest='a' * 64, confirmed=True)
        for bad in (False, 'yes', 1):
            with self.subTest(confirmed=bad):
                response = views.portable_confirm(self.request('POST', {**fields, 'confirmed': bad}), UUID(RUN))
                self.assertEqual(response.status_code, 422)
                self.service.confirm.assert_not_called()
        views.portable_confirm(self.request('POST', fields), UUID(RUN))
        self.service.confirm.assert_called_once_with('isolated-session', run_ref=RUN, **fields)
        for bad in (0, -1, True, 1.0, 2**54):
            with self.subTest(revision=bad):
                self.assertEqual(views.portable_cancel(self.request('POST', {'expected_revision': bad}), UUID(RUN)).status_code, 422)
                self.service.cancel.assert_not_called()
        views.portable_cancel(self.request('POST', {'expected_revision': 2}), UUID(RUN))
        self.service.cancel.assert_called_once_with('isolated-session', run_ref=RUN, expected_revision=2)
        response = views.portable_current(self.request(), UUID(PROFILE))
        self.service.current.assert_called_once_with('isolated-session', profile_ref=PROFILE)
        self.assertEqual(json.loads(response.content)['manifest'], None)
        self.assertEqual(response['Cache-Control'], 'no-store')
        # Routing is additive and uses typed opaque references, not an active tab.
        match = resolve('/enhance/runs/' + RUN + '/status', urlconf='cabinet.portable_urls')
        self.assertIs(match.func, views.portable_status)
        self.assertEqual(match.kwargs, {'run_ref': UUID(RUN)})

    def test_native_actions_escape_disclosure_redirect_without_replay_and_close_unavailable(self):
        request = self.request(native=True, query={'run_ref': RUN, 'review': '1'})
        response = views.portable_page(request)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        doc = Document(html)
        self.assertEqual(doc.tagged('img'), [])
        body = next(node for node in doc.tagged('textarea') if node.attrs.get('aria-label') == 'Exact request body')
        self.assertEqual(body.text(), self.disclosure['body'])
        self.assertIn('readonly', body.attrs)
        forms = doc.tagged('form')
        self.assertEqual({form.attrs['action'] for form in forms},
                         {'/enhance/runs/' + RUN + '/' + action for action in ('preview', 'confirm', 'cancel')})
        checkbox = next(node for node in doc.tagged('input') if node.attrs.get('name') == 'confirmed')
        self.assertIn('required', checkbox.attrs)
        self.assertNotIn('checked', checkbox.attrs)
        self.service.confirm.assert_not_called()
        response = views.portable_preview(self.request('POST', native=True), UUID(RUN))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response['Location'], '/enhance?run_ref=' + RUN + '&review=1')
        self.service.confirm.assert_not_called()
        self.service.status.return_value = {**self.status, 'stage': 'unknown', 'state': 'unknown',
                                           'actions': ['status', 'cancel']}
        response = views.portable_page(self.request(native=True, query={'run_ref': RUN}))
        doc = Document(response.content.decode())
        self.assertEqual([form.attrs['action'] for form in doc.tagged('form')], ['/enhance/runs/' + RUN + '/cancel'])
        self.assertIn('must not resend', doc.root.text(visible=True))
        self.service.readiness.return_value = {'available': False, 'run_ready': False,
                                               'code': 'CAPABILITY_UNAVAILABLE', 'actions': []}
        response = views.portable_page(self.request(native=True, query={'profile_ref': PROFILE, 'connection_ref': CONNECTION}))
        self.assertEqual(Document(response.content.decode()).tagged('form'), [])
        response = portable_setup_views.enhance_status(self.request())
        self.assertFalse(json.loads(response.content)['run_ready'])
        self.service.readiness.assert_called_with('isolated-session')

    def cabinet_choices(self):
        return {'profiles': [{'profile_ref': PROFILE, 'label': 'Synthetic cabinet'}],
                'connections': [{'connection_ref': CONNECTION, 'model': 'Synthetic configured model', 'revision': 1}],
                'can_create': False, 'owner_revision': 1}

    def publication(self, source_url='https://source.invalid/work?x=1&y=2'):
        card = {'key': 'candidate-one', 'category': 'book', 'title': 'Published <img src=x onerror=alert(1)>',
            'medium': 'Book', 'provider': 'Synthetic source', 'source_url': source_url,
            'observed_at': '2026-01-01T00:00:00Z', 'valid_until': '2026-01-02T00:00:00Z',
            'facts': [{'property': 'relationship', 'value': '<script>source text only</script>'}],
            'unlisted_private_field': 'DO-NOT-PROJECT-THIS'}
        categories = [{'category': 'book', 'state': 'fresh', 'code': 'OK', 'cards': [card]}]
        for key, state in [('movie', 'empty_bounded'), ('tv', 'unavailable'), ('artist', 'failed'),
                           ('events', 'cancelled'), ('outings', 'retained_previous_stale')]:
            categories.append({'category': key, 'state': state,
                'code': 'CAPABILITY_UNAVAILABLE' if state == 'unavailable' else 'OK', 'cards': []})
        return {'schema': 'cabinet-current/v1', 'published': True, 'publication_revision': 3, 'code': 'OK',
            'manifest': {'schema': 'cabinet-manifest/v1', 'categories': categories,
                'for_you': [{'candidate_key': 'candidate-one', 'text': kind + ' supplied <svg> explanation',
                             'kind': kind, 'evidence_refs': ['synthetic-evidence']}
                            for kind in ('explicit', 'source', 'inferred')]}}

    def cabinet_request(self, **extra):
        return self.request(native=True, query={'profile_ref': PROFILE, 'connection_ref': CONNECTION, **extra})

    def test_filtered_publication_renders_ordered_categories_facts_and_grounded_labels_safely(self):
        self.service.current.return_value = self.publication()
        with patch.object(portable_setup_views, 'cabinet_choices', return_value=self.cabinet_choices()):
            response = views.portable_cabinet(self.cabinet_request())
        self.assertEqual(response.status_code, 200)
        doc = Document(response.content.decode())
        self.assertEqual(doc.tagged('img'), [])
        self.assertEqual(doc.tagged('svg'), [])
        self.assertEqual(doc.tagged('script'), [])
        text = doc.root.text(visible=True)
        for expected in ('Published <img', '<script>source text only</script>', 'Synthetic source',
                         '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z',
                         'Explicit taste', 'Source evidence', 'Inferred connection',
                         'within the collection limits', 'Not available with the current connections or integration.',
                         'not an empty result', 'Collection cancelled', 'Older picks retained'):
            self.assertIn(expected, text)
        self.assertNotIn('DO-NOT-PROJECT-THIS', response.content.decode())
        self.assertNotIn('candidate-one', response.content.decode())
        sources = [node for node in doc.tagged('a') if node.text() == 'View source record']
        self.assertEqual([node.attrs['href'] for node in sources], ['https://source.invalid/work?x=1&y=2'])
        category_nav = next(node for node in doc.tagged('nav') if node.attrs.get('aria-label') == 'Cabinet categories')
        labels = [node.text() for node in category_nav.nodes() if node.tag == 'a']
        self.assertEqual(labels, ['All categories', 'Books', 'Films', 'TV series', 'Music artists', 'Events', 'Outings'])
        self.service.current.assert_called_once_with('isolated-session', profile_ref=PROFILE)
        self.service.start.assert_not_called()
        self.service.status.assert_not_called()

    def test_category_selection_keeps_enhance_profile_wide_and_stage_instructions_actionable(self):
        self.service.current.return_value = self.publication()
        with patch.object(portable_setup_views, 'cabinet_choices', return_value=self.cabinet_choices()):
            response = views.portable_cabinet(self.cabinet_request(category='movie'))
        doc = Document(response.content.decode())
        self.assertNotIn('Published <img', doc.root.text(visible=True))
        enhance_link = next(node for node in doc.tagged('a') if node.text() == 'Enhance this cabinet')
        self.assertEqual(enhance_link.attrs['href'], '/enhance?profile_ref=' + PROFILE + '&connection_ref=' + CONNECTION)
        self.assertNotIn('category', enhance_link.attrs['href'])
        response = views.portable_page(self.request(native=True, query={'profile_ref': PROFILE, 'connection_ref': CONNECTION}))
        doc = Document(response.content.decode())
        self.assertIn('Choose “Start a round and review API requests”', doc.root.text(visible=True))
        self.assertNotIn('Wait for collection status', doc.root.text(visible=True))
        fields = {n.attrs.get('name'): n.attrs.get('value') for n in doc.tagged('input')}
        self.assertEqual(fields['profile_ref'], PROFILE)
        self.assertNotIn('category', fields)
        value = {**self.status, 'stage': 'ready', 'state': 'ready',
                 'disclosure': {**self.disclosure, 'kind': 'inference'}}
        response = views._portable_html(self.request(native=True), value)
        doc = Document(response.content.decode())
        self.assertIn('explicitly approve it if you want this potentially paid attempt', doc.root.text(visible=True))
        self.assertNotIn('This page cannot start an inference send', doc.root.text(visible=True))
        self.assertFalse(any('checked' in node.attrs for node in doc.tagged('input')))
        self.service.confirm.assert_not_called()

    def test_unsafe_links_pending_and_refused_publications_never_restore_or_cache_old_cards(self):
        with patch.object(portable_setup_views, 'cabinet_choices', return_value=self.cabinet_choices()):
            for link in ('javascript:alert(1)', 'http://source.invalid', '//source.invalid',
                         'https://user:secret@source.invalid/', 'https://source.invalid/\nunsafe'):
                with self.subTest(link=link):
                    self.service.current.return_value = self.publication(link)
                    response = views.portable_cabinet(self.cabinet_request())
                    doc = Document(response.content.decode())
                    self.assertFalse(any(node.text() == 'View source record' for node in doc.tagged('a')))
                    self.assertIn('No validated source link', doc.root.text(visible=True))
            self.service.current.return_value = self.publication()
            self.service.status.return_value = {**self.status, 'stage': 'failed', 'state': 'failed'}
            with patch.object(views, '_run_selection', return_value={'profile_ref': PROFILE, 'connection_ref': CONNECTION}):
                request = self.cabinet_request(run_ref=RUN)
                response = views.portable_cabinet(request)
            self.assertIn('Published &lt;img', response.content.decode())
            self.assertIn('does not replace the publication', response.content.decode())
            self.assertEqual(set(request.session), {'portable_session'})
            self.service.status.return_value = {**self.status, 'stage': 'unknown', 'state': 'unknown'}
            with patch.object(views, '_run_selection', return_value={'profile_ref': PROFILE, 'connection_ref': CONNECTION}):
                response = views.portable_cabinet(self.cabinet_request(run_ref=RUN))
            doc = Document(response.content.decode())
            self.assertFalse(any(node.text() == 'Enhance this cabinet' for node in doc.tagged('a')))
            nav = next(node for node in doc.tagged('nav') if node.attrs.get('aria-label') == 'Cabinet categories')
            self.assertTrue(all('run_ref=' + RUN in node.attrs['href'] for node in nav.nodes() if node.tag == 'a'))
            self.service.current.side_effect = AuthorityFault('PREVIEW_STALE', 409)
            request = self.cabinet_request()
            response = views.portable_cabinet(request)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('Published &lt;img', response.content.decode())
            self.assertIn('No older copy has been restored', response.content.decode())
            self.assertEqual(set(request.session), {'portable_session'})
            self.service.start.assert_not_called()
            self.service.confirm.assert_not_called()

    def test_workflow_feedback_binding_is_fresh_read_only_and_refusal_does_not_revive_cards(self):
        self.service.current.return_value = self.publication()
        with patch.object(portable_setup_views, 'cabinet_choices', return_value=self.cabinet_choices()):
            response = views.portable_cabinet(self.cabinet_request())
            self.assertEqual(response.status_code, 200)
            self.feedback_read.assert_called_once()
            args, kwargs = self.feedback_read.call_args
            self.assertEqual(args, (self.authority, 'isolated-session', PROFILE))
            self.assertEqual(set(kwargs), {'target_ref'})
            self.feedback_read.side_effect = AuthorityFault('PREVIEW_STALE', 409)
            response = views.portable_cabinet(self.cabinet_request())
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('Published &lt;img', response.content.decode())
        self.assertNotIn('name="target_ref"', response.content.decode())
        self.service.start.assert_not_called();self.service.confirm.assert_not_called()

    def test_workflow_html_progress_labels_all_domains_without_changing_source_receipt(self):
        from copy import deepcopy
        from shelf.category_registry import CATEGORIES
        value = {**self.status, 'category_outcomes': [
            {'category': r.key, 'state': 'unavailable', 'candidate_count': 0,
             'code': 'CAPABILITY_UNAVAILABLE'} for r in CATEGORIES]}
        before = deepcopy(value)
        response = views._portable_html(self.request(native=True), value,
            selection={'profile_ref': PROFILE, 'connection_ref': CONNECTION})
        doc = Document(response.content.decode())
        outcomes = doc.marked('data-enhance-outcomes')
        self.assertEqual([n.text() for n in outcomes.nodes() if n.tag == 'strong'],
                         [r.label for r in CATEGORIES])
        self.assertEqual(value, before)
        self.assertTrue(any(n.attrs.get('href') == '/cabinet/taste?profile_ref=' + PROFILE
                            for n in doc.tagged('a')))
        self.service.start.assert_not_called();self.service.confirm.assert_not_called()
