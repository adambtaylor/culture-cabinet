"""Default composition must remain unavailable while real producers are absent."""
from shelf.portable_enhance_service import EnhanceService, get_service
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
    def test_existing_history_exact_source_previews_detail_and_one_personal_publication(self):
        from contextlib import contextmanager
        from dataclasses import replace
        from unittest.mock import patch
        import uuid
        from shelf import grant_authority as ga
        from shelf.disclosure import canonical_bytes, read_json
        from shelf.models import DiscoveryState, ProfileCabinetState, PortableConnection, SynthesisRun
        from shelf.inference_adapters import HostedResponsesAdapter, probe_body
        from shelf.inference_transport import CheckedTransport, ProbePermit, PersonalPermit
        from shelf.source_authority import SourcePolicyProvider, SourceRights
        from .test_inference_adapters import response_document
        from .test_source_evidence import policy
        trace, exact_previews = [], []
        def external_model(transport, body, permit):
            if type(permit) is ProbePermit:
                permit.authorize(body)
                return canonical_bytes(response_document())
            self.assertIs(type(permit), PersonalPermit)
            with permit.authorize(body):
                self.assertEqual(body, exact_previews[-1])
                trace.append('personal')
                submitted = read_json(read_json(body)['input'].encode())
                self.assertIn('Exact synthetic note', read_json(body)['input'])
                keys = [row['key'] for row in submitted['profile']['signals']]
                result = {'schema': 'cabinet-inference-result/v1',
                    'associations': {'basis': 'inferred_taste_association', 'clusters': [
                        {'label': 'Tentative', 'members': keys, 'explanation': 'Synthetic hypothesis.'}], 'links': []},
                    'candidate_assessments': [dict(candidate_key=c['key'], decision='retain', signal_keys=keys,
                        evidence_refs=c['source_roots'], hypothesis='Synthetic hypothesis.') for c in submitted['candidates']]}
                return canonical_bytes(response_document(result))
        def external_media(operation, **kwargs):
            self.assertTrue(kwargs['revalidate']())
            trace.append(operation.operation)
            if operation.operation == 'book_search':
                document = {'data': {'search': {'ids': [81001], 'error': None}}}
            elif operation.operation == 'book_details':
                document = {'data': {'books': [{'id': 81001, 'title': 'Synthetic book', 'slug': 'synthetic'}]}}
            else:
                document = {'results': [{'id': 81002, 'title': 'Synthetic film'}]}
            return 200, canonical_bytes(document), (('cache-control', 'max-age=300'),)
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
        with patch.object(CheckedTransport, 'send', external_model), \
             patch('shelf.discovery_transport.media_authorized', side_effect=external_media):
            self.service.adapter = HostedResponsesAdapter()
            del self.authority._disclosure_fixture_token
            self.put(); self.confirm_probe()
            profile = self.bind(); state = ProfileCabinetState.objects.get()
            card = dict(key='source:book:judged', title='Synthetic judged title', medium='book',
                source='Synthetic', url='https://example.invalid/book', provenance='live-automated',
                lane='', roots=[], fetched_at=self.clock.wall.isoformat())
            common = dict(key=card['key'], card=card, mode='household', origin='authenticated-human-post', at=self.clock.wall.isoformat())
            DiscoveryState.objects.create(actor_id=state.actor_id, profile_id=state.profile_id,
                mode='household', nonce='ordinary-history', history=[
                    dict(common, operation='rate', basis='enjoyment', value=5, experienced=True, choice=''),
                    dict(common, operation='respond', choice='intrigued', note='Exact synthetic note')])
            ga.bind_existing_profile(self.authority, self.ref, profile_id=state.profile_id, confirmed=True)
            self.consent(profile, classes=['canonical_seed', 'experienced_enjoyment', 'anticipated_reactions', 'raw_notes'])
            for adapter in ('tmdb_movie', 'hardcover'):
                row = self.vault.configure(self.ref, dict(adapter=adapter, model='', reasoning=''))
                self.vault.replace(self.ref, row.pk, row.config_revision, b'SYNTHETIC-SOURCE')
                ga.change_grant(self.authority, self.ref, profile_ref=profile, expected_revision=0,
                    purpose='catalogue_lookup', connection_ref=str(row.pk), allowed_data_classes=['canonical_seed', 'experienced_enjoyment', 'anticipated_reactions'],
                    expires_at=self.expires, action='grant')
            service = EnhanceService.compose(self.authority, self.service, policy_provider=Policies(), clock=Clock())
            status = service.start(self.ref, profile_ref=profile, connection_ref=str(self.config.pk),
                client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
            plans = dict(service._memory[status['run_ref']]['work'].source_plans)
            self.assertEqual(len(plans), 2)
            self.assertEqual(len({plan.bindings.connection_digest for plan in plans.values()}), 2)
            for _ in range(5):
                preview = service.preview(self.ref, run_ref=status['run_ref'])['disclosure']
                if preview['kind'] == 'catalogue':
                    self.assertNotIn('Exact synthetic note', preview['body'])
                exact_previews.append(preview['body'].encode())
                status = service.confirm(self.ref, run_ref=status['run_ref'], preview_ref=preview['preview_ref'],
                    body_digest=preview['body_digest'], confirmed=True)
                if status['published']: break
            self.assertTrue(status['published'])
            self.assertEqual(trace.count('book_search'), 1)
            self.assertEqual(trace.count('book_details'), 1)
            self.assertEqual(trace.count('personal'), 1)
            self.assertLess(trace.index('book_details'), trace.index('personal'))
            row = SynthesisRun.objects.get(pk=status['run_ref'])
            self.assertEqual(row.attempts, 1)
            current = service.current(self.ref, profile_ref=profile)
            self.assertEqual(len(current['manifest']['categories']), len(service.registry.categories))
            self.assertEqual(service.current(self.ref, profile_ref=profile), current)
            self.assertEqual(service.confirm(self.ref, run_ref=status['run_ref'], preview_ref=preview['preview_ref'],
                body_digest=preview['body_digest'], confirmed=True)['publication_revision'], current['publication_revision'])
            self.assertEqual(trace.count('personal'), 1)
