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


from .test_portable_feedback_views import NativeFeedbackCase


class NativeArtistSourceTests(NativeFeedbackCase):
    """Real native source bindings and collection; wire replies alone are synthetic."""
    def compose_enhance(self):
        from types import SimpleNamespace
        from shelf.portable_enhance_service import EnhanceService
        self.workflow_clock = SimpleNamespace(now=self.authority.now,
            monotonic=lambda: self.clock.tick, sleep=self.clock.advance)
        return EnhanceService.compose(self.authority, self.service,
            policy_provider=self.policies(), clock=self.workflow_clock)

    def configure(self, artist=True):
        from shelf.source_authority import MUSICBRAINZ_ARTIST
        adapter = MUSICBRAINZ_ARTIST if artist else MUSICBRAINZ
        page = '/cabinet/source/musicbrainz' + ('/artist' if artist else '')
        self.assertEqual(self.post(page + '/configure', dict(profile_ref=self.profile_ref,
            expected_revision='0', confirmed='yes')).status_code, 303)
        return str(PortableConnection.objects.get(adapter=adapter).pk)

    def grant(self, connection, artist=True, **changes):
        fields = dict(profile_ref=self.profile_ref, source_connection_ref=connection,
            expected_revision='0', action='grant', confirmed='yes', allow_anticipated_reactions='yes')
        fields.update(changes)
        return self.post('/cabinet/source/musicbrainz' + ('/artist' if artist else '') + '/consent', fields)

    def policies(self):
        from dataclasses import replace
        from shelf.native_source_policy import build_native_source_policy
        from shelf.source_authority import MUSICBRAINZ_ARTIST, SourceRights
        from .test_source_evidence import policy
        entries = {}
        for source in (MUSICBRAINZ, MUSICBRAINZ_ARTIST):
            fields = policy()
            fields = replace(fields, ref='synthetic:' + source,
                permissions=tuple((key, tuple(use for use in uses if use != 'cache'))
                                  for key, uses in fields.permissions))
            entries[source] = SourceRights(fields, 1, 'synthetic-cc0', 'synthetic-local-disclosure',
                'synthetic-owner-scope', 'CultureCabinet/fixture (test@example.invalid)',
                'synthetic-processor', source=source)
        return build_native_source_policy(self.authority, approved_policies=entries)

    def test_native_artist_configuration_and_consent_are_distinct_from_recording_and_processor(self):
        from unittest.mock import patch
        from shelf.source_authority import MUSICBRAINZ_ARTIST, _connection
        artist, recording = self.configure(), self.configure(False)
        self.assertEqual(self.grant(recording, False).status_code, 303)
        self.assertEqual(self.grant(recording).status_code, 403)
        self.fault('AUTHORITY_DENIED', ga.current_view, self.authority, self.ref,
            profile_ref=self.profile_ref, connection_ref=artist, purpose='catalogue_lookup')
        self.assertEqual(self.grant(artist).status_code, 303)
        with patch.object(self.vault, 'describe', side_effect=AssertionError('No source key lookup')):
            _, binding, _ = _connection(self.authority, self.ref, artist)
            self.assertEqual((binding['path'], binding['credential_mode'], binding['credential_generation']),
                             ('/ws/2/artist/', 'none', 0))
            view = ga.current_view(self.authority, self.ref, profile_ref=self.profile_ref,
                connection_ref=artist, purpose='catalogue_lookup')
        self.assertEqual(view.context['adapter_id'], MUSICBRAINZ_ARTIST)
        self.fault('AUTHORITY_DENIED', ga.current_view, self.authority, self.ref,
            profile_ref=self.profile_ref, connection_ref=artist, purpose='inference')
        self.assertEqual(self.http.get('/cabinet/source/musicbrainz/artist',
            {'profile_ref': self.profile_ref}, **self.headers).status_code, 200)

    def test_normal_artist_recording_collection_shares_limiter_keeps_categories_and_excludes_notes(self):
        from unittest.mock import patch
        from shelf.category_registry import KEYS
        from shelf.disclosure import canonical_bytes, read_json
        from shelf.models import SynthesisRun
        from shelf.source_authority import MUSICBRAINZ_ARTIST
        artist, recording = self.configure(), self.configure(False)
        self.assertEqual(self.grant(artist).status_code, 303)
        self.assertEqual(self.grant(recording, False).status_code, 303)
        self.assertEqual(self.post('/cabinet/taste', self.fields(category='artist',
            title='Synthetic Artist + Duo')).status_code, 303)
        self.assertEqual(self.post('/cabinet/taste', self.fields(category='recording',
            title='RECORDING_IS_NOT_AN_ARTIST')).status_code, 303)
        self.assertEqual(self.post('/cabinet/taste', self.note_fields(category='artist',
            text='NOTE_IS_NOT_A_QUERY')).status_code, 303)
        self.consent(self.profile_ref, classes=['anticipated_reactions'])
        service = self.enhance
        _, collector = service._normal_context(self.ref, self.profile_ref)
        self.assertIs(collector.backends[MUSICBRAINZ].limiter, collector.backends[MUSICBRAINZ_ARTIST].limiter)
        run = service.start(self.ref, profile_ref=self.profile_ref, connection_ref=str(self.config.pk),
            client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
        calls = []
        def reply(operation, *, revalidate, **bounds):
            self.assertTrue(revalidate())
            self.assertEqual(operation.artist_name, 'Synthetic Artist + Duo')
            calls.append(operation.source)
            row = {'id': '81000000-0000-4000-8000-000000000001', 'score': 'NEVER_PROJECT'}
            document = {'artists': [{**row, 'name': 'Actual synthetic artist'}]} if operation.source == MUSICBRAINZ_ARTIST else {
                'recordings': [{**row, 'title': 'Actual synthetic recording'}]}
            return 200, canonical_bytes(document), ()
        with patch('shelf.discovery_transport.musicbrainz_authorized', side_effect=reply):
            for _ in range(3):
                wire = service.preview(self.ref, run_ref=run['run_ref'])['disclosure']
                if wire['kind'] == 'inference':
                    break
                self.assertNotIn('RECORDING_IS_NOT_AN_ARTIST', wire['query'])
                self.assertNotIn('NOTE_IS_NOT_A_QUERY', wire['query'])
                service.confirm(self.ref, run_ref=run['run_ref'], preview_ref=wire['preview_ref'],
                                body_digest=wire['body_digest'], confirmed=True)
        inference = service.preview(self.ref, run_ref=run['run_ref'])['disclosure']
        self.assertEqual(inference['kind'], 'inference')
        submitted = read_json(read_json(inference['body'].encode())['input'].encode())
        self.assertEqual({row['medium'] for row in submitted['candidates']}, {'artist', 'recording'})
        self.assertNotIn('NEVER_PROJECT', inference['body'])
        self.assertNotIn('NOTE_IS_NOT_A_QUERY', inference['body'])
        self.assertEqual(set(calls), {MUSICBRAINZ, MUSICBRAINZ_ARTIST})
        row = SynthesisRun.objects.get(pk=run['run_ref'])
        self.assertEqual(row.attempts, 0)
        outcomes = {item['category']: item for item in row.evidence['outcomes']}
        self.assertEqual(set(outcomes), set(KEYS))
        self.assertEqual((outcomes['artist']['candidate_count'], outcomes['recording']['candidate_count']), (1, 1))
        self.assertTrue(all(value['state'] == 'unavailable' for key, value in outcomes.items()
                            if key not in ('artist', 'recording')))

    def test_artist_preview_operation_binding_revocation_and_original_expiry(self):
        from shelf.category_registry import registry_view
        from shelf.enhance_coordinator import EnhanceCoordinator
        from shelf.enhance_contract import EnhanceFault
        from shelf.source_authority import SourceAuthority
        artist = self.configure()
        self.assertEqual(self.grant(artist).status_code, 303)
        self.assertEqual(self.post('/cabinet/taste', self.fields(category='artist', title='Synthetic Artist')).status_code, 303)
        view = ga.current_view(self.authority, self.ref, profile_ref=self.profile_ref,
                              connection_ref=artist, purpose='catalogue_lookup')
        plan = EnhanceCoordinator(None, now=self.authority.now).plan(view, registry_view(('artist',)),
            run_ref=str(uuid.uuid4()), client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4())).plan
        issuer = SourceAuthority(self.authority, self.ref, profile_ref=self.profile_ref,
                                 connection_ref=artist, policy_provider=self.policies())
        preview = issuer.preview(plan)
        request = issuer._previews[preview['preview_ref']].request
        self.assertEqual((request.source, request.operation, preview['path']),
                         ('musicbrainz_artist', 'artists_by_name', '/ws/2/artist/'))
        issuer.confirm(preview['preview_ref'], plan_digest=preview['plan_digest'],
                       operation_digest=preview['operation_digest'], confirmed=True)
        operation = issuer.operation_for(request, plan, now=self.authority.now())
        permit = issuer.authorize(request, plan, operation, now=self.authority.now())
        self.assertTrue(issuer.revalidate(permit, request, plan, operation, now=self.authority.now()))
        self.assertEqual(self.post('/cabinet/source/musicbrainz/artist/consent', dict(
            profile_ref=self.profile_ref, source_connection_ref=artist, expected_revision='1',
            action='revoke', confirmed='yes')).status_code, 303)
        with self.assertRaises((ga.AuthorityFault, EnhanceFault)):
            issuer.revalidate(permit, request, plan, operation, now=self.authority.now())
        self.assertEqual(self.grant(artist, expected_revision='2', action='regrant').status_code, 303)
        view = ga.current_view(self.authority, self.ref, profile_ref=self.profile_ref,
                              connection_ref=artist, purpose='catalogue_lookup')
        plan = EnhanceCoordinator(None, now=self.authority.now).plan(view, registry_view(('artist',)),
            run_ref=str(uuid.uuid4()), client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4())).plan
        issuer = SourceAuthority(self.authority, self.ref, profile_ref=self.profile_ref,
                                 connection_ref=artist, policy_provider=self.policies())
        preview = issuer.preview(plan)
        request = issuer._previews[preview['preview_ref']].request
        issuer.confirm(preview['preview_ref'], plan_digest=preview['plan_digest'],
                       operation_digest=preview['operation_digest'], confirmed=True)
        operation = issuer.operation_for(request, plan, now=self.authority.now())
        permit = issuer.authorize(request, plan, operation, now=self.authority.now())
        self.clock.advance(301)
        with self.assertRaises((ga.AuthorityFault, EnhanceFault)):
            issuer.revalidate(permit, request, plan, operation, now=self.authority.now())
