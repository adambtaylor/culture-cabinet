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


class NativeMediaSourceTests(NativeFeedbackCase):
    """Actual cold owner/create/chooser and fixed protected source callers."""
    MEDIA_PAGES = {'hardcover': '/cabinet/source/hardcover',
                   'tmdb_movie': '/cabinet/source/tmdb/movie', 'tmdb_tv': '/cabinet/source/tmdb/tv'}

    def configure_media(self, adapter):
        page = self.MEDIA_PAGES[adapter]
        fields = dict(profile_ref=self.profile_ref, expected_revision='0', confirmed='yes')
        self.assertEqual(self.post(page+'/configure', fields).status_code, 303)
        row = PortableConnection.objects.get(adapter=adapter, installation_id=self.boundary.installation_id)
        self.assertEqual(self.post(page+'/configure', fields).status_code, 303)
        self.assertEqual(PortableConnection.objects.filter(adapter=adapter, installation_id=self.boundary.installation_id).count(), 1)
        return row

    def credential_media(self, row, **changes):
        fields = dict(profile_ref=self.profile_ref, connection_ref=str(row.pk), revision='1',
                      secret='SYNTHETIC-NATIVE-SOURCE-TOKEN')
        fields.update(changes)
        return self.post(self.MEDIA_PAGES[row.adapter]+'/credential', fields)

    def consent_media(self, row, **changes):
        fields = dict(profile_ref=self.profile_ref, source_connection_ref=str(row.pk),
            expected_revision='0', action='grant', confirmed='yes', allow_anticipated_reactions='yes')
        fields.update(changes)
        return self.post(self.MEDIA_PAGES[row.adapter]+'/consent', fields)

    def setup_context(self, adapter):
        from unittest.mock import patch
        with patch.object(views, '_native_cabinet_html', wraps=views._native_cabinet_html) as rendered:
            response = self.http.get(self.MEDIA_PAGES[adapter], {'profile_ref':self.profile_ref}, **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'SYNTHETIC-NATIVE-SOURCE-TOKEN', response.content)
        return rendered.call_args.args[1]

    def compose_enhance(self):
        # Select the synthetic trusted startup packet before normal composition;
        # never hot-mutate a running provider or install a second native service.
        self.model_context_enabled = self._testMethodName != 'test_local_only_media_remains_sealed_with_explicit_category_progress'
        return self.media_service(model_context=self.model_context_enabled)

    def media_service(self, *, model_context=True):
        from dataclasses import replace
        from types import SimpleNamespace
        from shelf.native_source_policy import build_native_source_policy
        from shelf.portable_enhance_service import EnhanceService
        from shelf.source_authority import SourceRights
        from .test_source_evidence import policy
        entries = {}
        for adapter in self.MEDIA_PAGES:
            fields = policy()
            if not model_context:
                fields = replace(fields,permissions=tuple((key,tuple(use for use in uses if use != 'model_context'))
                                                          for key,uses in fields.permissions))
            entries[adapter] = SourceRights(fields,1,'synthetic-public-fields','synthetic-local-use',
                'synthetic-personal-application-policy','CultureCabinet/fixture (example.invalid)',
                'synthetic-processor' if model_context else None,source=adapter)
        provider = build_native_source_policy(self.authority,approved_policies=entries)
        clock = SimpleNamespace(now=self.authority.now,monotonic=lambda:self.clock.tick,sleep=self.clock.advance)
        return EnhanceService.compose(self.authority,self.service,policy_provider=provider,clock=clock)

    def run_media_collection(self, *, model_context=True, missing_hardcover=False):
        from unittest.mock import patch
        from shelf.disclosure import canonical_bytes,read_json
        from shelf.models import SynthesisRun
        from shelf.provider_catalogue import BOOK_DETAILS_MINIMAL
        for adapter,category in (('hardcover','book'),('tmdb_movie','movie'),('tmdb_tv','tv')):
            row = self.configure_media(adapter)
            if missing_hardcover and adapter == 'hardcover':
                continue
            self.assertEqual(self.credential_media(row).status_code,303)
            self.assertEqual(self.consent_media(row).status_code,303)
            self.assertEqual(self.post('/cabinet/taste',self.fields(category=category,
                title='Synthetic '+category+' title')).status_code,303)
        self.consent(self.profile_ref,classes=['anticipated_reactions'])
        self.assertEqual(model_context,self.model_context_enabled)
        service = self.enhance
        run = service.start(self.ref,profile_ref=self.profile_ref,connection_ref=str(self.config.pk),
                            client_key=str(uuid.uuid4()),refresh_nonce=str(uuid.uuid4()))
        calls, previews = [], []
        def reply(operation, *, secret, revalidate, **bounds):
            self.assertTrue(revalidate())
            self.assertEqual(bytes(secret),b'SYNTHETIC-NATIVE-SOURCE-TOKEN')
            calls.append((operation.source,operation.operation))
            self.assertIn((operation.body.decode(),operation.query),previews)
            if operation.operation == 'book_search':
                body = {'data':{'search':{'ids':[81001],'error':None}}}
            elif operation.operation == 'book_details':
                self.assertEqual(read_json(operation.body)['query'],BOOK_DETAILS_MINIMAL)
                self.assertEqual(operation.ids,(81001,))
                body = {'data':{'books':[{'id':81001,'title':'Synthetic minimal book','slug':'synthetic-book'}]}}
            else:
                self.assertIn('include_adult=false',operation.query)
                key = 'title' if operation.source == 'tmdb_movie' else 'name'
                body = {'results':[{'id':81002,key:'Synthetic '+operation.source}]}
            self.assertTrue(revalidate())
            return 200,canonical_bytes(body),(('cache-control','max-age=300'),)
        with patch('shelf.discovery_transport.media_authorized',side_effect=reply), \
             patch('socket.socket.connect',side_effect=AssertionError('No provider network')):
            # Three independent source approvals plus a separately derived book
            # detail approval; stop before any inference confirmation/reservation.
            for _ in range(5):
                state = service.status(self.ref,run_ref=run['run_ref'])
                if state['stage'] == 'sealed':
                    break
                wire = service.preview(self.ref,run_ref=run['run_ref'])['disclosure']
                if wire['kind'] == 'inference':
                    break
                previews.append((wire['body'],wire['query']))
                service.confirm(self.ref,run_ref=run['run_ref'],preview_ref=wire['preview_ref'],
                                body_digest=wire['body_digest'],confirmed=True)
        row = SynthesisRun.objects.get(pk=run['run_ref'])
        self.assertEqual(row.attempts,0)
        return service,run,row,calls

    def test_actual_media_search_detail_and_sibling_sources_reach_exact_inference_preview(self):
        from shelf.category_registry import KEYS
        from shelf.disclosure import read_json
        service,run,row,calls = self.run_media_collection()
        self.assertEqual(calls.count(('hardcover','book_search')),1)
        self.assertEqual(calls.count(('hardcover','book_details')),1)
        self.assertEqual(set(calls),{('hardcover','book_search'),('hardcover','book_details'),
                                     ('tmdb_movie','search_movie'),('tmdb_tv','search_tv')})
        wire = service.preview(self.ref,run_ref=run['run_ref'])['disclosure']
        self.assertEqual(wire['kind'],'inference')
        body = read_json(read_json(wire['body'].encode())['input'].encode())
        self.assertEqual({c['medium'] for c in body['candidates']},{'book','movie','tv'})
        self.assertNotIn('SYNTHETIC-NATIVE-SOURCE-TOKEN',wire['body'])
        book = next(c for c in body['candidates'] if c['medium']=='book')
        self.assertEqual(book['title'],'Synthetic minimal book')
        self.assertEqual(set(o['category'] for o in row.evidence['outcomes']),set(KEYS))

    def test_local_only_media_remains_sealed_with_explicit_category_progress(self):
        service,run,row,calls = self.run_media_collection(model_context=False)
        self.assertEqual(service.status(self.ref,run_ref=run['run_ref'])['stage'],'sealed')
        self.assertEqual(row.evidence['phase'],'source_model_context_unavailable')
        self.assertEqual(len(calls),4)
        self.assertTrue(all(v['model_eligible_count']==0 for v in row.evidence['source_use']))
        self.assertEqual(sum(v['collected_count'] for v in row.evidence['source_use']),3)

    def test_missing_hardcover_credential_keeps_other_media_results_and_all_outcomes(self):
        from shelf.disclosure import read_json
        service,run,row,calls = self.run_media_collection(missing_hardcover=True)
        self.assertEqual(set(calls),{('tmdb_movie','search_movie'),('tmdb_tv','search_tv')})
        wire = service.preview(self.ref,run_ref=run['run_ref'])['disclosure']
        body = read_json(read_json(wire['body'].encode())['input'].encode())
        self.assertEqual({c['medium'] for c in body['candidates']},{'movie','tv'})
        book = next(o for o in row.evidence['outcomes'] if o['category']=='book')
        self.assertEqual(book['state'],'unavailable')

    def test_media_setup_without_key_is_actionable_but_never_grants_or_sends(self):
        from unittest.mock import patch
        from shelf.models import PortableProbeAttempt
        from shelf.source_authority import _connection
        with patch('socket.socket.connect', side_effect=AssertionError('No provider I/O')):
            for adapter, page in self.MEDIA_PAGES.items():
                before = PortableConnection.objects.count()
                context = self.setup_context(adapter)
                self.assertEqual(context['source_readiness'], 'not_configured')
                self.assertEqual(PortableConnection.objects.count(), before)
                row = self.configure_media(adapter)
                context = self.setup_context(adapter)
                self.assertEqual(context['source_readiness'], 'credential_required')
                self.assertTrue(context['source_credential_required'])
                self.assertFalse(context['source_credential_present'])
                self.assertEqual(context['source_credential_action'], page+'/credential')
                self.fault('CREDENTIAL_REQUIRED', _connection, self.authority, self.ref, str(row.pk))
                self.assertEqual(self.consent_media(row).status_code, 503)
                self.assertEqual(row.credential_generation, 0)
            self.assertFalse(PortableDisclosureGrant.objects.exists())
            # Only AuthorityCase's preexisting neutral fixture exists.
            self.assertEqual(PortableProbeAttempt.objects.count(), 1)

    def test_media_credential_then_independent_grant_revoke_regrant_and_expiry(self):
        from shelf.source_authority import _connection
        for adapter in self.MEDIA_PAGES:
            row = self.configure_media(adapter)
            self.assertEqual(self.credential_media(row).status_code, 303)
            row.refresh_from_db()
            self.assertEqual(row.credential_generation, 1)
            self.assertEqual(self.setup_context(adapter)['source_readiness'], 'permission_required')
            self.fault('AUTHORITY_DENIED', ga.current_view, self.authority, self.ref,
                profile_ref=self.profile_ref, connection_ref=str(row.pk), purpose='catalogue_lookup')
            self.assertEqual(self.consent_media(row).status_code, 303)
            context = self.setup_context(adapter)
            self.assertEqual(context['source_readiness'], 'ready')
            self.assertEqual(context['source_consent_groups'], [
                'Anticipated '+context['source_category']+' interest'])
            view = current_binding(self.authority, self.ref, profile_ref=self.profile_ref,
                                   connection_ref=str(row.pk))
            self.assertEqual(view.context['adapter_id'], adapter)
            self.fault('AUTHORITY_DENIED', ga.current_view, self.authority, self.ref,
                profile_ref=self.profile_ref, connection_ref=str(row.pk), purpose='inference')
            fields = dict(profile_ref=self.profile_ref,source_connection_ref=str(row.pk),
                          expected_revision='1',action='revoke',confirmed='yes')
            self.assertEqual(self.post(self.MEDIA_PAGES[adapter]+'/consent', fields).status_code, 303)
            self.fault('AUTHORITY_DENIED', current_binding, self.authority, self.ref,
                profile_ref=self.profile_ref, connection_ref=str(row.pk))
            self.assertEqual(self.consent_media(row, expected_revision='2',action='regrant').status_code, 303)
            self.assertTrue(self.setup_context(adapter)['source_consent_active'])
            self.assertEqual(_connection(self.authority,self.ref,str(row.pk))[1]['credential_mode'],
                             'session-memory-bearer')
        self.clock.advance(301)
        for adapter in self.MEDIA_PAGES:
            self.assertEqual(self.setup_context(adapter)['source_readiness'], 'permission_required')

    def test_media_forms_deny_wrong_profile_adapter_revision_csrf_and_unknown_fields(self):
        movie, tv = self.configure_media('tmdb_movie'), self.configure_media('tmdb_tv')
        for changes, code in (({'profile_ref':str(uuid.uuid4())},403),
                              ({'connection_ref':str(tv.pk)},403),({'revision':'2'},409),
                              ({'adapter':'tmdb_tv'},422),({'revision':'01'},422)):
            self.assertEqual(self.credential_media(movie,**changes).status_code, code)
        fields = dict(profile_ref=self.profile_ref,connection_ref=str(movie.pk),revision='1',secret='SYNTHETIC')
        self.assertEqual(self.post(self.MEDIA_PAGES['tmdb_movie']+'/credential',fields,csrf=False).status_code,403)
        duplicates = urlencode([*fields.items(),('revision','1')])
        response = self.http.post(self.MEDIA_PAGES['tmdb_movie']+'/credential',duplicates,
            content_type='application/x-www-form-urlencoded',HTTP_X_CSRFTOKEN=self.csrf(),**self.headers)
        self.assertEqual(response.status_code,422)
        movie.refresh_from_db(); tv.refresh_from_db()
        self.assertEqual((movie.credential_generation,tv.credential_generation),(0,0))
        self.assertEqual(self.credential_media(movie).status_code,303)
        self.assertEqual(self.consent_media(movie).status_code,303)
        self.assertEqual(self.consent_media(tv,source_connection_ref=str(movie.pk)).status_code,403)
        self.fault('CREDENTIAL_REQUIRED',current_binding,self.authority,self.ref,
                   profile_ref=self.profile_ref,connection_ref=str(tv.pk))
        self.clock.advance(301)
        self.assertEqual(self.credential_media(movie).status_code,401)
        self.assertEqual(self.consent_media(movie).status_code,401)
        self.assertEqual(self.post(self.MEDIA_PAGES['hardcover']+'/configure',dict(
            profile_ref=self.profile_ref,expected_revision='0',confirmed='yes')).status_code,401)

    def test_source_presentation_rejects_substituted_secret_action_or_descriptor(self):
        from shelf.enhancement_views import _source_setup_display
        base = dict(profile_ref=self.profile_ref,source_adapter='hardcover')
        self.assertEqual(_source_setup_display(base)['source_credential_action'],
                         '/cabinet/source/hardcover/credential')
        for changed in ({'source_credential_action':'https://foreign.invalid/secret'},
                        {'source_category':'movie'},{'source_adapter':'openai_responses'},
                        {'source_credential_required':False},{'source_credential_present':'yes'},
                        {'source_readiness':'provider_approved'}):
            self.fault('CONTRACT_INVALID',_source_setup_display,{**base,**changed})

    def test_missing_media_token_cannot_block_existing_grant_revocation(self):
        from shelf.enhance_contract import EnhanceFault
        from shelf.source_authority import SourceAuthority, _connection
        from shelf.category_registry import registry_view
        from shelf.enhance_coordinator import EnhanceCoordinator
        for adapter in self.MEDIA_PAGES:
            row = self.configure_media(adapter)
            self.assertEqual(self.credential_media(row).status_code,303)
            self.assertEqual(self.consent_media(row).status_code,303)
            view = current_binding(self.authority,self.ref,profile_ref=self.profile_ref,connection_ref=str(row.pk))
            plan = EnhanceCoordinator(None,now=self.authority.now).plan(view,registry_view(),
                run_ref=str(uuid.uuid4()),client_key=str(uuid.uuid4()),refresh_nonce=str(uuid.uuid4())).plan
            self.vault.remove(self.ref,row.pk,row.config_revision)
            self.assertEqual(self.setup_context(adapter)['source_readiness'],'credential_required')
            self.assertEqual(self.consent_media(row).status_code,503)
            issuer = SourceAuthority(self.authority,self.ref,profile_ref=self.profile_ref,connection_ref=str(row.pk))
            self.fault('CREDENTIAL_REQUIRED',issuer.preview,plan)
            fields = dict(profile_ref=self.profile_ref,source_connection_ref=str(row.pk),
                          expected_revision='1',action='revoke',confirmed='yes')
            self.assertEqual(self.post(self.MEDIA_PAGES[adapter]+'/consent',fields).status_code,303)
            # Exact already-revoked revision remains idempotent, with no new grant.
            fields['expected_revision']='2'
            self.assertEqual(self.post(self.MEDIA_PAGES[adapter]+'/consent',fields).status_code,303)
            grant = PortableDisclosureGrant.objects.get(connection=row,purpose='catalogue_lookup')
            self.assertEqual((grant.state,grant.revision),('revoked',2))
            self.fault('CREDENTIAL_REQUIRED',_connection,self.authority,self.ref,str(row.pk))
            self.fault('CREDENTIAL_REQUIRED',current_binding,self.authority,self.ref,
                       profile_ref=self.profile_ref,connection_ref=str(row.pk))
            self.assertEqual(self.consent_media(row,expected_revision='2',action='regrant').status_code,503)
            self.assertEqual(self.consent_media(row,expected_revision='2').status_code,503)

    def test_expired_media_token_revocation_keeps_owner_source_revision_csrf_and_recent_auth_guards(self):
        from datetime import timedelta
        from shelf.models import PortableInstallation
        foreign = PortableInstallation.objects.create(id=uuid.uuid4(),root_identity=uuid.uuid4().hex*2,
                                                       process_generation=uuid.uuid4())
        foreign_row = PortableConnection.objects.create(id=uuid.uuid4(),installation=foreign,
                                                        adapter='hardcover',model='',reasoning='')
        self.fault('AUTHORITY_DENIED',ga.change_grant,self.authority,self.ref,
            profile_ref=self.profile_ref,connection_ref=str(foreign_row.pk),expected_revision=1,
            purpose='catalogue_lookup',allowed_data_classes=[],action='revoke',
            expires_at=self.authority.now()+timedelta(minutes=5))
        for adapter in self.MEDIA_PAGES:
            row = self.configure_media(adapter)
            self.assertEqual(self.credential_media(row).status_code,303)
            self.assertEqual(self.consent_media(row).status_code,303)
            # Only this owned synthetic lease expires; the real session and
            # production credential/session limits are not changed or extended.
            self.vault._leases[row.pk].expires = self.clock.tick
            fields = dict(profile_ref=self.profile_ref,source_connection_ref=str(row.pk),
                          expected_revision='1',action='revoke',confirmed='yes')
            page = self.MEDIA_PAGES[adapter]
            self.assertEqual(self.post(page+'/consent',{**fields,'profile_ref':str(uuid.uuid4())}).status_code,403)
            self.assertEqual(self.post(page+'/consent',{**fields,'source_connection_ref':str(foreign_row.pk)}).status_code,403)
            self.assertEqual(self.post(page+'/consent',{**fields,'expected_revision':'0'}).status_code,409)
            self.assertEqual(self.post(page+'/consent',fields,csrf=False).status_code,403)
            other_page = next(value for key,value in self.MEDIA_PAGES.items() if key != adapter)
            self.assertEqual(self.post(other_page+'/consent',fields).status_code,403)
            duplicate = urlencode([*fields.items(),('expected_revision','1')])
            response = self.http.post(page+'/consent',duplicate,content_type='application/x-www-form-urlencoded',
                                      HTTP_X_CSRFTOKEN=self.csrf(),**self.headers)
            self.assertEqual(response.status_code,422)
            grant = PortableDisclosureGrant.objects.get(connection=row,purpose='catalogue_lookup')
            self.assertEqual((grant.state,grant.revision),('active',1))
            self.assertEqual(self.post(page+'/consent',fields).status_code,303)
            grant.refresh_from_db()
            self.assertEqual((grant.state,grant.revision),('revoked',2))
        row = self.configure_media('hardcover')
        self.clock.advance(301)
        fields = dict(profile_ref=self.profile_ref,source_connection_ref=str(row.pk),
                      expected_revision='2',action='revoke',confirmed='yes')
        self.assertEqual(self.post(self.MEDIA_PAGES['hardcover']+'/consent',fields).status_code,401)
