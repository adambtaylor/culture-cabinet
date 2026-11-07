"""Real authenticated browser forms for reviewed qualitative observation intake."""
import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from shelf.authority import SELECTION_KEY
from shelf.models import AuthoredDiscoveryState, Grant, Profile
from shelf.tests.test_authored_observations import observation
from shelf.tests.test_taste_funnel import BrowserFormClient


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class ObservationViewTests(TestCase):
    base = '/profiles/1/taste/observations/'

    def setUp(self):
        self.actor = get_user_model().objects.create_user(pk=1, username='SYNTHETIC-observation-ui')
        self.profile = Profile.objects.create(pk=1, kind='owner', active=True, label='SYNTHETIC owner')
        self.grant = Grant.objects.create(user=self.actor, profile=self.profile, can_view=True, can_select=True)
        self.client = BrowserFormClient(enforce_csrf_checks=True)
        self.client.force_login(self.actor)
        session = self.client.session
        session[SELECTION_KEY] = self.profile.pk
        session.save()
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('No source access permitted')))

    def payload(self, page=None, rows=None):
        page = page or self.client.get(self.base)
        return dict(mode='household', revision=page.context['revision'], nonce=page.context['nonce'],
                    packet=json.dumps({'observations': rows or [observation()]}),
                    csrfmiddlewaretoken=self.client.cookies[settings.CSRF_COOKIE_NAME].value)

    def review(self, rows=None):
        payload = self.payload(rows=rows)
        response = self.client.post(self.base + 'review/', payload)
        self.assertEqual(response.status_code, 200)
        return response, payload

    def confirmation(self, rows=None):
        response, payload = self.review(rows)
        return dict(payload, confirmed='on', review_token=response.context['review_token'])

    def test_get_and_preview_are_inert_and_escape_preserved_source_wording(self):
        page = self.client.get(self.base)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '0 stored observations')
        row = observation(source_text='  <script>source</script>\n  ')
        response, _ = self.review([row])
        self.assertContains(response, 'Pending review — not stored')
        self.assertContains(response, '&lt;script&gt;source&lt;/script&gt;')
        self.assertNotContains(response, '<script>source</script>')
        self.assertEqual(response.context['pending'][0]['source_text'], row['source_text'])
        self.assertEqual(response.context['stored_count'], 0)
        self.assertEqual(response.context['pending_eligible_count'], 1)
        self.assertEqual(response.context['pending_excluded_count'], 0)
        for text in ('user:1', 'synthetic-message-1', 'reported_experience', 'Unknown', 'a' * 64):
            self.assertContains(response, text)
        self.assertFalse(AuthoredDiscoveryState.objects.exists())
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')

    def test_confirm_import_replay_and_eligible_origin_counts(self):
        assistant = observation(id='assistant')
        assistant['provenance']['actor_origin'] = 'assistant'
        unknown = observation(id='unknown', identity_status='unresolved', uncertainty='Exact title unknown')
        unknown['provenance']['actor_origin'] = 'unknown'
        payload = self.confirmation([observation(), assistant, unknown])
        for _ in range(2):
            self.assertEqual(self.client.post(self.base + 'import/', payload).status_code, 302)
        state = AuthoredDiscoveryState.objects.get()
        self.assertEqual(state.revision, 1)
        self.assertEqual(len(state.data['observations']), 3)
        response = self.client.get(self.base)
        self.assertEqual(response.context['stored_count'], 3)
        self.assertEqual(response.context['eligible_count'], 1)
        self.assertEqual(response.context['excluded_count'], 2)
        self.assertEqual(response.context['assistant_count'], 1)
        self.assertContains(response, 'Exact title unknown')
        self.assertContains(response, 'The internal weekly trigger may queue a pending-review snapshot; it never executes inference.')
        self.assertNotContains(response, 'name="rating"')

    def test_confirmation_requires_exact_signed_review_and_checkbox(self):
        payload = self.confirmation()
        for change, status in (({'review_token': ''}, 400), ({'confirmed': ''}, 400),
                               ({'review_token': 'forged'}, 409),
                               ({'packet': json.dumps({'observations': [observation(reaction='like')]})}, 409),
                               ({'revision': 1}, 409), ({'nonce': 'another'}, 409)):
            with self.subTest(change=change):
                self.assertEqual(self.client.post(self.base + 'import/', dict(payload, **change)).status_code, status)
        self.assertFalse(AuthoredDiscoveryState.objects.exists())

    def test_expired_review_and_stale_preview_do_not_store(self):
        payload = self.confirmation()
        with patch('django.core.signing.time.time', return_value=0):
            expired = self.confirmation()
        self.assertEqual(self.client.post(self.base + 'import/', expired).status_code, 409)
        self.assertFalse(AuthoredDiscoveryState.objects.exists())
        self.assertEqual(self.client.post(self.base + 'import/', payload).status_code, 302)
        self.assertEqual(self.client.post(self.base + 'review/', {k: v for k, v in payload.items()
            if k not in ('confirmed', 'review_token')}).status_code, 409)
        self.assertEqual(AuthoredDiscoveryState.objects.get().revision, 1)

    def test_csrf_unknown_duplicate_oversized_unicode_and_sample_forms_rejected(self):
        payload = self.payload()
        variants = [({'csrfmiddlewaretoken': ''}, 403), ({'extra': 'value'}, 400),
                    ({'packet': ['one', 'two']}, 400), ({'mode': 'sample'}, 400),
                    ({'packet': 'x' * (128 * 1024 + 1)}, 400),
                    ({'packet': 'é' * (70 * 1024)}, 400), ({'packet': '{"observations":[]}'}, 409)]
        for change, status in variants:
            with self.subTest(change=str(change)[:80]):
                self.assertEqual(self.client.post(self.base + 'review/', dict(payload, **change)).status_code, status)
        upload_client = Client()
        upload_client.force_login(self.actor)
        session = upload_client.session
        session[SELECTION_KEY] = 1
        session.save()
        uploaded = dict(payload, upload=SimpleUploadedFile('packet.json', b'{}'))
        self.assertEqual(upload_client.post(self.base + 'review/', uploaded).status_code, 400)
        self.assertFalse(AuthoredDiscoveryState.objects.exists())

    def test_current_grant_and_persisted_selection_required_on_every_route(self):
        payload = self.confirmation()
        for field in ('can_select', 'can_view'):
            setattr(self.grant, field, False)
            self.grant.save()
            self.assertEqual(self.client.get(self.base).status_code, 404)
            self.assertEqual(self.client.post(self.base + 'review/', self.payload_from(payload)).status_code, 404)
            self.assertEqual(self.client.post(self.base + 'import/', payload).status_code, 404)
            setattr(self.grant, field, True)
            self.grant.save()
        session = self.client.session
        session.pop(SELECTION_KEY)
        session.save()
        self.assertEqual(self.client.get(self.base).status_code, 404)
        self.assertEqual(self.client.post(self.base + 'import/', payload).status_code, 404)
        self.assertFalse(AuthoredDiscoveryState.objects.exists())

    @staticmethod
    def payload_from(payload):
        return {key: value for key, value in payload.items() if key not in ('review_token', 'confirmed')}

    def test_logged_out_and_other_profile_requests_do_not_bypass_owner_authority(self):
        payload = self.confirmation()
        self.assertEqual(self.client.get('/profiles/2/taste/observations/').status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.base).status_code, 302)
        self.assertIn(self.client.post(self.base + 'import/', payload).status_code, (302, 403))
        self.assertFalse(AuthoredDiscoveryState.objects.exists())

    def test_method_boundaries_and_unknown_get_filters(self):
        self.assertEqual(self.client.get(self.base + '?mode=sample').status_code, 400)
        self.assertEqual(self.client.get(self.base + 'review/').status_code, 405)
        self.assertEqual(self.client.get(self.base + 'import/').status_code, 405)
