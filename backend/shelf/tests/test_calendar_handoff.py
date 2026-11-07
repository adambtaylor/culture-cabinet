"""Synthetic handoffs only: no real calendar, recipient resolution or service calls."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from shelf.acquisition_contract import Blocked, ContractError
from shelf import calendar_handoff as c


STAMP = '2026-10-28T22:00:00+00:00'
TARGET = c.CalendarTarget('fixture-calendar', 'Synthetic private calendar')
RECIPIENT = c.Recipient('fixture-person', 'Synthetic recipient', 'recipient@example.invalid')


def event(**changes):
    return replace(c.Event('fixture-event', 'fixture-session', 'Synthetic concert',
        '2027-01-15T09:00:00+00:00', '2027-01-15T11:00:00+00:00',
        'Australia/Melbourne', 'Synthetic venue', 'https://events.example.invalid/concert',
        'Synthetic official-source fixture', status='scheduled', source_verified=True), **changes)


def intent(**changes):
    return c.CalendarHandoff('fixture-actor', 'fixture-request', event(**changes), STAMP)


def approve(state, key='fixture-export', operation='export', target=None, recipient=None, **changes):
    values = dict(actor_id=state.actor_id, expected_revision=state.revision,
        preview_digest=state.preview(operation, target=target, recipient=recipient)['digest'],
        key=key, operation=operation, target=target, recipient=recipient, user_confirmed=True)
    return state.approve(**(values | changes))


class CalendarHandoffTests(TestCase):
    def test_exact_preview_includes_local_times_source_privacy_and_no_effect(self):
        state = intent()
        view = state.preview()
        self.assertEqual(view['event']['start'], '2027-01-15T20:00:00+11:00')
        self.assertEqual(view['event']['end'], '2027-01-15T22:00:00+11:00')
        self.assertEqual(view['event']['timezone'], 'Australia/Melbourne')
        self.assertEqual(view['event']['official_source'], state.event.official_source)
        self.assertEqual(view['source_label'], 'Synthetic official-source fixture')
        self.assertIn('Private', view['privacy_label'])
        self.assertEqual(view['blockers'], [])
        self.assertEqual(state.receipts, ())

    def test_export_needs_explicit_exact_actor_bound_approval(self):
        state = intent()
        with self.assertRaises(Blocked):
            state.export_ics('fixture-export', actor_id=state.actor_id)
        for changes in ({'actor_id': 'another-actor'}, {'expected_revision': 1},
                        {'preview_digest': 'wrong'}, {'user_confirmed': False}, {'user_confirmed': 1}):
            with self.subTest(changes=changes), self.assertRaises(Blocked):
                approve(state, **changes)

    def test_add_share_and_invite_are_distinct_exact_commands(self):
        state = intent()
        self.assertIn('Calendar target unresolved', state.preview('add')['blockers'])
        for operation in ('share', 'invite'):
            self.assertTrue(state.preview(operation, target=TARGET)['blockers'])
            with self.assertRaises(Blocked):
                approve(state, operation=operation, target=TARGET)
        state, added = approve(state, key='add', operation='add', target=TARGET)
        for operation in ('share', 'invite'):
            with self.assertRaises(Blocked):
                approve(state, key=operation, operation=operation, target=TARGET,
                        recipient=RECIPIENT, preview_digest=added.preview_digest)
        self.assertEqual(len(state.receipts), 1)
        self.assertIsNone(added.recipient)
        with self.assertRaises(ContractError):
            state.preview('add', target=TARGET, recipient=RECIPIENT)

    def test_changed_recipient_or_target_requires_new_preview(self):
        state = intent()
        view = state.preview('invite', target=TARGET, recipient=RECIPIENT)
        for target, recipient in [(replace(TARGET, calendar_id='different'), RECIPIENT),
                                  (TARGET, replace(RECIPIENT, address='other@example.invalid')),
                                  (TARGET, replace(RECIPIENT, recipient_id='other-person'))]:
            with self.subTest(target=target, recipient=recipient), self.assertRaises(Blocked):
                approve(state, operation='invite', target=target, recipient=recipient,
                        preview_digest=view['digest'])

    def test_duplicate_click_new_key_and_json_restart_reuse_receipt(self):
        state, receipt = approve(intent())
        state = c.CalendarHandoff.from_dict(json.loads(json.dumps(state.to_dict())))
        for key in (receipt.key, 'another-click'):
            replay, same = approve(state, key=key)
            self.assertEqual(replay, state)
            self.assertEqual(same, receipt)
            self.assertEqual(len(replay.receipts), 1)
        self.assertEqual(state.export_ics(receipt.key, actor_id=state.actor_id),
                         state.export_ics(receipt.key, actor_id=state.actor_id))

    def test_changed_time_invalidates_approval_and_preserves_uid(self):
        state, receipt = approve(intent())
        updated = state.revise(replace(state.event, start='2027-01-15T10:00:00+00:00'),
            actor_id=state.actor_id, expected_revision=0, modified_at='2026-10-29T00:00:00+00:00')
        self.assertEqual(updated.uid, state.uid)
        self.assertEqual(updated.revision, 1)
        with self.assertRaises(Blocked):
            updated.export_ics(receipt.key, actor_id=state.actor_id)
        with self.assertRaises(Blocked):
            approve(updated, preview_digest=receipt.preview_digest)
        with self.assertRaises(Blocked):
            approve(updated, key=receipt.key)
        updated, fresh = approve(updated, key='update')
        output = updated.export_ics(fresh.key, actor_id=state.actor_id)
        self.assertIn(b'SEQUENCE:1\r\n', output)
        self.assertIn(b'DTSTART:20270115T100000Z\r\n', output)

    def test_other_event_edits_invalidate_exact_approval(self):
        state, receipt = approve(intent())
        for change in ({'title': 'Changed title'}, {'location': 'Other venue'},
                       {'end': '2027-01-15T12:00:00+00:00'}, {'timezone': 'UTC'},
                       {'official_source': 'https://events.example.invalid/updated'},
                       {'privacy': 'confidential'}, {'status': 'cancelled'}):
            updated = state.revise(replace(state.event, **change), actor_id=state.actor_id,
                                  expected_revision=0, modified_at=STAMP)
            with self.subTest(change=change), self.assertRaises(Blocked):
                approve(updated, key='new', preview_digest=receipt.preview_digest)

    def test_unknown_dates_status_and_initial_cancellation_block(self):
        for changes in ({'start': None}, {'end': None}, {'timezone': None},
                        {'status': 'unknown'}, {'status': 'cancelled'}, {'source_verified': False}):
            state = intent(**changes)
            self.assertTrue(state.preview()['blockers'])
            with self.subTest(changes=changes), self.assertRaises(Blocked):
                approve(state)

    def test_cancelled_update_is_noninviting_publish_with_stable_uid(self):
        state, original = approve(intent())
        state = state.revise(replace(state.event, status='cancelled'), actor_id=state.actor_id,
                             expected_revision=0, modified_at=STAMP)
        state, receipt = approve(state, key='cancel')
        output = state.export_ics(receipt.key, actor_id=state.actor_id)
        self.assertIn(b'METHOD:PUBLISH\r\n', output)
        self.assertIn(b'STATUS:CANCELLED\r\n', output)
        self.assertIn(b'SEQUENCE:1\r\n', output)
        self.assertNotIn(b'METHOD:CANCEL', output)
        self.assertNotIn(b'ATTENDEE', output)

    def test_ics_escaping_octet_folding_and_crlf(self):
        title = 'Synthetic: ' + ('é🎹' * 65) + ', semi; slash\\'
        state, receipt = approve(intent(title=title, location='Room 1, Building; A\\B'))
        raw = state.export_ics(receipt.key, actor_id=state.actor_id)
        self.assertTrue(raw.endswith(b'END:VCALENDAR\r\n'))
        lines = raw.split(b'\r\n')
        self.assertTrue(all(len(line) <= 75 for line in lines))
        for line in lines:
            line.decode('utf-8')
        self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))
        unfolded = raw.replace(b'\r\n ', b'').decode()
        self.assertIn('SUMMARY:' + title.replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,'), unfolded)
        self.assertIn('CLASS:PRIVATE\r\n', unfolded)
        self.assertIn('DESCRIPTION:Source: Synthetic official-source fixture', unfolded)
        self.assertNotIn('fixture-actor', unfolded)
        self.assertNotIn('ATTENDEE', unfolded)
        self.assertNotIn('ORGANIZER', unfolded)

    def test_text_and_url_injection_rejected_before_export(self):
        for field in ('title', 'location', 'source_label'):
            with self.subTest(field=field), self.assertRaises(ContractError):
                event(**{field: 'Synthetic\r\nATTENDEE:mailto:someone@example.invalid'})
        for url in ('javascript:alert(1)', 'https://user:password@example.invalid/a',
                    'https://events.example.invalid/a\r\nATTENDEE:x',
                    'https://events.example.invalid/%0d%0aATTENDEE:x',
                    'https://events.example.invalid/%250d%250aATTENDEE:x',
                    'https://events.example.invalid/\\evil',
                    'https://events.example.invalid/?token=private',
                    'https://events.example.invalid/#private'):
            with self.subTest(url=url), self.assertRaises(ContractError):
                event(official_source=url)

    def test_dst_gap_overlap_and_utc_conversion(self):
        with self.assertRaises(Blocked):
            c.local_time('2027-10-03T02:30:00', 'Australia/Melbourne')
        with self.assertRaises(Blocked):
            c.local_time('2027-04-04T02:30:00', 'Australia/Melbourne')
        early = c.local_time('2027-04-04T02:30:00', 'Australia/Melbourne', fold=0)
        late = c.local_time('2027-04-04T02:30:00', 'Australia/Melbourne', fold=1)
        self.assertEqual((c.instant(late) - c.instant(early)).total_seconds(), 3600)
        state, receipt = approve(intent(start=early, end=late))
        raw = state.export_ics(receipt.key, actor_id=state.actor_id)
        self.assertIn(b'DTSTART:20270403T153000Z', raw)
        self.assertIn(b'DTEND:20270403T163000Z', raw)
        winter = intent(start='2027-07-15T09:00:00Z', end='2027-07-15T11:00:00Z')
        self.assertEqual(winter.preview()['event']['start'], '2027-07-15T19:00:00+10:00')

    def test_invalid_time_zone_naive_fractional_and_reverse_dates(self):
        for change in ({'timezone': 'Invented/Zone'}, {'start': '2027-01-15T09:00:00'},
                       {'start': '2027-01-15T09:00:00.001Z'},
                       {'start': '2027-01-15T09:00:00+00:00:00.5'},
                       {'end': '2027-01-15T08:00:00Z'}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                event(**change)

    def test_local_date_only_never_invents_midnight(self):
        with self.assertRaises(ContractError):
            c.local_time('2027-01-15', 'Australia/Melbourne')
        with self.assertRaises(ContractError):
            c.local_time('2027-04-04T02:30:00', 'Australia/Melbourne', fold=True)

    def test_default_adapter_blocks_all_external_operations(self):
        for operation in ('add', 'share', 'invite'):
            state, receipt = approve(intent(), operation=operation, target=TARGET,
                                     recipient=RECIPIENT if operation != 'add' else None)
            with self.subTest(operation=operation), self.assertRaises(Blocked):
                state.reserve(receipt.key, actor_id=state.actor_id)
            self.assertEqual(receipt.state, 'approved')

    def test_unknown_submission_restart_and_new_key_never_reserve_twice(self):
        adapter = Mock(spec=c.CalendarAdapter)
        state, receipt = approve(intent(), operation='add', target=TARGET)
        state, submitted, fresh = state.reserve(receipt.key, actor_id=state.actor_id, adapter=adapter)
        self.assertTrue(fresh)
        self.assertEqual(submitted.state, 'submitted_unknown')
        state = c.CalendarHandoff.from_dict(json.loads(json.dumps(state.to_dict())))
        state, duplicate = approve(state, key='new-click', operation='add', target=TARGET)
        _, _, fresh = state.reserve(duplicate.key, actor_id=state.actor_id, adapter=adapter)
        self.assertFalse(fresh)
        adapter.check.assert_called_once()
        state = state.revise(replace(state.event, title='Updated'), actor_id=state.actor_id,
                             expected_revision=0, modified_at=STAMP)
        state, update = approve(state, key='update', operation='add', target=TARGET)
        with self.assertRaises(Blocked):
            state.reserve(update.key, actor_id=state.actor_id, adapter=adapter)
        with self.assertRaises(Blocked):
            state.confirm(receipt.key, actor_id=state.actor_id)
        state = state.confirm(receipt.key, actor_id=state.actor_id, externally_verified=True)
        _, _, fresh = state.reserve(update.key, actor_id=state.actor_id, adapter=adapter)
        self.assertTrue(fresh)

    def test_invite_approval_cannot_be_exported_or_leak_recipient(self):
        state, invited = approve(intent(), operation='invite', target=TARGET, recipient=RECIPIENT)
        with self.assertRaises(Blocked):
            state.export_ics(invited.key, actor_id=state.actor_id)
        state, exported = approve(state, key='export')
        output = state.export_ics(exported.key, actor_id=state.actor_id)
        self.assertNotIn(RECIPIENT.address.encode(), output)
        self.assertNotIn(b'ATTENDEE', output)

    def test_receipt_restore_rejects_corruption_and_extra_fields(self):
        state, receipt = approve(intent())
        for change in ({'schema': 9}, {'revision': -1}, {'event': {}}, {'token': 'not-supported'}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                c.CalendarHandoff.from_dict(state.to_dict() | change)
        data = json.loads(json.dumps(state.to_dict()))
        data['event']['title'] = 'Tampered'
        with self.assertRaises(ContractError):
            c.CalendarHandoff.from_dict(data)

    def test_new_request_keeps_event_uid_and_different_actor_gets_another(self):
        state = intent()
        self.assertEqual(replace(state, request_id='new-request').uid, state.uid)
        self.assertNotEqual(replace(state, actor_id='other-actor').uid, state.uid)

    def test_reused_key_conflicts_even_if_another_receipt_matches_digest(self):
        state, _ = approve(intent(), key='export')
        state, _ = approve(state, key='add', operation='add', target=TARGET)
        with self.assertRaises(Blocked):
            approve(state, key='add', operation='export')

    def test_unknown_calendar_receipt_cannot_be_bypassed_by_renaming_target(self):
        adapter = Mock(spec=c.CalendarAdapter)
        state, receipt = approve(intent(), key='add', operation='add', target=TARGET)
        state, _, _ = state.reserve(receipt.key, actor_id=state.actor_id, adapter=adapter)
        state, renamed = approve(state, key='renamed', operation='add',
                                  target=replace(TARGET, label='New display name'))
        with self.assertRaises(Blocked):
            state.reserve(renamed.key, actor_id=state.actor_id, adapter=adapter)
        adapter.check.assert_called_once()

    def test_cancelled_unapproved_revision_does_not_create_cancellation_export(self):
        state = intent()
        state = state.revise(replace(state.event, status='cancelled'), actor_id=state.actor_id,
                             expected_revision=0, modified_at=STAMP)
        with self.assertRaises(Blocked):
            approve(state)

    def test_missing_persistence_fields_are_not_reset_to_defaults(self):
        state, _ = approve(intent())
        for key in state.to_dict():
            data = state.to_dict()
            del data[key]
            with self.subTest(key=key), self.assertRaises(ContractError):
                c.CalendarHandoff.from_dict(data)

    def test_new_process_restores_unknown_receipt_without_adapter_call(self):
        state, receipt = approve(intent(), operation='add', target=TARGET)
        state, _, _ = state.reserve(receipt.key, actor_id=state.actor_id, adapter=Mock(spec=c.CalendarAdapter))
        with TemporaryDirectory(prefix='calendar-restart-') as directory:
            path = Path(directory) / 'synthetic-receipt.json'
            path.write_text(json.dumps(state.to_dict()))
            script = '''import json, sys
from shelf.calendar_handoff import CalendarHandoff
with open(sys.argv[1]) as handle:
    state = CalendarHandoff.from_dict(json.load(handle))
restored, receipt, fresh = state.reserve('fixture-export', actor_id='fixture-actor')
assert restored == state and not fresh and receipt.state == 'submitted_unknown'
print('reconciliate-only')
'''
            result = subprocess.run([sys.executable, '-B', '-c', script, str(path)],
                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'reconciliate-only')
