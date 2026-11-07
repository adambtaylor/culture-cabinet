"""PS1 component tests; PA1/publication ports are isolated, not native Save proof.

These cases perform no SQL, network, acquisition or fixture-resource work. N1's
real selection-generation and persistence tests remain the integration boundary.
"""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from shelf import grant_authority as ga
from shelf.models import PortableCabinetManifest
from shelf.portable_authority import AuthorityFault
from shelf.portable_enhance_service import EnhanceService, SavedCandidateEvidence, SavedCandidateUnavailable
from shelf.source_evidence import FieldPolicy


class SavedCandidateTests(SimpleTestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.profile_ref = '00000000-0000-4000-8000-000000000041'
        self.state = SimpleNamespace(installation_id='00000000-0000-4000-8000-000000000010',
            profile_ref=self.profile_ref, actor_id=17, profile_id=41, data_schema_version=2,
            active_manifest_ref='00000000-0000-4000-8000-000000000050')
        self.session = SimpleNamespace(owner_revision=3)
        self.publication = SimpleNamespace(pk=self.state.active_manifest_ref,
            run_id='00000000-0000-4000-8000-000000000060', publication_revision=7,
            expires_at=self.now + timedelta(minutes=5))
        self.card = dict(key='tmdb:movie:81001', category='movie', title='Exact source title é',
            medium='movie', provider='tmdb', source_id='81001',
            source_url='https://www.themoviedb.org/movie/81001', identity_status='confirmed',
            evidence_type='live_lookup', observed_at='2026-01-01T00:00:00Z',
            valid_until='2026-01-01T00:05:00Z', rights_ref='rights-1',
            source_roots=['evidence-1'], facts=[])
        self.current = dict(published=True, publication_revision=7,
            manifest={'categories': [{'cards': [self.card]}]})
        self.service = object.__new__(EnhanceService)
        self.service.authority = SimpleNamespace(_lock=threading.RLock(), now=lambda: self.now)
        self.service._isolated = None
        self.service.current = Mock(side_effect=lambda *a, **k: deepcopy(self.current))
        self.active = None
        self.generation = 1
        case = self

        class PortableSavedAccess:
            def __init__(self):
                self.authority, self.session_ref = case.service.authority, 'synthetic-session'
                self.profile_ref, self.generation, self.closed = case.profile_ref, case.generation, False
                self.calls = []

            def require(self, *, actor_id, profile_id, action):
                self.calls.append((actor_id, profile_id, action))
                if (self.closed or self.generation != case.generation
                        or (actor_id, profile_id) != (17, 41) or action != 'save'):
                    raise AuthorityFault('AUTHORITY_DENIED', 403)
                return SimpleNamespace(actor_id=actor_id, profile_id=profile_id)

        self.access_type = PortableSavedAccess
        self.access = self.active = PortableSavedAccess()
        module = ModuleType('shelf.portable_saved_authority')
        module.PortableSavedAccess = PortableSavedAccess
        module.active_portable_access = lambda: self.active
        self.start_patch(patch.dict(sys.modules, {module.__name__: module}))
        self.start_patch(patch.object(ga, 'recovery_guard', side_effect=lambda *a, **k: nullcontext()))
        self.start_patch(patch.object(ga, '_bound', return_value=(self.state, self.session)))
        query = Mock()
        query.first.side_effect = lambda: self.publication
        self.filter = self.start_patch(patch.object(PortableCabinetManifest.objects, 'filter', return_value=query))

    def start_patch(self, value):
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def resolve(self, **changes):
        arguments = dict(profile_ref=self.profile_ref, expected_publication_revision=7,
                         candidate_key=self.card['key'])
        arguments.update(changes)
        return self.service.resolve_saved_candidate('synthetic-session', **arguments)

    def projection(self, card=None):
        return self.service._saved_evidence(self.state, self.session, self.publication, card or self.card)

    def test_projection_preserves_exact_attribution_with_bounded_opaque_lineage(self):
        value = self.projection()
        self.assertEqual((value.actor_id, value.profile_id, value.title), (17, 41, self.card['title']))
        self.assertEqual((value.provider, value.medium, value.source_id), ('tmdb', 'movie', 81001))
        self.assertEqual(value.fetched_at, self.now)
        self.assertEqual(value.schema, 'portable-saved-candidate/v1')
        self.assertEqual(value.publication_ref, self.state.active_manifest_ref)
        self.assertEqual(len(value.candidate_digest), 64)
        self.assertNotIn('notes', {field.name for field in fields(value)})
        self.assertNotIn('source_payload', {field.name for field in fields(value)})
        self.assertNotIn(self.card['title'], repr(value))
        changed = deepcopy(self.card)
        changed['source_roots'] = ['evidence-2']
        self.assertNotEqual(self.projection(changed).source_evidence_digest, value.source_evidence_digest)
        for provider, medium, url in [('tmdb', 'tv', 'https://www.themoviedb.org/tv/81001'),
                                      ('hardcover', 'book', 'https://hardcover.app/books/exact-book')]:
            changed = dict(self.card, provider=provider, medium=medium, category=medium, source_url=url)
            self.assertEqual(self.projection(changed).url, url)

    def test_projection_refuses_missing_wrong_expired_or_noncanonical_source_identity(self):
        attacks = [dict(source_id='081001'), dict(source_id=81001), dict(source_id='1.5'),
            dict(source_id='2147483648'), dict(provider='unknown'), dict(provider=[]),
            dict(medium='artist'), dict(category='event'), dict(source_url='https://example.invalid/81001'),
            dict(source_url=self.card['source_url'] + '?token=forbidden'), dict(title=''),
            dict(title='x' * 301), dict(observed_at='2026-01-01T00:00:01Z'),
            dict(observed_at='2026-01-01T00:00:00'), dict(valid_until='2026-01-01T00:00:00Z'),
            dict(identity_status='unverified'), dict(evidence_type='caller_assertion'),
            dict(source_roots=[]), dict(rights_ref=''), dict(raw_notes='not a PS1 field')]
        for delta in attacks:
            with self.subTest(fields=delta), self.assertRaises(AuthorityFault):
                self.projection(dict(self.card, **delta))
        with self.assertRaises(AuthorityFault):
            replace(self.projection(), owner_revision=True)

    def test_missing_or_finite_retention_permission_denies_without_any_write(self):
        # This is the production C4 denial, not a fixture granting durable rights.
        policy = FieldPolicy('synthetic-finite-policy', 1, self.now,
            self.now + timedelta(minutes=5), (('title', ('display', 'retain')),
                ('source_url', ('display', 'retain'))))
        self.assertTrue(policy.allows('title', 'retain'))
        for material in (self.card, dict(self.card, policy=policy, durable=True,
                                        terms_ref='arbitrary-indefinite-string')):
            with self.subTest(has_claim='durable' in material), self.assertRaises(SavedCandidateUnavailable) as error:
                self.service._saved_retention(material)
            self.assertEqual(error.exception.status, 503)
            self.assertIn('durable saved copy', error.exception.message)
        with self.assertRaises(SavedCandidateUnavailable):
            self.resolve()
        self.service.current.assert_called_once()
        with self.assertRaises(SavedCandidateUnavailable):
            with self.service.saved_candidate_window('synthetic-session', evidence=self.projection()):
                self.fail('Missing durable rights reached a SavedItem write window')
        # SimpleTestCase prohibits every database query, including SavedItem,
        # lineage and FeedbackEvent writes; only the read port above is mocked.

    def test_resolver_requires_exact_active_capability_before_reading_publication(self):
        for mutate in (lambda: setattr(self, 'active', None),
                       lambda: setattr(self.access, 'closed', True),
                       lambda: setattr(self.access, 'session_ref', 'different-session'),
                       lambda: setattr(self.access, 'profile_ref', 'foreign-profile'),
                       lambda: setattr(self.access, 'authority', object())):
            self.active = self.access
            self.access.closed = False
            self.access.session_ref = 'synthetic-session'
            self.access.profile_ref = self.profile_ref
            self.access.authority = self.service.authority
            mutate()
            with self.assertRaises(AuthorityFault):
                self.resolve()
            self.service.current.assert_not_called()
        with patch.dict(sys.modules, {'shelf.portable_saved_authority': None}), self.assertRaises(AuthorityFault) as error:
            self.resolve()
        self.assertEqual(error.exception.code, 'CAPABILITY_UNAVAILABLE')

    def test_filtered_stale_missing_or_duplicate_candidate_has_no_historical_fallback(self):
        for revision, cards in ((6, [self.card]), (7, []), (7, [self.card, self.card])):
            self.current['publication_revision'] = revision
            self.current['manifest']['categories'][0]['cards'] = cards
            with self.subTest(revision=revision, count=len(cards)), self.assertRaises(AuthorityFault):
                self.resolve()
        self.current['manifest']['categories'][0]['cards'] = [self.card]
        self.publication = None
        with self.assertRaises(AuthorityFault):
            self.resolve()

    def test_selection_switch_away_and_back_during_read_refuses_old_scope(self):
        def read(*args, **kwargs):
            self.generation += 2  # The selected profile is the same again, but A is obsolete.
            return deepcopy(self.current)
        self.service.current.side_effect = read
        # Isolate the later live-selection boundary; never a native Save success.
        with patch.object(self.service, '_saved_retention'), self.assertRaises(AuthorityFault):
            self.resolve()
        self.assertEqual(self.access.calls, [(17, 41, 'save'), (17, 41, 'save')])

    def test_window_revalidates_evidence_before_yield_and_never_reads_after_own_write(self):
        evidence = self.projection()
        # This component exercises window ordering with an eligible resolver port.
        # The actual resolver's C4 denial above is never bypassed for native Save.
        self.service.resolve_saved_candidate = Mock(return_value=evidence)
        with self.service.saved_candidate_window('synthetic-session', evidence=evidence) as current:
            self.assertEqual(current, evidence)
            self.service.resolve_saved_candidate.assert_called_once()
            self.service.resolve_saved_candidate.side_effect = AssertionError('Old publication re-read after Save')
        self.assertEqual(self.access.calls, [(17, 41, 'save'), (17, 41, 'save')])
        self.service.current.assert_not_called()

    def test_window_rejects_changed_lineage_and_denies_scope_escape(self):
        evidence = self.projection()
        for changed in (replace(evidence, owner_revision=4), replace(evidence, candidate_digest='f' * 64),
                        replace(evidence, title='changed'), replace(evidence, source_evidence_digest='e' * 64)):
            self.service.resolve_saved_candidate = Mock(return_value=changed)
            with self.subTest(field=changed), self.assertRaises(AuthorityFault):
                with self.service.saved_candidate_window('synthetic-session', evidence=evidence):
                    self.fail('A stale frame reached the mutation window')
        self.service.resolve_saved_candidate = Mock(return_value=evidence)
        with self.assertRaises(AuthorityFault):
            with self.service.saved_candidate_window('synthetic-session', evidence=evidence):
                self.active = self.access_type()
        self.active = self.access
        self.service.resolve_saved_candidate = Mock(side_effect=AuthorityFault('PREVIEW_STALE', 409))
        with self.assertRaises(AuthorityFault):
            with self.service.saved_candidate_window('synthetic-session', evidence=evidence):
                self.fail('A refused fresh publication reached the write window')
        self.service.resolve_saved_candidate = Mock(return_value=evidence)
        with self.assertRaises(AuthorityFault):
            with self.service.saved_candidate_window('synthetic-session', evidence=evidence):
                self.access.closed = True

    def test_window_refuses_redacting_read_inside_rollback_capable_transaction(self):
        evidence = self.projection()
        from django.db import connection
        with patch.object(connection, 'in_atomic_block', True):
            with self.assertRaises(AuthorityFault):
                self.resolve()
            with self.assertRaises(AuthorityFault):
                with self.service.saved_candidate_window('synthetic-session', evidence=evidence):
                    self.fail('A Save transaction surrounded a redacting read')
        self.service.current.assert_not_called()

    def test_read_refusal_does_not_wrap_or_undo_prior_redaction(self):
        state = {'redacted': False}
        def redacting_read(*args, **kwargs):
            from django.db import connection
            self.assertFalse(connection.in_atomic_block)
            state['redacted'] = True
            raise AuthorityFault('PREVIEW_STALE', 409)
        self.service.current.side_effect = redacting_read
        with self.assertRaises(AuthorityFault):
            self.resolve()
        self.assertTrue(state['redacted'])
