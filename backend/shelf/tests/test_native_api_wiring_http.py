"""Normal native HTTP callers; only provider transport replies are synthetic."""
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import patch
import uuid

from shelf.category_registry import KEYS
from shelf.disclosure import canonical_bytes, read_json
from shelf.models import PortableConnection, SynthesisRun
from shelf.native_source_policy import build_native_source_policy
from shelf.portable_enhance_service import EnhanceService
from shelf.source_authority import SourceRights
from .test_portable_feedback_views import NativeFeedbackCase
from .test_source_evidence import policy


class NativeApiWiringHttpTests(NativeFeedbackCase):
    PAGES = {
        'musicbrainz_recording': '/cabinet/source/musicbrainz',
        'musicbrainz_artist': '/cabinet/source/musicbrainz/artist',
        'tmdb_movie': '/cabinet/source/tmdb/movie',
        'tmdb_tv': '/cabinet/source/tmdb/tv',
    }
    CATEGORIES = {'musicbrainz_recording': 'recording', 'musicbrainz_artist': 'artist',
                  'tmdb_movie': 'movie', 'tmdb_tv': 'tv'}
    PATHS = {'musicbrainz_recording': '/ws/2/recording/', 'musicbrainz_artist': '/ws/2/artist/',
             'tmdb_movie': '/3/search/movie', 'tmdb_tv': '/3/search/tv'}

    def compose_enhance(self):
        entries = {}
        for source in self.PAGES:
            fields = policy()
            fields = replace(fields, ref='synthetic:' + source,
                permissions=tuple((key, tuple(use for use in uses if use != 'cache'))
                                  for key, uses in fields.permissions))
            entries[source] = SourceRights(fields, 1, 'synthetic-public-fields',
                'synthetic-local-use', 'synthetic-owner-scope',
                'CultureCabinet/fixture (test@example.invalid)', 'synthetic-processor', source=source)
        clock = SimpleNamespace(now=self.authority.now, monotonic=lambda: self.clock.tick,
                                sleep=self.clock.advance)
        return EnhanceService.compose(self.authority, self.service,
            policy_provider=build_native_source_policy(self.authority, approved_policies=entries), clock=clock)

    def setUp(self):
        super().setUp()
        # Fail at the first personal inference reservation/dispatch, even if an
        # accidental route transition would otherwise use the fixture adapter.
        for name in ('reserve_attempt', 'dispatch'):
            self.enterContext(patch('shelf.portable_enhance_service._DurablePorts.' + name,
                                    side_effect=AssertionError('No personal inference')))
        self.rows, self.previews, self.calls, self.limiters = {}, {}, [], {}

    def configure_sources(self, *, missing=None, revoked=None):
        for source, page in self.PAGES.items():
            self.assertEqual(self.post(page + '/configure', dict(profile_ref=self.profile_ref,
                expected_revision='0', confirmed='yes')).status_code, 303)
            row = PortableConnection.objects.get(adapter=source)
            self.rows[source] = row
            if source.startswith('tmdb') and source != missing:
                self.assertEqual(self.post(page + '/credential', dict(profile_ref=self.profile_ref,
                    connection_ref=str(row.pk), revision='1', secret='SYNTHETIC-' + source)).status_code, 303)
            if source != missing:
                self.assertEqual(self.grant(source).status_code, 303)
            if source == revoked:
                self.assertEqual(self.grant(source, action='revoke', revision='1').status_code, 303)
        self.assertEqual(self.post('/cabinet/taste/artist', dict(profile_ref=self.profile_ref,
            entry_ref=str(uuid.uuid4()), mutation_id=str(uuid.uuid4()), base_revision='0',
            operation='set', signal='interest', title='Synthetic Artist + Duo', value='yes')).status_code, 303)
        for category in ('movie', 'tv'):
            self.assertEqual(self.post('/cabinet/taste', self.fields(category=category,
                title='Synthetic ' + category)).status_code, 303)
        self.assertEqual(self.post('/cabinet/consent/change', dict(profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), expected_revision='0', action='grant',
            confirmed='yes', allow_anticipated_reactions='yes')).status_code, 303)

    def grant(self, source, *, action='grant', revision='0'):
        return self.post(self.PAGES[source] + '/consent', dict(profile_ref=self.profile_ref,
            source_connection_ref=str(self.rows[source].pk), expected_revision=revision,
            action=action, confirmed='yes', **({'allow_anticipated_reactions': 'yes'} if action != 'revoke' else {})))

    def command(self, path, fields, *, csrf=True):
        token = self.csrf() if csrf else None
        values = {**fields, **({'csrfmiddlewaretoken': token} if csrf else {})}
        return self.http.post(path, urlencode(values), content_type='application/x-www-form-urlencoded',
            HTTP_ACCEPT='application/json',
            **({'HTTP_X_CSRFTOKEN': token} if csrf else {}), **self.headers)

    def start_run(self):
        fields = dict(profile_ref=self.profile_ref, connection_ref=str(self.config.pk),
                      client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
        self.assertEqual(self.command('/enhance/start', fields, csrf=False).status_code, 403)
        result = self.command('/enhance/start', fields)
        self.assertEqual(result.status_code, 200, result.content)
        return '/enhance/runs/' + result.json()['run_ref']

    def get_status(self, path):
        response = self.http.get(path + '/status', HTTP_ACCEPT='application/json', **self.headers)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def preview(self, path):
        response = self.command(path + '/preview', {})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['disclosure']

    @staticmethod
    def confirmation(wire):
        return dict(preview_ref=wire['preview_ref'], body_digest=wire['body_digest'], confirmed='yes')

    def wire_reply(self, operation, *, revalidate, limiter, secret=None, **bounds):
        self.assertTrue(revalidate())
        source = operation.source
        wire = self.previews[source]
        material = operation.material
        for key in ('method', 'origin', 'path', 'query'):
            self.assertEqual(material[key], wire[key])
        self.assertEqual(operation.body, wire['body'].encode())
        self.assertEqual(operation.operation_digest, wire['operation_digest'])
        self.assertNotIn(self.profile_ref, operation.query)
        self.assertNotIn(str(self.config.pk), operation.query)
        self.assertNotIn(source, self.calls)
        self.calls.append(source)
        self.limiters[source] = limiter
        if source.startswith('musicbrainz'):
            self.assertIsNone(secret)
            self.assertEqual(material['headers'], wire['headers'])
            key = 'recordings' if source.endswith('recording') else 'artists'
            title_key = 'title' if key == 'recordings' else 'name'
            body = {key: [{'id': '81000000-0000-4000-8000-000000000001',
                           title_key: 'Synthetic ' + source, 'score': 'UNPROJECTED'}]}
        else:
            self.assertEqual(bytes(secret), ('SYNTHETIC-' + source).encode())
            title_key = 'title' if source == 'tmdb_movie' else 'name'
            body = {'results': [{'id': 81002, title_key: 'Synthetic ' + source}]}
        self.assertTrue(revalidate())
        return 200, canonical_bytes(body), ()

    def collect(self, path):
        for _ in range(5):
            wire = self.preview(path)
            if wire['kind'] == 'inference':
                return wire
            source = next(source for source in self.rows if self.PATHS[source] == wire['path'])
            self.assertNotIn(source, self.previews)
            self.previews[source] = wire
            response = self.command(path + '/confirm', self.confirmation(wire))
            self.assertEqual(response.status_code, 200, response.content)
        self.fail('Mixed source collection did not reach inference preview')

    def assert_collection(self, path, expected):
        row = SynthesisRun.objects.get(pk=path.rsplit('/', 1)[-1])
        self.assertEqual(row.attempts, 0)
        self.assertIsNone(row.transport_request_id)
        self.assertTrue(row.collection_digest)
        # Inspect actual collector output, without substituting its authority or
        # seal: every source retains its own plan and observation lineage.
        collected = self.enhance._memory[str(row.pk)]['work'].collected
        seal = collected.seal
        seal.verify(collected.planned.plan, collected.planned.registry, now=self.authority.now())
        self.assertEqual(seal.collection_digest, row.collection_digest)
        plans = dict(collected.planned.source_plans)
        self.assertEqual(set(plans), {str(self.rows[source].pk) for source in expected})
        for source in expected:
            selected = plans[str(self.rows[source].pk)]
            self.assertEqual(selected.body_digest, self.previews[source]['plan_digest'])
            record, = (record for record in seal.records
                       if record['candidate']['category'] == self.CATEGORIES[source])
            roots = record['candidate']['source_roots']
            self.assertTrue(roots)
            self.assertTrue(set(roots) <= set(seal.observations))
            self.assertTrue(all(field['observation']['ref'] in roots for field in record['fields']))
        outcomes = {value['category']: value for value in row.evidence['outcomes']}
        uses = {value['category']: value for value in row.evidence['source_use']}
        self.assertEqual(set(outcomes), set(KEYS))
        self.assertEqual(set(uses), set(KEYS))
        for source, category in self.CATEGORIES.items():
            count = int(source in expected)
            self.assertEqual(outcomes[category]['candidate_count'], count)
            self.assertEqual(uses[category]['collected_count'], count)
            self.assertEqual(uses[category]['model_eligible_count'], count)
            if not count:
                self.assertEqual(outcomes[category]['state'], 'unavailable')
        for _ in range(2):
            self.assertEqual(set(v['category'] for v in self.get_status(path)['category_outcomes']), set(KEYS))
        return row

    def test_mixed_normal_http_approvals_wire_lineage_seal_and_replay(self):
        self.configure_sources()
        with patch('shelf.discovery_transport.musicbrainz_authorized', side_effect=self.wire_reply), \
             patch('shelf.discovery_transport.media_authorized', side_effect=self.wire_reply):
            path = self.start_run()
            wire = self.collect(path)
            self.assertEqual(set(self.calls), set(self.PAGES))
            self.assertIs(self.limiters['musicbrainz_recording'], self.limiters['musicbrainz_artist'])
            self.assertIs(self.limiters['tmdb_movie'], self.limiters['tmdb_tv'])
            body = read_json(read_json(wire['body'].encode())['input'].encode())
            self.assertEqual({c['medium'] for c in body['candidates']}, set(self.CATEGORIES.values()))
            self.assertNotIn('UNPROJECTED', wire['body'])
            self.assertNotIn('SYNTHETIC-tmdb', wire['body'])
            self.assert_collection(path, set(self.PAGES))
            for source_wire in self.previews.values():
                replay = self.command(path + '/confirm', self.confirmation(source_wire))
                self.assertEqual(replay.status_code, 409)
            self.assertEqual(len(self.calls), 4)

    def test_missing_credential_and_revoked_binding_keep_permitted_siblings(self):
        self.configure_sources(missing='tmdb_movie', revoked='musicbrainz_artist')
        with patch('shelf.discovery_transport.musicbrainz_authorized', side_effect=self.wire_reply), \
             patch('shelf.discovery_transport.media_authorized', side_effect=self.wire_reply):
            path = self.start_run()
            self.collect(path)
            expected = {'musicbrainz_recording', 'tmdb_tv'}
            self.assertEqual(set(self.calls), expected)
            self.assert_collection(path, expected)

    def test_replaced_native_credential_invalidates_pending_run_before_any_send(self):
        self.configure_sources()
        with patch('shelf.discovery_transport.musicbrainz_authorized', side_effect=AssertionError('No stale send')), \
             patch('shelf.discovery_transport.media_authorized', side_effect=AssertionError('No stale send')):
            path = self.start_run()
            wire = self.preview(path)
            row = self.rows['tmdb_tv']
            self.assertEqual(self.post(self.PAGES['tmdb_tv'] + '/credential', dict(profile_ref=self.profile_ref,
                connection_ref=str(row.pk), revision='1', secret='SYNTHETIC-REPLACED-TV')).status_code, 303)
            response = self.command(path + '/confirm', self.confirmation(wire))
            self.assertEqual(response.status_code, 409, response.content)
            self.assertEqual(SynthesisRun.objects.get(pk=path.rsplit('/', 1)[-1]).attempts, 0)
