"""Only fictional mailboxes; recipient resolution is a caller responsibility."""
from dataclasses import replace
import json
from unittest import TestCase

from shelf import calendar_handoff as c
from shelf.acquisition_contract import Blocked, ContractError
from shelf.tests.test_calendar_handoff import TARGET, intent, approve

PERSONAL = c.Recipient('fixture-person', 'Synthetic person — personal', 'personal@example.invalid')
WORK = c.Recipient('fixture-person', 'Synthetic person — work', 'work@example.invalid')


class CalendarRecipientListTests(TestCase):
    def test_both_mailboxes_for_same_person_remain_visible_without_side_effect(self):
        state = intent()
        for operation in ('share', 'invite'):
            view = state.preview(operation, target=TARGET, recipients=[PERSONAL, WORK])
            self.assertEqual([r['address'] for r in view['recipients']], [PERSONAL.address, WORK.address])
            self.assertEqual([v['recipient']['address'] for v in view['previews']], [PERSONAL.address, WORK.address])
            self.assertEqual(view['blockers'], [])
            self.assertIn('Preview only', view['effect'])
            self.assertIn('separate exact approval', view['effect'])
            self.assertEqual(json.loads(json.dumps(view))['recipients'], view['recipients'])
            self.assertEqual(state.receipts, ())

    def test_group_digest_cannot_approve_one_or_all_mailboxes(self):
        state = intent()
        group = state.preview('invite', target=TARGET, recipients=[PERSONAL, WORK])
        for recipient in (PERSONAL, WORK):
            with self.assertRaises(Blocked):
                approve(state, operation='invite', target=TARGET, recipient=recipient,
                        preview_digest=group['digest'])
        # Each existing exact command still works locally and the adapter still blocks.
        for index, recipient in enumerate((PERSONAL, WORK)):
            state, receipt = approve(state, key=f'invite-{index}', operation='invite', target=TARGET,
                recipient=recipient, preview_digest=group['previews'][index]['digest'])
            with self.assertRaises(Blocked):
                state.reserve(receipt.key, actor_id=state.actor_id)
        restored = c.CalendarHandoff.from_dict(json.loads(json.dumps(state.to_dict())))
        self.assertEqual([r.recipient.address for r in restored.receipts], [PERSONAL.address, WORK.address])

    def test_list_change_changes_group_digest_and_preserves_individual_binding(self):
        state = intent()
        before = state.preview('share', target=TARGET, recipients=[PERSONAL, WORK])
        after = state.preview('share', target=TARGET, recipients=[PERSONAL, replace(WORK, address='other@example.invalid')])
        self.assertNotEqual(before['digest'], after['digest'])
        self.assertEqual(before['previews'][0]['digest'], after['previews'][0]['digest'])
        self.assertNotEqual(before['previews'][1]['digest'], after['previews'][1]['digest'])

    def test_list_must_be_explicit_bounded_and_nonduplicated(self):
        state = intent()
        for recipients in ([], [PERSONAL] * 11, [PERSONAL, PERSONAL], [object()], iter([PERSONAL])):
            with self.assertRaises(ContractError):
                state.preview('invite', target=TARGET, recipients=recipients)
        for operation in ('export', 'add'):
            with self.assertRaises(ContractError):
                state.preview(operation, target=TARGET, recipients=[PERSONAL, WORK])
        with self.assertRaises(ContractError):
            state.preview('invite', target=TARGET, recipient=PERSONAL, recipients=[WORK])

    def test_unresolved_target_and_event_blockers_remain_visible(self):
        view = intent(status='unknown').preview('invite', recipients=[PERSONAL, WORK])
        self.assertIn('Calendar target unresolved', view['blockers'])
        self.assertIn('Event status unknown', view['blockers'])
        self.assertEqual(len(view['recipients']), 2)
