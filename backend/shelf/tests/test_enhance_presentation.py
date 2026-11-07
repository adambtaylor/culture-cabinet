"""Real template regressions for the dormant, server-owned Enhance projection.

No view/service fixture grants authority. These tests exercise initial HTML and
escaping; client focus, timing and actual publication need separate runtime and
rendered acceptance. They do not substitute collected candidates for new picks.
"""
from html.parser import HTMLParser
import json
from pathlib import Path

from django.template import Context, Engine
from django.test import SimpleTestCase

from shelf.enhance_contract import CATEGORY_STATES, STAGES, TERMINALS, StageEvent


ROOT = Path(__file__).resolve().parents[3]


class Element:
    def __init__(self, tag='', attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.nodes()

    def text(self, visible=False):
        if visible and ('hidden' in self.attrs or self.tag == 'script'):
            return ''
        return ''.join(child.text(visible) if isinstance(child, Element) else child
                       for child in self.children)


class Document(HTMLParser):
    VOID = {'input', 'br', 'hr', 'meta', 'link', 'img', 'area', 'base', 'col',
            'embed', 'param', 'source', 'track', 'wbr'}

    def __init__(self, html):
        super().__init__()
        self.root = Element()
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Element(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, text):
        self.stack[-1].children.append(text)

    def tagged(self, tag):
        return [node for node in self.root.nodes() if node.tag == tag]

    def marked(self, marker):
        return next(node for node in self.root.nodes() if marker in node.attrs)


class EnhancePresentationTests(SimpleTestCase):
    def render(self, enhancement, *, page=False, **changes):
        # Isolate page content from the unrelated authenticated navigation. The
        # real leased status/page templates and Django autoescaping still run.
        engine = Engine(dirs=[ROOT / 'templates'], loaders=[
            ('django.template.loaders.locmem.Loader', {
                'provider_base.html': '{% block content %}{% endblock %}'}),
            'django.template.loaders.filesystem.Loader'])
        context = {'enhancement': enhancement, 'csrf_token': 's' * 64,
                   'enhance_url': '/existing-enhance', 'enhance_digest': 'digest',
                   'enhance_token': 'preview', 'active_queue': 'movie',
                   'catalogue_url': '/existing-picks', **changes}
        template = 'cabinet/enhancement_page.html' if page else 'cabinet/enhancement_status.html'
        html = engine.get_template(template).render(Context(context, use_l10n=False))
        return html, Document(html)

    def stage(self, stage, **changes):
        event = StageEvent('synthetic-round', stage,
                           publication_revision=7 if stage == 'published' else None).public()
        return {**event, **changes}

    def test_legacy_forms_keep_csrf_bindings_and_supervised_fallback(self):
        receipt = {'state': 'completed', 'application_state': 'pending',
                   'apply_url': '/existing-apply', 'cancel_url': '/existing-cancel',
                   'status_url': '/existing-status'}
        html, doc = self.render(receipt, page=True)
        self.assertEqual([form.attrs['action'] for form in doc.tagged('form')],
                         ['/existing-enhance', '/existing-apply', '/existing-cancel'])
        for form in doc.tagged('form'):
            inputs = {n.attrs.get('name'): n.attrs.get('value')
                      for n in form.nodes() if n.tag == 'input'}
            self.assertEqual(inputs['csrfmiddlewaretoken'], 's' * 64)
            self.assertEqual(form.attrs['method'], 'post')
        self.assertIn('hidden', doc.marked('data-enhance-progress').attrs)
        self.assertNotIn('enhance-stage-receipt', [n.attrs.get('id') for n in doc.tagged('script')])
        self.assertIn('manual, supervised Astra high model handoff', doc.root.text(visible=True))
        self.assertIn('Queued or awaiting an authorized worker', html)

    def test_every_frozen_stage_has_one_native_visible_status_and_api_first_order(self):
        for stage in STAGES + TERMINALS:
            with self.subTest(stage=stage):
                _, doc = self.render(self.stage(stage))
                statuses = [n for n in doc.root.nodes() if 'data-enhance-stage' in n.attrs]
                visible = [n for n in statuses if 'hidden' not in n.attrs]
                self.assertEqual([n.attrs['data-enhance-stage'] for n in visible], [stage])
                self.assertTrue(visible[0].text().strip())
                self.assertEqual(doc.marked('data-enhance-stages').attrs['aria-live'], 'polite')
                self.assertEqual(doc.marked('data-enhance-stages').attrs['aria-atomic'], 'true')
                order = next(n for n in doc.tagged('ol') if n.attrs.get('aria-label') == 'Enhance order')
                self.assertEqual([n.text() for n in order.children if isinstance(n, Element)],
                                 ['Collect and enrich API candidates', 'Review and confirm inference',
                                  'Validate and publish picks'])
                self.assertIn('whichever tab', doc.root.text(visible=True))

    def test_all_category_outcomes_distinguish_partial_failure_and_retained_picks(self):
        rows = [{'category': key, 'state': state, 'candidate_count': 2 if state == 'fresh' else 0,
                 'code': 'CAPABILITY_UNAVAILABLE' if state == 'unavailable' else 'OK'}
                for key, state in zip(('book', 'movie', 'tv', 'artist', 'events', 'outings'), CATEGORY_STATES)]
        _, doc = self.render(self.stage('sealed', category_outcomes=rows))
        outcomes = doc.marked('data-enhance-outcomes')
        items = [n for n in outcomes.children if isinstance(n, Element)]
        self.assertEqual(len(items), 6)
        self.assertEqual([next(n for n in item.nodes() if n.tag == 'strong').text() for item in items],
                         [row['category'] for row in rows])
        for phrase in ('2 sourced candidates', 'within this round’s collection limits',
                       'Not available with the current connections or integration', 'not an empty result',
                       'Collection cancelled', 'Older picks retained; not refreshed'):
            self.assertIn(phrase, outcomes.text())
        self.assertIn('hidden', doc.marked('data-enhance-coverage-pending').attrs)
        self.assertIn('your current picks have not changed', doc.root.text(visible=True))

    def test_progress_receipts_never_create_commands_even_with_extra_url_fields(self):
        for stage in ('collecting', 'awaiting_preview', 'ready', 'unknown', 'failed', 'published'):
            with self.subTest(stage=stage):
                _, doc = self.render(self.stage(stage, status_url='/not-a-capability',
                    apply_url='/not-an-apply-capability', cancel_url='/not-a-cancel-capability',
                    application_state='pending', run_ready=True))
                self.assertEqual(doc.tagged('form'), [])
                for marker in ('check', 'apply', 'cancel', 'status-link'):
                    self.assertIn('hidden', doc.marked('data-enhance-' + marker).attrs)
                self.assertTrue(all(button.attrs.get('type') == 'button' for button in doc.tagged('button')))
                self.assertIn('cannot start API calls or inference', doc.root.text(visible=True))
                if stage == 'unknown':
                    self.assertIn('must not resend', doc.root.text(visible=True))
                if stage == 'awaiting_preview':
                    self.assertIn('Review the actual inference body', doc.root.text(visible=True))
                    self.assertIn('Nothing is sent automatically', doc.root.text(visible=True))

    def test_untrusted_category_and_json_cannot_escape_into_active_markup(self):
        hostile = '</script><img src=x onerror=alert(1)>'
        receipt = self.stage('sealed', category_outcomes=[{
            'category': hostile, 'state': 'unavailable', 'candidate_count': 0, 'code': 'SOURCE_UNAVAILABLE'}])
        html, doc = self.render(receipt)
        self.assertEqual(doc.tagged('img'), [])
        scripts = doc.tagged('script')
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0].attrs['type'], 'application/json')
        self.assertEqual(json.loads(scripts[0].text()), receipt)
        self.assertNotIn(hostile, html)
        self.assertIn(hostile, doc.marked('data-enhance-outcomes').text())
        self.assertIn('Source unavailable; no fresh result', doc.root.text(visible=True))

    def test_only_published_revision_is_confirmation_not_pending_or_no_candidates(self):
        for stage in ('collecting', 'validated', 'staged', 'no_candidates', 'published'):
            with self.subTest(stage=stage):
                _, doc = self.render(self.stage(stage), page=True)
                visible = doc.root.text(visible=True)
                self.assertEqual('Publication revision: 7' in visible, stage == 'published')
                self.assertEqual('Publication confirmed' in visible, stage == 'published')
                self.assertIn('Return to your picks', visible)
                self.assertIn('API collection and enrichment come before reviewed inference', visible)
                if stage != 'published':
                    self.assertNotIn('hidden', doc.marked('data-enhance-coverage-pending').attrs)
                    self.assertIn('Missing results do not mean an empty category', visible)
                if stage == 'no_candidates':
                    self.assertIn('Keep your current picks', visible)

    def test_native_recovery_and_category_causes_agree_with_separate_live_summary(self):
        codes = ('UNAUTHENTICATED', 'AUTHORITY_DENIED', 'CREDENTIAL_REQUIRED',
                 'CAPABILITY_UNAVAILABLE', 'SOURCE_UNAVAILABLE', 'RATE_LIMITED',
                 'UNKNOWN_RECONCILIATION_REQUIRED', 'PREVIEW_STALE', 'VALIDATION_STALE',
                 'RUN_STALE', 'REVISION_CONFLICT', 'LIMIT_EXCEEDED',
                 'NO_GROUNDED_CANDIDATES', 'IDEMPOTENCY_CONFLICT', 'CONTRACT_INVALID')
        for code in codes:
            with self.subTest(code=code):
                _, doc = self.render(self.stage('blocked', code=code, category_outcomes=[{
                    'category': 'events', 'state': 'unavailable', 'candidate_count': 0, 'code': code}]))
                reason = doc.marked('data-enhance-reason')
                cause = doc.marked('data-enhance-category-cause')
                normalize = lambda node: ' '.join(node.text(visible=True).split())
                self.assertEqual(normalize(reason), normalize(cause))
                self.assertTrue(normalize(reason))
                self.assertNotIn(code, doc.root.text(visible=True))
                self.assertEqual(doc.tagged('form'), [])
                summary = doc.marked('data-enhance-update')
                self.assertEqual(summary.attrs['role'], 'status')
                self.assertEqual(summary.attrs['aria-live'], 'polite')
                self.assertEqual(summary.attrs['aria-atomic'], 'true')
                self.assertEqual(summary.text(), '')
                # The list stays browsable, outside the concise summary and stage
                # announcement. Actual same-stage speech/focus needs a browser.
                self.assertNotIn(doc.marked('data-enhance-outcomes'), list(summary.nodes()))
                self.assertNotIn(doc.marked('data-enhance-outcomes'),
                                 list(doc.marked('data-enhance-stages').nodes()))

    def cabinet(self, **changes):
        engine = Engine(dirs=[ROOT / 'templates'], loaders=[
            ('django.template.loaders.locmem.Loader', {
                'test-parent': '<main id="main">{% block content %}{% endblock %}</main>'}),
            'django.template.loaders.filesystem.Loader'])
        context = {'enhancement_base': 'test-parent', 'profile_label': 'Fictional cabinet',
            'enhance_url': '/enhance?profile_ref=fictional-profile&connection_ref=fictional-connection',
            'all_url': '/cabinet?profile_ref=fictional-profile', 'published': True,
            'publication_revision': 4, 'categories': [{'category': 'book', 'label': 'Books',
                'state': 'fresh', 'status': 'Published picks', 'visible': True,
                'url': '/cabinet?profile_ref=fictional-profile&category=book', 'cards': [{
                    'title': 'Fictional <img> book', 'medium': 'Book', 'provider': 'Fictional source',
                    'observed_at': '2026-01-01T00:00:00Z', 'valid_until': '2026-01-02T00:00:00Z',
                    'source_url': 'https://source.invalid/work', 'facts': [], 'reasons': []}]}],
            **changes}
        html = engine.get_template('cabinet/portable_cabinet.html').render(Context(context, use_l10n=False))
        return html, Document(html)

    def test_one_primary_enhance_action_keeps_profile_scope_and_discloses_provenance(self):
        html, doc = self.cabinet(active_category='book')
        actions = [n for n in doc.tagged('a') if 'cabinet-action' in n.attrs.get('class', '').split()]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].text(), 'Enhance this cabinet')
        self.assertEqual(actions[0].attrs['href'],
            '/enhance?profile_ref=fictional-profile&connection_ref=fictional-connection')
        self.assertNotIn('category=', actions[0].attrs['href'])
        self.assertIn('Every enabled category participates', doc.root.text(visible=True))
        self.assertEqual(doc.tagged('img'), [])
        self.assertIn('Fictional &lt;img&gt; book', html)
        source = next(n for n in doc.tagged('details') if n.attrs.get('class') == 'cabinet-source')
        self.assertNotIn('open', source.attrs)
        self.assertIn('Fictional source', source.text())
        self.assertEqual(doc.tagged('h1')[0].attrs['id'], 'cabinet-title')

    def test_unknown_has_one_recorded_action_and_retains_only_supplied_current_cards(self):
        _, doc = self.cabinet(round_blocks_start=True, round_url='/enhance?run_ref=existing', round_pending=True)
        actions = [n for n in doc.tagged('a') if 'cabinet-action' in n.attrs.get('class', '').split()]
        self.assertEqual([n.attrs['href'] for n in actions], ['/enhance?run_ref=existing'])
        self.assertNotIn('Enhance this cabinet', doc.root.text(visible=True))
        self.assertIn('must not be replaced by a new round', doc.root.text(visible=True))
        self.assertIn('Fictional <img> book', doc.root.text(visible=True))
        _, denied = self.cabinet(published=False, categories=[], read_code='PREVIEW_STALE')
        self.assertNotIn('Fictional <img> book', denied.root.text(visible=True))
        self.assertIn('No older copy has been restored', denied.root.text(visible=True))

    def test_current_only_explanations_do_not_reconstruct_suppressed_personal_reasons(self):
        _, doc = self.cabinet()
        self.assertIn('No current For You explanation is available', doc.root.text(visible=True))
        self.assertEqual(doc.tagged('h4'), [])
        categories = [{'category': 'movie', 'label': 'Films', 'visible': True,
            'url': '/cabinet?category=movie', 'status': 'Published picks', 'cards': [{
                'title': 'Fictional film', 'facts': [], 'reasons': [{'label': 'Source evidence',
                    'text': 'Current <script> sourced relationship', 'evidence_count': 1}]}]}]
        html, current = self.cabinet(categories=categories)
        self.assertIn('Source evidence', current.root.text(visible=True))
        self.assertIn('Current &lt;script&gt; sourced relationship', html)
        self.assertEqual(current.tagged('script'), [])
        self.assertNotIn('No current For You explanation', current.root.text(visible=True))

    def test_native_next_action_preserves_csrf_exact_confirmation_and_stable_stage_target(self):
        disclosure = {'kind': 'inference', 'preview_ref': 'fictional-preview', 'body_digest': 'a' * 64,
            'body': '{"input":"fictional <script>"}', 'destinations': ['https://processor.invalid'],
            'expires_at': '2026-01-01T00:05:00Z'}
        _, doc = self.render(self.stage('ready', revision=2), portable_ui=True,
            portable_commands={'confirm': '/authorized-confirm', 'status': '/authorized-status'},
            disclosure=disclosure)
        forms = doc.tagged('form')
        self.assertEqual([n.attrs['action'] for n in forms], ['/authorized-confirm'])
        fields = {n.attrs.get('name'): n.attrs.get('value') for n in forms[0].nodes() if n.tag == 'input'}
        self.assertEqual(fields['csrfmiddlewaretoken'], 's' * 64)
        self.assertEqual(fields['preview_ref'], 'fictional-preview')
        self.assertEqual(fields['body_digest'], 'a' * 64)
        checkbox = next(n for n in doc.tagged('input') if n.attrs.get('name') == 'confirmed')
        self.assertIn('required', checkbox.attrs)
        self.assertNotIn('checked', checkbox.attrs)
        stage = doc.marked('data-enhance-stages')
        self.assertEqual(stage.attrs['id'], 'enhance-current-stage')
        self.assertEqual(stage.attrs['tabindex'], '-1')
        self.assertTrue(any(n.attrs.get('href') == '#enhance-current-stage' for n in doc.tagged('a')))
        self.assertEqual(doc.tagged('script')[0].attrs['type'], 'application/json')
