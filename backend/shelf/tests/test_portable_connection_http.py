"""Isolated owner HTTP commands with fake generation and canary credentials."""
import json
import sys
from unittest.mock import patch
import uuid

from django.contrib.sessions.models import Session
from django.core.signals import got_request_exception
from django.test import Client, override_settings
from django.views.debug import ExceptionReporter

from shelf.models import PortableConnection, PortableProbeAttempt
from .test_connection_validation import CONFIG, SECRET, ConnectionCase
from .test_portable_authority import PASSWORD


class ConnectionHTTPTests(ConnectionCase):
    def setUp(self):
        super().setUp()
        self.options = self.settings(ROOT_URLCONF='cabinet.portable_urls', PORTABLE_AUTHORITY=self.authority,
            PORTABLE_CONNECTIONS=self.service, SESSION_COOKIE_NAME=self.boundary.session_cookie,
            CSRF_COOKIE_NAME=self.boundary.csrf_cookie,
            MIDDLEWARE=['cabinet.portable_boundary.PortableSecurityMiddleware',
                        'django.contrib.sessions.middleware.SessionMiddleware',
                        'django.middleware.csrf.CsrfViewMiddleware'])
        self.options.enable()
        self.addCleanup(self.options.disable)
        self.http = Client(enforce_csrf_checks=True)
        self.headers = {'HTTP_HOST': self.boundary.origin.removeprefix('http://'), 'HTTP_ORIGIN': self.boundary.origin}
        self.assertEqual(self.post('/setup/login', {'passphrase': PASSWORD}).status_code, 200)
        self.ref = self.http.session['portable_session']

    def token(self, client=None):
        return (client or self.http).get('/setup/status', **self.headers).json()['csrf_token']

    def post(self, path, value, client=None):
        client = client or self.http
        return client.post(path, json.dumps(value), content_type='application/json',
            HTTP_X_CSRFTOKEN=self.token(client), **self.headers)

    def credential(self):
        return self.post('/setup/connections/credential', {'connection_ref': str(self.config.pk),
            'revision': 1, 'secret': SECRET.decode()})

    def status(self):
        return self.http.get('/setup/connections/status',
            {'connection_ref': str(self.config.pk), 'revision': 1}, **self.headers)

    def preview(self):
        return self.post('/setup/connections/preview', {'connection_ref': str(self.config.pk), 'revision': 1})

    def test_authenticated_configuration_is_explicitly_unproven(self):
        response = self.post('/setup/connections/configure', CONFIG)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['validation'], 'UNPROVEN')
        self.assertEqual(PortableConnection.objects.count(), 2)

    def test_unauthenticated_configuration_cannot_write(self):
        response = self.post('/setup/connections/configure', CONFIG, client=Client(enforce_csrf_checks=True))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(PortableConnection.objects.count(), 1)

    def test_transient_secret_is_masked_and_absent_from_session_and_persisted_models(self):
        response = self.credential()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SECRET, response.content)
        self.assertNotIn(SECRET.decode(), json.dumps(list(PortableConnection.objects.values()), default=str))
        self.assertNotIn(SECRET.decode(), json.dumps([s.get_decoded() for s in Session.objects.all()]))
        self.assertEqual(self.vault.resolve(self.ref, self.config.pk, 1), SECRET)

    def test_persistent_storage_or_uploaded_proof_fields_are_rejected(self):
        value = {'connection_ref': str(self.config.pk), 'revision': 1, 'secret': SECRET.decode()}
        for extra in [{'storage': 'plaintext'}, {'proof_kind': 'real_transport_contract'}, {'validated': True}]:
            self.assertEqual(self.post('/setup/connections/credential', {**value, **extra}).status_code, 422)
        self.assertFalse(self.vault._leases)

    def test_preview_exposes_exact_technical_body_with_no_authentication(self):
        self.credential()
        response = self.preview()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['requires_billable_confirmation'])
        self.assertNotIn(SECRET, response.content)
        self.assertNotIn('Authorization', response.json()['body'])
        self.assertEqual(self.fake.calls, 0)

    def test_confirm_requires_true_and_fake_pass_never_enables_run(self):
        self.credential()
        preview = self.preview().json()
        value = {'preview_ref': preview['preview_ref'], 'intent_ref': str(uuid.uuid4()), 'confirm_billable_test': False}
        self.assertEqual(self.post('/setup/connections/confirm', value).status_code, 422)
        value['confirm_billable_test'] = True
        self.assertEqual(self.post('/setup/connections/confirm', value).json()['state'], 'completed')
        status = self.status().json()
        self.assertFalse(status['connection_ready'])
        self.assertFalse(status['run_ready'])
        self.assertEqual(status['proof_kind'], 'isolated_test_fake')

    def test_remove_invalidates_lease_without_erasing_nonsecret_setup(self):
        self.credential()
        response = self.post('/setup/connections/remove', {'connection_ref': str(self.config.pk), 'revision': 1, 'disable': False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.status().json()['outcome'], 'missing_credential')
        self.assertEqual(PortableConnection.objects.count(), 1)

    def test_connection_mutations_require_csrf_and_exact_origin(self):
        body = json.dumps({'connection_ref': str(self.config.pk), 'revision': 1, 'secret': SECRET.decode()})
        response = self.http.post('/setup/connections/credential', body, content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, 403)
        token = self.token()
        response = self.http.post('/setup/connections/credential', body, content_type='application/json',
            HTTP_X_CSRFTOKEN=token, **{**self.headers, 'HTTP_ORIGIN': 'https://foreign.invalid'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.vault._leases)

    def test_duplicate_json_types_and_foreign_refs_fail_safely(self):
        for body in ['{"connection_ref":"a","connection_ref":"b","revision":1,"secret":"synthetic"}',
                     json.dumps({'connection_ref': str(self.config.pk), 'revision': True, 'secret': SECRET.decode()}),
                     json.dumps({'connection_ref': 'invalid', 'revision': 1, 'secret': SECRET.decode()})]:
            response = self.http.post('/setup/connections/credential', body, content_type='application/json',
                HTTP_X_CSRFTOKEN=self.token(), **self.headers)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(SECRET, response.content)

    def test_missing_or_foreign_service_context_is_unavailable(self):
        with override_settings(PORTABLE_CONNECTIONS=None):
            self.assertEqual(self.status().status_code, 503)

    def test_sensitive_changes_need_recent_reauthentication(self):
        self.clock.advance(300)
        self.assertEqual(self.credential().status_code, 401)
        self.assertEqual(self.post('/setup/reauthenticate', {'passphrase': PASSWORD}).status_code, 200)
        self.assertEqual(self.credential().status_code, 200)

    def test_connection_status_polling_does_not_refresh_idle_session(self):
        self.credential()
        for _ in range(2):
            self.clock.advance(600)
            self.assertEqual(self.status().status_code, 200)
        self.clock.advance(600)
        self.assertEqual(self.status().status_code, 401)
        self.assertFalse(self.vault._leases)

    def test_unexpected_credential_error_redacts_json_and_django_diagnostics(self):
        reports = []
        def receive(sender, request, **kwargs):
            reports.append(ExceptionReporter(request, *sys.exc_info()).get_traceback_text())
        got_request_exception.connect(receive, weak=False)
        client = self.http
        client.raise_request_exception = False
        try:
            with patch.object(self.vault, 'replace', side_effect=RuntimeError('SYNTHETIC-CREDENTIAL-DIAGNOSTIC-FAULT')):
                response = self.credential()
        finally:
            got_request_exception.disconnect(receive)
            client.raise_request_exception = True
        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(reports), 1)
        self.assertIn('SYNTHETIC-CREDENTIAL-DIAGNOSTIC-FAULT', reports[0])
        self.assertIn('*********', reports[0])
        self.assertNotIn(SECRET.decode(), reports[0])
        self.assertNotIn(SECRET, response.content)
