"""Normal Enhance HTTP collection must respect display-only source rights."""
from urllib.parse import urlencode
from unittest.mock import patch
import uuid

from shelf.category_registry import KEYS
from shelf.disclosure import canonical_bytes, read_json
from shelf.models import PortableCabinetManifest, SynthesisRun
from shelf.provider_catalogue import BOOK_DETAILS_MINIMAL
from .test_portable_feedback_views import NativeFeedbackCase
from . import test_portable_source_views as source_fixtures


class NativeSourceModelDenialTests(NativeFeedbackCase):
    # Reuse the accepted source setup without inheriting its service-only tests.
    MEDIA_PAGES = source_fixtures.NativeMediaSourceTests.MEDIA_PAGES
    configure_media = source_fixtures.NativeMediaSourceTests.configure_media
    credential_media = source_fixtures.NativeMediaSourceTests.credential_media
    consent_media = source_fixtures.NativeMediaSourceTests.consent_media
    media_service = source_fixtures.NativeMediaSourceTests.media_service

    def compose_enhance(self):
        return self.media_service(model_context=False)

    def enhance_post(self, path, fields):
        token = self.csrf()
        response = self.http.post(path, urlencode({'csrfmiddlewaretoken': token, **fields}),
            content_type='application/x-www-form-urlencoded',
            HTTP_X_CSRFTOKEN=token, HTTP_ACCEPT='application/json', **self.headers)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_display_only_collection_seals_without_inference_or_replay_send(self):
        for adapter, category in (('hardcover', 'book'), ('tmdb_movie', 'movie'), ('tmdb_tv', 'tv')):
            row = self.configure_media(adapter)
            self.assertEqual(self.credential_media(row).status_code, 303)
            self.assertEqual(self.consent_media(row).status_code, 303)
            self.assertEqual(self.post('/cabinet/taste', self.fields(category=category,
                title='Synthetic '+category+' title')).status_code, 303)
        # Keep ordinary inference-authority admission, even though these source
        # rights must stop the run before any inference preview or reservation.
        self.consent(self.profile_ref, classes=['anticipated_reactions'])
        self.assertTrue(self.enhance._normal)
        self.assertIsNone(self.enhance._pipeline)
        start_fields = dict(profile_ref=self.profile_ref, connection_ref=str(self.config.pk),
            client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
        calls, previews = [], []

        def reply(operation, *, secret, revalidate, **bounds):
            self.assertTrue(revalidate())
            self.assertEqual(bytes(secret), b'SYNTHETIC-NATIVE-SOURCE-TOKEN')
            self.assertIn((operation.body.decode(), operation.query), previews)
            calls.append((operation.source, operation.operation))
            if operation.operation == 'book_search':
                body = {'data': {'search': {'ids': [81001], 'error': None}}}
            elif operation.operation == 'book_details':
                self.assertEqual(read_json(operation.body)['query'], BOOK_DETAILS_MINIMAL)
                self.assertEqual(operation.ids, (81001,))
                body = {'data': {'books': [{'id': 81001, 'title': 'Synthetic minimal book',
                                           'slug': 'synthetic-book'}]}}
            else:
                self.assertIn('include_adult=false', operation.query)
                key = 'title' if operation.source == 'tmdb_movie' else 'name'
                body = {'results': [{'id': 81002, key: 'Synthetic '+operation.source}]}
            self.assertTrue(revalidate())
            return 200, canonical_bytes(body), (('cache-control', 'max-age=300'),)

        # Transport replies are synthetic; reservation/dispatch mocks are only
        # tripwires and cannot manufacture a successful authority or result.
        with patch('shelf.discovery_transport.media_authorized', side_effect=reply), \
             patch('socket.socket.connect', side_effect=AssertionError('No provider network')), \
             patch('shelf.portable_enhance_service._DurablePorts.reserve_attempt',
                   side_effect=AssertionError('No inference reservation')) as reserve, \
             patch.object(self.enhance.personal_transport, 'dispatch',
                          side_effect=AssertionError('No personal dispatch')) as dispatch:
            run = self.enhance_post('/enhance/start', start_fields)
            path = '/enhance/runs/'+run['run_ref']
            state = run
            for _ in range(4):
                wire = self.enhance_post(path+'/preview', {})['disclosure']
                self.assertNotEqual(wire['kind'], 'inference')
                previews.append((wire['body'], wire['query']))
                confirmation = dict(preview_ref=wire['preview_ref'],
                    body_digest=wire['body_digest'], confirmed='yes')
                state = self.enhance_post(path+'/confirm', confirmation)
                if state['stage'] == 'sealed':
                    break
            self.assertEqual(state['stage'], 'sealed')
            self.assertEqual(state['code'], 'AUTHORITY_DENIED')
            self.assertEqual(state['dependency'], 'source_model_context_unavailable')
            self.assertEqual(state['allowed_actions'], ['status', 'cancel'])
            self.assertEqual(tuple(v['category'] for v in state['category_outcomes']), KEYS)
            self.assertEqual(tuple(v['category'] for v in state['source_use']), KEYS)
            self.assertEqual(sum(v['collected_count'] for v in state['source_use']), 3)
            self.assertTrue(all(v['model_eligible_count'] == 0 for v in state['source_use']))
            row = SynthesisRun.objects.get(pk=run['run_ref'])
            self.assertEqual(row.evidence['phase'], 'source_model_context_unavailable')
            self.assertEqual(row.state, 'queued')
            self.assertEqual(row.attempts, 0)
            self.assertEqual(len(row.collection_digest), 64)
            sealed_digest = row.collection_digest
            # Each replay traverses the actual HTTP caller and reauthorizes; a
            # sealed run must retain its collection rather than sending again.
            self.assertEqual(self.enhance_post(path+'/confirm', confirmation), state)
            self.assertEqual(self.enhance_post('/enhance/start', start_fields), state)
            for _ in range(2):
                response = self.http.get(path+'/status', HTTP_ACCEPT='application/json', **self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), state)
            token = self.csrf()
            denied_preview = self.http.post(path+'/preview', urlencode({'csrfmiddlewaretoken': token}),
                content_type='application/x-www-form-urlencoded', HTTP_X_CSRFTOKEN=token,
                HTTP_ACCEPT='application/json', **self.headers)
            self.assertEqual(denied_preview.status_code, 403)
            row.refresh_from_db()
            self.assertEqual(row.collection_digest, sealed_digest)
            self.assertEqual(row.attempts, 0)
            self.assertEqual(SynthesisRun.objects.count(), 1)
            self.assertEqual(PortableCabinetManifest.objects.count(), 0)
            self.assertCountEqual(calls, [('hardcover', 'book_search'), ('hardcover', 'book_details'),
                ('tmdb_movie', 'search_movie'), ('tmdb_tv', 'search_tv')])
            reserve.assert_not_called()
            dispatch.assert_not_called()
