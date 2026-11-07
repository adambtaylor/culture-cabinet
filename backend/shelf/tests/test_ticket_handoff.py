"""Fictional receipts are state-machine evidence, never purchase/Wallet evidence."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from shelf.acquisition_contract import Blocked, ContractError
from shelf.calendar_handoff import Event
from shelf import ticket_handoff as t


def offer(**changes):
    event = Event('fixture-event', 'fixture-session', 'Synthetic concert',
        '2027-01-15T20:00:00+11:00', '2027-01-15T22:00:00+11:00',
        'Australia/Melbourne', 'Synthetic venue', 'https://events.example.invalid/concert',
        'Synthetic source fixture', status='scheduled', source_verified=True)
    return replace(t.TicketOffer(event, 2, 'AUD', '120.50',
        'https://seller.example.invalid/concert', 'Synthetic official seller',
        seller_verified=True, shop_eligibility='eligible'), **changes)


def requested():
    return t.TicketHandoff('fixture-actor', 'fixture-request', offer())


def approve(state=None, **changes):
    state = state or requested()
    values = dict(actor_id=state.actor_id, expected_revision=state.revision,
                  preview_digest=state.preview_digest, user_confirmed=True)
    return state.approve(**(values | changes))


def reserve(state=None, **changes):
    state = state or approve()
    values = dict(actor_id=state.actor_id, current_offer=state.offer,
                  expected_revision=state.revision, preview_digest=state.preview_digest,
                  human_final_click_authorized=True, adapter=t.ManualOfficialTicketAdapter())
    return state.reserve_submission(**(values | changes))


def purchased():
    state, _ = reserve()
    return state.confirm_purchase(actor_id=state.actor_id, offer_digest=state.preview_digest,
        order_reference='fixture-order', issuer_verified=True)


def issued():
    state = purchased()
    return state.record_ticket_issued(actor_id=state.actor_id, order_reference='fixture-order',
        ticket_reference='fixture-ticket', issuer_verified=True)


def artifact(**changes):
    return replace(t.WalletArtifact('issuer_pkpass', 'https://issuer.example.invalid/pass.pkpass',
        'https://issuer.example.invalid', 'Synthetic issuer', issuer_verified=True), **changes)


class TicketHandoffTests(TestCase):
    def test_preview_is_exact_private_and_never_implies_purchase(self):
        state = requested()
        view = state.preview()
        self.assertEqual(view['purchase_state'], 'requested')
        self.assertEqual(view['quantity'], 2)
        self.assertEqual(view['currency'], 'AUD')
        self.assertEqual(view['total'], '120.50')
        self.assertEqual(view['event']['session_id'], 'fixture-session')
        self.assertEqual(view['official_seller_url'], state.offer.official_seller_url)
        self.assertEqual(view['source_label'], 'Synthetic source fixture')
        self.assertIn('Private', view['privacy_label'])
        for key in ('autonomous_payment', 'ticket_issued', 'issuer_wallet_pass_available', 'user_wallet_add_confirmed'):
            self.assertFalse(view[key])

    def test_approval_is_explicit_actor_revision_and_exact_preview_bound(self):
        for changes in ({'actor_id': 'other'}, {'expected_revision': 1},
                        {'preview_digest': 'wrong'}, {'user_confirmed': False}, {'user_confirmed': 1}):
            with self.subTest(changes=changes), self.assertRaises(Blocked):
                approve(**changes)
        state = approve()
        self.assertEqual(state.purchase_state, 'approved')
        self.assertEqual(approve(state), state)
        self.assertIsNone(state.order_reference)

    def test_every_material_offer_change_invalidates_approval(self):
        state = approve()
        changes = [replace(state.offer, **values) for values in (
            {'quantity': 3}, {'currency': 'USD'}, {'total': '121.00'},
            {'official_seller_url': 'https://seller.example.invalid/changed'},
            {'seller_verified': False}, {'shop_eligibility': 'unknown'})]
        changes += [replace(state.offer, event=replace(state.offer.event, **values)) for values in (
            {'event_id': 'other-event'}, {'session_id': 'other-session'},
            {'start': '2027-01-15T21:00:00+11:00'}, {'status': 'cancelled'})]
        for changed in changes:
            with self.subTest(changed=changed):
                revised = state.revise(changed, actor_id=state.actor_id, expected_revision=0)
                self.assertEqual(revised.purchase_state, 'requested')
                self.assertIsNone(revised.approval_digest)
                self.assertEqual(revised.revision, 1)
                with self.assertRaises(Blocked):
                    approve(revised, preview_digest=state.preview_digest)
                with self.assertRaises(Blocked):
                    reserve(state, current_offer=changed)

    def test_unknown_unsupported_shop_never_falls_back_to_manual(self):
        for eligibility in ('unknown', 'unsupported'):
            state = replace(requested(), offer=offer(shop_eligibility=eligibility))
            with self.subTest(eligibility=eligibility), self.assertRaises(Blocked):
                approve(state)
            with self.assertRaises(Blocked):
                t.ManualOfficialTicketAdapter().prepare(state.offer)
            self.assertIn('no fallback bypass', ' '.join(state.preview()['blockers']))

    def test_unknown_cancelled_and_unverified_events_block(self):
        for change in ({'start': None}, {'end': None}, {'timezone': None},
                       {'status': 'unknown'}, {'status': 'cancelled'}, {'source_verified': False}):
            state = requested()
            state = replace(state, offer=replace(state.offer, event=replace(state.offer.event, **change)))
            with self.subTest(change=change), self.assertRaises(Blocked):
                approve(state)
        with self.assertRaises(Blocked):
            approve(replace(requested(), offer=offer(seller_verified=False)))

    def test_adapter_default_and_missing_final_click_authorization_block(self):
        state = approve()
        with self.assertRaises(Blocked):
            reserve(state, adapter=None)
        with self.assertRaises(Blocked):
            reserve(state, human_final_click_authorized=False)
        with self.assertRaises(Blocked):
            reserve(requested())
        self.assertEqual(state.purchase_state, 'approved')

    def test_manual_handoff_only_human_payment_and_unknown_receipt(self):
        state, instructions = reserve()
        self.assertEqual(state.purchase_state, 'submitted')
        self.assertEqual(state.submission_outcome, 'unknown')
        self.assertIsNone(state.order_reference)
        self.assertEqual(instructions['payment_actor'], 'human')
        self.assertFalse(instructions['autonomous_payment'])
        self.assertIn('remote desktop', instructions['action'])
        self.assertIn('do not purchase again', state.preview()['message'])

    def test_repeat_click_and_restart_never_prepare_another_attempt(self):
        adapter = Mock(wraps=t.ManualOfficialTicketAdapter())
        state, _ = reserve(adapter=adapter)
        state = t.TicketHandoff.from_dict(json.loads(json.dumps(state.to_dict())))
        for _ in range(3):
            duplicate, instructions = reserve(state, adapter=adapter)
            self.assertEqual(duplicate, state)
            self.assertIsNone(instructions)
        adapter.prepare.assert_called_once()
        self.assertEqual(approve(state), state)
        with self.assertRaises(Blocked):
            state.revise(offer(total='130.00'), actor_id=state.actor_id, expected_revision=0)

    def test_purchase_confirmation_is_separate_and_issuer_bound(self):
        state, _ = reserve()
        args = dict(actor_id=state.actor_id, offer_digest=state.preview_digest, order_reference='fixture-order', issuer_verified=True)
        for change in ({'issuer_verified': False}, {'offer_digest': 'wrong'}, {'actor_id': 'other'}):
            with self.subTest(change=change), self.assertRaises(Blocked):
                state.confirm_purchase(**(args | change))
        with self.assertRaises(Blocked):
            approve().confirm_purchase(**args)
        state = state.confirm_purchase(**args)
        self.assertEqual(state.purchase_state, 'confirmed')
        self.assertFalse(state.preview()['ticket_issued'])
        self.assertEqual(state.confirm_purchase(**args), state)
        with self.assertRaises(Blocked):
            state.confirm_purchase(**(args | {'order_reference': 'different-order'}))
        _, instructions = reserve(state)
        self.assertIsNone(instructions)

    def test_ticket_issued_is_not_wallet_available_or_added(self):
        state = purchased()
        args = dict(actor_id=state.actor_id, order_reference='fixture-order', ticket_reference='fixture-ticket', issuer_verified=True)
        for change in ({'issuer_verified': False}, {'order_reference': 'other-order'}, {'actor_id': 'other'}):
            with self.subTest(change=change), self.assertRaises(Blocked):
                state.record_ticket_issued(**(args | change))
        state = state.record_ticket_issued(**args)
        self.assertTrue(state.preview()['ticket_issued'])
        self.assertFalse(state.preview()['issuer_wallet_pass_available'])
        self.assertFalse(state.preview()['user_wallet_add_confirmed'])
        self.assertEqual(state.record_ticket_issued(**args), state)

    def test_only_verified_issuer_artifact_and_origin_are_accepted(self):
        for changes in ({'issuer_verified': False}, {'kind': 'generated_barcode'},
                        {'url': 'https://counterfeit.example.invalid/pass.pkpass'},
                        {'url': 'javascript:alert(1)'},
                        {'url': 'https://issuer.example.invalid/%0d%0aHeader:x'}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                artifact(**changes)
        with self.assertRaises(Blocked):
            purchased().record_wallet_available(artifact(), actor_id='fixture-actor', ticket_reference='fixture-ticket')
        self.assertEqual(artifact(kind='apple_wallet_url').kind, 'apple_wallet_url')

    def test_wallet_availability_needs_ticket_binding_and_explicit_user_add(self):
        state = issued()
        with self.assertRaises(Blocked):
            state.record_wallet_available(artifact(), actor_id=state.actor_id, ticket_reference='other-ticket')
        state = state.record_wallet_available(artifact(), actor_id=state.actor_id, ticket_reference='fixture-ticket')
        self.assertTrue(state.preview()['issuer_wallet_pass_available'])
        self.assertFalse(state.preview()['user_wallet_add_confirmed'])
        args = dict(actor_id=state.actor_id, artifact_digest=artifact().fingerprint, user_confirmed=True)
        for change in ({'actor_id': 'other'}, {'user_confirmed': False}, {'artifact_digest': 'wrong'}):
            with self.subTest(change=change), self.assertRaises(Blocked):
                state.confirm_wallet_add(**(args | change))
        state = state.confirm_wallet_add(**args)
        self.assertTrue(state.preview()['user_wallet_add_confirmed'])
        self.assertNotIn('wallet_installed', state.preview())
        self.assertEqual(state.confirm_wallet_add(**args), state)
        changed = artifact(url='https://issuer.example.invalid/reissued.pkpass')
        state = state.record_wallet_available(changed, actor_id=state.actor_id, ticket_reference='fixture-ticket')
        self.assertFalse(state.wallet_add_confirmed)

    def test_wallet_private_url_and_order_references_stay_out_of_preview(self):
        private = artifact(url='https://issuer.example.invalid/pass.pkpass?delivery=synthetic-private')
        state = issued().record_wallet_available(private, actor_id='fixture-actor', ticket_reference='fixture-ticket')
        public = json.dumps(state.preview()) + repr(state)
        for secret in ('synthetic-private', 'fixture-order', 'fixture-ticket', 'pass.pkpass'):
            self.assertNotIn(secret, public)
        restored = t.TicketHandoff.from_dict(json.loads(json.dumps(state.to_dict())))
        self.assertEqual(restored, state)

    def test_invalid_money_quantity_and_seller_url_are_rejected(self):
        for change in ({'quantity': True}, {'quantity': 0}, {'quantity': 101},
                       {'currency': 'aud'}, {'total': 120.50}, {'total': 'NaN'},
                       {'total': '-1'}, {'total': '1e2'}, {'total': '1.00001'},
                       {'official_seller_url': 'https://seller.example.invalid/%250aInjected'},
                       {'official_seller_url': 'https://user:secret@seller.example.invalid/path'}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                offer(**change)
        self.assertEqual(offer(currency='JPY', total='120').total, '120')

    def test_restore_rejects_mismatched_intent_impossible_states_and_credentials(self):
        state = approve()
        for change in ({'schema': 99}, {'purchase_state': 'confirmed'}, {'submission_outcome': 'unknown'},
                       {'wallet_add_confirmed': True}, {'card_number': 'synthetic-not-a-card'}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                t.TicketHandoff.from_dict(state.to_dict() | change)
        data = state.to_dict()
        data['offer']['total'] = '121.00'
        with self.assertRaises(ContractError):
            t.TicketHandoff.from_dict(data)

    def test_active_key_does_not_change_with_request_id_price_or_quantity(self):
        state = requested()
        self.assertEqual(replace(state, request_id='new-request').active_key, state.active_key)
        self.assertEqual(replace(state, offer=offer(total='150.00', quantity=3)).active_key, state.active_key)
        self.assertNotEqual(replace(state, actor_id='other-actor').active_key, state.active_key)

    def test_missing_persistence_fields_cannot_reset_unknown_submission(self):
        state, _ = reserve()
        for key in state.to_dict():
            data = state.to_dict()
            del data[key]
            with self.subTest(key=key), self.assertRaises(ContractError):
                t.TicketHandoff.from_dict(data)

    def test_new_process_restores_unknown_receipt_without_purchase_instructions(self):
        state, _ = reserve()
        with TemporaryDirectory(prefix='ticket-restart-') as directory:
            path = Path(directory) / 'synthetic-receipt.json'
            path.write_text(json.dumps(state.to_dict()))
            script = '''import json, sys
from shelf.ticket_handoff import TicketHandoff
with open(sys.argv[1]) as handle:
    state = TicketHandoff.from_dict(json.load(handle))
restored, instructions = state.reserve_submission(actor_id=state.actor_id,
    current_offer=state.offer, expected_revision=state.revision, preview_digest=state.preview_digest)
assert restored == state and instructions is None and state.submission_outcome == 'unknown'
print('reconciliate-only')
'''
            result = subprocess.run([sys.executable, '-B', '-c', script, str(path)],
                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'reconciliate-only')
