"""Synthetic contract tests, authored for a separately admitted stdlib runner.

No Django, database, source transport, authority capture or provider is involved.
All current revision/rights views below are explicitly trusted test assertions.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import unittest
from uuid import UUID

from shelf.disclosure import DisclosureError, canonical_bytes, prepare, read_json


def uid(number):
    return str(UUID(int=number))


NOW = datetime(2026, 11, 1, 1, tzinfo=timezone.utc)
START = '2026-11-01T00:00:00Z'
END = '2026-11-01T02:00:00Z'
SESSION_END = '2026-11-01T03:00:00Z'
SUBJECT = uid(4)
PARTNER = uid(5)
SCALARS = ('key', 'category', 'title', 'medium', 'source_url', 'provider',
           'source_id', 'identity_status', 'evidence_type', 'observed_at', 'valid_until')


def evidence(number=10, *, basis='favourite', value=True, subjects=None, parents=None):
    return {
        'id': uid(number), 'subject_ref': SUBJECT, 'key': 'private:seed',
        'title': 'A synthetic favourite', 'medium': 'book', 'basis': basis, 'value': value,
        'data_classes': ['raw_notes'] if basis == 'feedback_note' else ['canonical_seed'],
        'source_ref': 'private:source', 'identity_status': 'confirmed', 'author_ref': SUBJECT,
        'applicable_subjects': subjects or [SUBJECT], 'origin': 'owner', 'sensitivity': 'ordinary',
        'purposes': ['inference'], 'direct_parent_ids': parents or [],
    }


def trait(number, parents, *, text='A synthetic hypothesis'):
    return {
        'id': uid(number), 'subject_ref': SUBJECT, 'text': text, 'evidence_ids': list(parents),
        'lineage_complete': True, 'author_ref': SUBJECT, 'applicable_subjects': [SUBJECT],
        'origin': 'inferred_hypothesis', 'sensitivity': 'ordinary', 'purposes': ['inference'],
        'direct_parent_ids': list(parents),
    }


def resign(record, binding):
    candidate = record['candidate']
    value = (next(f for f in candidate['facts'] if f['id'] == binding['fact_id'])
             if binding['target'] == 'fact' else candidate[binding['target']])
    material = {k: binding[k] for k in
                ('target', 'fact_id', 'classification', 'direct_parent_ids', 'observation')}
    material['value'] = value
    # Independent stdlib encoding, rather than asking the projection to sign itself.
    raw = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                     allow_nan=False).encode('utf-8')
    binding['rights']['binding_digest'] = hashlib.sha256(raw).hexdigest()


def source(key='source:book:a', *, facts=None):
    record = {
        'schema': 'candidate-source-rights/v1', 'record_ref': 'rights:' + key,
        'direct_parent_ids': [],
        'candidate': {
            'key': key, 'category': 'books', 'title': 'Synthetic public title', 'medium': 'book',
            'source_url': 'https://example.invalid/books/a', 'provider': 'synthetic-test',
            'source_id': key, 'identity_status': 'confirmed', 'evidence_type': 'cached_lookup',
            'observed_at': START, 'valid_until': SESSION_END, 'rights_ref': 'rights:' + key,
            'source_roots': ['observation:1'], 'facts': facts or [],
        },
        'fields': [],
    }
    targets = [(name, None) for name in SCALARS]
    targets.extend(('fact', fact['id']) for fact in record['candidate']['facts'])
    for target, fact_id in targets:
        binding = {
            'target': target, 'fact_id': fact_id, 'classification': 'public_source',
            'direct_parent_ids': [],
            'observation': {'ref': 'observation:1', 'revision': 7, 'digest': 'c' * 64,
                            'locator': target + ':' + (fact_id or ''),
                            'observed_at': START, 'expires_at': END},
            'rights': {'policy_ref': 'policy:1', 'revision': 3, 'disposition': 'allowed',
                       'checked_at': START, 'expires_at': END, 'binding_digest': ''},
        }
        resign(record, binding)
        record['fields'].append(binding)
    return record


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.context = {
            'schema': 'disclosure-context/v1', 'installation_id': uid(1), 'actor_ref': uid(2),
            'profile_ref': uid(3), 'applicable_subjects': [SUBJECT], 'purpose': 'inference',
            'adapter_id': 'synthetic', 'adapter_version': 'v1',
            'destination_origin': 'https://example.invalid', 'config_revision': 9,
            'policy_epoch': 12, 'grant_revisions': {}, 'session_expires_at': SESSION_END,
            'isolated_fixture': False,
        }
        self.snapshot = {
            'schema': 'profile-snapshot/v1', 'installation_id': uid(1), 'actor_ref': uid(2),
            'profile_ref': uid(3), 'profile_kind': 'owner', 'applicability_ref': 'private:applicability',
            'profile_revision_digest': 'a' * 64, 'source_revisions': {'private:source': 1},
            'policy_epoch': 12, 'restriction_watermark': 'private:restriction', 'evidence': [],
            'derived_traits': [], 'hard_exclusions': [], 'seen_keys': [],
            'category_registry_revision': 1, 'hard_constraints': [],
        }
        self.grants = []
        self.candidates = []
        self.observations = {'observation:1': {'revision': 7, 'digest': 'c' * 64}}
        self.policies = {'policy:1': {'revision': 3}}

    def grant(self, classes, *, subject=SUBJECT, subjects=None, number=50):
        record = {name: self.context[name] for name in
                  ('installation_id', 'actor_ref', 'profile_ref', 'purpose', 'adapter_id',
                   'adapter_version', 'destination_origin', 'config_revision', 'policy_epoch')}
        record.update(schema='grant-authority/v1', grant_id=uid(number), granting_subject=subject,
                      applicable_subjects=subjects or [subject], allowed_data_classes=list(classes),
                      grant_revision=4, issued_at=START, expires_at=END, state='active')
        self.context['grant_revisions'][uid(number)] = 4
        self.grants.append(record)
        return record

    def project(self, **overrides):
        args = dict(snapshot=self.snapshot, grants=self.grants, context=self.context,
                    candidates=self.candidates, observations=self.observations, policies=self.policies,
                    now=NOW)
        args.update(overrides)
        return prepare(**args)

    def body(self):
        result = self.project()
        self.assertTrue(result.ok)
        return json.loads(result.profile_bytes)

    def assert_code(self, code, **overrides):
        with self.assertRaises(DisclosureError) as raised:
            self.project(**overrides)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)

    def test_zero_signals_and_empty_candidate_pool_are_valid_preparation(self):
        self.assertEqual(self.body(), {'schema': 'cabinet-inference-input/v1', 'candidates': [],
            'profile': {'schema': 'disclosed-profile/v1', 'mode': 'broad', 'signals': []}})
        self.assertEqual(self.project().candidate_projection, ())

    def test_distinct_anticipated_and_experienced_meanings(self):
        self.snapshot['evidence'] = [evidence(10, basis='anticipated_reaction', value='intrigued'),
                                     evidence(11, basis='enjoyment', value=5)]
        self.grant(['canonical_seed', 'anticipated_reactions'])
        signals = self.body()['profile']['signals']
        self.assertEqual([(s['basis'], s['value']) for s in signals], [('anticipated_reaction', 'intrigued')])
        self.grant(['canonical_seed', 'experienced_enjoyment'], number=51)
        self.assertEqual({s['basis'] for s in self.body()['profile']['signals']},
                         {'anticipated_reaction', 'enjoyment'})

    def test_favourite_needs_its_own_class(self):
        self.snapshot['evidence'] = [evidence()]
        self.grant(['canonical_seed', 'reaction'])
        self.assertEqual(self.body()['profile']['signals'], [])
        self.grant(['canonical_seed', 'explicit_favourites'], number=51)
        self.assertEqual(self.body()['profile']['signals'][0]['value'], 'explicit_positive')

    def test_unresolved_personal_identity_needs_separate_text_grant(self):
        node = evidence()
        node['identity_status'] = 'unresolved_personal'
        self.snapshot['evidence'] = [node]
        self.grant(['canonical_seed', 'explicit_favourites'])
        self.assertEqual(self.body()['profile']['signals'], [])
        self.grant(['unresolved_personal_text'], number=51)
        self.assertEqual(len(self.body()['profile']['signals']), 1)

    def test_denied_deep_note_and_trait_changes_leave_bytes_and_pool_identical(self):
        note = evidence(11, basis='feedback_note', value={'reaction': 'intrigued', 'text': 'PRIVATE A'})
        self.snapshot['evidence'] = [evidence(), note]
        self.snapshot['derived_traits'] = [trait(12, [uid(11)]), trait(13, [uid(12)])]
        self.grant(['canonical_seed', 'explicit_favourites', 'inferred_traits'])
        self.candidates = [source('source:book:z'), source('source:book:a')]
        before = self.project()
        note['value']['text'] = 'PRIVATE B with a different length'
        self.snapshot['derived_traits'][0]['text'] = 'Changed denied intermediate'
        self.snapshot['derived_traits'][1]['text'] = 'Changed denied summary'
        self.snapshot['profile_revision_digest'] = 'b' * 64
        self.snapshot['evidence'].reverse()
        self.candidates.reverse()
        after = self.project()
        self.assertEqual(before.profile_bytes, after.profile_bytes)
        self.assertEqual(before.candidate_projection, after.candidate_projection)
        self.assertNotIn(b'PRIVATE', after.profile_bytes)
        self.assertNotIn('inferred_traits', json.loads(after.profile_bytes)['profile'])
        self.assertIn('raw_notes', after.omitted_classes)

    def test_granted_notes_preserve_unicode_whitespace_and_line_endings_exactly(self):
        text = '  Cafe\u0301 / Caf\u00e9\t\r\n\u96ea\n  '
        self.snapshot['evidence'] = [evidence(11, basis='feedback_note',
                                             value={'reaction': 'intrigued', 'text': text})]
        self.snapshot['derived_traits'] = [trait(12, [uid(11)]), trait(13, [uid(12)])]
        self.grant(['raw_notes', 'anticipated_reactions', 'inferred_traits'])
        profile = self.body()['profile']
        self.assertEqual(profile['raw_notes'][0]['text'], text)
        self.assertEqual(len(profile['inferred_traits']), 1)  # Identical permitted summaries coalesce.
        self.assertEqual(profile['inferred_traits'][0]['evidence_keys'], ['note-1'])
        self.assertEqual(profile['signals'], [])

    def test_feedback_note_anticipated_response_requires_both_classes(self):
        for reaction in ('love_it', 'intrigued', 'mildly_interested', 'sounds_boring', 'hate_it'):
            for classes in (['raw_notes'], ['anticipated_reactions'], ['raw_notes', 'legacy_choices']):
                with self.subTest(reaction=reaction, classes=classes):
                    self.grants.clear()
                    self.context['grant_revisions'].clear()
                    self.snapshot['evidence'] = [evidence(11, basis='feedback_note',
                        value={'reaction': reaction, 'text': 'Unauthorized compound note'})]
                    self.snapshot['derived_traits'] = [trait(12, [uid(11)]), trait(13, [uid(12)])]
                    self.grant(classes + ['inferred_traits'])
                    profile = self.body()['profile']
                    self.assertNotIn('raw_notes', profile)
                    self.assertNotIn('inferred_traits', profile)
                    self.assertEqual(profile['signals'], [])

    def test_feedback_note_legacy_response_requires_both_classes(self):
        for reaction in ('enjoyed', 'interested', 'not_for_me', 'unknown', 'skipped'):
            for classes in (['raw_notes'], ['legacy_choices'], ['raw_notes', 'anticipated_reactions']):
                with self.subTest(reaction=reaction, classes=classes):
                    self.grants.clear()
                    self.context['grant_revisions'].clear()
                    self.snapshot['evidence'] = [evidence(11, basis='feedback_note',
                        value={'reaction': reaction, 'text': 'Unauthorized compound note'})]
                    self.snapshot['derived_traits'] = [trait(12, [uid(11)]), trait(13, [uid(12)])]
                    self.grant(classes + ['inferred_traits'])
                    profile = self.body()['profile']
                    self.assertNotIn('raw_notes', profile)
                    self.assertNotIn('inferred_traits', profile)
                    self.assertEqual(profile['signals'], [])

    def test_feedback_note_complete_grants_preserve_tuple_and_derivative(self):
        groups = [('anticipated_reactions', ('love_it', 'intrigued', 'mildly_interested',
                                              'sounds_boring', 'hate_it')),
                  ('legacy_choices', ('enjoyed', 'interested', 'not_for_me', 'unknown', 'skipped'))]
        for reaction_class, reactions in groups:
            for reaction in reactions:
                with self.subTest(reaction=reaction, reaction_class=reaction_class):
                    self.grants.clear()
                    self.context['grant_revisions'].clear()
                    value = {'reaction': reaction, 'text': '  Authorized note\t\r\n\u96ea  '}
                    self.snapshot['evidence'] = [evidence(11, basis='feedback_note', value=value)]
                    self.snapshot['derived_traits'] = [trait(12, [uid(11)], text='Direct hypothesis'),
                                                       trait(13, [uid(12)], text='Transitive hypothesis')]
                    self.grant(['raw_notes', reaction_class, 'inferred_traits'])
                    profile = self.body()['profile']
                    self.assertEqual(profile['raw_notes'], [{'id': 'note-1', 'key': 'note-1', **value}])
                    self.assertEqual(len(profile['inferred_traits']), 2)
                    self.assertTrue(all(t['evidence_keys'] == ['note-1'] for t in profile['inferred_traits']))
                    self.assertEqual(profile['signals'], [])

    def test_feedback_note_declared_classes_and_family_subjects_remain_required(self):
        self.context['applicable_subjects'].append(PARTNER)
        self.snapshot['profile_kind'] = 'family'
        note = evidence(11, basis='feedback_note', subjects=[SUBJECT, PARTNER],
                        value={'reaction': 'intrigued', 'text': 'Joint compound note'})
        note['data_classes'].append('sensitive')
        self.snapshot['evidence'] = [note]
        self.snapshot['derived_traits'] = [trait(12, [uid(11)])]
        self.grant(['raw_notes', 'anticipated_reactions', 'sensitive', 'inferred_traits'],
                   subjects=[SUBJECT, PARTNER])
        self.grant(['raw_notes', 'inferred_traits'], subject=PARTNER, number=51)
        profile = self.body()['profile']
        self.assertNotIn('raw_notes', profile)
        self.assertNotIn('inferred_traits', profile)
        self.grant(['anticipated_reactions'], subject=PARTNER, number=52)
        profile = self.body()['profile']
        self.assertNotIn('raw_notes', profile)
        self.assertNotIn('inferred_traits', profile)
        self.grant(['sensitive'], subject=PARTNER, number=53)
        profile = self.body()['profile']
        self.assertEqual(profile['raw_notes'][0]['reaction'], 'intrigued')
        self.assertEqual(profile['inferred_traits'][0]['evidence_keys'], ['note-1'])

    def test_trait_grant_does_not_authorize_raw_ancestry(self):
        self.snapshot['evidence'] = [evidence(11, basis='feedback_note',
                                             value={'reaction': 'intrigued', 'text': 'private'})]
        self.snapshot['derived_traits'] = [trait(12, [uid(11)])]
        self.grant(['inferred_traits'])
        self.assertNotIn('inferred_traits', self.body()['profile'])

    def test_family_requires_each_contributor_and_one_adult_cannot_grant_another(self):
        self.context['applicable_subjects'].append(PARTNER)
        self.snapshot['profile_kind'] = 'family'
        self.snapshot['evidence'] = [evidence(subjects=[SUBJECT, PARTNER])]
        self.grant(['canonical_seed', 'explicit_favourites'], subjects=[SUBJECT, PARTNER])
        self.assertEqual(self.body()['profile']['signals'], [])
        self.grant(['canonical_seed', 'explicit_favourites'], subject=PARTNER, number=51)
        self.assertEqual(len(self.body()['profile']['signals']), 1)

    def test_subject_cannot_be_hidden_by_a_different_applicability_list(self):
        self.context['applicable_subjects'].append(PARTNER)
        self.snapshot['evidence'] = [evidence(subjects=[PARTNER])]
        self.assert_code('AUTHORITY_DENIED')

    def test_missing_unknown_and_cyclic_ancestry_do_not_trust_lineage_flag(self):
        self.grant(['inferred_traits'])
        for rows in ([trait(12, [uid(999)])],
                     [trait(12, [uid(13)]), trait(13, [uid(12)])],
                     [trait(12, [uid(12)])]):
            with self.subTest(parents=rows[0]['evidence_ids']):
                self.snapshot['derived_traits'] = rows
                self.assertNotIn('inferred_traits', self.body()['profile'])

    def test_assistant_unknown_sensitivity_and_wrong_purpose_exclude_evidence(self):
        self.grant(['canonical_seed', 'explicit_favourites'])
        for field, value in [('origin', 'assistant'), ('origin', 'unknown'),
                             ('sensitivity', 'unknown'), ('purposes', ['catalogue_lookup'])]:
            with self.subTest(field=field, value=value):
                node = evidence()
                node[field] = value
                self.snapshot['evidence'] = [node]
                self.assertEqual(self.body()['profile']['signals'], [])

    def test_identical_permitted_values_do_not_use_private_ids_to_break_ties(self):
        self.grant(['canonical_seed', 'explicit_favourites', 'inferred_traits'])
        self.snapshot['evidence'] = [evidence(10), evidence(11)]
        self.snapshot['derived_traits'] = [trait(12, [uid(10)])]
        before = self.project().profile_bytes
        self.snapshot['evidence'].reverse()
        self.snapshot['derived_traits'][0]['evidence_ids'] = [uid(11)]
        self.snapshot['derived_traits'][0]['direct_parent_ids'] = [uid(11)]
        self.assertEqual(before, self.project().profile_bytes)
        self.assertEqual(len(self.body()['profile']['signals']), 1)
        for private in [SUBJECT, uid(1), uid(2), uid(3), uid(10), uid(11), 'private:', 'a' * 64]:
            self.assertNotIn(private.encode(), before)

    def test_foreign_scope_and_stale_authority_fail_without_echo(self):
        for field in ('installation_id', 'actor_ref', 'profile_ref'):
            with self.subTest(field=field):
                changed = deepcopy(self.snapshot)
                changed[field] = uid(999)
                self.assert_code('AUTHORITY_DENIED', snapshot=changed)
        self.snapshot['evidence'] = [evidence(subjects=[uid(999)])]
        self.assert_code('AUTHORITY_DENIED')

    def test_config_policy_and_regrant_aba_revisions_are_not_content_checks(self):
        grant = self.grant(['canonical_seed', 'explicit_favourites'])
        for field in ('config_revision', 'policy_epoch', 'grant_revision'):
            with self.subTest(field=field):
                previous = grant[field]
                grant[field] += 2  # A -> B -> A content still has a newer generation.
                self.assert_code('PREVIEW_STALE')
                grant[field] = previous
        self.snapshot['policy_epoch'] += 1
        self.assert_code('PREVIEW_STALE')

    def test_expired_revoked_and_overlong_grants(self):
        self.snapshot['evidence'] = [evidence()]
        grant = self.grant(['canonical_seed', 'explicit_favourites'])
        grant['expires_at'] = NOW.isoformat().replace('+00:00', 'Z')
        self.assertEqual(self.body()['profile']['signals'], [])
        grant['expires_at'], grant['state'] = END, 'revoked'
        self.assertEqual(self.body()['profile']['signals'], [])
        grant['expires_at'] = '2026-11-01T04:00:00Z'
        self.assert_code('CONTRACT_INVALID')
        self.assert_code('AUTHORITY_DENIED', now=datetime(2026, 11, 1, 3, tzinfo=timezone.utc))

    def test_public_source_projection_is_local_sorted_and_preserves_original_time(self):
        self.candidates = [source('source:book:z'), source('source:book:a')]
        result = self.project()
        self.assertEqual([c['key'] for c in result.candidate_projection], ['source:book:a', 'source:book:z'])
        projected = result.candidate_projection[0]
        self.assertEqual(projected['observed_at'], START)
        self.assertEqual(projected['valid_until'], END)
        self.assertEqual(projected['source_roots'], ['observation:1'])
        self.assertEqual(projected['rights_ref'], 'rights:source:book:a')
        self.assertEqual(json.loads(result.profile_bytes)['candidates'], [])
        self.assertNotIn(b'observation:', result.profile_bytes)

    def test_any_required_scalar_denied_unknown_or_expired_drops_whole_candidate(self):
        for target in SCALARS:
            for disposition in ('denied', 'unknown', 'expired'):
                with self.subTest(target=target, disposition=disposition):
                    record = source()
                    binding = next(b for b in record['fields'] if b['target'] == target)
                    if disposition == 'expired':
                        binding['rights']['expires_at'] = NOW.isoformat().replace('+00:00', 'Z')
                    else:
                        binding['rights']['disposition'] = disposition
                    self.candidates = [record]
                    self.assertEqual(self.project().candidate_projection, ())

    def test_optional_fact_denial_omits_whole_tuple_and_orders_retained_facts(self):
        facts = [{'id': name, 'property': 'test', 'value': name, 'observation_ref': 'observation:1'}
                 for name in ('z', 'private-fact', 'a')]
        for disposition in ('denied', 'unknown', 'expired', 'ancestry'):
            with self.subTest(disposition=disposition):
                record = source(facts=facts)
                binding = next(b for b in record['fields'] if b['fact_id'] == 'private-fact')
                if disposition == 'ancestry':
                    binding['direct_parent_ids'] = [uid(999)]
                    binding['classification'] = 'personal_or_derived'
                    resign(record, binding)
                elif disposition == 'expired':
                    binding['rights']['expires_at'] = NOW.isoformat().replace('+00:00', 'Z')
                else:
                    binding['rights']['disposition'] = disposition
                self.candidates = [record]
                result = self.project()
                self.assertTrue(result.ok)
                retained = result.candidate_projection[0]['facts']
                self.assertEqual([fact['id'] for fact in retained], ['a', 'z'])
                self.assertNotIn('private-fact', json.dumps(result.candidate_projection))

    def test_rights_are_conjunctive_with_personal_grants_even_for_public_label(self):
        self.snapshot['evidence'] = [evidence()]
        record = source()
        binding = next(b for b in record['fields'] if b['target'] == 'title')
        binding['direct_parent_ids'] = [uid(10)]  # A public label is not proof of independence.
        resign(record, binding)
        self.candidates = [record]
        self.assertEqual(self.project().candidate_projection, ())
        self.grant(['canonical_seed', 'explicit_favourites'])
        self.assertEqual(len(self.project().candidate_projection), 1)
        binding['rights']['disposition'] = 'denied'
        self.assertEqual(self.project().candidate_projection, ())

    def test_tainted_pool_returns_fixed_blocked_result_instead_of_stripping_candidate(self):
        self.snapshot['evidence'] = [evidence(11, basis='feedback_note',
                                             value={'reaction': 'intrigued', 'text': 'denied A'})]
        self.snapshot['derived_traits'] = [trait(12, [uid(11)]), trait(13, [uid(12)])]
        self.grant(['inferred_traits'])
        record = source()
        record['direct_parent_ids'] = [uid(13)]
        self.candidates = [record, source('source:book:z')]
        first = self.project()
        self.assertFalse(first.ok)
        self.assertEqual((first.code, first.profile_bytes, first.candidate_projection, first.omitted_classes),
                         ('AUTHORITY_DENIED', None, (), ()))
        self.snapshot['evidence'][0]['value']['text'] = 'denied B'
        record['candidate']['title'] = 'Changed tainted title'
        resign(record, next(b for b in record['fields'] if b['target'] == 'title'))
        self.assertEqual(first, self.project())

    def test_changed_or_missing_source_revision_and_policy_revision_are_stale(self):
        self.candidates = [source()]
        for observations, policies in [({}, self.policies),
            ({'observation:1': {'revision': 9, 'digest': 'c' * 64}}, self.policies),
            ({'observation:1': {'revision': 7, 'digest': 'd' * 64}}, self.policies),
            (self.observations, {}), (self.observations, {'policy:1': {'revision': 5}})]:
            with self.subTest(observations=observations, policies=policies):
                self.assert_code('PREVIEW_STALE', observations=observations, policies=policies)

    def test_binding_covers_complete_tuple_and_exact_lineage(self):
        fact = {'id': 'f1', 'property': 'length', 'value': 20, 'observation_ref': 'observation:1'}
        for mutation in ('value', 'property', 'parents', 'classification', 'locator'):
            with self.subTest(mutation=mutation):
                record = source(facts=[deepcopy(fact)])
                binding = record['fields'][-1]
                if mutation in ('value', 'property'):
                    record['candidate']['facts'][0][mutation] = 'changed'
                elif mutation == 'parents':
                    binding['direct_parent_ids'] = [uid(999)]
                elif mutation == 'classification':
                    binding['classification'] = 'personal_or_derived'
                    binding['direct_parent_ids'] = [uid(999)]
                else:
                    binding['observation']['locator'] = 'different'
                self.candidates = [record]
                self.assert_code('CONTRACT_INVALID')

    def test_unbound_duplicate_unknown_fields_and_provenance_are_rejected(self):
        for mutation in ('extra', 'duplicate', 'missing', 'roots', 'rights_ref', 'unknown_target'):
            with self.subTest(mutation=mutation):
                record = source()
                if mutation == 'extra':
                    record['fields'][0]['rights']['display'] = 'allowed'
                elif mutation == 'duplicate':
                    record['fields'][-1] = deepcopy(record['fields'][0])
                elif mutation == 'missing':
                    record['fields'].pop()
                elif mutation == 'roots':
                    record['candidate']['source_roots'] = ['unknown:observation']
                elif mutation == 'rights_ref':
                    record['candidate']['rights_ref'] = 'different'
                else:
                    record['fields'][0]['target'] = 'arbitrary'
                self.candidates = [record]
                self.assert_code('CONTRACT_INVALID')

    def test_synthetic_identity_is_accepted_only_in_explicit_isolated_fixture(self):
        record = source('fixture:a')
        record['candidate']['identity_status'] = 'fixture_identity_only'
        record['candidate']['evidence_type'] = 'fixture_not_live'
        for binding in record['fields']:
            resign(record, binding)
        self.candidates = [record]
        self.assert_code('AUTHORITY_DENIED')
        self.context['isolated_fixture'] = True
        self.assertEqual(len(self.project().candidate_projection), 1)
        record['candidate']['identity_status'] = 'unresolved_personal'
        self.assert_code('CONTRACT_INVALID')

    def test_no_restamping_and_earliest_retained_deadline(self):
        record = source()
        binding = record['fields'][0]
        binding['observation']['expires_at'] = '2026-11-01T01:30:00Z'
        resign(record, binding)
        self.candidates = [record]
        projected = self.project().candidate_projection[0]
        self.assertEqual(projected['observed_at'], START)
        self.assertEqual(projected['valid_until'], '2026-11-01T01:30:00Z')

    def test_unknown_versions_duplicate_nodes_and_malformed_notes_are_bounded(self):
        self.snapshot['schema'] = 'profile-snapshot/v999'
        self.assert_code('CONTRACT_INVALID')
        self.snapshot['schema'] = 'profile-snapshot/v1'
        malformed = evidence()
        malformed['subject_ref'] = {'private': 'invalid subject'}
        self.snapshot['evidence'] = [malformed]
        self.assert_code('CONTRACT_INVALID')
        self.snapshot['evidence'] = [evidence(), evidence()]
        self.assert_code('CONTRACT_INVALID')
        self.snapshot['evidence'] = [evidence(basis='feedback_note',
            value={'reaction': 'intrigued', 'text': 'private\u0000'})]
        self.assert_code('CONTRACT_INVALID')

    def test_caps_reject_instead_of_truncating(self):
        self.snapshot['evidence'] = [evidence(i + 1000) for i in range(501)]
        self.assert_code('LIMIT_EXCEEDED')
        self.snapshot['evidence'] = []
        self.candidates = [source('source:book:' + str(i)) for i in range(21)]
        self.assert_code('LIMIT_EXCEEDED')
        self.candidates = [source()] * 101
        self.assert_code('LIMIT_EXCEEDED')
        self.candidates = []
        self.snapshot['seen_keys'] = ['x' * 300 + str(i) for i in range(1000)]
        self.assert_code('LIMIT_EXCEEDED')


class CanonicalJsonTests(unittest.TestCase):
    def test_exact_utf8_compact_order_and_no_unicode_normalization(self):
        self.assertEqual(canonical_bytes({'z': '\u96ea', 'a': 'e\u0301'}),
                         b'{"a":"e\xcc\x81","z":"\xe9\x9b\xaa"}')
        self.assertNotEqual(canonical_bytes('e\u0301'), canonical_bytes('\u00e9'))
        self.assertEqual(read_json(b'{"z":2,"a":1}'), {'z': 2, 'a': 1})

    def test_duplicate_nonfinite_invalid_utf8_and_surrogates(self):
        for raw in (b'{"x":1,"x":2}', b'NaN', b'Infinity', b'1e999', b'"\xff"',
                    b'"\\ud800"', b'{"x":', b'{"nested":{"x":1,"x":2}}'):
            with self.subTest(raw=raw):
                with self.assertRaises(DisclosureError) as raised:
                    read_json(raw)
                self.assertEqual(str(raised.exception), 'CONTRACT_INVALID')

    def test_byte_and_depth_limits_and_unsupported_python_values(self):
        for value in ({'x': float('nan')}, {'x': float('inf')}, {'x': (1, 2)}, {1: 'x'}):
            with self.subTest(value=value):
                with self.assertRaises(DisclosureError):
                    canonical_bytes(value)
        for value in ('\u96ea' * 90000, [[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]):
            with self.assertRaises(DisclosureError) as raised:
                canonical_bytes(value)
            self.assertEqual(raised.exception.code, 'LIMIT_EXCEEDED')
        with self.assertRaises(DisclosureError) as raised:
            read_json(b' ' * 262145)
        self.assertEqual(raised.exception.code, 'LIMIT_EXCEEDED')


class PortableProjectionTests(unittest.TestCase):
    def fixture(self, records):
        # Reuse only the established pure synthetic inputs, not another projector.
        fixture = DisclosureTests()
        fixture.setUp()
        fixture.candidates = records
        return fixture

    def test_body_projection_replaces_only_local_provenance_with_scoped_aliases(self):
        from shelf.disclosure import portable_input
        record = source(facts=[{'id': 'local-fact', 'property': 'dimension', 'value': 'Public fact',
                               'observation_ref': 'observation:1'}])
        prepared = self.fixture([record]).project()
        projected = portable_input(prepared)['candidates'][0]
        self.assertEqual(projected['key'], record['candidate']['key'])
        self.assertEqual(projected['title'], record['candidate']['title'])
        self.assertEqual(projected['rights_ref'], 'rights-1')
        self.assertEqual(projected['source_roots'], ['evidence-1'])
        self.assertEqual(projected['facts'], [{'id': 'fact-1-1', 'property': 'dimension',
                                               'value': 'Public fact', 'observation_ref': 'evidence-1'}])
        self.assertEqual(prepared.candidate_projection[0]['rights_ref'], record['record_ref'])

    def test_private_provenance_names_never_break_wire_alias_or_order_ties(self):
        from shelf.disclosure import portable_input
        record = source()
        fixture = self.fixture([record])
        before = canonical_bytes(portable_input(fixture.project()))
        record['record_ref'] = record['candidate']['rights_ref'] = 'PRIVATE-RIGHTS-CHANGED'
        record['candidate']['source_roots'] = ['PRIVATE-OBS-CHANGED']
        for binding in record['fields']:
            binding['observation']['ref'] = 'PRIVATE-OBS-CHANGED'
            binding['rights']['policy_ref'] = 'PRIVATE-POLICY-CHANGED'
            resign(record, binding)
        fixture.observations = {'PRIVATE-OBS-CHANGED': {'revision': 7, 'digest': 'c' * 64}}
        fixture.policies = {'PRIVATE-POLICY-CHANGED': {'revision': 3}}
        after = canonical_bytes(portable_input(fixture.project()))
        self.assertEqual(before, after)
        self.assertNotIn(b'PRIVATE', after)

    def test_denied_fact_changes_do_not_seed_wire_ids_roots_or_bytes(self):
        from shelf.disclosure import portable_input
        record = source(facts=[{'id': 'a', 'property': 'retained', 'value': 'Public', 'observation_ref': 'observation:1'},
                               {'id': 'z', 'property': 'denied', 'value': 'DENIED-A', 'observation_ref': 'observation:1'}])
        denied = next(b for b in record['fields'] if b['fact_id'] == 'z')
        denied['rights']['disposition'] = 'denied'
        fixture = self.fixture([record])
        before = canonical_bytes(portable_input(fixture.project()))
        record['candidate']['facts'][1]['value'] = 'DENIED-B-CHANGED'
        record['candidate']['facts'][1]['observation_ref'] = 'PRIVATE-DENIED-OBS'
        denied['observation']['ref'] = 'PRIVATE-DENIED-OBS'
        fixture.observations['PRIVATE-DENIED-OBS'] = {'revision': 7, 'digest': 'c' * 64}
        resign(record, denied)
        after = canonical_bytes(portable_input(fixture.project()))
        self.assertEqual(before, after)
        self.assertNotIn(b'DENIED', after)

    def test_wire_projection_keeps_note_and_descendant_component_grants_conjunctive(self):
        from shelf.disclosure import portable_input
        fixture = self.fixture([source()])
        fixture.snapshot['evidence'] = [evidence(11, basis='feedback_note',
            value={'reaction': 'intrigued', 'text': 'Exact permitted compound note'})]
        fixture.snapshot['derived_traits'] = [trait(12, [uid(11)])]
        fixture.grant(['raw_notes', 'inferred_traits'])
        denied = portable_input(fixture.project())['profile']
        self.assertNotIn('raw_notes', denied)
        self.assertNotIn('inferred_traits', denied)
        fixture.grant(['anticipated_reactions'], number=51)
        permitted = portable_input(fixture.project())['profile']
        self.assertEqual(permitted['raw_notes'][0]['text'], 'Exact permitted compound note')
        self.assertEqual(permitted['inferred_traits'][0]['evidence_keys'], ['note-1'])


class CatalogueProjectionTests(unittest.TestCase):
    def test_catalogue_terms_and_order_ignore_denied_transitive_notes_and_local_hashes(self):
        from shelf.disclosure import catalogue_input
        fixture = DisclosureTests()
        fixture.setUp()
        fixture.context['purpose'] = 'catalogue_lookup'
        favourite = evidence(10)
        note = evidence(11, basis='feedback_note', value={'reaction': 'intrigued', 'text': 'DENIED-QUERY-A'})
        derived = trait(12, [uid(11)])
        for node in (favourite, note, derived):
            node['purposes'] = ['catalogue_lookup']
        fixture.snapshot['evidence'] = [favourite, note]
        fixture.snapshot['derived_traits'] = [derived]
        fixture.grant(['canonical_seed', 'explicit_favourites', 'inferred_traits'])
        categories = ('books', 'films', 'tv', 'music', 'events', 'outings')
        first, omitted = catalogue_input(fixture.snapshot, fixture.grants, fixture.context, categories, now=NOW)
        note['value']['text'] = 'DENIED-QUERY-B with different length'
        derived['text'] = 'Changed denied derived query'
        fixture.snapshot['profile_revision_digest'] = 'b' * 64
        fixture.snapshot['evidence'].reverse()
        second, _ = catalogue_input(fixture.snapshot, fixture.grants, fixture.context, categories, now=NOW)
        self.assertEqual(first, second)
        self.assertIn('raw_notes', omitted)
        self.assertNotIn(b'DENIED', second)
        self.assertNotIn(b'bbbbbbbbbbbbbbbb', second)
        value = json.loads(second)
        self.assertEqual(value['categories'], list(categories))
        self.assertEqual([q['terms'] for q in value['queries']], [['A synthetic favourite']] * len(categories))
        self.assertNotIn('inferred_traits', value['profile'])
