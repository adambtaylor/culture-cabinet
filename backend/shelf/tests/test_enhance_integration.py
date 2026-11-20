"""Default composition must remain unavailable while real producers are absent."""
from dataclasses import replace
from unittest.mock import patch
import uuid
from shelf.disclosure import canonical_bytes, read_json
from shelf.inference_adapters import HostedResponsesAdapter
from shelf.inference_transport import CheckedTransport, ProbePermit
from shelf.models import PortableConnection, SynthesisRun
from shelf.portable_enhance_service import EnhanceService, get_service
from shelf.source_authority import SourcePolicyProvider, SourceRights
from .test_inference_adapters import response_document
from .test_source_evidence import policy
from .test_portable_cabinet_store import StoreCase


class EnhanceIntegrationTests(StoreCase):
    def test_default_service_never_promotes_probe_or_missing_ports_to_working_enhance(self):
        service = get_service(self.authority)
        self.assertIs(get_service(self.authority), service)
        ready = service.readiness(self.ref, profile_ref=self.profile_ref, connection_ref=str(self.config.pk))
        self.assertFalse(ready['available'])
        self.assertFalse(ready['run_ready'])
        self.assertEqual(ready['actions'], [])
        import uuid
        self.fault('CAPABILITY_UNAVAILABLE', service.start, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
        self.assertEqual(self.fake.calls, 1)  # Only the pre-existing isolated neutral probe.
        self.assertFalse(service.current(self.ref, profile_ref=self.profile_ref)['published'])


from .test_grant_authority import RecoveryCase


class OrdinaryEnhanceCompositionTests(RecoveryCase):
    # The complete sequence needs a longer synthetic socket lease, not new authority.
    bound_socket_lifetime = 25

    def _source_runtime(self):
        class Policies(SourcePolicyProvider):
            def current(inner, authority, session_ref, *, connection_ref, **kwargs):
                adapter = PortableConnection.objects.get(pk=connection_ref).adapter
                return SourceRights(replace(policy(), ref='synthetic:' + adapter), 1,
                    'synthetic-terms', 'synthetic-privacy', 'synthetic-locale',
                    'CultureCabinet/fixture (example.invalid)', 'synthetic-processor', source=adapter)
        class Clock:
            def now(inner): return self.clock.wall
            def monotonic(inner): return self.clock.tick
            def sleep(inner, seconds): self.clock.advance(seconds)
        return Policies(), Clock()

    def _bind_owner_history_and_sources(self, *, adapters, medium='book', grant_overrides=None, connection_ids=None):
        from shelf import grant_authority as ga
        from shelf.models import DiscoveryState
        from .test_profile_snapshot import _legacy_owner_state
        self.put(); self.confirm_probe()
        state = _legacy_owner_state(self.authority, self.ref)
        profile = str(state.profile_ref)
        card = dict(key='source:' + medium + ':selected', title='Synthetic selected seed', medium=medium,
            source='Synthetic', url='https://example.invalid/item', provenance='live-automated', lane='',
            roots=[], fetched_at=self.clock.wall.isoformat())
        common = dict(key=card['key'], card=card, mode='household', origin='authenticated-human-post',
            at=self.clock.wall.isoformat())
        DiscoveryState.objects.create(actor_id=state.actor_id, profile_id=state.profile_id,
            mode='household', nonce='selected-profile-' + medium, history=[
                dict(common, operation='rate', basis='enjoyment', value=5, experienced=True, choice=''),
                dict(common, operation='respond', choice='intrigued', note='Synthetic only')])
        ga.bind_existing_profile(self.authority, self.ref, profile_id=state.profile_id, confirmed=True)
        self.consent(profile, classes=['canonical_seed', 'experienced_enjoyment',
            'anticipated_reactions', 'raw_notes'])
        for adapter in adapters:
            with patch('shelf.secret_leases.uuid.uuid4', return_value=(connection_ids or {}).get(adapter, uuid.uuid4())):
                row = self.vault.configure(self.ref, dict(adapter=adapter, model='', reasoning=''))
            self.vault.replace(self.ref, row.pk, row.config_revision, b'SYNTHETIC-SOURCE')
            ga.change_grant(self.authority, self.ref, profile_ref=profile, expected_revision=0,
                purpose='catalogue_lookup', connection_ref=str(row.pk),
                allowed_data_classes=(grant_overrides or {}).get(adapter,
                    ['canonical_seed', 'experienced_enjoyment', 'anticipated_reactions']),
                expires_at=self.expires, action='grant')
        return profile

    def test_hardcover_starter_without_terms_reaches_the_sealed_consumer_once(self):
        media_calls = []

        def model_send(transport, body, permit):
            if type(permit) is ProbePermit:
                permit.authorize(body)
                return canonical_bytes(response_document())
            raise AssertionError('Unavailable inference must not send a personal request')

        def media_send(operation, **bounds):
            self.assertTrue(bounds['revalidate']())
            media_calls.append((operation.source, operation.operation))
            if operation.source == 'tmdb_movie':
                return 200, canonical_bytes({'results': []}), (('cache-control', 'max-age=300'),)
            return 200, canonical_bytes({'data': {'books': [
                {'id': 81001, 'title': 'Starter book', 'slug': 'starter-book'}]}}), (('cache-control', 'max-age=300'),)

        with patch.object(CheckedTransport, 'send', model_send), \
             patch('shelf.discovery_transport.media_authorized', side_effect=media_send):
            self.service.adapter = HostedResponsesAdapter()
            del self.authority._disclosure_fixture_token
            profile = self._bind_owner_history_and_sources(adapters=('tmdb_movie', 'hardcover'),
                grant_overrides={'hardcover': ['experienced_enjoyment', 'anticipated_reactions']},
                connection_ids={'tmdb_movie': uuid.UUID(int=1), 'hardcover': uuid.UUID(int=2)})
            policies, clock = self._source_runtime()
            service = EnhanceService.compose(self.authority, self.service,
                policy_provider=policies, clock=clock)
            service._inference_available = lambda: False
            status = service.start(self.ref, profile_ref=profile, connection_ref=str(self.config.pk),
                client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
            work = service._memory[status['run_ref']]['work']
            catalogue = service._memory[status['run_ref']]['coordinator'].catalogue
            own = dict(work.source_plans)[catalogue.backends['hardcover'].issuer.connection_ref]
            self.assertTrue(next(row['terms'] for row in read_json(work.plan.body)['queries'] if row['category'] == 'book'))
            self.assertEqual(next(row['terms'] for row in read_json(own.body)['queries'] if row['category'] == 'book'), [])
            for _ in range(2):
                disclosure = service.preview(self.ref, run_ref=status['run_ref'])['disclosure']
                status = service.confirm(self.ref, run_ref=status['run_ref'], preview_ref=disclosure['preview_ref'],
                    body_digest=disclosure['body_digest'], confirmed=True)

            self.assertEqual(status['stage'], 'sealed')
            outcomes = {row['category']: row for row in status['category_outcomes']}
            self.assertEqual((outcomes['book']['state'], outcomes['book']['candidate_count']), ('fresh', 1))
            source_use = {row['category']: row for row in status['source_use']}
            self.assertEqual(source_use['book']['collected_count'], 1)
            self.assertEqual((media_calls.count(('hardcover', 'starter_books')), len(media_calls)), (1, 2))
            self.assertEqual(SynthesisRun.objects.get(pk=status['run_ref']).attempts, 0)

    def test_preparatory_hardcover_denial_survives_model_eligible_tmdb_without_inference(self):
        media_calls, probe_calls = [], []

        def model_send(transport, body, permit):
            if type(permit) is ProbePermit:
                permit.authorize(body)
                probe_calls.append('probe')
                return canonical_bytes(response_document())
            raise AssertionError('Empty source collection must not invoke inference')

        def media_send(operation, **bounds):
            self.assertTrue(bounds['revalidate']())
            media_calls.append((operation.source, operation.operation))
            if operation.operation == 'book_search':
                return 403, canonical_bytes({'errors': [{'message': 'Synthetic denial.'}]}), ()
            if operation.operation in ('search_movie', 'starter_movie_science'):
                return 200, canonical_bytes({'results': [{'id': 82001, 'title': 'Synthetic film'}]}), (('cache-control', 'max-age=300'),)
            raise AssertionError('Unexpected synthetic media operation')

        with patch.object(CheckedTransport, 'send', model_send), \
             patch('shelf.discovery_transport.media_authorized', side_effect=media_send):
            self.service.adapter = HostedResponsesAdapter()
            del self.authority._disclosure_fixture_token
            profile = self._bind_owner_history_and_sources(adapters=('tmdb_movie', 'hardcover'))
            policies, clock = self._source_runtime()
            service = EnhanceService.compose(self.authority, self.service,
                policy_provider=policies, clock=clock)
            service._inference_available = lambda: False
            status = service.start(self.ref, profile_ref=profile, connection_ref=str(self.config.pk),
                client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
            for _ in range(2):
                disclosure = service.preview(self.ref, run_ref=status['run_ref'])['disclosure']
                status = service.confirm(self.ref, run_ref=status['run_ref'],
                    preview_ref=disclosure['preview_ref'], body_digest=disclosure['body_digest'],
                    confirmed=True)

            separate_status = service.status(self.ref, run_ref=status['run_ref'])
            self.assertEqual((separate_status['stage'], separate_status['code']),
                             ('sealed', 'CAPABILITY_UNAVAILABLE'))
            outcomes = {row['category']: row for row in separate_status['category_outcomes']}
            self.assertEqual((outcomes['book']['state'], outcomes['book']['code']),
                             ('unavailable', 'AUTHORITY_DENIED'))
            self.assertEqual((outcomes['movie']['state'], outcomes['movie']['candidate_count']),
                             ('fresh', 1))
            source_use = {row['category']: row for row in separate_status['source_use']}
            self.assertEqual(source_use['movie']['model_eligible_count'], 1)
            row = SynthesisRun.objects.get(pk=status['run_ref'])
            self.assertEqual(row.attempts, 0)
            self.assertEqual((media_calls.count(('hardcover', 'book_search')),
                sum(source == 'tmdb_movie' for source, _ in media_calls), len(media_calls)), (1, 1, 2))
            self.assertEqual(probe_calls, ['probe'])
