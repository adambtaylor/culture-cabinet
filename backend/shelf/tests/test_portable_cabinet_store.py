"""Strict memory-fixture durable core; not live transport or normal readiness."""
from datetime import timedelta
import hashlib
import json
import uuid

from django.db import DatabaseError, IntegrityError, transaction

from shelf import grant_authority as ga
from shelf.disclosure import canonical_bytes, read_json
from shelf.enhance_contract import EnhanceBindings, EnhanceIntent, RegistryView, digest
from shelf.models import PortableCabinetManifest, ProfileCabinetState, SynthesisRun
from shelf.portable_authority import AuthorityFault
from shelf.portable_cabinet_store import CabinetStore, CurrentState, StateProvider
from .test_grant_authority import AuthorityCase


def candidate():
    return {'key': 'fixture:book:1', 'category': 'books', 'title': 'Isolated fixture book',
        'medium': 'book', 'source_url': 'https://example.invalid/1', 'provider': 'isolated',
        'source_id': '1', 'identity_status': 'fixture_identity_only', 'evidence_type': 'fixture_not_live',
        'observed_at': '2026-01-01T00:00:00Z', 'valid_until': '2026-01-01T00:10:00Z',
        'rights_ref': 'rights-1', 'source_roots': ['evidence-1'], 'facts': []}


def result():
    return canonical_bytes({'schema': 'cabinet-inference-result/v1', 'associations': None,
        'candidate_assessments': [{'candidate_key': candidate()['key'], 'decision': 'uncertain',
            'signal_keys': [], 'evidence_refs': ['evidence-1'], 'hypothesis': 'Isolated assessment only.'}]})


class IsolatedStateProvider(StateProvider):
    """Executable fixture for the explicitly MISSING production evidence producer.

    Uses actual current owner/session/proof/grants and actual ORM history. Only
    portable bookkeeping is excluded from the legacy-derived revision stream;
    no authority check is patched away and no normal producer is registered.
    """
    def __init__(self, case):
        self.case, self.source_revision, self.drop = case, 'isolated-source-rev-1', False

    def current(self, authority, session_ref, *, profile_ref, connection_ref):
        view = ga.current_view(authority, session_ref, profile_ref=profile_ref,
            connection_ref=connection_ref, purpose='catalogue_lookup')
        revisions = view.source_revisions
        legacy = list(SynthesisRun.objects.filter(profile_id=view.profile_id,
            contract_version='legacy/v1').order_by('pk').values())
        revisions['derived_history'] = hashlib.sha256(json.dumps(legacy, sort_keys=True,
            separators=(',', ':'), ensure_ascii=False, allow_nan=False, default=ga._local_default).encode()).hexdigest()
        # The added legacy-default columns are present even before the first run;
        # the same canonical empty stream remains stable through portable writes.
        evidence = digest(canonical_bytes(revisions))
        registry = self.case.registry
        bindings = EnhanceBindings(view.installation_id, view.actor_ref, view.profile_ref,
            evidence, view.policy_epoch, view.restriction_watermark, registry.revision,
            view.context['config_revision'], digest(canonical_bytes(view.grants)),
            digest(canonical_bytes(view.connection_binding)))
        return CurrentState(view, bindings, registry, self.source_revision, min(view.expires_at, self.case.expires))

    def filter_manifest(self, current, manifest):
        if self.drop:
            for row in manifest['categories']:
                row['cards'] = []
                if row['state'] == 'fresh':
                    row['state'] = 'empty_bounded'
            manifest['for_you'] = []
        return manifest


class StoreCase(AuthorityCase):
    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.consent(self.profile_ref)
        self.consent(self.profile_ref, purpose='catalogue_lookup')
        self.registry = RegistryView('1', ('books', 'films', 'tv', 'music', 'events', 'outings'))
        self.provider = IsolatedStateProvider(self)
        self.store = self.new_store()

    def new_store(self):
        return CabinetStore.isolated_test(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), state_provider=self.provider)

    def intent(self, *, key=None, refresh=None):
        return EnhanceIntent(str(uuid.uuid4()), key or str(uuid.uuid4()), refresh or str(uuid.uuid4()),
                             self.store.current().bindings)

    def sealed(self, intent=None):
        row = self.store.admit(intent or self.intent())
        outcomes = [{'category': key, 'state': 'fresh' if key == 'books' else 'unavailable',
                     'candidate_count': 1 if key == 'books' else 0,
                     'code': 'OK' if key == 'books' else 'CAPABILITY_UNAVAILABLE'} for key in self.registry.categories]
        refs = {'signals': [], 'candidates': [{'key': candidate()['key'], 'evidence_refs': ['evidence-1']}]}
        return self.store.checkpoint(str(row.pk), stage='awaiting_preview', outcomes=outcomes,
            metadata={'submitted_refs': refs, 'phase': 'inference'}, collection_digest='c' * 64,
            body_digest='b' * 64, sealed_run_digest='d' * 64)

    def completed(self, row=None):
        row = row or self.sealed()
        row = self.store.reserve(str(row.pk), body_digest=row.body_digest, expected_revision=row.revision)
        return self.store.record_result(str(row.pk), result())

    def manifest(self):
        return {'schema': 'cabinet-manifest/v1', 'categories': [
            {'category': key, 'state': 'fresh' if key == 'books' else 'unavailable',
             'code': 'OK' if key == 'books' else 'CAPABILITY_UNAVAILABLE',
             'cards': [candidate()] if key == 'books' else []} for key in self.registry.categories], 'for_you': []}


class PortableCabinetStoreTests(StoreCase):
    def test_idempotency_is_scoped_and_changed_intent_or_input_alias_conflicts(self):
        intent = self.intent()
        row = self.store.admit(intent)
        self.assertEqual(self.store.admit(intent).pk, row.pk)
        changed = EnhanceIntent(str(uuid.uuid4()), intent.client_key, str(uuid.uuid4()), intent.bindings)
        self.fault('IDEMPOTENCY_CONFLICT', self.store.admit, changed)
        alias = EnhanceIntent(str(uuid.uuid4()), str(uuid.uuid4()), intent.refresh_nonce, intent.bindings)
        self.fault('IDEMPOTENCY_CONFLICT', self.store.admit, alias)
        self.assertEqual(SynthesisRun.objects.filter(contract_version='enhance/v1').count(), 1)

    def test_reservation_is_durable_once_and_unknown_blocks_other_keys_after_object_loss(self):
        row = self.sealed()
        reserved = self.store.reserve(str(row.pk), body_digest=row.body_digest, expected_revision=row.revision)
        request_id = reserved.transport_request_id
        self.assertEqual((reserved.attempts, reserved.state), (1, 'running'))
        restarted = self.new_store()
        unknown = restarted.mark_unknown(str(row.pk))
        self.assertEqual(unknown.transport_request_id, request_id)
        self.fault('UNKNOWN_RECONCILIATION_REQUIRED', restarted.admit, self.intent())
        self.fault('UNKNOWN_RECONCILIATION_REQUIRED', restarted.reserve, str(row.pk),
                   body_digest=row.body_digest, expected_revision=unknown.revision)
        self.assertEqual(SynthesisRun.objects.get(pk=row.pk).attempts, 1)

    def test_owner_profile_scope_and_stale_grant_or_expiry_cannot_reserve(self):
        row = self.sealed()
        self.fault('AUTHORITY_DENIED', self.store.status, str(uuid.uuid4()))
        self.provider.source_revision = 'changed-current-source-policy'
        self.fault('PREVIEW_STALE', self.store.reserve, str(row.pk), body_digest=row.body_digest, expected_revision=row.revision)
        self.provider.source_revision = 'isolated-source-rev-1'
        row = self.store.checkpoint(str(row.pk), stage='awaiting_preview', portable_expires_at=self.clock.wall + timedelta(seconds=2))
        self.clock.advance(3)
        self.fault('PREVIEW_STALE', self.store.reserve, str(row.pk), body_digest=row.body_digest, expected_revision=row.revision)
        self.assertEqual(SynthesisRun.objects.get(pk=row.pk).attempts, 0)

    def test_stale_cancel_cas_and_cancel_after_reserve_never_reset_attempt(self):
        row = self.sealed()
        self.fault('REVISION_CONFLICT', self.store.cancel, str(row.pk), expected_revision=row.revision + 1)
        cancelled = self.store.cancel(str(row.pk), expected_revision=row.revision)
        self.assertEqual((cancelled.portable_stage, cancelled.attempts), ('cancelled', 0))
        other = self.sealed()
        reserved = self.store.reserve(str(other.pk), body_digest=other.body_digest, expected_revision=other.revision)
        cancelled = self.store.cancel(str(other.pk), expected_revision=reserved.revision)
        self.assertEqual((cancelled.state, cancelled.attempts), ('unknown', 1))
        self.fault('UNKNOWN_RECONCILIATION_REQUIRED', self.store.admit, self.intent())

    def test_atomic_complete_manifest_cas_preserves_newer_pointer_on_old_response(self):
        old = self.sealed()
        newer = self.completed()
        publication = self.store.publish(str(newer.pk), manifest=self.manifest(), expected_publication_revision=0,
                                         allowed_candidates=[candidate()])
        old = self.completed(old)
        self.fault('REVISION_CONFLICT', self.store.publish, str(old.pk), manifest=self.manifest(),
                   expected_publication_revision=0, allowed_candidates=[candidate()])
        state = ProfileCabinetState.objects.get()
        self.assertEqual((state.active_manifest_ref, state.publication_revision), (publication.pk, 1))
        self.assertEqual(self.store.publish(str(newer.pk), manifest=self.manifest(),
                         expected_publication_revision=0, allowed_candidates=[candidate()]).pk, publication.pk)
        self.assertEqual(PortableCabinetManifest.objects.count(), 1)

    def test_incomplete_or_invented_publication_is_refused_and_reads_filter_current_cards(self):
        row = self.completed()
        incomplete = self.manifest()
        incomplete['categories'].pop()
        self.fault('CONTRACT_INVALID', self.store.publish, str(row.pk), manifest=incomplete,
                   expected_publication_revision=0, allowed_candidates=[candidate()])
        publication = self.store.publish(str(row.pk), manifest=self.manifest(), expected_publication_revision=0,
                                         allowed_candidates=[candidate()])
        self.provider.drop = True
        _, current = self.store.read_current()
        self.assertEqual(current['categories'][0]['cards'], [])
        self.provider.source_revision = 'revoked-policy'
        self.fault('PREVIEW_STALE', self.store.read_current)
        self.assertEqual(ProfileCabinetState.objects.get().active_manifest_ref, publication.pk)

    def test_raw_bulk_deletion_and_attempt_regression_guards_preserve_legacy_defaults(self):
        legacy = SynthesisRun.objects.create(actor_id=self.store.current().view.actor_id,
            profile_id=self.store.current().view.profile_id, input_snapshot={}, input_digest='a' * 64)
        self.assertEqual((legacy.contract_version, legacy.mode), ('legacy/v1', 'household'))
        row = self.sealed()
        for operation in (lambda: SynthesisRun.objects.filter(pk=row.pk).update(attempts=1),
                          lambda: SynthesisRun.objects.filter(pk=row.pk).delete()):
            with self.assertRaises(DatabaseError), transaction.atomic():
                operation()
        reserved = self.store.reserve(str(row.pk), body_digest=row.body_digest, expected_revision=row.revision)
        with self.assertRaises(DatabaseError), transaction.atomic(), ga._write_window():
            SynthesisRun.objects.filter(pk=row.pk).update(attempts=0, revision=reserved.revision + 1)
        self.assertEqual(SynthesisRun.objects.get(pk=row.pk).attempts, 1)

    def test_reconciliation_accepts_only_original_authenticated_receipt_without_reset(self):
        row = self.sealed()
        reserved = self.store.reserve(str(row.pk), body_digest=row.body_digest, expected_revision=row.revision)
        self.store.mark_unknown(str(row.pk))
        self.fault('CAPABILITY_UNAVAILABLE', self.store.reconcile, str(row.pk), receipt_ref=str(uuid.uuid4()), reconciler=None)
        class ReceiptPort:
            def verify(inner, run, receipt_ref):
                return (str(reserved.transport_request_id), 'proven_no_send', None)
        self.fault('CONTRACT_INVALID', self.store.reconcile, str(row.pk), receipt_ref={'state': 'completed'}, reconciler=ReceiptPort())
        settled = self.store.reconcile(str(row.pk), receipt_ref=str(uuid.uuid4()), reconciler=ReceiptPort())
        self.assertEqual((settled.state, settled.attempts), ('failed', 1))
        self.assertEqual(settled.transport_request_id, reserved.transport_request_id)


    def test_content_redaction_retains_unknown_attempt_pointer_and_rejects_refill(self):
        completed = self.completed()
        publication = self.store.publish(str(completed.pk), manifest=self.manifest(),
            expected_publication_revision=0, allowed_candidates=[candidate()])
        pending = self.sealed()
        pending = self.store.reserve(str(pending.pk), body_digest=pending.body_digest,
                                     expected_revision=pending.revision)
        pending = self.store.mark_unknown(str(pending.pk))
        request = pending.transport_request_id
        self.assertTrue(self.store.redact_profile())
        self.assertFalse(self.store.redact_profile())
        publication.refresh_from_db(); pending.refresh_from_db(); completed.refresh_from_db()
        self.assertEqual(publication.manifest, {})
        self.assertEqual((pending.state, pending.attempts, pending.transport_request_id), ('unknown', 1, request))
        self.assertEqual(pending.output, {}); self.assertEqual(pending.evidence, {})
        self.assertEqual(len(pending.content_digest), 64)
        self.assertIsNotNone(pending.content_redacted_at)
        state = ProfileCabinetState.objects.get()
        self.assertEqual((state.active_manifest_ref, state.publication_revision), (publication.pk, 1))
        with self.assertRaises(DatabaseError), transaction.atomic(), ga._write_window():
            SynthesisRun.objects.filter(pk=pending.pk).update(output={'refilled': True}, revision=pending.revision + 1)
        with self.assertRaises(DatabaseError), transaction.atomic(), ga._write_window():
            PortableCabinetManifest.objects.filter(pk=publication.pk).update(manifest=self.manifest())
        self.fault('UNKNOWN_RECONCILIATION_REQUIRED', self.store.admit, self.intent())
        self.fault('PREVIEW_STALE', self.store.read_current)
        class Receipt:
            def verify(self, row, ref):
                return str(row.transport_request_id), 'proven_no_send', None
        resolved = self.store.reconcile(str(pending.pk), receipt_ref=str(uuid.uuid4()), reconciler=Receipt())
        self.assertEqual((resolved.state, resolved.attempts, resolved.transport_request_id), ('failed', 1, request))
        self.assertEqual(resolved.output, {})
