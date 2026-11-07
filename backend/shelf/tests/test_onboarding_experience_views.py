"""Safe presentation contracts; no launcher, credentials, database or transport.

The Root projection is a boundary stub only for GET dispatch tests. These tests
cannot prove owner authorization or actual generation; those have separate Root
HTTP/runtime evidence. Rendering exercises real Django escaping and native forms.
"""
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils.safestring import mark_safe

from shelf import onboarding_experience_views as views, portable_setup_views


CONNECTION = '11111111-1111-4111-8111-111111111111'
PREVIEW = '22222222-2222-4222-8222-222222222222'
INTENT = '33333333-3333-4333-8333-333333333333'
CANARY = 'FICTIONAL-secret-must-not-be-returned'
TOKEN = 's' * 64


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.body = {}
        self.forms = {}
        self.current = None
        self.inputs = []
        self.tags = []
        self.pre_text = ''
        self.in_pre = False
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.tags.append((tag, attrs))
        if tag == 'body':
            self.body = attrs
        elif tag == 'form':
            self.current = {'attrs': attrs, 'inputs': [], 'buttons': []}
            self.forms[attrs.get('data-form')] = self.current
        elif tag == 'input':
            self.inputs.append(attrs)
            if self.current is not None:
                self.current['inputs'].append(attrs)
        elif tag == 'button' and self.current is not None:
            self.current['buttons'].append(attrs)
        elif tag == 'pre':
            self.in_pre = True

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None
        elif tag == 'pre':
            self.in_pre = False

    def handle_data(self, data):
        if self.in_pre:
            self.pre_text += data


def forbidden_context_processor(request):
    raise AssertionError('Request context must not reach presentation')


class Poison:
    def __str__(self):
        raise AssertionError('Unexpected object entered presentation')


class GetRequest:
    method = 'GET'
    path = '/setup'

    def __init__(self):
        self.META = {}

    @property
    def POST(self):
        raise AssertionError('Presentation inspected request.POST')

    @property
    def session(self):
        raise AssertionError('Presentation read the owner session directly')


class OnboardingExperienceTests(SimpleTestCase):
    def model(self, **changes):
        context = dict(state='model', configured=True, credential_configured=False,
                    connection_ready=False, connection_ref=CONNECTION, revision=1,
                    outcome='missing_credential')
        context.update(changes)
        return context

    def render(self, context, **options):
        response = views.render_setup({**context, 'csrf_token': TOKEN}, **options)
        html = response.content.decode()
        return response, html, Document(html)

    def preview(self):
        return {'body': '{"input":"FICTIONAL neutral <tag> & no personal taste"}\n',
                'preview_ref': PREVIEW, 'intent_ref': INTENT}

    def test_native_credits_keep_approved_logo_notice_and_source_link_in_every_state(self):
        import hashlib
        logo = Path(__file__).resolve().parents[3] / 'templates/assets/tmdb-approved.svg'
        # Official approved asset bytes, not a recreated mark or generic source
        # URL. Provenance URL and response hash are retained in the release.
        self.assertEqual(hashlib.sha256(logo.read_bytes()).hexdigest(),
                         '8e7b30f73a4020692ccca9c88bafe5dcb6f8a62a4c6bc55cd9ba82bb2cd95f6c')
        for context in ({'state': 'claim'}, {'state': 'login'}, self.model()):
            with self.subTest(state=context['state']):
                _, html, doc = self.render(context)
                credits = next(attrs for tag, attrs in doc.tags if attrs.get('id') == 'about-credits')
                self.assertNotIn('hidden', credits)
                self.assertIn('This product uses the TMDB API but is not endorsed or certified by TMDB.', html)
                self.assertTrue(any(tag == 'svg' for tag, attrs in doc.tags))
                self.assertTrue(any(tag == 'a' and attrs.get('href') == 'https://www.themoviedb.org'
                                    for tag, attrs in doc.tags))
                self.assertIn('href="#about-credits"', html)
                self.assertNotIn('https://www.themoviedb.org/assets/', html)

    def test_musicbrainz_credit_is_core_only_and_does_not_advertise_other_category_coverage(self):
        for context in ({'state': 'claim'}, {'state': 'login'}, self.model()):
            with self.subTest(state=context['state']):
                _, html, doc = self.render(context)
                for phrase in ('recording titles, artist credits and recording identifiers',
                               'CC0 core data', 'no audio rights are implied',
                               'matches names, not a verified artist identity',
                               'supports recordings only',
                               'TMDB and Hardcover are unavailable'):
                    self.assertIn(phrase, html)
                self.assertTrue(any(tag == 'a' and attrs.get('href') ==
                                    'https://musicbrainz.org/doc/About/Data_License'
                                    for tag, attrs in doc.tags))
                # Credits are links, never automatic external asset/source loads.
                self.assertFalse(any(tag in ('img', 'script', 'iframe') and
                    'musicbrainz' in attrs.get('src', '') for tag, attrs in doc.tags))

    def test_processor_disclosure_precedes_credentials_without_promising_zero_retention(self):
        _, html, doc = self.render(self.model(credential_configured=True, secret=CANARY))
        self.assertLess(html.index('Before sharing a request'), html.index('name="secret"'))
        for copy in ('store=false', '2048 output tokens', 'up to 30 days', 'up to 24 hours',
                     'does not mean zero retention', 'unless the account opts in',
                     'have not been verified', 'exact request confirmation'):
            self.assertIn(copy, html)
        self.assertNotIn(CANARY, html)
        self.assertNotIn('confirm', doc.forms)
        self.assertTrue(all('value' not in row for row in doc.inputs if row.get('type') == 'password'))

    def test_claim_has_only_native_owner_claim_with_empty_secret_fields(self):
        response, html, doc = self.render({'state': 'claim'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(doc.body['data-state'], 'claim')
        self.assertEqual(set(doc.forms), {'claim'})
        fields = {row['name']: row for row in doc.forms['claim']['inputs']}
        for name in ('code', 'passphrase'):
            self.assertEqual(fields[name]['type'], 'password')
            self.assertNotIn('value', fields[name])
        self.assertEqual(fields['code']['autocomplete'], 'one-time-code')
        self.assertEqual(fields['passphrase']['autocomplete'], 'new-password')
        self.assertIn('one-time code', html)

    def test_login_cannot_offer_another_owner_claim(self):
        _, _, doc = self.render({'state': 'login'})
        self.assertEqual(set(doc.forms), {'login'})
        password = next(row for row in doc.inputs if row['name'] == 'passphrase')
        self.assertEqual(password['autocomplete'], 'current-password')
        self.assertNotIn('value', password)

    def test_unconfigured_model_has_fixed_config_without_credential_control(self):
        _, html, doc = self.render({'state': 'model'})
        self.assertEqual(set(doc.forms), {'configure', 'logout'})
        values = {row['name']: row.get('value') for row in doc.forms['configure']['inputs']}
        self.assertEqual(values['adapter'], 'openai_responses')
        self.assertEqual(values['model'], 'gpt-6-astra')
        self.assertEqual(values['reasoning'], 'high')
        self.assertIn('does not establish working inference', html)

    def test_saved_config_exposes_bound_revision_and_write_only_key_form(self):
        _, _, doc = self.render(self.model())
        self.assertEqual(set(doc.forms), {'credential', 'logout'})
        values = {row['name']: row.get('value') for row in doc.forms['credential']['inputs']}
        self.assertEqual(values['connection_ref'], CONNECTION)
        self.assertEqual(values['revision'], '1')
        self.assertIsNone(values['secret'])
        self.assertEqual(doc.body['data-configured'], 'true')
        self.assertEqual(doc.body['data-credential-configured'], 'false')

    def test_current_key_exposes_explicit_preview_and_remove_forms(self):
        context = self.model()
        context['credential_configured'] = True
        _, html, doc = self.render(context)
        self.assertEqual(set(doc.forms), {'credential', 'preview', 'remove', 'logout'})
        self.assertEqual(doc.body['data-credential-configured'], 'true')
        self.assertIn('Replace session-only API key', html)
        self.assertNotIn('confirm', doc.forms)

    def test_neutral_preview_is_exact_text_and_not_executable_markup(self):
        context = self.model()
        context.update(credential_configured=True, preview=self.preview())
        _, html, doc = self.render(context)
        self.assertEqual(doc.pre_text, self.preview()['body'])
        self.assertIn('&lt;tag&gt;', html)
        self.assertFalse(any(tag == 'tag' for tag, attrs in doc.tags))

    def test_confirmation_is_bound_and_separate_from_preview(self):
        context = self.model()
        context.update(credential_configured=True, preview=self.preview())
        _, html, doc = self.render(context)
        values = {row['name']: row for row in doc.forms['confirm']['inputs']}
        self.assertEqual(values['preview_ref']['value'], PREVIEW)
        self.assertEqual(values['intent_ref']['value'], INTENT)
        self.assertEqual(values['confirm_billable_test']['type'], 'checkbox')
        self.assertIn('required', values['confirm_billable_test'])
        self.assertIn('2048 tokens', html)
        self.assertIn('one potentially paid send', html)
        self.assertIn('no automatic retry', html)
        self.assertIn('api.openai.com/v1/responses', html)

    def test_unknown_outcome_disables_both_paid_test_paths_but_not_removal(self):
        context = self.model()
        context.update(credential_configured=True, outcome='unknown', preview=self.preview())
        _, html, doc = self.render(context)
        for name in ('preview', 'confirm'):
            self.assertIn('disabled', doc.forms[name]['buttons'][0])
        self.assertNotIn('disabled', doc.forms['remove']['buttons'][0])
        self.assertIn('Do not send another test blindly', html)

    def test_connection_proof_does_not_create_a_personal_run_control(self):
        context = self.model()
        context.update(credential_configured=True, connection_ready=True, outcome='passed', run_ready=True)
        _, html, doc = self.render(context)
        self.assertEqual(doc.body['data-connection-ready'], 'true')
        self.assertEqual(doc.body['data-run-ready'], 'false')
        self.assertFalse({'run', 'enhance', 'start'} & set(doc.forms))
        self.assertIn('Prepare Enhance in your cabinet', html)
        self.assertIn('Each API request and inference request still needs its own review', html)

    def test_synthetic_result_remains_visibly_unproved(self):
        context = self.model()
        context.update(credential_configured=True, outcome='isolated_test_only')
        _, html, doc = self.render(context)
        self.assertEqual(doc.body['data-connection-ready'], 'false')
        self.assertIn('Only a synthetic test result', html)
        self.assertIn('Working inference has not been established', html)

    def test_unlisted_context_cannot_expose_secrets_requests_or_objects(self):
        context = self.model()
        context.update(secret=CANARY, passphrase=CANARY, code=CANARY, request=Poison(),
                       portable_session=CANARY, credential_handle=CANARY, traceback=CANARY)
        _, html, _ = self.render(context)
        self.assertNotIn(CANARY, html)

    def test_untrusted_notice_is_fixed_copy_and_secret_inputs_remain_empty(self):
        _, html, doc = self.render({'state': 'claim', 'notice': CANARY}, status=422)
        self.assertNotIn(CANARY, html)
        notice = next(attrs for tag, attrs in doc.tags if attrs.get('id') == 'setup-notice')
        self.assertEqual(notice['role'], 'alert')
        self.assertIn('autofocus', notice)
        self.assertTrue(all('value' not in row for row in doc.inputs if row.get('type') == 'password'))
        self.assertIn('secret fields have been cleared', html)

    def test_known_error_copy_does_not_return_the_code_as_diagnostics(self):
        _, html, _ = self.render({'state': 'login', 'notice': 'UNAUTHENTICATED'})
        self.assertIn('Your owner session has ended', html)
        self.assertNotIn('UNAUTHENTICATED', html)

    def test_pre_marked_safe_preview_cannot_disable_escaping(self):
        context = self.model()
        context.update(credential_configured=True, preview={**self.preview(), 'body': mark_safe('<script>bad</script>')})
        with self.assertRaises(ValueError):
            self.render(context)

    def test_truthy_nonbooleans_cannot_claim_config_or_readiness(self):
        for name in ('configured', 'credential_configured', 'connection_ready'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                context = self.model()
                context[name] = 'true'
                self.render(context)

    def test_foreign_markup_cannot_become_a_hidden_connection_reference(self):
        context = self.model()
        context['connection_ref'] = '<input name="secret" value="' + CANARY + '">'
        with self.assertRaises(ValueError):
            self.render(context)

    def test_preview_requires_current_key_and_canonical_bound_refs(self):
        for changes in ({'preview': self.preview()},
                        {'credential_configured': True, 'preview': {**self.preview(), 'intent_ref': CANARY}}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                context = self.model()
                context.update(changes)
                self.render(context)

    def test_global_request_processor_is_never_invoked(self):
        templates = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
                      'DIRS': [Path(__file__).resolve().parents[3] / 'templates'],
                      'OPTIONS': {'context_processors': [__name__ + '.forbidden_context_processor']}}]
        with override_settings(TEMPLATES=templates):
            response, _, _ = self.render({'state': 'claim', 'request': Poison()})
        self.assertEqual(response.status_code, 200)

    def test_get_delegates_to_root_without_reading_post_or_session(self):
        # Stub only the trusted projection boundary, not authorization behavior.
        with patch.object(portable_setup_views, 'setup_context', return_value={'state': 'claim', 'csrf_token': TOKEN}) as project:
            response = views.setup(GetRequest())
        self.assertEqual(response.status_code, 200)
        project.assert_called_once()
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_post_is_not_a_presentation_entry_point(self):
        request = GetRequest()
        request.method = 'POST'
        with patch.object(portable_setup_views, 'setup_context') as project:
            response = views.setup(request)
        self.assertEqual(response.status_code, 405)
        project.assert_not_called()

    def test_forms_have_explicit_local_actions_csrf_and_submit_buttons(self):
        context = self.model()
        context.update(credential_configured=True, preview=self.preview())
        for state in ({'state': 'claim'}, {'state': 'login'}, context):
            _, _, doc = self.render(state)
            self.assertFalse(any(tag == 'script' for tag, attrs in doc.tags))
            assets = [attrs['href'] for tag, attrs in doc.tags if tag == 'link']
            self.assertEqual(assets, ['/setup/style.css'])
            for form in doc.forms.values():
                self.assertEqual(form['attrs']['method'], 'post')
                self.assertTrue(form['attrs']['action'].startswith('/setup/form/'))
                self.assertTrue(all(button.get('type') == 'submit' for button in form['buttons']))
                self.assertTrue(any(row.get('name') == 'csrfmiddlewaretoken' and row.get('value') == TOKEN for row in form['inputs']))

    def test_ambiguous_configuration_is_not_chosen_or_replaced_implicitly(self):
        _, html, doc = self.render({'state': 'model', 'notice': 'CONNECTION_SELECTION_REQUIRED'})
        self.assertEqual(set(doc.forms), {'logout'})
        self.assertIn('none was chosen automatically', html)
        self.assertEqual(doc.body['data-configured'], 'false')

    def test_success_receipts_are_status_not_false_error_alerts(self):
        _, html, doc = self.render({'state': 'model', 'notice': 'CONFIG_SAVED'})
        self.assertIn('Model configuration saved', html)
        self.assertFalse(any(attrs.get('id') == 'setup-notice' for tag, attrs in doc.tags))
        self.assertTrue(any(attrs.get('id') == 'setup-status' and attrs.get('role') == 'status' for tag, attrs in doc.tags))

    def test_multiple_owned_connections_have_explicit_get_selection_only(self):
        other = '44444444-4444-4444-8444-444444444444'
        _, html, doc = self.render({'state': 'model', 'connections': [
            {'connection_ref': CONNECTION, 'revision': 1}, {'connection_ref': other, 'revision': 2}]})
        self.assertEqual(set(doc.forms), {'select', 'logout'})
        self.assertEqual(doc.forms['select']['attrs']['method'], 'get')
        self.assertEqual(doc.forms['select']['attrs']['action'], '/setup')
        self.assertIn(CONNECTION, html)
        self.assertIn(other, html)
        self.assertEqual(doc.body['data-configured'], 'false')

    def test_stylesheet_is_fixed_bundle_asset_without_request_context(self):
        response = views.style(GetRequest())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/css; charset=utf-8')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn(b':focus-visible', response.content)

    def test_authoritative_failure_states_remain_distinguishable(self):
        for outcome, text in [('denied', 'did not accept'), ('unsupported', 'unsupported'),
                              ('exhausted', 'capacity or allowance'), ('unreachable', 'could not be reached'),
                              ('incompatible', 'did not meet')]:
            with self.subTest(outcome=outcome):
                _, html, doc = self.render(self.model(outcome=outcome))
                self.assertIn(text, html)
                self.assertEqual(doc.body['data-connection-ready'], 'false')

    def test_projected_urls_cannot_redirect_native_secret_forms(self):
        context = self.model()
        context['actions'] = {'credential': 'https://fictional.invalid/collect'}
        _, html, doc = self.render(context)
        self.assertEqual(doc.forms['credential']['attrs']['action'], '/setup/form/credential')
        self.assertNotIn('fictional.invalid', html)

    def test_live_session_password_confirmation_is_empty_bound_and_reachable(self):
        context = self.model(credential_configured=True, reauthentication_required=True,
                             notice='REAUTHENTICATION_REQUIRED', passphrase=CANARY, secret=CANARY)
        _, html, doc = self.render(context)
        self.assertEqual(doc.body['data-state'], 'model')
        self.assertEqual(doc.body['data-configured'], 'true')
        self.assertEqual(doc.body['data-credential-configured'], 'true')
        self.assertNotIn('login', doc.forms)
        form = doc.forms['reauthenticate']
        self.assertEqual(form['attrs']['action'], '/setup/form/reauthenticate')
        self.assertEqual(form['attrs']['method'], 'post')
        fields = {row['name']: row for row in form['inputs']}
        self.assertEqual(set(fields), {'csrfmiddlewaretoken', 'passphrase', 'connection_ref'})
        self.assertEqual(fields['csrfmiddlewaretoken']['value'], TOKEN)
        self.assertEqual(fields['connection_ref']['value'], CONNECTION)
        self.assertEqual(fields['passphrase']['type'], 'password')
        self.assertEqual(fields['passphrase']['autocomplete'], 'current-password')
        self.assertNotIn('value', fields['passphrase'])
        self.assertNotIn('disabled', form['buttons'][0])
        self.assertNotIn(CANARY, html)
        self.assertIn('Your session is still open', html)
        self.assertIn('Nothing is resubmitted automatically', html)
        self.assertNotIn('Your owner session has ended', html)
        context['notice'] = 'AUTHORITY_DENIED'
        _, error_html, rejected = self.render(context)
        self.assertIn('The owner passphrase was not accepted', error_html)
        self.assertNotIn(CANARY, error_html)
        self.assertNotIn('Check the terminal code', error_html)
        password = next(row for row in rejected.forms['reauthenticate']['inputs']
                        if row['name'] == 'passphrase')
        self.assertNotIn('value', password)

    def test_confirmation_without_selection_and_ended_session_are_distinct(self):
        _, html, doc = self.render({'state': 'model', 'reauthentication_required': True,
                                   'notice': 'UNAUTHENTICATED'})
        fields = {row['name'] for row in doc.forms['reauthenticate']['inputs']}
        self.assertEqual(fields, {'csrfmiddlewaretoken', 'passphrase'})
        self.assertNotIn('Your owner session has ended', html)
        self.assertIn('disabled', doc.forms['configure']['buttons'][0])
        # An actually ended session stays on Root's login state, even if stale
        # presentation fields happen to accompany it; it must not suggest that
        # a vault lease survives session expiry.
        _, ended, login = self.render({'state': 'login', 'notice': 'UNAUTHENTICATED',
                                      'reauthentication_required': True})
        self.assertEqual(set(login.forms), {'login'})
        self.assertIn('Your owner session has ended', ended)
        self.assertNotIn('reauthenticate', login.forms)

    def test_password_confirmation_never_replays_key_or_paid_preview(self):
        context = self.model(credential_configured=True, reauthentication_required=True,
                             preview=self.preview(), secret=CANARY)
        _, html, doc = self.render(context)
        self.assertNotIn('confirm', doc.forms)
        self.assertNotIn(PREVIEW, html)
        self.assertNotIn(INTENT, html)
        for action in ('credential', 'preview', 'remove'):
            self.assertIn('disabled', doc.forms[action]['buttons'][0])
        self.assertNotIn('disabled', doc.forms['reauthenticate']['buttons'][0])
        self.assertNotIn(CANARY, html)
        self.assertEqual(context['preview'], self.preview())
        # After Root confirms the same live session, only an explicit later
        # click can retry an action. No old form body or preview is carried.
        _, confirmed, ready = self.render(self.model(credential_configured=True,
                                                    notice='OWNER_UNLOCKED'))
        self.assertNotIn('reauthenticate', ready.forms)
        self.assertNotIn('confirm', ready.forms)
        self.assertNotIn('disabled', ready.forms['preview']['buttons'][0])
        self.assertIn('Retry your intended action explicitly', confirmed)
        self.assertIn('no key or connection test was resubmitted', confirmed)
