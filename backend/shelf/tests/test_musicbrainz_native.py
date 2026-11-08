"""Actual anonymous issuer, separate grants, native feedback and query codec."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from shelf import grant_authority as ga, profile_snapshot
from shelf.category_registry import registry_view
from shelf.disclosure import canonical_bytes, read_json
from shelf.enhance_contract import EnhanceFault
from shelf.enhance_coordinator import EnhanceCoordinator
from shelf.native_source_policy import build_native_source_policy
from shelf.portable_enhance_service import EnhanceService
from shelf.portable_taste_inputs import PortableTasteInputs
from shelf.source_authority import (MUSICBRAINZ, SourceAuthority, SourceRights,
                                    configure_source, current_binding, _connection)
from .test_grant_authority import RecoveryCase
from .test_source_evidence import policy


class MusicBrainzNativeAuthorityTests(RecoveryCase):
    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.source_ref = configure_source(self.authority, self.ref, confirmed=True, adapter=MUSICBRAINZ)
        rights = policy()
        rights = replace(rights, permissions=tuple((key, tuple(use for use in uses if use != 'cache'))
                                                  for key, uses in rights.permissions))
        self.rights = SourceRights(rights, 1, 'synthetic-cc0', 'synthetic-local-disclosure',
            'synthetic-profile-scope', 'CultureCabinet/fixture (test@example.invalid)',
            'synthetic-processor', source=MUSICBRAINZ)
        self.policies = build_native_source_policy(self.authority, approved_policies={MUSICBRAINZ: self.rights})
        self.issuer = SourceAuthority(self.authority, self.ref, profile_ref=self.profile_ref,
                                     connection_ref=self.source_ref, policy_provider=self.policies)

    def source_consent(self, *, action='grant', revision=0):
        return ga.change_grant(self.authority, self.ref, profile_ref=self.profile_ref,
            expected_revision=revision, purpose='catalogue_lookup', connection_ref=self.source_ref,
            allowed_data_classes=['anticipated_reactions'], expires_at=self.expires, action=action)

    def planned(self):
        view = current_binding(self.authority, self.ref, profile_ref=self.profile_ref, connection_ref=self.source_ref)
        return EnhanceCoordinator(None, now=self.authority.now).plan(view, registry_view(('recording',)),
            run_ref=str(uuid.uuid4()), client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))

    def test_anonymous_binding_no_vault_lookup_or_processor_fallback_wrong_purpose_denied(self):
        with patch.object(self.vault, 'describe', side_effect=AssertionError('No credential lookup')), \
             patch.object(self.service, '_current', side_effect=AssertionError('No processor validation')):
            row, binding, _ = _connection(self.authority, self.ref, self.source_ref)
            self.assertEqual((binding['credential_mode'], binding['credential_generation']), ('none', 0))
            self.source_consent()
            view = current_binding(self.authority, self.ref, profile_ref=self.profile_ref, connection_ref=self.source_ref)
            self.assertEqual(view.context['adapter_id'], MUSICBRAINZ)
            self.fault('AUTHORITY_DENIED', ga._connection, self.authority, self.ref,
                       self.source_ref, purpose='inference')
        self.vault.replace(self.ref, row.pk, row.config_revision, b'SYNTHETIC-UNEXPECTED-CREDENTIAL')
        self.fault('CAPABILITY_UNAVAILABLE', _connection, self.authority, self.ref, self.source_ref)

    def test_direct_artist_reaches_exact_source_preview_permit_and_revocation(self):
        with self.controlled_recovery_state('clear'):
            PortableTasteInputs(self.authority).mutate(self.ref, self.profile_ref,
                entry_ref=str(uuid.uuid4()), mutation_id=str(uuid.uuid4()), base_revision=0,
                operation='set', signal='interest', title='Fictional Artist + Duo', value='yes')
            self.source_consent()
            planned = self.planned()
            terms = read_json(planned.plan.body)['queries'][0]['terms']
            self.assertEqual(terms, ['Fictional Artist + Duo'])
            preview = self.issuer.preview(planned.plan)
            request = self.issuer._previews[preview['preview_ref']].request
            self.assertEqual(request.terms, tuple(terms[:1]))
            self.assertEqual((preview['method'], preview['origin'], preview['path']),
                             ('GET', 'https://musicbrainz.org', '/ws/2/recording/'))
            self.issuer.confirm(preview['preview_ref'], plan_digest=preview['plan_digest'],
                                operation_digest=preview['operation_digest'], confirmed=True)
            operation = self.issuer.operation_for(request, planned.plan, now=self.authority.now())
            permit = self.issuer.authorize(request, planned.plan, operation, now=self.authority.now())
            self.assertEqual(permit.credential_generation, 0)
            self.assertTrue(self.issuer.revalidate(permit, request, planned.plan, operation, now=self.authority.now()))
            self.source_consent(action='revoke', revision=1)
            with self.assertRaises((ga.AuthorityFault, EnhanceFault)):
                self.issuer.revalidate(permit, request, planned.plan, operation, now=self.authority.now())

    def test_empty_disclosed_artist_set_returns_missing_input_without_request(self):
        self.source_consent()
        planned = self.planned()
        self.assertEqual(read_json(planned.plan.body)['queries'][0]['terms'], [])
        with self.assertRaises(EnhanceFault) as raised:
            self.issuer.preview(planned.plan)
        self.assertEqual(raised.exception.code, 'NO_GROUNDED_CANDIDATES')
        self.assertEqual(self.issuer._previews, {})

    def test_real_normal_composition_selects_keyless_backend_and_same_policy_instance(self):
        from shelf.musicbrainz_source import MusicBrainzBackend
        with patch.object(self.vault, 'describe', side_effect=AssertionError('No credential lookup')):
            service = EnhanceService.compose(self.authority, self.service, policy_provider=self.policies)
            provider, collector = service._normal_context(self.ref, self.profile_ref)
            self.assertIs(type(collector.backends[MUSICBRAINZ]), MusicBrainzBackend)
            issuer, = provider.sources
            self.assertIs(issuer.provider, self.policies)
            self.assertEqual(issuer._rights(self.authority.now()), self.rights)

    def test_normal_service_collects_mocked_core_reply_before_exact_model_preview(self):
        from shelf.category_registry import KEYS
        from shelf.models import SynthesisRun
        with self.controlled_recovery_state('clear'):
            PortableTasteInputs(self.authority).mutate(self.ref, self.profile_ref,
                entry_ref=str(uuid.uuid4()), mutation_id=str(uuid.uuid4()), base_revision=0,
                operation='set', signal='interest', title='Synthetic Artist', value='yes')
            self.source_consent()
            self.consent(self.profile_ref, classes=['anticipated_reactions'])
            clock = SimpleNamespace(now=self.authority.now, monotonic=lambda: self.clock.tick,
                                    sleep=self.clock.advance)
            service = EnhanceService.compose(self.authority, self.service,
                policy_provider=self.policies, clock=clock)
            run = service.start(self.ref, profile_ref=self.profile_ref, connection_ref=str(self.config.pk),
                client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
            wire = service.preview(self.ref, run_ref=run['run_ref'])['disclosure']
            self.assertEqual((wire['kind'], wire['method'], wire['body']), ('catalogue', 'GET', ''))
            self.assertIn('artistname', wire['query'])
            self.assertNotIn(self.profile_ref, wire['query'])
            def reply(operation, *, revalidate, **bounds):
                self.assertTrue(revalidate())
                self.assertEqual(operation.artist_name, 'Synthetic Artist')
                return 200, canonical_bytes({'recordings': [{
                    'id': '81000000-0000-4000-8000-000000000001',
                    'title': 'Synthetic recording', 'score': 'MUST_NOT_PROJECT'}]}), ()
            # Only the wire reply is replaced: normal policy, grants, issuer,
            # collector, all-category outcomes and model serialization run intact.
            with patch('shelf.discovery_transport.musicbrainz_authorized', side_effect=reply) as sent:
                service.confirm(self.ref, run_ref=run['run_ref'], preview_ref=wire['preview_ref'],
                    body_digest=wire['body_digest'], confirmed=True)
            self.assertEqual(sent.call_count, 1)
            inference = service.preview(self.ref, run_ref=run['run_ref'])['disclosure']
            self.assertEqual(inference['kind'], 'inference')
            submitted = read_json(read_json(inference['body'].encode())['input'].encode())
            self.assertEqual([c['title'] for c in submitted['candidates']], ['Synthetic recording'])
            self.assertNotIn('MUST_NOT_PROJECT', inference['body'])
            row = SynthesisRun.objects.get(pk=run['run_ref'])
            self.assertEqual(row.attempts, 0)
            outcomes = {item['category']: item for item in row.evidence['outcomes']}
            self.assertEqual(set(outcomes), set(KEYS))
            self.assertEqual(outcomes['recording']['candidate_count'], 1)
            self.assertTrue(all(value['state'] == 'unavailable' for key, value in outcomes.items()
                                if key != 'recording'))
