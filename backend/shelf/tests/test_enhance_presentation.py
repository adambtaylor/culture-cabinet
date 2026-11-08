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

    def test_collection_without_model_permission_shows_counts_without_publication_or_send(self):
        summary = [{'category': 'movie', 'collected_count': 4, 'model_eligible_count': 0},
                   {'category': 'book', 'collected_count': 0, 'model_eligible_count': 0}]
        _, doc = self.render(self.stage('sealed', code='AUTHORITY_DENIED',
            dependency='source_model_context_unavailable', source_use=summary), portable_ui=True)
        text = doc.root.text(visible=True)
        self.assertIn('Source facts were collected, but model use is not yet cleared', text)
        self.assertIn('No inference was sent', text)
        counts = doc.marked('data-enhance-source-use')
        self.assertIn('Films', counts.text())
        self.assertIn('4 collected; 0 eligible for model use', counts.text())
        self.assertIn('Books', counts.text())
        self.assertNotIn('Publication confirmed', text)
        self.assertNotIn('TMDB', counts.text())
        self.assertEqual(doc.tagged('form'), [])
        self.assertIn('Source policy setup must be completed', doc.marked('data-enhance-reason').text())
        self.assertIn('hidden', doc.marked('data-enhance-publication').attrs)

    def test_mixed_collection_preserves_counts_and_unavailable_is_not_empty(self):
        _, doc = self.render(self.stage('sealed', source_use=[
            {'category': 'movie', 'collected_count': 3, 'model_eligible_count': 2},
            {'category': 'book', 'collected_count': 5, 'model_eligible_count': 0},
            {'category': 'artist', 'collected_count': 0, 'model_eligible_count': 0}],
            category_outcomes=[{'category': 'movie', 'state': 'fresh', 'candidate_count': 3, 'code': 'OK'},
                {'category': 'book', 'state': 'unavailable', 'candidate_count': 0, 'code': 'AUTHORITY_DENIED'},
                {'category': 'artist', 'state': 'empty_bounded', 'candidate_count': 0, 'code': 'OK'}]))
        counts = doc.marked('data-enhance-source-use').text()
        self.assertIn('3 collected; 2 eligible for model use', counts)
        self.assertIn('5 collected; 0 eligible for model use', counts)
        outcomes = doc.marked('data-enhance-outcomes').text()
        self.assertIn('Unavailable under current permissions; this is not an empty search', outcomes)
        self.assertIn('No candidates within this round’s collection limits', outcomes)

    def test_mixed_zero_collection_denial_does_not_infer_model_permission_cause(self):
        # Another category's collected pool cannot explain this category's
        # denial: missing local retention or consent can also produce 0/0.
        _, doc = self.render(self.stage('sealed', source_use=[
            {'category': 'movie', 'collected_count': 3, 'model_eligible_count': 2},
            {'category': 'book', 'collected_count': 0, 'model_eligible_count': 0}],
            category_outcomes=[{'category': 'movie', 'state': 'fresh', 'candidate_count': 3, 'code': 'OK'},
                {'category': 'book', 'state': 'unavailable', 'candidate_count': 0, 'code': 'AUTHORITY_DENIED'}]))
        items = [n for n in doc.marked('data-enhance-outcomes').nodes() if n.tag == 'li']
        denied = next(n for n in items if any(child.tag == 'strong' and child.text() == 'book'
                                            for child in n.nodes()))
        self.assertIn('Unavailable under current permissions', denied.text())
        self.assertIn('not an empty search', denied.text())
        self.assertNotIn('Model use', denied.text())
        self.assertNotIn('collected', denied.text())
        self.assertNotIn('No candidates', denied.text())

    def test_source_summary_absence_preserves_legacy_and_summary_text_is_escaped(self):
        _, legacy = self.render({'state': 'queued', 'status_url': '/existing-status'})
        self.assertFalse(any('data-enhance-source-use' in node.attrs for node in legacy.root.nodes()))
        html, doc = self.render(self.stage('sealed', source_use=[{
            'category': '<img src=x onerror=alert(1)>', 'collected_count': 1, 'model_eligible_count': 0}]))
        self.assertEqual(doc.tagged('img'), [])
        self.assertIn('&lt;img', html)

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

    def test_musicbrainz_configuration_and_consent_are_separate_explicit_native_forms(self):
        # Scalar presentation fixtures are not source authority or HTTP proof.
        context = dict(source_setup=True, profile_ref='fictional-profile',
            source_revision=0, csrf_token='s' * 64, source_consent_revision=3,
            source_connection_ref='fictional-source', source_consent_action='grant',
            source_choices=[{'code': code, 'label': code.replace('_', ' ')} for code in
                ('canonical_seed', 'explicit_favourites', 'anticipated_reactions', 'experienced_enjoyment')])
        _, unconfigured = self.cabinet(**context)
        configure = unconfigured.tagged('form')
        self.assertEqual([f.attrs['action'] for f in configure], ['/cabinet/source/musicbrainz/configure'])
        config_fields = {n.attrs.get('name'): n.attrs.get('value') for n in configure[0].nodes() if n.tag == 'input'}
        self.assertEqual(config_fields['expected_revision'], '0')
        _, doc = self.cabinet(**context, source_configured=True)
        forms = doc.tagged('form')
        self.assertEqual([f.attrs['action'] for f in forms], ['/cabinet/source/musicbrainz/consent'])
        for form in configure + forms:
            self.assertEqual(form.attrs['method'], 'post')
            fields = {n.attrs.get('name'): n.attrs.get('value') for n in form.nodes() if n.tag == 'input'}
            self.assertEqual(fields['csrfmiddlewaretoken'], 's' * 64)
            self.assertEqual(fields['profile_ref'], 'fictional-profile')
            confirmation = next(n for n in form.nodes() if n.attrs.get('name') == 'confirmed')
            self.assertIn('required', confirmation.attrs)
            self.assertFalse(any('checked' in n.attrs for n in form.nodes()))
            self.assertFalse({'adapter', 'origin', 'purpose', 'secret', 'allow_notes'} & fields.keys())
        permission = {n.attrs.get('name'): n.attrs.get('value') for n in forms[0].nodes() if n.tag == 'input'}
        self.assertEqual(permission['source_connection_ref'], 'fictional-source')
        self.assertEqual(permission['expected_revision'], '3')
        self.assertEqual(permission['action'], 'grant')
        self.assertEqual(sorted(k for k in permission if k.startswith('allow_')),
            ['allow_anticipated_reactions', 'allow_canonical_seed', 'allow_experienced_enjoyment', 'allow_explicit_favourites'])
        text = doc.root.text(visible=True)
        for phrase in ('separate from inference permission', 'does not send a request or approve model use',
                       'name match does not verify an exact artist identity', 'supports recordings'):
            self.assertIn(phrase, text)
        self.assertEqual(doc.tagged('script'), [])

    def test_musicbrainz_revoke_expired_and_regrant_states_never_imply_active_permission(self):
        for active in (True, False):
            with self.subTest(active=active):
                _, doc = self.cabinet(source_setup=True, source_configured=True,
                    source_consent_record_active=True, source_consent_active=active,
                    source_consent_revision=4, csrf_token='s' * 64,
                    source_consent_expires='2026-01-01T00:05:00Z')
                forms = doc.tagged('form')
                self.assertEqual(len(forms), 1)
                self.assertEqual([n.attrs['value'] for n in forms[0].nodes()
                                  if n.attrs.get('name') == 'action'], ['revoke'])
                self.assertIn('Revoke source permission', forms[0].text())
                self.assertNotIn('Grant source permission', doc.root.text(visible=True))
                self.assertEqual(doc.tagged('fieldset'), [])
                if active:
                    self.assertIn('active until 2026-01-01T00:05:00Z', doc.root.text())
                else:
                    self.assertIn('expired or no longer matches', doc.root.text())
        _, regrant = self.cabinet(source_setup=True, source_configured=True,
            source_consent_record_active=False, source_consent_action='regrant')
        self.assertEqual([n.attrs['value'] for n in regrant.tagged('input')
                          if n.attrs.get('name') == 'action'], ['regrant'])
        self.assertIn('No current source permission is active', regrant.root.text())

    def test_artist_forms_have_no_inferred_response_and_keep_native_replay_bindings(self):
        html, doc = self.cabinet(artist_input=True, profile_ref='fictional-profile',
            entry_ref='fictional-entry', newmutation_id='fictional-mutation', base_revision=0,
            csrf_token='s' * 64, title='<script>untrusted</script>')
        forms = doc.tagged('form')
        self.assertEqual(len(forms), 2)
        for form, signal, values in zip(forms, ('interest', 'enjoyment'),
                                       (['', 'no', 'maybe', 'yes'], ['', '1', '2', '3', '4', '5'])):
            self.assertEqual(form.attrs['action'], '/cabinet/taste/artist')
            self.assertEqual(form.attrs['method'], 'post')
            fields = {n.attrs.get('name'): n.attrs for n in form.nodes() if n.tag == 'input'}
            for name, value in (('csrfmiddlewaretoken', 's' * 64), ('profile_ref', 'fictional-profile'),
                ('entry_ref', 'fictional-entry'), ('mutation_id', 'fictional-mutation'),
                ('base_revision', '0'), ('operation', 'set'), ('signal', signal)):
                self.assertEqual(fields[name]['value'], value)
            self.assertEqual(fields['title']['maxlength'], '200')
            self.assertIn('required', fields['title'])
            self.assertNotIn('value', fields['title'])
            select = next(n for n in form.nodes() if n.tag == 'select')
            self.assertEqual(select.attrs['name'], 'value')
            self.assertIn('required', select.attrs)
            self.assertEqual([n.attrs['value'] for n in select.nodes() if n.tag == 'option'], values)
            if signal == 'enjoyment':
                self.assertIn('required', fields['experienced_ack'])
                self.assertEqual(fields['experienced_ack']['value'], 'yes')
            else:
                self.assertNotIn('experienced_ack', fields)
        self.assertFalse(any('checked' in n.attrs or 'selected' in n.attrs for n in doc.root.nodes()))
        self.assertIn('not a verified MusicBrainz identity or imported history', doc.root.text())
        self.assertIn('does not run Enhance or share it', doc.root.text())
        self.assertNotIn('<script>', html)

    def test_source_groups_and_artist_labels_escape_and_navigation_keeps_profile_scope(self):
        html, doc = self.cabinet(source_setup=True, source_configured=True,
            profile_label='<script>label</script>', source_choices=[{'code': 'canonical_seed',
                'label': '<img src=x>'}])
        self.assertIn('&lt;script&gt;label&lt;/script&gt;', html)
        self.assertIn('&lt;img src=x&gt;', html)
        self.assertEqual(doc.tagged('script') + doc.tagged('img'), [])
        _, cabinet = self.cabinet(profile_ref='fictional-profile')
        links = [n.attrs['href'] for n in cabinet.tagged('a')]
        self.assertIn('/cabinet/source/musicbrainz?profile_ref=fictional-profile', links)
        self.assertIn('/cabinet/taste/artist?profile_ref=fictional-profile', links)

    def test_recording_collection_keeps_other_category_unavailability_visible(self):
        _, doc = self.render(self.stage('sealed', source_use=[
            {'category': 'recording', 'collected_count': 3, 'model_eligible_count': 3}],
            category_outcomes=[{'category': 'recording', 'state': 'fresh', 'candidate_count': 3, 'code': 'OK'},
                {'category': 'movie', 'state': 'unavailable', 'candidate_count': 0, 'code': 'CAPABILITY_UNAVAILABLE'},
                {'category': 'book', 'state': 'unavailable', 'candidate_count': 0, 'code': 'CAPABILITY_UNAVAILABLE'}]))
        summary = doc.marked('data-enhance-source-use').text()
        self.assertIn('Recordings', summary)
        self.assertIn('3 collected; 3 eligible for model use', summary)
        outcomes = doc.marked('data-enhance-outcomes')
        self.assertEqual([n.text() for n in outcomes.nodes() if n.tag == 'strong'],
                         ['recording', 'movie', 'book'])
        self.assertEqual(outcomes.text().count('Not available with the current connections or integration.'), 2)
        self.assertIn('your current picks have not changed', doc.root.text(visible=True))
        self.assertEqual(doc.tagged('form'), [])

    def test_artist_undo_binds_only_effective_event_and_does_not_replay_response(self):
        entries = [{'entry_ref': 'fictional-entry', 'title': 'Fictional <img> artist', 'signals': [
            {'signal': 'interest', 'revision': 3, 'value': 'maybe', 'event_ref': 'fictional-event',
             'can_undo': True, 'undo_mutation_id': 'fictional-undo'},
            {'signal': 'enjoyment', 'revision': 4, 'value': None, 'event_ref': '',
             'can_undo': False, 'undo_mutation_id': 'not-current'}]}]
        html, doc = self.cabinet(artist_input=True, profile_ref='fictional-profile',
            artist_entries=entries, csrf_token='s' * 64)
        forms = [f for f in doc.tagged('form') if 'data-artist-undo' in f.attrs]
        self.assertEqual(len(forms), 1)
        form = forms[0]
        fields = {n.attrs.get('name'): n.attrs.get('value') for n in form.nodes() if n.tag == 'input'}
        self.assertEqual(fields, {'csrfmiddlewaretoken': 's' * 64,
            'profile_ref': 'fictional-profile', 'entry_ref': 'fictional-entry',
            'mutation_id': 'fictional-undo', 'base_revision': '3', 'operation': 'reverse',
            'signal': 'interest', 'reverses_id': 'fictional-event'})
        self.assertEqual(form.attrs['action'], '/cabinet/taste/artist')
        button = next(n for n in form.nodes() if n.tag == 'button')
        self.assertEqual(button.attrs['aria-label'], 'Undo interest for Fictional <img> artist')
        self.assertIn('Interest: maybe', doc.root.text())
        self.assertIn('Enjoyment: No current response', doc.root.text())
        self.assertIn('Fictional &lt;img&gt; artist', html)
        self.assertEqual(doc.tagged('img'), [])
        self.assertNotIn('not-current', html)

    def taste_groups(self, *, note='  A quiet preference.\nKeep the pause — café.  '):
        def signal(name, value=None, revision=0, can_undo=False):
            return {'signal': name, 'value': value, 'revision': revision,
                'mutation_id': 'new-' + name, 'event_ref': 'event-' + name,
                'undo_mutation_id': 'undo-' + name, 'can_undo': can_undo}
        return [
            {'entry_ref': 'new-item', 'entry_kind': 'item', 'is_new': True, 'title': '',
             'signals': [signal('interest'), signal('enjoyment'), signal('note')]},
            {'entry_ref': 'category-note', 'entry_kind': 'category_note', 'is_new': True,
             'title': 'Category context', 'signals': [signal('note')]},
            {'entry_ref': 'saved-item', 'entry_kind': 'item', 'is_new': False,
             'title': 'Fictional <img> work', 'signals': [signal('note', note, 3, True)]}]

    def test_workflow_preferences_keep_all_registry_domains_independent_of_publication(self):
        from shelf.category_registry import CATEGORIES
        _, doc = self.cabinet(taste_input=True, published=False,
            taste_categories=[{'key': row.key, 'label': row.label} for row in CATEGORIES],
            taste_category='dining', profile_ref='fictional-profile', taste_groups=[])
        form = doc.marked('data-taste-category')
        self.assertEqual(form.attrs['method'], 'get')
        self.assertEqual(form.attrs['action'], '/cabinet/taste')
        self.assertEqual([n.attrs['value'] for n in form.nodes() if n.tag == 'option'],
                         [row.key for row in CATEGORIES])
        self.assertEqual(len(doc.tagged('option')), 13)
        selected = [n.attrs['value'] for n in doc.tagged('option') if 'selected' in n.attrs]
        self.assertEqual(selected, ['dining'])
        self.assertEqual({n.attrs.get('name') for n in form.nodes() if n.tag in ('input', 'select')},
                         {'profile_ref', 'category'})
        self.assertIn('unavailable categories separately from an empty search', doc.root.text())
        self.assertNotIn('No published picks yet', doc.root.text())

    def test_workflow_intake_posts_one_signal_and_no_browser_authority_or_implicit_rating(self):
        _, doc = self.cabinet(taste_input=True, taste_category='book',
            taste_groups=self.taste_groups()[:2], csrf_token='s' * 64)
        forms = [n for n in doc.tagged('form') if 'data-taste-set' in n.attrs]
        self.assertEqual(len(forms), 4)
        for form in forms:
            fields = {n.attrs.get('name'): n for n in form.nodes() if n.tag in ('input', 'select', 'textarea')}
            signal = fields['signal'].attrs['value']
            self.assertEqual(form.attrs['action'], '/cabinet/taste')
            self.assertEqual(form.attrs['method'], 'post')
            common = {'csrfmiddlewaretoken', 'profile_ref', 'entry_ref', 'entry_kind', 'category',
                      'mutation_id', 'base_revision', 'operation', 'signal'}
            kind = fields['entry_kind'].attrs['value']
            expected = common | ({'title'} if kind == 'item' else set())
            expected |= {'text'} if signal == 'note' else {'value'}
            if signal == 'enjoyment':
                expected.add('experienced_ack')
                self.assertIn('required', fields['experienced_ack'].attrs)
                self.assertNotIn('checked', fields['experienced_ack'].attrs)
            self.assertEqual(set(fields), expected)
            self.assertEqual(fields['csrfmiddlewaretoken'].attrs['value'], 's' * 64)
            self.assertEqual(fields['base_revision'].attrs['value'], '0')
            self.assertEqual(fields['operation'].attrs['value'], 'set')
            if signal != 'note':
                self.assertIn('required', fields['value'].attrs)
                self.assertFalse(any('selected' in n.attrs for n in fields['value'].nodes()))
                self.assertEqual(next(n for n in fields['value'].nodes() if n.tag == 'option').attrs['value'], '')
        self.assertIn('not a hard rule or a source fact', doc.root.text())
        self.assertIn('Notes stay local by default', doc.root.text())

    def test_workflow_note_markup_preserves_exact_whitespace_unicode_and_escapes_text(self):
        note = '\n  Gentle <script>fiction</script> — café.\nSecond line.  '
        html, doc = self.cabinet(taste_input=True, taste_category='gallery',
            taste_groups=self.taste_groups(note=note)[2:])
        saved = next(n for n in doc.tagged('p') if n.attrs.get('class') == 'cabinet-exact-note')
        self.assertEqual(saved.text(), note)
        textarea = doc.tagged('textarea')[0]
        # HTML consumes the first newline immediately after textarea. The
        # deliberate sentinel preserves even an authored leading newline.
        self.assertEqual(textarea.text(), '\n' + note)
        self.assertEqual(textarea.attrs['maxlength'], '500')
        self.assertEqual(textarea.attrs['name'], 'text')
        self.assertEqual(doc.tagged('script') + doc.tagged('img'), [])
        self.assertIn('&lt;script&gt;fiction&lt;/script&gt;', html)
        self.assertNotIn('name="constraint"', html)

    def test_workflow_undo_carries_current_event_without_replacement_note_or_title(self):
        _, doc = self.cabinet(taste_input=True, taste_category='book',
            taste_groups=self.taste_groups(), csrf_token='s' * 64, profile_ref='fictional-profile')
        undo = [n for n in doc.tagged('form') if 'data-taste-undo' in n.attrs]
        self.assertEqual(len(undo), 1)
        fields = {n.attrs.get('name'): n.attrs.get('value') for n in undo[0].nodes() if n.tag == 'input'}
        self.assertEqual(fields, {'csrfmiddlewaretoken': 's' * 64, 'profile_ref': 'fictional-profile',
            'entry_ref': 'saved-item', 'mutation_id': 'undo-note', 'base_revision': '3',
            'operation': 'reverse', 'signal': 'note', 'reverses_id': 'event-note'})
        self.assertFalse({'text', 'title', 'value', 'category', 'entry_kind'} & fields.keys())

    def test_workflow_stale_response_shows_current_values_and_no_automatic_resubmit(self):
        for code in ('REVISION_CONFLICT', 'IDEMPOTENCY_CONFLICT', 'PREVIEW_STALE'):
            with self.subTest(code=code):
                _, doc = self.cabinet(taste_input=True, taste_category='book',
                    taste_groups=self.taste_groups()[2:], feedback_error_code=code)
                alert = next(n for n in doc.root.nodes() if n.attrs.get('id') == 'taste-feedback-error')
                self.assertEqual(alert.attrs['role'], 'alert')
                self.assertEqual(alert.attrs['tabindex'], '-1')
                self.assertIn('Review the current saved values', alert.text())
                self.assertIn('Nothing is retried automatically', alert.text())
                self.assertIn('Keep the pause — café.', doc.root.text())
                self.assertEqual(doc.tagged('script'), [])

    def test_workflow_card_forms_bind_only_backend_target_and_keep_note_out_of_other_signals(self):
        signals = [{**r, 'can_set': True} for r in self.taste_groups()[0]['signals']]
        feedback = {'target_ref': 'opaque-backend-target', 'can_set': True, 'signals': signals}
        categories = [{'category': 'book', 'label': 'Books', 'visible': True, 'cards': [
            {'title': 'Fictional card', 'feedback': feedback, 'reasons': [], 'facts': []}]}]
        _, doc = self.cabinet(categories=categories, feedback_available=True,
            feedback_url='/cabinet/taste?profile_ref=fictional-profile', csrf_token='s' * 64)
        forms = [n for n in doc.tagged('form') if 'data-card-feedback' in n.attrs]
        self.assertEqual(len(forms), 3)
        for form in forms:
            fields = {n.attrs.get('name'): n for n in form.nodes() if n.tag in ('input', 'select', 'textarea')}
            signal = fields['signal'].attrs['value']
            expected = {'csrfmiddlewaretoken', 'profile_ref', 'target_ref', 'mutation_id',
                        'base_revision', 'operation', 'signal'}
            expected |= {'text'} if signal == 'note' else {'value'}
            if signal == 'enjoyment':expected.add('experienced_ack')
            self.assertEqual(set(fields), expected)
            self.assertEqual(form.attrs['action'], '/cabinet/feedback')
            self.assertEqual(fields['target_ref'].attrs['value'], 'opaque-backend-target')
            self.assertFalse({'source_id', 'candidate_key', 'title', 'provider', 'actor_id'} & fields.keys())
        self.assertIn('Preferences and notes', doc.root.text())
        self.assertNotIn('Ratings and reactions are not connected', doc.root.text())

    def test_workflow_card_undo_remains_available_without_enabling_set(self):
        response = {**self.taste_groups()[2]['signals'][0], 'can_set': False}
        feedback = {'target_ref': 'opaque-backend-target', 'can_set': False, 'signals': [response]}
        _, doc = self.cabinet(categories=[{'category': 'book', 'label': 'Books', 'visible': True,
            'cards': [{'title': 'Fictional card', 'feedback': feedback}]}], csrf_token='s' * 64)
        self.assertFalse(any('data-card-feedback' in n.attrs for n in doc.tagged('form')))
        form = next(n for n in doc.tagged('form') if 'data-card-undo' in n.attrs)
        fields = {n.attrs.get('name'): n.attrs.get('value') for n in form.nodes() if n.tag == 'input'}
        self.assertEqual(set(fields), {'csrfmiddlewaretoken', 'profile_ref', 'target_ref',
            'mutation_id', 'base_revision', 'operation', 'signal', 'reverses_id'})
        self.assertEqual(fields['reverses_id'], 'event-note')
        self.assertEqual(fields['base_revision'], '3')

    def backend_feedback_fixture(self, note='\n  Quiet — café.\nKeep this spacing.  '):
        from uuid import uuid4
        from shelf.category_registry import CATEGORIES
        def uid():return str(uuid4())
        def signals(saved=False):
            return [{'signal': name, 'value': None, 'text': note if name == 'note' and saved else '',
                'revision': 2 if saved and name == 'note' else 0,
                'event_ref': uid() if saved and name == 'note' else '',
                'can_undo': saved and name == 'note', 'mutation_id': uid(), 'undo_mutation_id': uid()}
                    for name in ('interest', 'enjoyment', 'note')]
        return {'feedback_version': 'workflow-taste/v1', 'taste_input': True,
            'profile_ref': uid(), 'profile_label': 'Fictional test profile',
            'feedback_categories': [vars(row).copy() for row in CATEGORIES], 'feedback_category': 'book',
            'new_entry': {'entry_ref': uid(), 'base_revision': 0,
                'signals': [{'signal': r['signal'], 'mutation_id': r['mutation_id']} for r in signals()]},
            'feedback_entries': [{'entry_ref': uid(), 'entry_kind': 'item', 'category': 'book',
                'title': 'Fictional known book', 'signals': signals(True)},
                {'entry_ref': uid(), 'entry_kind': 'category_note', 'category': 'dining',
                 'title': 'Dining context', 'signals': signals(True)}],
            'taste_action': '/cabinet/taste', 'feedback_action': '/cabinet/feedback',
            'feedback_code': '', 'cabinet_url': '/cabinet'}

    def test_workflow_real_renderer_binds_frozen_handler_shape_and_exact_note_text(self):
        from django.test import RequestFactory
        from shelf import enhancement_views as views
        value = self.backend_feedback_fixture()
        value['feedback_code'] = 'REVISION_CONFLICT'
        response = views._native_cabinet_html(RequestFactory().get('/cabinet/taste'), value, status=409)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response['Cache-Control'], 'no-store')
        doc = Document(response.content.decode())
        note = next(n for n in doc.tagged('p') if n.attrs.get('class') == 'cabinet-exact-note')
        self.assertEqual(note.text(), value['feedback_entries'][0]['signals'][2]['text'])
        self.assertNotIn('Dining context', doc.root.text())
        forms = [n for n in doc.tagged('form') if 'data-taste-set' in n.attrs]
        self.assertEqual(len(forms), 7)  # 3 new item + 1 category note + 3 saved signals.
        saved_title = [n for n in doc.tagged('input') if n.attrs.get('name') == 'title'
                       and n.attrs.get('value') == 'Fictional known book']
        self.assertTrue(saved_title)
        self.assertTrue(all(n.attrs['type'] == 'hidden' for n in saved_title))
        self.assertIn('Your response was not saved', doc.root.text())

    def test_workflow_projection_refuses_malformed_note_bindings_instead_of_normalizing(self):
        from copy import deepcopy
        from shelf import enhancement_views as views
        from shelf.portable_authority import AuthorityFault
        original = self.backend_feedback_fixture()
        changes = [lambda x:x['new_entry'].update(base_revision=False),
                   lambda x:x['feedback_entries'][0]['signals'][2].update(text='bad\x00note'),
                   lambda x:x['feedback_entries'][0]['signals'][2].update(revision=True),
                   lambda x:x['feedback_entries'][0]['signals'][2].update(can_undo=True, event_ref=''),
                   lambda x:x['feedback_categories'].append('not a descriptor')]
        for change in changes:
            value = deepcopy(original);change(value)
            with self.subTest(change=changes.index(change)), self.assertRaises(AuthorityFault):
                views._taste_display_context(value)

    def test_workflow_card_projection_uses_handler_actions_and_rejects_identity_mismatch(self):
        from shelf import enhancement_views as views
        from shelf.portable_authority import AuthorityFault
        value = self.backend_feedback_fixture()
        raw = {'target_ref': value['new_entry']['entry_ref'], 'profile_ref': value['profile_ref'],
            'signals': value['feedback_entries'][0]['signals'], 'actions': ['interest'],
            'feedback_action': '/cabinet/feedback'}
        display = views._card_feedback_display(raw, profile_ref=raw['profile_ref'], target_ref=raw['target_ref'])
        self.assertEqual([r['signal'] for r in display['signals'] if r['can_set']], ['interest'])
        self.assertEqual(display['signals'][2]['value'], raw['signals'][2]['text'])
        with self.assertRaises(AuthorityFault):
            views._card_feedback_display(raw, profile_ref='a-different-profile', target_ref=raw['target_ref'])

    def test_workflow_note_sharing_classes_are_separate_explicit_unchecked_choices(self):
        from shelf.enhancement_views import _INFERENCE_CHOICES
        _, doc = self.cabinet(consent=True, consent_record_active=False,
            inference_choices=[{'code': code, 'label': label} for code, label in _INFERENCE_CHOICES])
        checks = {n.attrs.get('name'): n for n in doc.tagged('input') if n.attrs.get('type') == 'checkbox'}
        self.assertIn('allow_raw_notes', checks)
        self.assertIn('allow_free_text_entry', checks)
        self.assertFalse(any('checked' in n.attrs for n in checks.values()))
        self.assertIn('Leaving either unchecked keeps those notes out of the request', doc.root.text())
