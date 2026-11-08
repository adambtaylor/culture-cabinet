"""Actual native callers and original freshness; only HTTP bytes are synthetic."""
from dataclasses import replace
from datetime import datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from urllib.parse import urlencode
import uuid
from unittest import TestCase
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory
from django.urls import resolve

from shelf import grant_authority as ga, portable_source_views as views
from shelf.apple_podcast_native import SOURCE, CATEGORY, ApplePodcastBackend
from shelf.category_registry import registry_view
from shelf.disclosure import canonical_bytes, read_json
from shelf.enhance_contract import CollectionBudget, EnhanceFault
from shelf.enhance_coordinator import EnhanceCoordinator
from shelf.models import PortableConnection, PortableDisclosureGrant, SynthesisRun
from shelf.native_source_policy import build_native_source_policy
from shelf.portable_enhance_service import EnhanceService
from shelf.portable_taste_inputs import PortableTasteInputs
from shelf.source_authority import SourceAuthority, SourceRights, configure_source, current_binding, _connection
from shelf.source_collection import SourceCollection
from .test_apple_podcast_native import FixtureBackend, Issuer, document
from .test_grant_authority import RecoveryCase
from .test_source_collection import Backend as SiblingBackend, Clock, plan
from .test_source_evidence import NOW, policy
from shelf.discovery_transport import MediaHostLimiter

PAGE = '/cabinet/source/apple/podcasts'


class AppleNativeWiringTests(RecoveryCase):
    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.factory = RequestFactory()
        rights = policy()
        rights = replace(rights, permissions=tuple((key, tuple(use for use in uses if use != 'cache'))
                                                   for key, uses in rights.permissions))
        self.rights = SourceRights(rights, 1, 'synthetic-episode-fields', 'synthetic-title-disclosure',
            'synthetic-personal-AU', 'CultureCabinet/fixture (test@example.invalid)',
            'synthetic-processor', source=SOURCE)
        self.policies = build_native_source_policy(self.authority, approved_policies={SOURCE: self.rights})

    def request(self, method='GET', fields=None, *, csrf=True):
        if method == 'GET':
            request = self.factory.get(PAGE, fields or {'profile_ref': self.profile_ref})
        else:
            values = [('csrfmiddlewaretoken', 'a' * 32), *list((fields or {}).items())]
            request = self.factory.post(PAGE, urlencode(values),
                content_type='application/x-www-form-urlencoded', HTTP_ACCEPT='application/json')
        request.portable_authority = self.authority
        request.session = {'portable_session': self.ref}
        if csrf:
            request.COOKIES[settings.CSRF_COOKIE_NAME] = 'a' * 32
        return request

    def configure(self):
        self.source_ref = configure_source(self.authority, self.ref, confirmed=True, adapter=SOURCE)
        return self.source_ref

    def grant(self, *, action='grant', revision=0):
        return ga.change_grant(self.authority, self.ref, profile_ref=self.profile_ref,
            expected_revision=revision, purpose='catalogue_lookup', connection_ref=self.source_ref,
            allowed_data_classes=['anticipated_reactions'], expires_at=self.expires, action=action)

    def add_title(self):
        PortableTasteInputs(self.authority).mutate_entry(self.ref, self.profile_ref,
            entry_ref=str(uuid.uuid4()), mutation_id=str(uuid.uuid4()), base_revision=0,
            operation='set', signal='interest', entry_kind='item', category=CATEGORY,
            title='Synthetic Episode', value='yes')

    def test_ordinary_fixed_routes_configure_and_revoke_keyless_source_with_correct_copy(self):
        for suffix, handler in (('', views.source_page), ('/configure', views.source_configure),
                                ('/consent', views.source_consent)):
            matched = resolve(PAGE + suffix, urlconf='cabinet.portable_urls')
            self.assertIs(matched.func, handler)
            self.assertEqual(matched.kwargs, {'adapter': SOURCE})
        response = views.source_page(self.request(), adapter=SOURCE)
        self.assertContains(response, 'Apple Podcasts episodes')
        self.assertContains(response, 'No API key is needed')
        self.assertNotContains(response, 'data-source-credential')
        self.assertFalse(PortableConnection.objects.filter(adapter=SOURCE).exists())
        fields = {'profile_ref': self.profile_ref, 'expected_revision': '0', 'confirmed': 'yes'}
        self.assertEqual(views.source_configure(self.request('POST', fields), adapter=SOURCE).status_code, 303)
        row = PortableConnection.objects.get(adapter=SOURCE)
        self.source_ref = str(row.pk)
        self.assertEqual(row.credential_generation, 0)
        self.assertContains(views.source_page(self.request(), adapter=SOURCE), 'Choose the episode signal groups')
        consent = dict(profile_ref=self.profile_ref, source_connection_ref=self.source_ref,
                       expected_revision='0', action='grant', confirmed='yes', allow_anticipated_reactions='yes')
        with patch.object(self.vault, 'describe', side_effect=AssertionError('No key lookup')):
            self.assertEqual(views.source_consent(self.request('POST', consent), adapter=SOURCE).status_code, 303)
            response = views.source_page(self.request(), adapter=SOURCE)
            self.assertContains(response, 'Anticipated episode interest')
            self.assertContains(response, 'ready for a reviewed lookup')
            self.assertEqual(_connection(self.authority, self.ref, self.source_ref)[1]['credential_mode'], 'none')
            self.fault('AUTHORITY_DENIED', ga._connection, self.authority, self.ref,
                       self.source_ref, purpose='inference')
            revoke = dict(profile_ref=self.profile_ref, source_connection_ref=self.source_ref,
                          expected_revision='1', action='revoke', confirmed='yes')
            self.assertEqual(views.source_consent(self.request('POST', revoke), adapter=SOURCE).status_code, 303)
        self.assertFalse(PortableDisclosureGrant.objects.filter(connection=row, purpose='inference').exists())
        self.fault('AUTHORITY_DENIED', current_binding, self.authority, self.ref,
                   profile_ref=self.profile_ref, connection_ref=self.source_ref)

    def test_source_forms_refuse_foreign_fields_profile_csrf_and_credential_submission(self):
        fields = {'profile_ref': self.profile_ref, 'expected_revision': '0', 'confirmed': 'yes'}
        for changes, csrf, code in (({'origin': 'https://foreign.invalid'}, True, 422),
                                    ({'profile_ref': str(uuid.uuid4())}, True, 403), ({}, False, 403)):
            self.assertEqual(views.source_configure(self.request('POST', {**fields, **changes}, csrf=csrf),
                                                    adapter=SOURCE).status_code, code)
        self.assertFalse(PortableConnection.objects.filter(adapter=SOURCE).exists())
        self.assertEqual(views.source_credential(self.request('POST', {}), adapter=SOURCE).status_code, 422)

    def test_normal_service_collects_original_headers_before_inference_and_expiry_prevents_send(self):
        with self.controlled_recovery_state('clear'):
            self.configure(); self.add_title(); self.grant()
            self.consent(self.profile_ref, classes=['anticipated_reactions'])
            clock = SimpleNamespace(now=self.authority.now, monotonic=lambda: self.clock.tick,
                                    sleep=self.clock.advance)
            service = EnhanceService.compose(self.authority, self.service,
                policy_provider=self.policies, clock=clock)
            provider, collector = service._normal_context(self.ref, self.profile_ref)
            self.assertIs(type(collector.backends[SOURCE]), ApplePodcastBackend)
            run = service.start(self.ref, profile_ref=self.profile_ref, connection_ref=str(self.config.pk),
                client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
            wire = service.preview(self.ref, run_ref=run['run_ref'])['disclosure']
            self.assertEqual((wire['kind'], wire['origin'], wire['path']),
                             ('catalogue', 'https://itunes.apple.com', '/search'))
            self.assertIn('Synthetic+Episode', wire['query'])
            observed = self.authority.now()
            headers = (('cache-control', 'max-age=300'), ('age', '120'), ('date', format_datetime(observed)))
            def reply(operation, *, revalidate, **bounds):
                self.assertTrue(revalidate())
                self.assertEqual(operation.term, 'Synthetic Episode')
                return 200, canonical_bytes(document()), headers
            with patch('shelf.apple_podcast_native.apple_podcast_authorized', side_effect=reply) as sent:
                service.confirm(self.ref, run_ref=run['run_ref'], preview_ref=wire['preview_ref'],
                                body_digest=wire['body_digest'], confirmed=True)
            sent.assert_called_once()
            inference = service.preview(self.ref, run_ref=run['run_ref'])['disclosure']
            self.assertEqual(inference['kind'], 'inference')
            submitted = read_json(read_json(inference['body'].encode())['input'].encode())
            candidate, = submitted['candidates']
            self.assertEqual(candidate['title'], 'Synthetic episode')
            self.assertEqual(datetime.fromisoformat(candidate['observed_at']), observed)
            self.assertLessEqual(datetime.fromisoformat(candidate['valid_until']), observed + timedelta(seconds=180))
            self.assertNotIn('MUST_NOT', inference['body'])
            self.assertEqual(SynthesisRun.objects.get(pk=run['run_ref']).attempts, 0)
            self.clock.advance(181)
            with self.assertRaises((ga.AuthorityFault, EnhanceFault)):
                service.confirm(self.ref, run_ref=run['run_ref'], preview_ref=inference['preview_ref'],
                                body_digest=inference['body_digest'], confirmed=True)
            self.assertEqual(SynthesisRun.objects.get(pk=run['run_ref']).attempts, 0)


class AppleFreshnessWiringTests(TestCase):
    def collect(self, headers, *, policy_seconds=1800, plan_seconds=2000, ttl=900, sibling=False):
        clock, issuer = Clock(), Issuer()
        issuer.rights = replace(issuer.rights, expires_at=NOW + timedelta(seconds=policy_seconds))
        registry, query = plan((CATEGORY, 'movie'))
        query = replace(query, expires_at=NOW + timedelta(seconds=plan_seconds))
        backend = FixtureBackend(issuer, MediaHostLimiter(SOURCE), clock=clock)
        configured, backends = [(CATEGORY, (SOURCE,))], {SOURCE: backend}
        if sibling:
            configured.append(('movie', ('tmdb_movie',)))
            backends['tmdb_movie'] = SiblingBackend('tmdb_movie', clock,
                [(200, {'results': [{'id': 81002, 'title': 'Synthetic sibling movie'}]}, ())])
        collector = SourceCollection(tuple(configured), backends,
                                     clock=clock, isolated_fixture=True)
        from shelf.source_adapters import registered_source
        def spec(name):
            value = registered_source(name)
            return replace(value, ttl_seconds=ttl) if name == SOURCE else value
        with patch('shelf.apple_podcast_native.apple_podcast_authorized',
                   return_value=(200, canonical_bytes(document()), headers)) as send, \
             patch('shelf.source_collection.registered_source', side_effect=spec), \
             patch('shelf.source_adapters.registered_source', side_effect=spec):
            result = collector.collect(query, registry, CollectionBudget())
        send.assert_called_once()
        # A useful sibling may keep its own permitted cache; Apple has no
        # cache permission and must never borrow that sibling's retained batch.
        self.assertTrue(all(not any(row['candidate']['provider'] == 'apple'
            for row in read_json(item.batch.material)['records'])
            for item in collector.state.cache.values()))
        self.assertEqual(tuple(row.category for row in result.outcomes), registry.categories)
        return result, collector, query, registry, clock

    def test_age_date_expires_and_parent_deadlines_only_shorten_original_freshness(self):
        for headers, seconds in (
                ((('cache-control', 'max-age=300'), ('age', '120')), 180),
                ((('date', format_datetime(NOW - timedelta(seconds=30))),), 870),
                ((('expires', format_datetime(NOW + timedelta(seconds=20))),), 20),
                ((), 900),
                ((('date', format_datetime(NOW + timedelta(days=1))),
                  ('expires', format_datetime(NOW + timedelta(days=2)))), 900)):
            with self.subTest(headers=headers):
                result, collector, query, registry, clock = self.collect(headers)
                candidate = result.records[0]['candidate']
                self.assertEqual(datetime.fromisoformat(candidate['observed_at']), NOW)
                self.assertEqual(datetime.fromisoformat(candidate['valid_until']), NOW + timedelta(seconds=seconds))
                clock.elapsed = seconds
                with self.assertRaises(EnhanceFault):
                    collector.revalidate_collection(result, query, registry)
        for policy_seconds, plan_seconds, seconds in ((40, 2000, 40), (1800, 30, 30)):
            result, *_ = self.collect((), policy_seconds=policy_seconds, plan_seconds=plan_seconds)
            self.assertEqual(datetime.fromisoformat(result.records[0]['candidate']['valid_until']),
                             NOW + timedelta(seconds=seconds))
        result, *_ = self.collect((('age', '20'),), ttl=100)
        self.assertEqual(datetime.fromisoformat(result.records[0]['candidate']['valid_until']),
                         NOW + timedelta(seconds=80))

    def test_expired_or_malformed_headers_refuse_evidence_with_sibling_outcomes(self):
        for headers in ((('age', '900'),), (('age', '901'),), (('age', '-1'),),
                        (('date', 'not-a-date'),), (('expires', format_datetime(NOW)),)):
            with self.subTest(headers=headers):
                result, *_ = self.collect(headers, sibling=True)
                self.assertEqual([row['candidate']['title'] for row in result.records], ['Synthetic sibling movie'])
                outcomes = {row.category: row for row in result.outcomes}
                self.assertEqual(outcomes[CATEGORY].state, 'failed')
                self.assertEqual(outcomes[CATEGORY].code, 'SOURCE_UNAVAILABLE')
                self.assertEqual(outcomes['movie'].state, 'fresh')
