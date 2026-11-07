"""Authority orchestration fixtures; full codec/noninterference belongs to Taste.

The isolated codec double tests gate ordering and exact reconstruction only. It
is never selectable by product composition, and never substitutes for live proof.
"""
from dataclasses import dataclass
from datetime import timedelta
import json
from unittest.mock import patch

from django.db import transaction

from shelf import grant_authority as ga, portable_disclosure as pd
from shelf.models import DiscoveryState, PortableDisclosurePreview
from .test_grant_authority import AuthorityCase, RecoveryCase


@dataclass(frozen=True)
class EncodedFixture:
    body: bytes
    serializer_version: str
    omitted_groups: tuple
    expires_at: object


class PortableDisclosureTests(AuthorityCase):
    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.consent(self.profile_ref)
        self.collection = self.disclosure.fixture_collection(
            [{'explicit_test_candidate': True}], {}, {}, expires_at=self.expires)
        self.body = b'{"schema":"isolated-codec-double","note":"SYNTHETIC-NOTE-CANARY"}'
        self.encoded = EncodedFixture(self.body, 'isolated-codec-double/v1', ('raw_notes',), self.expires)
        self.codec = self.enterContext(patch.object(pd, '_compose', return_value=self.encoded))

    def preview(self, **overrides):
        arguments = dict(profile_ref=self.profile_ref, connection_ref=str(self.config.pk),
                         collection_ref=self.collection.ref)
        arguments.update(overrides)
        return pd.preview(self.authority, self.ref, **arguments)

    def capture(self, receipt):
        return pd.isolated_capture(receipt['preview_ref'], self.authority, self.ref)

    def test_exact_capture_uses_one_codec_without_persisting_body_or_secret(self):
        receipt = self.preview()
        self.assertEqual(receipt['body'], self.body)
        self.assertEqual(self.capture(receipt), self.body)
        self.assertFalse(receipt['run_ready'])
        row = PortableDisclosurePreview.objects.get()
        stored = json.dumps({'refs': row.input_refs, 'bindings': row.bindings, 'omissions': row.omitted_groups})
        for canary in ('SYNTHETIC-NOTE-CANARY', 'SYNTHETIC-CONNECTION-CANARY-KEY', self.ref):
            self.assertNotIn(canary, stored)
        self.assertEqual(self.fake.calls, 1)  # Existing synthetic neutral probe only.

    def test_changed_codec_bytes_or_version_are_never_captured(self):
        receipt = self.preview()
        self.codec.return_value = EncodedFixture(b'{"changed":true}', 'isolated-codec-double/v1', ('raw_notes',), self.expires)
        self.fault('PREVIEW_STALE', self.capture, receipt)
        self.assertEqual(PortableDisclosurePreview.objects.get().state, 'stale')
        self.assertEqual(self.fake.calls, 1)

    def test_denied_note_edit_invalidates_complete_revision_with_same_body(self):
        state = ga.ProfileCabinetState.objects.get()
        history = DiscoveryState.objects.create(actor_id=state.actor_id, profile_id=state.profile_id,
            mode='household', nonce='synthetic', history=[])
        receipt = self.preview()
        history.history = [{'operation': 'respond', 'key': 'synthetic:one',
                            'choice': 'intrigued', 'note': 'unshared changed note'}]
        history.revision += 1
        history.save(update_fields=['history', 'revision'])
        self.fault('PREVIEW_STALE', self.capture, receipt)
        self.assertEqual(PortableDisclosurePreview.objects.get().state, 'stale')
        self.assertEqual(self.codec.call_count, 1)  # Stale before codec/capture.

    def test_grant_aba_and_credential_change_do_not_revive_old_preview(self):
        receipt = self.preview()
        self.consent(self.profile_ref, action='revoke', revision=1)
        self.consent(self.profile_ref, action='regrant', revision=2)
        self.fault('PREVIEW_STALE', self.capture, receipt)
        newer = self.preview()
        self.put()
        self.fault('VALIDATION_STALE', self.capture, newer)
        self.assertEqual(self.fake.calls, 1)

    def test_either_clock_expiry_and_lost_memory_fail_closed(self):
        receipt = self.preview()
        self.clock.tick += 300
        self.fault('PREVIEW_STALE', self.capture, receipt)
        self.clock.tick -= 300
        receipt = self.preview()
        self.clock.wall += timedelta(seconds=300)
        self.fault('PREVIEW_STALE', self.capture, receipt)
        self.clock.wall -= timedelta(seconds=300)
        receipt = self.preview()
        self.disclosure._live.clear()
        self.fault('PREVIEW_STALE', self.capture, receipt)

    def test_foreign_refs_missing_collection_and_normal_fake_proof_are_denied(self):
        import uuid
        self.fault('AUTHORITY_DENIED', self.preview, profile_ref=str(uuid.uuid4()))
        self.fault('CAPABILITY_UNAVAILABLE', self.preview, collection_ref=str(uuid.uuid4()))
        del self.authority._disclosure_fixture_token
        self.fault('VALIDATION_STALE', self.preview)
        self.fault('CAPABILITY_UNAVAILABLE', pd.isolated_capture, str(uuid.uuid4()), self.authority, self.ref)
        self.assertFalse(PortableDisclosurePreview.objects.exists())

    def test_no_candidates_and_codec_failure_persist_no_preview(self):
        empty = self.disclosure.fixture_collection([], {}, {}, expires_at=self.expires)
        self.fault('NO_GROUNDED_CANDIDATES', self.preview, collection_ref=empty.ref)
        self.codec.side_effect = pd.DisclosureError('AUTHORITY_DENIED')
        self.fault('AUTHORITY_DENIED', self.preview)
        self.assertFalse(PortableDisclosurePreview.objects.exists())

    def test_preview_rewrite_and_delete_are_blocked_even_in_service_window(self):
        receipt = self.preview()
        self.rejected_write(lambda: PortableDisclosurePreview.objects.update(body_digest='a' * 64))
        def rewrite():
            with transaction.atomic(), ga._write_window():
                PortableDisclosurePreview.objects.update(body_digest='b' * 64)
        self.rejected_write(rewrite)
        self.rejected_write(lambda: PortableDisclosurePreview.objects.all().delete())
        self.assertEqual(self.capture(receipt), self.body)


class RecoveryDisclosureTests(RecoveryCase):
    def test_recovery_denial_prevents_preview_reconstruction_and_capture_bytes(self):
        profile = self.bind()
        self.consent(profile)
        collection = self.disclosure.fixture_collection([{'explicit_test_candidate': True}], {}, {},
            expires_at=self.expires)
        body = b'{"schema":"isolated-recovery-codec-double"}'
        encoded = EncodedFixture(body, 'isolated-codec-double/v1', (), self.expires)
        def make_preview():
            return pd.preview(self.authority, self.ref, profile_ref=profile,
                connection_ref=str(self.config.pk), collection_ref=collection.ref)
        def reconstruct(receipt):
            return pd.revalidate(receipt['preview_ref'], self.authority, self.ref)
        def capture(receipt):
            return pd.isolated_capture(receipt['preview_ref'], self.authority, self.ref)
        with patch.object(pd, '_compose', return_value=encoded) as codec:
            receipt = make_preview()
            self.assertEqual(reconstruct(receipt), body)
            self.assertEqual(capture(receipt), body)
            self.assertFalse(receipt['run_ready'])
            for state in ('pending', 'committed', 'foreign_witness', 'closed', 'blocked', 'lost'):
                with self.subTest(state=state), self.controlled_recovery_state(state) as current:
                    before = self.recovery_rows()
                    calls = codec.call_count
                    for operation in (make_preview, lambda: reconstruct(receipt), lambda: capture(receipt)):
                        self.fault('CAPABILITY_UNAVAILABLE', operation)
                        self.assertEqual(self.recovery_rows(), before)
                        self.assertEqual(codec.call_count, calls)
            for state in ('pending', 'closed', 'lost'):
                for operation_name in ('preview', 'reconstruct', 'capture'):
                    with self.subTest(during=state, operation=operation_name), self.controlled_recovery_state('clear') as current:
                        before = self.recovery_rows()
                        def interrupt(*args):
                            if state == 'pending':
                                self.prepare_restriction(current)
                            elif state == 'closed':
                                current.close()
                            else:
                                (current.directory / 'journal').unlink()
                            return encoded
                        with patch.object(pd, '_compose', side_effect=interrupt):
                            operation = {'preview': make_preview,
                                'reconstruct': lambda: reconstruct(receipt),
                                'capture': lambda: capture(receipt)}[operation_name]
                            self.fault('CAPABILITY_UNAVAILABLE', operation)
                        self.assertEqual(self.recovery_rows(), before)
            self.assertEqual(self.fake.calls, 1)
