"""Trusted ORM snapshots in the existing isolated owner/proof fixture only."""
from contextlib import contextmanager, ExitStack
from dataclasses import replace
from unittest.mock import patch
import uuid

from shelf import grant_authority as ga, profile_snapshot as snapshots
from shelf.models import DiscoveryState, IntakeRun, ProfileCabinetState, SavedItem
from .test_grant_authority import AuthorityCase, RecoveryCase, BrokerHistoryCase


class SnapshotCase(AuthorityCase):
    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.classes = ['canonical_seed', 'explicit_favourites', 'anticipated_reactions',
                        'experienced_enjoyment', 'inferred_traits']
        self.consent(self.profile_ref, classes=self.classes)
        self.state = ProfileCabinetState.objects.get()
        self.history = DiscoveryState.objects.create(actor_id=self.state.actor_id,
            profile_id=self.state.profile_id, mode='household', nonce='isolated-history', history=[])

    def card(self, key='source:book:a', **overrides):
        card = {'key': key, 'title': 'Synthetic judged title', 'medium': 'book',
                'source': 'Synthetic test', 'url': 'https://example.invalid/book',
                'provenance': 'live-automated', 'lane': '', 'roots': [],
                'fetched_at': self.clock.wall.isoformat()}
        card.update(overrides)
        return card

    def event(self, operation='respond', **fields):
        card = fields.pop('card', self.card())
        event = {'operation': operation, 'key': card['key'], 'card': card, 'choice': '',
            'mode': 'household', 'origin': 'authenticated-human-post', 'at': self.clock.wall.isoformat()}
        if operation in ('undo', 'restart'):
            event.pop('card')
            event['key'] = ''
        event.update(fields)
        self.history.history.append(event)
        self.history.revision += 1
        self.history.save(update_fields=['history', 'revision'])
        return event

    def snapshot(self):
        return snapshots.capture(self.view(self.profile_ref))

    def allow_notes(self):
        self.consent(self.profile_ref, action='revoke', revision=1, classes=self.classes)
        self.consent(self.profile_ref, action='regrant', revision=2, classes=self.classes + ['raw_notes'])


class ProfileSnapshotTests(SnapshotCase):
    def test_capture_uses_bound_owner_ids_complete_revision_and_detached_values(self):
        self.event(choice='intrigued', note='Original note')
        view = self.view(self.profile_ref)
        value = snapshots.capture(view)
        self.assertEqual(value['installation_id'], view.installation_id)
        self.assertEqual(value['profile_ref'], view.profile_ref)
        self.assertEqual(value['profile_revision_digest'], view.profile_revision_digest)
        self.assertEqual(value['source_revisions'], view.source_revisions)
        self.assertTrue(all(n['author_ref'] == view.actor_ref and n['applicable_subjects'] == [view.subject_ref]
                            for n in value['evidence']))
        value['evidence'][0]['title'] = 'Changed detached object'
        self.assertEqual(self.history.history[0]['card']['title'], 'Synthetic judged title')
        self.assertNotIn('Changed detached object', str(self.snapshot()))

    def test_exact_notes_anticipated_experienced_and_favourite_meanings_remain_distinct(self):
        text = '  e\u0301 / \u00e9\t\r\n\u96ea  '
        self.event(choice='intrigued', note=text)
        self.event('rate', basis='interest', value=2, experienced=False)
        self.event('rate', basis='enjoyment', value=5, experienced=True)
        SavedItem.objects.create(actor_id=self.state.actor_id, profile_id=self.state.profile_id,
            provider='hardcover', medium='book', source_id=10, list_kind='favorites',
            title='Explicit favourite', url='https://example.invalid/10', fetched_at=self.clock.wall)
        nodes = self.snapshot()['evidence']
        values = {n['basis']: n['value'] for n in nodes}
        self.assertEqual(values['feedback_note'], {'reaction': 'intrigued', 'text': text})
        self.assertEqual(values['anticipated_reaction'], 'intrigued')
        self.assertEqual(values['interest'], 2)
        self.assertEqual(values['enjoyment'], 5)
        self.assertIs(values['favourite'], True)

    def test_clear_undo_restart_and_note_replacement_replay_current_history(self):
        self.event('rate', basis='interest', value=3, experienced=False)
        self.event('rate', basis='enjoyment', value=4, experienced=True)
        self.event('rate', basis='interest', value=None, experienced=False)
        self.assertEqual([n['basis'] for n in self.snapshot()['evidence']], ['enjoyment'])
        self.event('undo')
        self.assertEqual({n['basis'] for n in self.snapshot()['evidence']}, {'interest', 'enjoyment'})
        self.event(choice='intrigued', note='Earlier exact note')
        self.event(choice='hate_it', note='Replacement note')
        self.event('undo')
        self.assertEqual(next(n['value']['text'] for n in self.snapshot()['evidence']
                              if n['basis'] == 'feedback_note'), 'Earlier exact note')
        self.event('restart')
        self.assertEqual(self.snapshot()['evidence'], [])
        self.event('undo')
        self.assertEqual(len(self.snapshot()['evidence']), 4)

    def test_foreign_actor_and_unknown_identity_lineage_are_never_adopted(self):
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user(username='Synthetic unrelated contributor')
        self.event(choice='intrigued')
        DiscoveryState.objects.create(actor=other, profile_id=self.state.profile_id,
            mode='household', nonce='foreign', history=self.history.history)
        self.assertEqual(len(self.snapshot()['evidence']), 1)
        self.event(choice='hate_it', card=self.card('source:book:b', roots=['unknown-derived-query']))
        node = next(n for n in self.snapshot()['evidence'] if n['key'] == 'source:book:b')
        self.assertEqual(len(node['direct_parent_ids']), 1)
        self.assertNotIn(node['direct_parent_ids'][0], {n['id'] for n in self.snapshot()['evidence']})

    def test_browser_shaped_views_wrong_scope_and_forged_tokens_are_rejected(self):
        view = self.view(self.profile_ref)
        for forged in ({'profile_ref': view.profile_ref}, replace(view, _token=object()),
                       replace(view, profile_ref=str(uuid.uuid4()))):
            with self.subTest(kind=type(forged)):
                self.fault('AUTHORITY_DENIED', snapshots.capture, forged)

    def test_note_edit_changes_full_local_revision_even_without_disclosure_permission(self):
        self.event(choice='intrigued', note='PRIVATE-NOTE-A')
        old = self.view(self.profile_ref)
        self.history.history[0]['note'] = 'PRIVATE-NOTE-B'
        self.history.save(update_fields=['history'])
        self.fault('PREVIEW_STALE', snapshots.capture, old)
        new = self.view(self.profile_ref)
        self.assertNotEqual(old.profile_revision_digest, new.profile_revision_digest)
        self.assertEqual(next(n['value']['text'] for n in snapshots.capture(new)['evidence']
                              if n['basis'] == 'feedback_note'), 'PRIVATE-NOTE-B')

    def test_change_during_capture_is_rechecked_before_returning_snapshot(self):
        self.event(choice='intrigued', note='Before race')
        view = self.view(self.profile_ref)
        original, calls = ga.assert_current, []
        def competing_write(current):
            calls.append(1)
            if len(calls) == 3:
                self.history.history[0]['note'] = 'After race'
                self.history.save(update_fields=['history'])
            return original(current)
        with patch.object(ga, 'assert_current', side_effect=competing_write):
            self.fault('PREVIEW_STALE', snapshots.capture, view)
        self.assertEqual(len(calls), 3)

    def test_unsupported_legacy_streams_and_malformed_history_fail_closed(self):
        self.history.history = [{'operation': 'rate', 'choice': '', 'key': 'x'}]
        self.history.save(update_fields=['history'])
        self.fault('CONTRACT_INVALID', self.snapshot)
        self.history.history = []
        self.history.save(update_fields=['history'])
        IntakeRun.objects.create(actor_id=self.state.actor_id, profile_id=self.state.profile_id,
                                 nonce='unsupported-import', history=[])
        self.fault('CAPABILITY_UNAVAILABLE', self.snapshot)


class RecoverySnapshotTests(RecoveryCase):
    def test_pending_and_lost_witness_cannot_return_snapshot(self):
        from django.db import connection
        from shelf.models import RecoveryRestriction

        profile = self.bind()
        self.consent(profile)
        initial = self.view(profile)
        DiscoveryState.objects.create(actor_id=initial.actor_id, profile_id=initial.profile_id,
            mode='household', nonce='recovery-snapshot', history=[])
        view = self.view(profile)
        expected = snapshots.capture(view)
        self.assertEqual(expected['profile_ref'], profile)
        self.assertEqual(expected['profile_revision_digest'], view.profile_revision_digest)
        for state in ('empty', 'clear'):
            with self.subTest(positive=state), self.controlled_recovery_state(state):
                self.assertEqual(snapshots.capture(view), expected)

        for state in ('pending', 'committed', 'lost', 'foreign_witness', 'closed', 'blocked', 'normal_disk'):
            with self.subTest(entry=state), self.controlled_recovery_state(state) as current:
                before, returned = self.recovery_rows(), []
                if state == 'pending':
                    self.assertIsNotNone(current.pending)
                    self.assertFalse(RecoveryRestriction.objects.exists())
                with patch.object(snapshots, '_history', side_effect=AssertionError('entry denial must precede reads')) as reads:
                    self.fault('CAPABILITY_UNAVAILABLE', lambda: returned.append(snapshots.capture(view)))
                reads.assert_not_called()
                self.assertEqual(returned, [])
                self.assertEqual(self.recovery_rows(), before)

        # Wrap the real guard without replacing its checks. Inject a real recovery
        # transition only after capture's reads/transaction, before its outer exit.
        # This distinguishes an enclosing barrier from transient authority checks.
        original_guard, original_history = ga.recovery_guard, snapshots._history
        for state in ('pending', 'committed', 'lost', 'foreign_witness', 'closed'):
            with self.subTest(after_reads=state), self.controlled_recovery_state('clear') as current:
                before, returned, read_complete, transition_rows = self.recovery_rows(), [], [], []
                depth = 0
                def history_read(current_view, row):
                    value = original_history(current_view, row)
                    read_complete.append(True)
                    return value
                @contextmanager
                def interrupted_guard(*args, **kwargs):
                    nonlocal depth
                    depth += 1
                    try:
                        with ExitStack() as held_states:
                            with original_guard(*args, **kwargs) as checkpoint:
                                yield checkpoint
                                if depth == 1:
                                    self.assertTrue(read_complete)
                                    self.assertFalse(connection.in_atomic_block)
                                    if state in ('pending', 'committed'):
                                        pending = self.prepare_restriction(current)
                                        self.assertFalse(RecoveryRestriction.objects.exists())
                                        if state == 'committed':
                                            current.apply_pending(connection.connection)
                                            current.finalize(pending, pending['local_commit_uuid'])
                                            self.assertIsNone(current.pending)
                                            self.assertEqual(len(current.committed_restrictions()), 1)
                                    elif state == 'lost':
                                        (current.directory / 'journal').unlink()
                                    elif state == 'foreign_witness':
                                        held_states.enter_context(self.controlled_recovery_state('foreign_witness'))
                                    else:
                                        current.close()
                                    transition_rows.append(self.recovery_rows())
                    finally:
                        depth -= 1
                with patch.object(ga, 'recovery_guard', interrupted_guard), patch.object(snapshots, '_history', history_read):
                    self.fault('CAPABILITY_UNAVAILABLE', lambda: returned.append(snapshots.capture(view)))
                self.assertTrue(read_complete)
                self.assertEqual(returned, [])
                # A real committed restriction may change its local Grant. Only
                # those recovery writes are permitted; capture adds no writes.
                self.assertEqual(len(transition_rows), 1)
                self.assertEqual(self.recovery_rows(), transition_rows[0])
                if state != 'committed':
                    self.assertEqual(self.recovery_rows(), before)
        self.assertEqual(self.fake.calls, 1)  # Existing neutral probe only; no extra send.


class BrokerSnapshotTests(BrokerHistoryCase):
    card = SnapshotCase.card
    event = SnapshotCase.event

    def test_guarded_progress_preserves_evidence_but_human_edits_stale_snapshots(self):
        from .test_portable_cabinet_store import StoreCase
        self.history = DiscoveryState.objects.create(actor_id=self.state.actor_id,
            profile_id=self.state.profile_id, mode='household', nonce='broker-human-history', history=[])
        self.event(choice='intrigued', note='Exact undisclosed note')
        view = self.view(self.profile_ref)
        expected = snapshots.capture(view)
        row = StoreCase.sealed(self, self.intent())
        self.assertEqual(snapshots.capture(view), expected)
        row = self.store.reserve(str(row.pk), body_digest=row.body_digest, expected_revision=row.revision)
        self.store.mark_unknown(str(row.pk))
        self.assertEqual(snapshots.capture(view), expected)

        def edit_note():
            self.history.history[0]['note'] = 'Changed undisclosed note'
            self.history.save(update_fields=['history'])
        def favourite():
            return SavedItem.objects.create(actor_id=self.state.actor_id, profile_id=self.state.profile_id,
                provider='hardcover', medium='book', source_id=81, list_kind='favorites',
                title='Synthetic explicit favourite', url='https://example.invalid/81', fetched_at=self.clock.wall)
        changes = [lambda: self.event('rate', basis='interest', value=4, experienced=False), edit_note,
                   lambda: self.event('rate', basis='interest', value=None, experienced=False),
                   favourite, lambda: SavedItem.objects.filter(source_id=81).delete(),
                   lambda: self.event('undo'), lambda: self.event('restart')]
        for index, change in enumerate(changes):
            with self.subTest(human_change=index):
                old = self.view(self.profile_ref)
                change()
                self.fault('PREVIEW_STALE', snapshots.capture, old)
                fresh = self.view(self.profile_ref)
                self.assertNotEqual(old.profile_revision_digest, fresh.profile_revision_digest)
                self.assertEqual(snapshots.capture(fresh)['profile_revision_digest'], fresh.profile_revision_digest)
        self.assertEqual(snapshots.capture(self.view(self.profile_ref))['evidence'], [])
        self.assertEqual(self.fake.calls, 1)

    def test_expired_revoked_and_removed_historical_bindings_never_authorize_new_send(self):
        from datetime import timedelta
        from shelf.models import SynthesisRun
        from shelf.portable_authority import AuthorityFault
        from .test_connection_validation import CONFIG
        from .test_portable_cabinet_store import StoreCase
        view = self.view(self.profile_ref)
        row = StoreCase.sealed(self, self.intent())
        row = self.store.checkpoint(str(row.pk), stage='awaiting_preview',
            portable_expires_at=self.clock.wall + timedelta(seconds=2))
        self.clock.advance(3)
        self.assertEqual(snapshots.capture(view)['profile_revision_digest'], view.profile_revision_digest)
        self.fault('PREVIEW_STALE', self.store.reserve, str(row.pk),
                   body_digest=row.body_digest, expected_revision=row.revision)
        self.consent(self.profile_ref, action='revoke', revision=1)
        self.fault('AUTHORITY_DENIED', self.view, self.profile_ref)
        self.state.refresh_from_db()
        self.assertEqual(ga.synthesis_history(self.state), [])
        self.consent(self.profile_ref, action='regrant', revision=2)
        fresh = self.view(self.profile_ref)
        self.assertGreater(fresh.policy_epoch, view.policy_epoch)
        self.assertEqual(snapshots.capture(fresh)['evidence'], [])
        self.fault('PREVIEW_STALE', snapshots.capture, view)
        self.config = self.vault.configure(self.ref, CONFIG, connection_id=self.config.pk, revision=1)
        self.state.refresh_from_db()
        self.assertEqual(ga.synthesis_history(self.state), [])
        with self.assertRaises(AuthorityFault):
            self.store.reserve(str(row.pk), body_digest=row.body_digest, expected_revision=row.revision)
        self.vault.remove(self.ref, self.config.pk, self.config.config_revision, disable=True)
        self.assertEqual(ga.synthesis_history(self.state), [])
        self.fault('AUTHORITY_DENIED', self.view, self.profile_ref)
        self.assertEqual(SynthesisRun.objects.get(pk=row.pk).attempts, 0)
        self.assertEqual(self.fake.calls, 1)


class BrokerRecoverySnapshotTests(RecoveryCase):
    def test_bookkeeping_exclusion_never_bypasses_pending_witness_or_policy_floors(self):
        from shelf.enhance_contract import EnhanceIntent, RegistryView
        from shelf.portable_cabinet_store import CabinetStore
        from .test_portable_cabinet_store import IsolatedStateProvider
        self.profile_ref = self.bind()
        self.consent(self.profile_ref)
        self.consent(self.profile_ref, purpose='catalogue_lookup')
        self.registry = RegistryView('1', ('books', 'films', 'tv', 'music', 'events', 'outings'))
        store = CabinetStore.isolated_test(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), state_provider=IsolatedStateProvider(self))
        bindings = store.current().bindings
        store.admit(EnhanceIntent(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), bindings))
        view = self.view(self.profile_ref)
        expected = snapshots.capture(view)
        with self.controlled_recovery_state('clear'):
            self.assertEqual(snapshots.capture(view), expected)
        for state in ('pending', 'committed', 'lost', 'foreign_witness', 'closed', 'blocked'):
            with self.subTest(recovery=state), self.controlled_recovery_state(state):
                self.fault('CAPABILITY_UNAVAILABLE', snapshots.capture, view)
        self.consent(self.profile_ref, action='revoke', revision=1)
        self.consent(self.profile_ref, action='regrant', revision=2)
        self.fault('PREVIEW_STALE', snapshots.capture, view)
        self.assertGreater(self.view(self.profile_ref).policy_epoch, view.policy_epoch)
        self.assertEqual(self.fake.calls, 1)
