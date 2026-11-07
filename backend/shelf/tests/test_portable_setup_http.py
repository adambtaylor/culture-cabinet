"""Isolated Django TestClient HTTP; no app server, browser or real credential."""
import json
import sys
import uuid
from urllib.parse import urlencode, urlsplit, parse_qs
from unittest.mock import patch
from django.core.signals import got_request_exception
from django.views.debug import ExceptionReporter

from django.contrib.sessions.models import Session
from django.test import Client, override_settings

from .test_portable_authority import BoundaryCase, PASSWORD
from shelf import grant_authority as ga
from shelf.models import Profile, ProfileCabinetState, Grant
from .test_grant_authority import AuthorityCase
from .test_enhance_presentation import Document


class SetupHTTPTests(BoundaryCase):
    def setUp(self):
        super().setUp()
        self.options = self.settings(ROOT_URLCONF='cabinet.portable_urls', PORTABLE_AUTHORITY=self.authority,
            SESSION_COOKIE_NAME=self.boundary.session_cookie, CSRF_COOKIE_NAME=self.boundary.csrf_cookie,
            MIDDLEWARE=['cabinet.portable_boundary.PortableSecurityMiddleware',
                        'django.contrib.sessions.middleware.SessionMiddleware',
                        'django.middleware.csrf.CsrfViewMiddleware'])
        self.options.enable()
        self.addCleanup(self.options.disable)
        self.http = Client(enforce_csrf_checks=True)
        self.headers = {'HTTP_HOST': self.boundary.origin.removeprefix('http://'), 'HTTP_ORIGIN': self.boundary.origin}

    def token(self, client=None):
        return (client or self.http).get('/setup/status', **self.headers).json()['csrf_token']

    def post(self, path, body, *, client=None, **headers):
        client = client or self.http
        token = self.token(client)
        return client.post(path, json.dumps(body), content_type='application/json',
                           HTTP_X_CSRFTOKEN=token, **{**self.headers, **headers})

    def claim_http(self):
        return self.post('/setup/claim', {'code': self.challenge(), 'passphrase': PASSWORD})

    def test_claim_creates_owner_and_authenticated_rotated_session(self):
        self.assertEqual(self.claim_http().status_code, 200)
        result = self.http.get('/setup/status', **self.headers).json()
        self.assertTrue(result['authenticated'])
        self.assertTrue(result['claimed'])
        data = Session.objects.get().get_decoded()
        self.assertEqual(set(data) - {'_session_expiry'}, {'portable_session'})
        self.assertNotIn(PASSWORD, json.dumps(data))

    def test_owner_mutation_without_csrf_is_denied(self):
        code = self.challenge()
        response = self.http.post('/setup/claim', json.dumps({'code': code, 'passphrase': PASSWORD}),
                                  content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(PASSWORD.encode(), response.content)
        self.assertNotIn(code.encode(), response.content)

    def test_wrong_origin_or_host_and_forwarding_are_denied(self):
        for headers in [{'HTTP_ORIGIN': 'https://foreign.invalid'}, {'HTTP_HOST': 'localhost:80'},
                        {'HTTP_X_FORWARDED_HOST': '127.0.0.1'}, {'HTTP_FORWARDED': 'for=127.0.0.1'},
                        {'HTTP_SEC_FETCH_SITE': 'cross-site'}]:
            token = self.token()
            response = self.http.post('/setup/logout', '{}', content_type='application/json',
                HTTP_X_CSRFTOKEN=token, **{**self.headers, **headers})
            self.assertEqual(response.status_code, 403)

    def test_missing_origin_is_denied(self):
        token = self.token()
        response = self.http.post('/setup/logout', '{}', content_type='application/json',
            HTTP_X_CSRFTOKEN=token, HTTP_HOST=self.headers['HTTP_HOST'])
        self.assertEqual(response.status_code, 403)

    def test_missing_server_authority_is_unavailable(self):
        with override_settings(PORTABLE_AUTHORITY=None):
            self.assertEqual(self.http.get('/setup/status', **self.headers).status_code, 503)

    def test_wrong_cookie_namespace_fails_closed(self):
        with override_settings(SESSION_COOKIE_NAME='foreign_session'):
            self.assertEqual(self.http.get('/setup/status', **self.headers).status_code, 503)

    def test_cookie_flags_and_no_store_security_headers(self):
        result = self.claim_http()
        cookie = result.cookies[self.boundary.session_cookie]
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Strict')
        self.assertFalse(cookie['secure'])
        self.assertEqual(result['Cache-Control'], 'no-store')
        self.assertEqual(result['X-Frame-Options'], 'DENY')
        self.assertTrue(result.cookies[self.boundary.csrf_cookie]['httponly'])

    def test_logout_removes_server_session(self):
        self.claim_http()
        self.assertEqual(self.post('/setup/logout', {}).status_code, 200)
        self.assertFalse(self.http.get('/setup/status', **self.headers).json()['authenticated'])
        self.assertFalse(Session.objects.exists())

    def test_restart_requires_password_and_new_cookie_namespace(self):
        self.claim_http()
        before = self.boundary.session_cookie
        self.restart()
        with override_settings(PORTABLE_AUTHORITY=self.authority, SESSION_COOKIE_NAME=self.boundary.session_cookie,
                               CSRF_COOKIE_NAME=self.boundary.csrf_cookie):
            self.headers = {'HTTP_HOST': self.boundary.origin.removeprefix('http://'), 'HTTP_ORIGIN': self.boundary.origin}
            self.assertFalse(self.http.get('/setup/status', **self.headers).json()['authenticated'])
            self.assertEqual(self.post('/setup/login', {'passphrase': PASSWORD}).status_code, 200)
            self.assertNotEqual(before, self.boundary.session_cookie)

    def test_reauthentication_rotates_cookie_and_csrf(self):
        self.claim_http()
        before = self.http.cookies[self.boundary.session_cookie].value
        csrf = self.http.cookies[self.boundary.csrf_cookie].value
        self.assertEqual(self.post('/setup/reauthenticate', {'passphrase': PASSWORD}).status_code, 200)
        self.assertNotEqual(before, self.http.cookies[self.boundary.session_cookie].value)
        self.assertNotEqual(csrf, self.http.cookies[self.boundary.csrf_cookie].value)

    def test_malformed_duplicate_extra_and_surrogate_bodies_fail_without_echo(self):
        for body in ['{', '{"passphrase":"one","passphrase":"two"}',
                     json.dumps({'passphrase': PASSWORD, 'origin': 'https://foreign.invalid'}),
                     json.dumps({'passphrase': 'synthetic-owner' + chr(0xD800)}), 'x' * 4097]:
            token = self.token()
            response = self.http.post('/setup/login', body, content_type='application/json',
                                      HTTP_X_CSRFTOKEN=token, **self.headers)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json(), {'ok': False, 'code': 'CONTRACT_INVALID'})

    def test_read_only_or_unauthenticated_actions_cannot_claim_owner(self):
        self.assertEqual(self.http.get('/setup/claim', **self.headers).status_code, 405)
        response = self.post('/setup/reauthenticate', {'passphrase': PASSWORD})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.authority._installation().owner_id)

    def test_other_cookie_client_does_not_inherit_owner_session(self):
        self.claim_http()
        outsider = Client(enforce_csrf_checks=True)
        self.assertFalse(outsider.get('/setup/status', **self.headers).json()['authenticated'])


    def test_unexpected_owner_error_redacts_json_code_and_password_diagnostics(self):
        reports = []
        def receive(sender, request, **kwargs):
            reports.append(ExceptionReporter(request, *sys.exc_info()).get_traceback_text())
        code = self.challenge()
        got_request_exception.connect(receive, weak=False)
        try:
            with patch('shelf.portable_authority.get_hasher', side_effect=RuntimeError('SYNTHETIC-OWNER-DIAGNOSTIC-FAULT')):
                response = self.post('/setup/claim', {'code': code, 'passphrase': PASSWORD},
                                     client=Client(enforce_csrf_checks=True, raise_request_exception=False))
        finally:
            got_request_exception.disconnect(receive)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(reports), 1)
        self.assertIn('SYNTHETIC-OWNER-DIAGNOSTIC-FAULT', reports[0])
        self.assertIn('portable_setup_views.py', reports[0])
        self.assertIn('*********', reports[0])
        for canary in [code, PASSWORD]:
            self.assertNotIn(canary, reports[0])
            self.assertNotIn(canary.encode(), response.content)




class CabinetEntryHTTPTests(AuthorityCase):
    """Existing isolated owner/connection/recovery fixture, real native commands."""
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(ROOT_URLCONF='cabinet.portable_urls',
            PORTABLE_AUTHORITY=self.authority, PORTABLE_CONNECTIONS=self.service,
            SESSION_COOKIE_NAME=self.boundary.session_cookie, CSRF_COOKIE_NAME=self.boundary.csrf_cookie,
            MIDDLEWARE=['cabinet.portable_boundary.PortableSecurityMiddleware',
                        'django.contrib.sessions.middleware.SessionMiddleware',
                        'django.middleware.csrf.CsrfViewMiddleware']))
        self.http = Client(enforce_csrf_checks=True)
        self.headers = {'HTTP_HOST': self.boundary.origin.removeprefix('http://'),
                        'HTTP_ORIGIN': self.boundary.origin}
        session = self.http.session
        session['portable_session'] = self.ref
        session.save()

    def create(self, revision=None, *, csrf=True):
        token = self.http.get('/setup/status', **self.headers).json()['csrf_token']
        revision = revision if revision is not None else self.authority.authorize(self.ref).owner_revision
        return self.http.post('/setup/cabinet/create', urlencode({'expected_owner_revision': revision}),
            content_type='application/x-www-form-urlencoded',
            **self.headers, **({'HTTP_X_CSRFTOKEN': token} if csrf else {}))

    def test_entry_get_never_creates_or_adopts_existing_unbound_profiles(self):
        response = self.http.get('/setup/cabinet', **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Profile.objects.exists())
        self.assertFalse(ProfileCabinetState.objects.exists())
        self.assertEqual(self.http.get('/setup/cabinet/create', **self.headers).status_code, 405)
        self.assertFalse(Profile.objects.exists())
        self.assertIn('data-cabinet-create', response.content.decode())
        with ga._write_window():
            Profile.objects.create(kind='partner', label='Synthetic unbound private-shaped profile', active=True)
        response = self.http.get('/setup/cabinet', **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('data-cabinet-create', response.content.decode())
        self.assertNotIn('Synthetic unbound private-shaped profile', response.content.decode())
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(Profile.objects.count(), 1)
        self.assertFalse(ProfileCabinetState.objects.exists())

    def test_create_requires_csrf_recent_owner_and_is_repeatable_without_extra_profile(self):
        self.assertEqual(self.create(csrf=False).status_code, 403)
        self.assertFalse(Profile.objects.exists())
        first = self.create()
        self.assertEqual(first.status_code, 303)
        ref = parse_qs(urlsplit(first['Location']).query)['profile_ref'][0]
        state = ProfileCabinetState.objects.get()
        self.assertEqual(str(state.profile_ref), ref)
        self.assertEqual(self.create()['Location'], first['Location'])
        self.assertEqual(Profile.objects.count(), 1)
        self.assertEqual(Grant.objects.count(), 1)
        self.assertEqual(self.create(revision=state.owner_revision + 1).status_code, 409)
        self.clock.advance(301)
        response = self.create()
        self.assertEqual(response.status_code, 401)
        self.assertIn('Confirm your owner passphrase', response.content.decode())
        self.assertEqual(Profile.objects.count(), 1)
        # No original command or manifest is retained in the Django session.
        self.assertEqual(set(self.http.session.keys()) - {'_session_expiry'}, {'portable_session'})

    def test_only_authorized_bound_choices_are_listed_and_selection_is_explicit(self):
        ref = self.bind()
        response = self.http.get('/setup/cabinet', **self.headers)
        doc = Document(response.content.decode())
        options = doc.tagged('option')
        self.assertTrue(any(node.attrs.get('value') == ref for node in options))
        self.assertTrue(any(node.attrs.get('value') == str(self.config.pk) for node in options))
        self.assertFalse(any('selected' in node.attrs for node in options))
        self.assertNotIn(ref, doc.root.text(visible=True))
        self.assertNotIn(str(self.config.pk), doc.root.text(visible=True))
        foreign = str(uuid.uuid4())
        self.assertEqual(self.http.get('/setup/cabinet', {'profile_ref': foreign}, **self.headers).status_code, 403)
        self.assertEqual(self.http.get('/cabinet', {'profile_ref': ref, 'connection_ref': foreign}, **self.headers).status_code, 403)
        with ga._write_window():
            Grant.objects.filter(profile_id=ProfileCabinetState.objects.get().profile_id).update(can_select=False)
        response = self.http.get('/setup/cabinet', **self.headers)
        self.assertNotIn(ref, response.content.decode())
        self.assertNotIn('data-cabinet-create', response.content.decode())
        self.assertEqual(self.http.get('/cabinet', {'profile_ref': ref}, **self.headers).status_code, 403)

    def inference_permission(self, ref, **changes):
        token = self.http.get('/setup/status', **self.headers).json()['csrf_token']
        value = {'profile_ref': ref, 'connection_ref': str(self.config.pk), 'expected_revision': '0',
                 'action': 'grant', 'confirmed': 'yes', 'allow_canonical_seed': 'yes', **changes}
        return self.http.post('/cabinet/consent/change', urlencode(value),
            content_type='application/x-www-form-urlencoded', HTTP_X_CSRFTOKEN=token, **self.headers)

    def test_native_inference_consent_is_explicit_scoped_short_lived_and_never_sends(self):
        from datetime import timedelta
        from shelf.models import PortableDisclosureGrant
        ref = self.bind()
        calls = self.fake.calls
        response = self.http.get('/cabinet/consent', {'profile_ref': ref, 'connection_ref': str(self.config.pk)}, **self.headers)
        self.assertEqual(response.status_code, 200)
        doc = Document(response.content.decode())
        self.assertFalse(any('checked' in n.attrs for n in doc.tagged('input')))
        self.assertIn('does not authorize API collection', doc.root.text(visible=True))
        self.assertEqual(self.http.post('/cabinet/consent/change', {}, **self.headers).status_code, 403)
        self.assertFalse(PortableDisclosureGrant.objects.exists())
        response = self.inference_permission(ref)
        self.assertEqual(response.status_code, 303)
        grant = PortableDisclosureGrant.objects.get()
        self.assertEqual(grant.purpose, 'inference')
        self.assertEqual(grant.allowed_data_classes, ['canonical_seed'])
        self.assertLessEqual(grant.expires_at, self.clock.wall + timedelta(minutes=5))
        self.assertNotIn('raw_notes', grant.allowed_data_classes)
        self.assertNotIn('inferred_traits', grant.allowed_data_classes)
        self.assertEqual(self.fake.calls, calls)
        self.assertEqual(set(self.http.session.keys()) - {'_session_expiry'}, {'portable_session'})

    def test_native_consent_rejects_forged_purpose_classes_revision_and_recent_owner(self):
        from shelf.models import PortableDisclosureGrant
        ref = self.bind()
        self.assertEqual(self.inference_permission(ref).status_code, 303)
        for forged in ({'purpose': 'catalogue_lookup'}, {'allow_typed_feedback': 'yes'},
                       {'body': 'not-authority'}, {'confirmed': 'no'}, {'allow_raw_notes': 'on'}):
            with self.subTest(forged=list(forged)):
                self.assertEqual(self.inference_permission(ref, **forged).status_code, 422)
        grant = PortableDisclosureGrant.objects.get()
        self.assertEqual(grant.revision, 1)
        response = self.inference_permission(ref, expected_revision='1', action='revoke')
        self.assertEqual(response.status_code, 303)
        grant.refresh_from_db()
        self.assertEqual((grant.state, grant.revision), ('revoked', 2))
        self.assertEqual(self.inference_permission(ref, expected_revision='1', action='revoke').status_code, 409)
        self.assertEqual(self.inference_permission(ref, connection_ref=str(uuid.uuid4()),
            expected_revision='2', action='regrant').status_code, 403)
        self.clock.advance(301)
        self.assertEqual(self.inference_permission(ref, expected_revision='2', action='regrant').status_code, 401)
        grant.refresh_from_db()
        self.assertEqual((grant.state, grant.revision), ('revoked', 2))

    def test_existing_binding_delegates_literal_confirmation_and_keeps_real_store_gate(self):
        # A owns installation provenance/history/idempotency proof. This fixture
        # tests only HTTP delegation; its fake is never normal registration.
        with ga._write_window():
            profile = Profile.objects.create(kind='owner', label='Synthetic existing history', active=True)
            Grant.objects.create(profile=profile, user_id=self.authority.authorize(self.ref).owner_id,
                                 can_view=True, can_select=True, can_mutate=True)
        result = str(uuid.uuid4())
        with patch.object(ga, 'bind_existing_profile', return_value=result, create=True) as bind:
            response = self.http.get('/setup/cabinet', **self.headers)
            self.assertIn('Bind an existing synthetic profile', response.content.decode())
            self.assertEqual(ProfileCabinetState.objects.count(), 0)
            token = self.http.get('/setup/status', **self.headers).json()['csrf_token']
            def post(confirmed):
                return self.http.post('/setup/cabinet/bind', urlencode({'profile_id': profile.pk, 'confirmed': confirmed}),
                    content_type='application/x-www-form-urlencoded', HTTP_X_CSRFTOKEN=token, **self.headers)
            self.assertEqual(post('no').status_code, 403)
            bind.assert_not_called()
            self.assertEqual(post('yes').status_code, 303)
            bind.assert_called_once_with(self.authority, self.ref, profile_id=profile.pk, confirmed=True)
            bind.reset_mock()
            with patch.object(ga, '_fixture_database', return_value=False):
                self.assertEqual(post('yes').status_code, 503)
            bind.assert_not_called()
            self.clock.advance(301)
            self.assertEqual(post('yes').status_code, 401)
            bind.assert_not_called()
        self.assertEqual(Profile.objects.count(), 1)
        self.assertEqual(ProfileCabinetState.objects.count(), 0)
