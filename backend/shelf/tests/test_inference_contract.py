"""Synthetic portable results only; no model, source, authority or transport use."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from shelf.inference_contract import ContractError, output_schema, validate_result


CANARY = 'PRIVATE_RESPONSE_CANARY_019'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def assessment(key='candidate:a', *, signals=None, refs=None):
    return {'candidate_key': key, 'decision': 'uncertain', 'signal_keys': signals or [],
            'evidence_refs': refs or [], 'hypothesis': 'A synthetic technical hypothesis.'}


def result(*, graph=None, rows=None):
    return {'schema': 'cabinet-inference-result/v1', 'associations': graph,
            'candidate_assessments': [assessment()] if rows is None else rows}


def graph(members=None, links=None):
    return {'basis': 'inferred_taste_association', 'clusters': [
        {'label': 'Synthetic association', 'members': ['signal:a'] if members is None else members,
         'explanation': 'A hypothesis, not a sourced relationship.'}],
        'links': [] if links is None else links}


class InferenceContractTests(unittest.TestCase):
    def setUp(self):
        self.signals = []
        self.candidates = [{'key': 'candidate:a', 'evidence_refs': ['evidence:a']}]
        # Fail closed even if a future edit accidentally adds transport here.
        for target in ('socket.getaddrinfo', 'socket.create_connection', 'socket.socket.connect'):
            self.enterContext(patch(target, side_effect=AssertionError('Network prohibited in result tests')))

    def validate(self, value, **overrides):
        args = {'signals': self.signals, 'candidates': self.candidates}
        args.update(overrides)
        return validate_result(encoded(value), **args)

    def assert_bad(self, value, code='CONTRACT_INVALID', *, raw=False, **overrides):
        args = {'signals': self.signals, 'candidates': self.candidates}
        args.update(overrides)
        with self.assertRaises(ContractError) as raised:
            validate_result(value if raw else encoded(value), **args)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)
        self.assertEqual(raised.exception.args, (code,))
        self.assertNotIn(CANARY, repr(raised.exception))

    def test_zero_signal_neutral_result_is_typed_without_invented_preferences(self):
        value = result(rows=[assessment(refs=['evidence:a'])])
        self.assertEqual(self.validate(value), value)
        self.assertEqual(self.validate(value)['candidate_assessments'][0]['signal_keys'], [])
        self.assertEqual(self.signals, [])

    def test_nonzero_associations_delegate_to_unchanged_legacy_validator(self):
        from shelf.synthesis import validate_output
        self.signals = [{'key': 'signal:a', 'medium': 'book'}, {'key': 'signal:b', 'medium': 'film'}]
        association = graph(['signal:a', 'signal:b'], [
            {'from': 'signal:a', 'to': 'signal:b', 'explanation': 'Possible cross-media association.'}])
        value = result(graph=association, rows=[assessment(signals=['signal:a'], refs=['evidence:a'])])
        with patch('shelf.synthesis.validate_output', wraps=validate_output) as legacy:
            self.assertEqual(self.validate(value), value)
        legacy.assert_called_once_with(association, self.signals)

    def test_zero_and_nonzero_association_requirement_cannot_be_bypassed(self):
        self.assert_bad(result(graph=graph()))
        self.signals = [{'key': 'signal:a', 'medium': 'book'}]
        self.assert_bad(result())
        for malformed in (False, [], {}, {'basis': 'inferred_taste_association', 'clusters': [], 'links': []}):
            with self.subTest(malformed=malformed):
                self.assert_bad(result(graph=malformed))

    def test_partial_and_empty_assessments_do_not_pad_or_promote_other_candidates(self):
        self.candidates.append({'key': 'candidate:b', 'evidence_refs': []})
        self.assertEqual(len(self.validate(result())['candidate_assessments']), 1)
        self.assertEqual(self.validate(result(rows=[]))['candidate_assessments'], [])
        self.assertEqual(self.validate(result(rows=[]), candidates=[])['candidate_assessments'], [])

    def test_decisions_and_exact_unicode_hypothesis_are_preserved(self):
        for decision in ('retain', 'reject', 'uncertain'):
            with self.subTest(decision=decision):
                value = result()
                value['candidate_assessments'][0].update(decision=decision, hypothesis='  e\u0301 / \u00e9\n\u96ea  ')
                self.assertEqual(self.validate(value), value)
        value['candidate_assessments'][0]['hypothesis'] = ''
        self.assertEqual(self.validate(value), value)

    def test_unknown_version_top_level_fields_and_missing_members_are_fixed_errors(self):
        for field in ('schema', 'associations', 'candidate_assessments'):
            with self.subTest(missing=field):
                value = result()
                del value[field]
                self.assert_bad(value)
        self.assert_bad(dict(result(), debug=CANARY))
        self.assert_bad(dict(result(), schema=CANARY))
        self.assert_bad({'basis': 'inferred_taste_association', 'clusters': [], 'links': []})

    def test_assessment_fields_are_closed_required_and_typed(self):
        for field in ('candidate_key', 'decision', 'signal_keys', 'evidence_refs', 'hypothesis'):
            with self.subTest(missing=field):
                row = assessment()
                del row[field]
                self.assert_bad(result(rows=[row]))
        for field, malformed in [('extra', CANARY), ('candidate_key', None), ('decision', CANARY),
                                  ('decision', []), ('signal_keys', {}), ('evidence_refs', None),
                                  ('hypothesis', {'text': CANARY})]:
            with self.subTest(field=field, malformed=malformed):
                row = assessment()
                row[field] = malformed
                self.assert_bad(result(rows=[row]))

    def test_duplicate_json_keys_are_rejected_at_every_depth(self):
        for raw in (b'{"schema":"cabinet-inference-result/v1","schema":"x","associations":null,"candidate_assessments":[]}',
                    b'{"schema":"cabinet-inference-result/v1","associations":null,"candidate_assessments":[{"candidate_key":"candidate:a","candidate_key":"candidate:a"}]}'):
            with self.subTest(raw=raw):
                self.assert_bad(raw, raw=True)

    def test_invalid_utf8_surrogate_nonfinite_and_truncated_json_are_safe(self):
        for raw in (b'"\xff"', b'"\\ud800"', b'NaN', b'Infinity', b'1e999',
                    b'{"x":NaN}', b'{"private":"PRIVATE_RESPONSE_CANARY_019"'):
            with self.subTest(raw=raw):
                self.assert_bad(raw, raw=True)

    def test_only_a_json_object_decoded_from_bytes_is_accepted(self):
        for raw in (result(), 'PRIVATE_RESPONSE_CANARY_019', bytearray(b'{}'), None):
            with self.subTest(raw=raw):
                self.assert_bad(raw, raw=True)
        for value in (None, [], True, 7, CANARY):
            with self.subTest(value=value):
                self.assert_bad(value)

    def test_candidate_ids_cannot_be_fabricated_by_result_or_suggested_fields(self):
        self.assert_bad(result(rows=[assessment(CANARY)]))
        self.assert_bad(dict(result(), suggested_candidates=[{'key': CANARY}]))

    def test_signal_ids_cannot_be_fabricated_or_derived_from_model_suggestions(self):
        self.assert_bad(result(rows=[assessment(signals=[CANARY])]))
        self.signals = [{'key': 'signal:a', 'medium': 'book'}]
        self.assert_bad(result(graph=graph(), rows=[assessment(signals=[CANARY])]))
        self.assert_bad(dict(result(graph=graph()), suggested_signals=[{'key': CANARY, 'medium': 'book'}]))

    def test_evidence_refs_must_belong_to_the_assessed_candidate(self):
        self.candidates.append({'key': 'candidate:b', 'evidence_refs': ['evidence:b']})
        for refs in ([CANARY], ['evidence:b']):
            with self.subTest(refs=refs):
                self.assert_bad(result(rows=[assessment(refs=refs)]))
        self.assertEqual(self.validate(result(rows=[assessment(refs=['evidence:a'])]))['candidate_assessments'][0]['evidence_refs'],
                         ['evidence:a'])

    def test_duplicate_assessments_and_references_are_rejected(self):
        self.assert_bad(result(rows=[assessment(), assessment()]))
        self.assert_bad(result(rows=[assessment(refs=['evidence:a', 'evidence:a'])]))
        self.signals = [{'key': 'signal:a', 'medium': 'book'}]
        self.assert_bad(result(graph=graph(), rows=[assessment(signals=['signal:a', 'signal:a'])]))

    def test_submitted_views_require_unique_bounded_typed_identities(self):
        for signals in ([{'key': 'a', 'medium': 'book'}, {'key': 'a', 'medium': 'book'}],
                        [{'key': 'a', 'medium': 'book'}, {'key': 'a', 'medium': 'film'}],
                        [{'key': 'a', 'medium': ''}], [{'key': None, 'medium': 'book'}],
                        [{'key': 'a', 'medium': 'book', 'title': CANARY}], (), {}):
            with self.subTest(signals=signals):
                self.assert_bad(result(), signals=signals)
        for candidates in ([{'key': 'candidate:a', 'evidence_refs': []}] * 2,
                           [{'key': 'candidate:a', 'evidence_refs': ['x', 'x']}],
                           [{'key': 'candidate:a', 'evidence_refs': [None]}],
                           [{'key': 'candidate:a', 'evidence_refs': [], 'title': CANARY}], None):
            with self.subTest(candidates=candidates):
                self.assert_bad(result(), candidates=candidates)

    def test_key_and_hypothesis_bounds_preserve_values_without_truncation(self):
        key = '\u96ea' * 300
        self.candidates = [{'key': key, 'evidence_refs': []}]
        row = assessment(key)
        row['hypothesis'] = 'x' * 1200
        self.assertEqual(self.validate(result(rows=[row]))['candidate_assessments'][0], row)
        row['hypothesis'] += 'x'
        self.assert_bad(result(rows=[row]))
        for invalid in ('', 'k' * 301):
            self.assert_bad(result(rows=[assessment(invalid)]))

    def test_hundred_candidate_and_assessment_cap_overrides_peer_five_hundred(self):
        self.candidates = [{'key': f'candidate:{i}', 'evidence_refs': []} for i in range(100)]
        rows = [assessment(candidate['key']) for candidate in self.candidates]
        self.assertEqual(len(self.validate(result(rows=rows))['candidate_assessments']), 100)
        self.assert_bad(result(rows=rows + [assessment('candidate:0')]), 'LIMIT_EXCEEDED')
        self.assert_bad(result(rows=[]), 'LIMIT_EXCEEDED',
                        candidates=self.candidates + [{'key': 'candidate:100', 'evidence_refs': []}])

    def test_five_hundred_signal_cap_keeps_existing_graph_cardinality(self):
        self.signals = [{'key': f'signal:{i}', 'medium': 'book'} for i in range(500)]
        value = result(graph=graph(['signal:0']))
        self.assertEqual(self.validate(value), value)
        self.assert_bad(value, 'LIMIT_EXCEEDED', signals=self.signals + [{'key': 'signal:500', 'medium': 'film'}])

    def test_result_limit_is_utf8_bytes_and_rejects_instead_of_truncating(self):
        raw = encoded(result())
        padded = raw + b' ' * (128 * 1024 - len(raw))
        self.assertEqual(validate_result(padded, signals=[], candidates=self.candidates), result())
        self.assert_bad(padded + b' ', 'LIMIT_EXCEEDED', raw=True)
        # Non-ASCII bytes count even when character count fits the limit.
        multibyte = b'"' + ('\u96ea' * 50000).encode('utf-8') + b'"'
        self.assert_bad(multibyte, 'LIMIT_EXCEEDED', raw=True)

    def test_depth_limit_applies_before_unknown_content_can_be_used(self):
        nested = CANARY
        for _ in range(17):
            nested = [nested]
        self.assert_bad(dict(result(), extra=nested), 'LIMIT_EXCEEDED')

    def test_assessment_citation_caps_are_independent_of_input_allowlist_size(self):
        refs = [f'evidence:{i}' for i in range(101)]
        self.candidates[0]['evidence_refs'] = refs
        self.assertEqual(len(self.validate(result(rows=[assessment(refs=refs[:100])]))['candidate_assessments'][0]['evidence_refs']), 100)
        self.assert_bad(result(rows=[assessment(refs=refs)]), 'LIMIT_EXCEEDED')
        self.signals = [{'key': f'signal:{i}', 'medium': 'book'} for i in range(500)]
        row = assessment(signals=[s['key'] for s in self.signals], refs=refs[:100])
        self.assertEqual(len(self.validate(result(graph=graph(['signal:0']), rows=[row]))['candidate_assessments'][0]['signal_keys']), 500)
        row['signal_keys'].append('signal:0')
        self.assert_bad(result(graph=graph(['signal:0']), rows=[row]), 'LIMIT_EXCEEDED')

    def test_legacy_cluster_count_and_member_limits_are_unchanged(self):
        self.signals = [{'key': f'signal:{i}', 'medium': 'book'} for i in range(101)]
        value = result(graph=graph([f'signal:{i}' for i in range(100)]))
        value['associations']['clusters'] *= 20
        self.assertEqual(len(self.validate(value)['associations']['clusters']), 20)
        value['associations']['clusters'].append(deepcopy(value['associations']['clusters'][0]))
        self.assert_bad(value)
        self.assert_bad(result(graph=graph([f'signal:{i}' for i in range(101)])))

    def test_legacy_cross_media_link_count_and_identity_rules_are_unchanged(self):
        self.signals = [{'key': 'book', 'medium': 'book'}] + [
            {'key': f'film:{i}', 'medium': 'film'} for i in range(41)]
        links = [{'from': 'book', 'to': f'film:{i}', 'explanation': 'Tentative association.'} for i in range(41)]
        self.assertEqual(len(self.validate(result(graph=graph(['book'], links[:40])))['associations']['links']), 40)
        self.assert_bad(result(graph=graph(['book'], links)))
        for invalid in ([links[0], dict(links[0], **{'from': 'film:0', 'to': 'book'})],
                        [{'from': 'film:0', 'to': 'film:1', 'explanation': 'Same medium.'}],
                        [{'from': 'book', 'to': CANARY, 'explanation': 'Unknown endpoint.'}]):
            with self.subTest(links=invalid):
                self.assert_bad(result(graph=graph(['book'], invalid)))

    def test_legacy_unknown_duplicate_members_and_bad_text_return_only_fixed_codes(self):
        self.signals = [{'key': 'signal:a', 'medium': 'book'}]
        for association in (graph([CANARY]), graph(['signal:a', 'signal:a']),
                            dict(graph(), extra=CANARY), dict(graph(), basis=CANARY)):
            with self.subTest(association=association):
                self.assert_bad(result(graph=association))
        for field in ('label', 'explanation'):
            association = graph()
            association['clusters'][0][field] = CANARY + '\u0000'
            self.assert_bad(result(graph=association))

    def test_output_schema_is_closed_bounded_and_keeps_legacy_graph_shape(self):
        schema = output_schema()
        self.assertEqual(schema['properties']['schema'], {'const': 'cabinet-inference-result/v1'})
        self.assertEqual(schema['properties']['candidate_assessments']['maxItems'], 100)
        assessment_schema = schema['$defs']['CandidateAssessment']
        self.assertEqual(assessment_schema['properties']['signal_keys']['maxItems'], 500)
        self.assertEqual(assessment_schema['properties']['evidence_refs']['maxItems'], 100)
        association = schema['$defs']['AssociationGraph']['properties']
        self.assertEqual(association['clusters']['minItems'], 1)
        self.assertEqual(association['clusters']['maxItems'], 20)
        self.assertEqual(association['clusters']['items']['properties']['members']['maxItems'], 100)
        self.assertEqual(association['links']['maxItems'], 40)
        pending = [schema]
        while pending:
            node = pending.pop()
            if type(node) is dict:
                if node.get('type') == 'object':
                    self.assertIs(node['additionalProperties'], False)
                    self.assertEqual(set(node['required']), set(node['properties']))
                pending.extend(node.values())
            elif type(node) is list:
                pending.extend(node)
        json.dumps(schema, allow_nan=False)

    def test_schema_and_results_do_not_share_mutable_input_or_schema_state(self):
        first, second = output_schema(), output_schema()
        first['$defs']['CandidateAssessment']['properties']['candidate_key']['maxLength'] = 1
        self.assertEqual(second['$defs']['CandidateAssessment']['properties']['candidate_key']['maxLength'], 300)
        self.assertEqual(first['$defs']['CandidateAssessment']['properties']['signal_keys']['items']['maxLength'], 300)
        candidate_copy = deepcopy(self.candidates)
        normalized = self.validate(result(rows=[assessment(refs=['evidence:a'])]))
        normalized['candidate_assessments'][0]['evidence_refs'].append(CANARY)
        self.assertEqual(self.candidates, candidate_copy)
        self.assertEqual(self.signals, [])
