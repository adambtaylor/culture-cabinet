"""Real owner/profile/CSRF/form/feedback callers; no listener or provider I/O."""
from urllib.parse import urlencode
import uuid

from django.conf import settings
from django.test import RequestFactory, override_settings

from shelf import grant_authority as ga, portable_source_views as views
from shelf.models import FeedbackEvent, FeedbackTarget, PortableConnection, PortableDisclosureGrant
from shelf.portable_taste_inputs import NAMESPACE
from shelf.source_authority import MUSICBRAINZ, current_binding
from .test_grant_authority import RecoveryCase


class NativeSourceViewTests(RecoveryCase):
    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.factory = RequestFactory()
        self.entry, self.mutation = str(uuid.uuid4()), str(uuid.uuid4())

    def request(self, method='GET', fields=None, *, csrf=True):
        if method == 'GET':
            request = self.factory.get('/cabinet/taste/artist', fields or {'profile_ref': self.profile_ref})
        else:
            values = [('csrfmiddlewaretoken', 'a' * 32), *list((fields or {}).items())]
            request = self.factory.post('/cabinet/taste/artist', urlencode(values),
                content_type='application/x-www-form-urlencoded', HTTP_ACCEPT='application/json')
        request.portable_authority = self.authority
        request.session = {'portable_session': self.ref}
        if csrf:
            request.COOKIES[settings.CSRF_COOKIE_NAME] = 'a' * 32
        return request

    def artist_fields(self, **changes):
        value = dict(profile_ref=self.profile_ref, entry_ref=self.entry, mutation_id=self.mutation,
                     base_revision='0', operation='set', title='Synthetic Artist', signal='interest', value='yes')
        value.update(changes)
        return value

    def test_artist_get_is_effect_free_real_post_replay_and_undo_use_exact_events(self):
        with self.controlled_recovery_state('clear'):
            response = views.artist(self.request())
            self.assertEqual(response.status_code, 200)
            self.assertFalse(FeedbackTarget.objects.filter(source_namespace=NAMESPACE).exists())
            self.assertContains(response, 'experienced_ack')
            fields = self.artist_fields()
            self.assertEqual(views.artist(self.request('POST', fields)).status_code, 303)
            self.assertEqual(views.artist(self.request('POST', fields)).status_code, 303)
            self.assertEqual(FeedbackEvent.objects.count(), 1)
            response = views.artist(self.request())
            self.assertContains(response, 'Synthetic Artist')
            event = FeedbackEvent.objects.get()
            reverse = dict(profile_ref=self.profile_ref, entry_ref=self.entry, mutation_id=str(uuid.uuid4()),
                base_revision='1', operation='reverse', signal='interest', reverses_id=str(event.pk))
            self.assertEqual(views.artist(self.request('POST', reverse)).status_code, 303)
            self.assertEqual(FeedbackEvent.objects.count(), 2)

    def test_csrf_closed_fields_recent_auth_and_experience_ack_are_enforced(self):
        with self.controlled_recovery_state('clear'):
            fields = self.artist_fields()
            self.assertEqual(views.artist(self.request('POST', fields, csrf=False)).status_code, 403)
            self.assertEqual(views.artist(self.request('POST', {**fields, 'actor_id': '1'})).status_code, 422)
            self.assertEqual(views.artist(self.request('POST', self.artist_fields(signal='enjoyment', value='4'))).status_code, 422)
            self.assertFalse(FeedbackEvent.objects.exists())
            self.clock.advance(300)
            self.assertEqual(views.artist(self.request('POST', fields)).status_code, 401)
            self.assertFalse(FeedbackTarget.objects.filter(source_namespace=NAMESPACE).exists())

    def test_keyless_configuration_and_separate_source_consent_revoke_regrant(self):
        fields = {'profile_ref': self.profile_ref, 'expected_revision': '0', 'confirmed': 'yes'}
        self.assertEqual(views.musicbrainz(self.request()).status_code, 200)
        self.assertFalse(PortableConnection.objects.filter(adapter=MUSICBRAINZ).exists())
        self.assertEqual(views.musicbrainz_configure(self.request('POST', fields)).status_code, 303)
        self.assertEqual(views.musicbrainz_configure(self.request('POST', fields)).status_code, 303)
        row = PortableConnection.objects.get(adapter=MUSICBRAINZ)
        self.assertEqual(row.credential_generation, 0)
        self.assertFalse(PortableDisclosureGrant.objects.filter(connection=row).exists())
        consent = dict(profile_ref=self.profile_ref, source_connection_ref=str(row.pk), expected_revision='0',
                       action='grant', confirmed='yes', allow_anticipated_reactions='yes')
        self.assertEqual(views.musicbrainz_consent(self.request('POST', consent)).status_code, 303)
        self.assertEqual(views.musicbrainz_consent(self.request('POST', consent)).status_code, 409)
        view = current_binding(self.authority, self.ref, profile_ref=self.profile_ref, connection_ref=str(row.pk))
        self.assertEqual(view.context['purpose'], 'catalogue_lookup')
        self.assertFalse(PortableDisclosureGrant.objects.filter(connection=row, purpose='inference').exists())
        revoked = dict(profile_ref=self.profile_ref, source_connection_ref=str(row.pk), expected_revision='1',
                       action='revoke', confirmed='yes')
        self.assertEqual(views.musicbrainz_consent(self.request('POST', revoked)).status_code, 303)
        self.fault('AUTHORITY_DENIED', current_binding, self.authority, self.ref,
                   profile_ref=self.profile_ref, connection_ref=str(row.pk))
        self.assertEqual(views.musicbrainz_consent(self.request('POST',
            {**consent, 'expected_revision': '2', 'action': 'regrant'})).status_code, 303)

    def test_source_actions_reject_wrong_scope_provider_model_permission_and_json(self):
        fields = {'profile_ref': self.profile_ref, 'expected_revision': '0', 'confirmed': 'yes'}
        self.assertEqual(views.musicbrainz_configure(self.request('POST', {**fields, 'origin': 'https://foreign.invalid'})).status_code, 422)
        self.assertEqual(views.musicbrainz_configure(self.request('POST', {**fields, 'profile_ref': str(uuid.uuid4())})).status_code, 403)
        self.assertEqual(views.musicbrainz_configure(self.request('POST', fields, csrf=False)).status_code, 403)
        consent = dict(profile_ref=self.profile_ref, source_connection_ref=str(self.config.pk), expected_revision='0',
                       action='grant', confirmed='yes', allow_anticipated_reactions='yes')
        self.assertEqual(views.musicbrainz_consent(self.request('POST', consent)).status_code, 503)
        self.assertFalse(PortableConnection.objects.filter(adapter=MUSICBRAINZ).exists())
