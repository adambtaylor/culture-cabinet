"""Local-only public-event state invariants; no Django, network or social account."""
from copy import deepcopy
import json
import unittest

from shelf import social_event_intake as intake

NOW = '2026-10-30T02:00:00+00:00'


def envelope(**changes):
    fields = dict(action='observe_event', platform='facebook', platform_event_id='123',
        canonical_url='https://www.facebook.com/events/123/', title='Synthetic public concert',
        organizer='', venue='Synthetic public hall', raw_date_time='Nov 21 at 7pm',
        timezone_name='unknown', observed_at='2026-10-30T01:00:00+00:00',
        evidence_ref='synthetic-browser:1', actor_origin='owner_confirmed',
        signal_type='interested', confidence='observed', lineage_ref='synthetic-action:1',
        public_confirmed=True, supersedes='', conflict_note='')
    fields.update(changes)
    return fields


def observe(data=None, **changes):
    return intake.apply_event_action(data or {}, envelope(**changes),
        captured_at=NOW, capture_actor='owner:1')


def withdraw(data, identity):
    return intake.apply_event_action(data, {'action': 'event_withdraw', 'identity': identity},
        captured_at=NOW, capture_actor='owner:1')


class EventIntakeTests(unittest.TestCase):
    def test_json_copy_identity_and_recapture_do_not_amplify(self):
        original = {'sources': [{'id': 'unchanged'}]}
        data = observe(original)
        self.assertNotIn('event_intake', original)
        self.assertEqual(json.loads(json.dumps(data)), data)
        again = intake.apply_event_action(data, envelope(), captured_at=NOW, capture_actor='another:2')
        self.assertEqual(again, data)
        self.assertEqual(len(intake.event_signals(data)), 1)
        row = intake.event_intake_view(data)[0]
        self.assertEqual((row['capture_actor'], row['actor_origin']), ('owner:1', 'owner_confirmed'))
        self.assertEqual(row['canonical_url'], 'https://www.facebook.com/events/123')
        row['title'] = 'mutated copy'
        self.assertEqual(intake.event_signals(data)[0]['title'], 'Synthetic public concert')

    def test_multiple_captures_conflicting_metadata_and_stronger_intent(self):
        data = observe()
        data = observe(data, signal_type='attending', venue='Another public hall',
                       observed_at='2026-10-30T01:30:00Z', evidence_ref='synthetic-browser:2')
        self.assertEqual(len(intake.event_intake_view(data)), 2)
        self.assertIn('venue', intake.event_intake_view(data)[0]['conflicts'])
        signal = intake.event_signals(data)[0]
        self.assertEqual(signal['value'], 'attending')
        self.assertEqual(set(signal), {'key', 'title', 'medium', 'source', 'basis', 'value'})
        self.assertEqual(signal['basis'], 'social_intent')

    def test_unknown_reported_and_follows_are_never_owner_taste(self):
        for changes in ({'actor_origin': 'unknown'}, {'confidence': 'reported'},
                        {'confidence': 'uncertain'}, {'signal_type': 'none'}):
            with self.subTest(changes=changes):
                self.assertEqual(intake.event_signals(observe(**changes)), [])
        self.assertEqual(intake.event_signals({'sources': [{'scope': 'followed'}]}), [])

    def test_agent_taint_survives_correction_and_owner_recapture(self):
        data = observe(actor_origin='agent_marked')
        first = intake.event_intake_view(data)[0]['id']
        data = observe(data, actor_origin='owner_confirmed', lineage_ref='different-owner-claim',
                       supersedes=first, conflict_note='Origin disputed; retain evidence')
        self.assertEqual(intake.event_signals(data), [])
        self.assertEqual(len(intake.event_intake_view(data)), 2)
        self.assertTrue(intake.event_intake_view(data)[0]['superseded'])
        for identity in intake.KNOWN_AGENT_EVENTS:
            known = observe(platform_event_id=identity, canonical_url='https://www.facebook.com/events/' + identity)
            self.assertEqual(intake.event_signals(known), [])
        self.assertEqual(len(intake.event_signals(observe(platform_event_id='1766911811290681',
            canonical_url='https://www.facebook.com/events/1766911811290681'))), 1)

    def test_shared_lineage_excludes_other_event_even_after_agent_withdrawal(self):
        data = observe()
        data = observe(data, platform='instagram', platform_event_id='SYNTHETIC_1',
            canonical_url='https://instagram.com/p/SYNTHETIC_1', actor_origin='agent_marked')
        self.assertEqual(intake.event_signals(data), [])
        agent = next(row for row in intake.event_intake_view(data) if row['platform'] == 'instagram')
        data = withdraw(data, agent['id'])
        self.assertEqual(intake.event_signals(data), [])
        self.assertEqual(len(intake.event_intake_view(data)), 1)

    def test_withdrawal_removes_metadata_but_preserves_taint_and_blocks_stale_restore(self):
        data = observe(actor_origin='agent_marked')
        row = intake.event_intake_view(data)[0]
        data = withdraw(data, row['id'])
        self.assertEqual(intake.event_intake_view(data), [])
        self.assertEqual(intake.event_candidates(data), [])
        self.assertNotIn('Synthetic public concert', json.dumps(data))
        self.assertNotIn('synthetic-action:1', json.dumps(data))
        self.assertTrue(data['event_intake']['events'][row['event_key']]['tainted'])
        with self.assertRaisesRegex(intake.EventIntakeError, 'withdrawn'):
            observe(data)

    def test_corrections_require_same_event_and_keep_conflicts(self):
        data = observe()
        first = intake.event_intake_view(data)[0]['id']
        corrected = observe(data, raw_date_time='Nov 22 at 8pm', supersedes=first)
        self.assertEqual(len(intake.event_intake_view(corrected)), 2)
        self.assertIn('raw_date_time', intake.event_intake_view(corrected)[1]['conflicts'])
        with self.assertRaisesRegex(intake.EventIntakeError, 'exact event'):
            observe(data, platform_event_id='124', canonical_url='https://facebook.com/events/124', supersedes=first)
        with self.assertRaises(intake.EventIntakeError):
            observe(data, supersedes='a' * 64)

    def test_strict_envelope_urls_timestamps_and_public_confirmation(self):
        invalid = [dict(public_confirmed=False), dict(public_confirmed='True'), dict(cookies='private'),
            dict(title='x' * 161), dict(evidence_ref=''), dict(observed_at='2026-10-30T01:00:00'),
            dict(observed_at='2026-10-31T01:00:00Z'), dict(timezone_name='guessed/Melbourne'),
            dict(canonical_url='http://facebook.com/events/123'),
            dict(canonical_url='https://facebook.com/events/123?token=secret'),
            dict(canonical_url='https://user@facebook.com/events/123'),
            dict(canonical_url='https://facebook.com:443/events/123'),
            dict(canonical_url='https://facebook.com/events/124'),
            dict(canonical_url='https://facebook.com/groups/private'),
            dict(canonical_url='https://facebook.com/events/%31%32%33'),
            dict(canonical_url='https://facebook.com/events/123\n'),
            dict(platform='instagram', canonical_url='https://instagram.com/stories/123')]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(intake.EventIntakeError):
                observe(**changes)
        data = observe(timezone_name='Australia/Melbourne')
        self.assertEqual(intake.event_intake_view(data)[0]['raw_date_time'], 'Nov 21 at 7pm')

    def test_candidates_exact_matches_and_provenance_remain_in_state(self):
        data = observe(actor_origin='agent_marked')
        row = intake.event_candidates(data)[0]
        self.assertEqual(row['social_evidence'][0]['actor_origin'], 'agent_marked')
        for existing in ([{'url': 'https://facebook.com/events/123/'}],
                         [{'event_url': 'https://www.facebook.com/events/123'}],
                         [{'platform': 'facebook', 'platform_event_id': '123'}],
                         [{'social_evidence': [{'post_url': 'https://facebook.com/events/123'}]}]):
            self.assertEqual(intake.event_candidates(data, existing), [])
        self.assertEqual(len(intake.event_candidates(data, [{'title': row['title'], 'url': 'https://example.com/other'}])), 1)
        self.assertEqual(len(intake.event_intake_view(data)), 1)

    def test_bounds_fail_atomically_and_cleanup_remains_available(self):
        data = {}
        for number in range(1, intake.MAX_OBSERVATIONS + 1):
            data = observe(data, platform_event_id=str(number),
                canonical_url='https://facebook.com/events/' + str(number), lineage_ref='lineage:' + str(number))
        before = deepcopy(data)
        with self.assertRaises(intake.EventIntakeError):
            observe(data, platform_event_id='9999', canonical_url='https://facebook.com/events/9999')
        self.assertEqual(data, before)
        data = withdraw(data, intake.event_intake_view(data)[0]['id'])
        self.assertEqual(len(intake.event_intake_view(data)), 99)
        with self.assertRaisesRegex(intake.EventIntakeError, 'lifetime limit'):
            observe(data, platform_event_id='9999', canonical_url='https://facebook.com/events/9999')
        self.assertLessEqual(len(data['event_intake']['audit']), intake.MAX_AUDIT)

    def test_revision_tracks_evidence_but_recapture_is_stable(self):
        data = observe()
        self.assertNotEqual(intake.event_revision({}), intake.event_revision(data))
        self.assertEqual(intake.event_revision(data), intake.event_revision(observe(data)))
        self.assertNotEqual(intake.event_revision(data), intake.event_revision(observe(data, evidence_ref='another-capture')))

    def test_unknown_origin_requires_explicit_correction_with_new_direct_evidence(self):
        data = observe(actor_origin='unknown')
        unknown_id = intake.event_intake_view(data)[0]['id']
        copied = observe(data, actor_origin='owner_confirmed')
        self.assertEqual(intake.event_signals(copied), [])
        same_evidence = observe(copied, supersedes=unknown_id)
        self.assertEqual(intake.event_signals(same_evidence), [])
        corrected = observe(copied, supersedes=unknown_id, evidence_ref='direct-owner-confirmation:2')
        self.assertEqual(len(intake.event_signals(corrected)), 1)
        self.assertEqual(intake.event_intake_view(corrected)[0]['actor_origin'], 'unknown')
        self.assertTrue(intake.event_intake_view(corrected)[0]['superseded'])
        self.assertEqual(len(intake.event_candidates(corrected)[0]['social_evidence']), 3)
        linked = observe(data, platform_event_id='124', canonical_url='https://facebook.com/events/124')
        self.assertEqual(intake.event_signals(linked), [])

    def test_none_requires_explicit_correction_and_strict_withdrawal(self):
        data = observe()
        first = intake.event_intake_view(data)[0]['id']
        contradictory = observe(data, signal_type='none')
        self.assertEqual(intake.event_signals(contradictory)[0]['value'], 'interested')
        corrected = observe(data, signal_type='none', supersedes=first)
        self.assertEqual(intake.event_signals(corrected), [])
        with self.assertRaisesRegex(intake.EventIntakeError, 'Unsupported'):
            intake.apply_event_action(data, {'action': 'event_withdraw', 'identity': first, 'private_notes': 'secret'},
                captured_at=NOW, capture_actor='owner:1')

    def test_unknown_lineage_cannot_be_laundered_by_withdrawal(self):
        data = observe(actor_origin='unknown')
        unknown = intake.event_intake_view(data)[0]
        data = observe(data, platform_event_id='124', canonical_url='https://facebook.com/events/124')
        self.assertEqual(intake.event_signals(data), [])
        data = withdraw(data, unknown['id'])
        self.assertEqual(intake.event_signals(data), [])
        self.assertNotIn('synthetic-browser:1', json.dumps(data['event_intake']['unknown_origins']))
