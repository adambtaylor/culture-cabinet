"""Fixed Responses wire codec tested with synthetic transport only."""
import json
import unittest
from unittest.mock import patch

from shelf.disclosure import canonical_bytes
from shelf.inference_adapters import CANDIDATE, MODEL, HostedResponsesAdapter, probe_body
from shelf.inference_contract import output_schema
from shelf.inference_transport import ProbePermit, TransportFault


def result_document():
    return {'schema': 'cabinet-inference-result/v1', 'associations': None,
        'candidate_assessments': [{'candidate_key': CANDIDATE['key'], 'decision': 'uncertain',
            'signal_keys': [], 'evidence_refs': CANDIDATE['evidence_refs'], 'hypothesis': 'Technical format check.'}]}


def response_document(result=None):
    return {'status': 'completed', 'model': MODEL, 'error': None, 'incomplete_details': None,
            'output': [{'type': 'reasoning', 'summary': []}, {'type': 'message', 'role': 'assistant',
                'status': 'completed', 'content': [{'type': 'output_text', 'text': json.dumps(result or result_document())}]}]}


class WireTests(unittest.TestCase):
    def setUp(self):
        self.adapter = HostedResponsesAdapter()
        self.body = probe_body()
        import hashlib
        self.permit = ProbePermit(hashlib.sha256(self.body).hexdigest(), lambda: b'SYNTHETIC-WIRE-KEY')
        self.dns = patch('shelf.inference_transport.socket.getaddrinfo', side_effect=AssertionError('Real DNS forbidden'))
        self.connect = patch('shelf.inference_transport.socket.socket', side_effect=AssertionError('Real connect forbidden'))
        self.dns.start()
        self.connect.start()
        self.addCleanup(self.dns.stop)
        self.addCleanup(self.connect.stop)

    def generate(self, value):
        raw = canonical_bytes(value) if type(value) is dict else value
        with patch.object(self.adapter.transport, 'send', return_value=raw) as spy:
            result = self.adapter.generate(self.body, self.permit)
        spy.assert_called_once_with(self.body, self.permit)
        return result

    def invalid(self, value):
        with self.assertRaises(TransportFault) as caught:
            self.generate(value)
        self.assertEqual(caught.exception.outcome, 'incompatible')

    def test_wire_uses_selected_model_reasoning_and_no_storage_tools_or_background(self):
        body = json.loads(self.body)
        self.assertEqual(body['model'], MODEL)
        self.assertEqual(body['reasoning'], {'effort': 'high'})
        self.assertFalse(body['store'])
        self.assertFalse(body['stream'])
        self.assertFalse(body['background'])
        self.assertEqual(body['tools'], [])
        self.assertEqual(body['max_output_tokens'], 2048)
        self.assertLessEqual(len(self.body), 16384)
        self.assertTrue(body['text']['format']['strict'])

    def test_provider_schema_uses_conservative_documented_forms_without_changing_domain_schema(self):
        wire = json.loads(self.body)['text']['format']['schema']
        self.assertNotIn('uniqueItems', json.dumps(wire))
        self.assertNotIn('"const"', json.dumps(wire))
        self.assertEqual(wire['properties']['schema']['enum'], ['cabinet-inference-result/v1'])
        self.assertIn('uniqueItems', json.dumps(output_schema()))

    def test_valid_neutral_result_records_provider_reported_identity_limits(self):
        result = self.generate(response_document())
        self.assertEqual(result['observed_model'], MODEL)
        self.assertIn('not independently attested', ' '.join(result['observation_limits']))

    def test_changed_body_never_reaches_transport(self):
        with patch.object(self.adapter.transport, 'send') as spy:
            with self.assertRaises(TransportFault):
                self.adapter.generate(self.body + b' ', self.permit)
        spy.assert_not_called()

    def test_wrong_model_and_incomplete_or_error_response_cannot_pass(self):
        for changes in [{'model': 'different'}, {'status': 'incomplete'}, {'error': {'message': 'synthetic'}},
                        {'incomplete_details': {'reason': 'max_output_tokens'}}]:
            self.invalid({**response_document(), **changes})

    def test_refusal_tool_and_unexpected_output_item_are_not_executed(self):
        for item in [{'type': 'function_call', 'name': 'send_secret'},
                     {'type': 'message', 'role': 'assistant', 'status': 'completed',
                      'content': [{'type': 'refusal', 'refusal': 'synthetic refusal'}]}]:
            value = response_document()
            value['output'] = [item]
            self.invalid(value)

    def test_invented_candidate_signal_and_evidence_are_rejected(self):
        for changes in [{'candidate_key': 'invented'}, {'signal_keys': ['invented']}, {'evidence_refs': ['invented']}]:
            result = result_document()
            result['candidate_assessments'][0].update(changes)
            self.invalid(response_document(result))

    def test_missing_or_duplicate_assessment_cannot_certify_probe_contract(self):
        for rows in [[], result_document()['candidate_assessments'] * 2]:
            value = result_document()
            value['candidate_assessments'] = rows
            self.invalid(response_document(value))

    def test_multiple_text_outputs_do_not_get_concatenated_into_a_success(self):
        value = response_document()
        value['output'].append(value['output'][-1])
        self.invalid(value)

    def test_malformed_duplicate_nonfinite_and_private_errors_are_safe(self):
        for raw in [b'{', b'{"status":"completed","status":"failed"}', b'{"x":NaN}', b'\xff']:
            with self.assertRaises(TransportFault) as caught:
                self.generate(raw)
            self.assertEqual(str(caught.exception), 'incompatible')
            self.assertNotIn('SYNTHETIC-WIRE-KEY', str(caught.exception))

    def test_zero_personal_probe_cannot_return_an_invented_association_graph(self):
        value = result_document()
        value['associations'] = {'basis': 'inferred_taste_association', 'clusters': [], 'links': []}
        self.invalid(response_document(value))

    def test_wire_and_prompt_do_not_contain_credential_or_caller_profile_state(self):
        text = self.body.decode()
        for forbidden in ['SYNTHETIC-WIRE-KEY', 'Authorization', 'installation_id', 'owner_id', 'session_id', 'config_digest']:
            self.assertNotIn(forbidden, text)


class PersonalAdapterTests(unittest.TestCase):
    def test_personal_result_uses_submitted_refs_and_neutral_permit_is_refused(self):
        from contextlib import contextmanager
        from datetime import datetime, timedelta, timezone
        from shelf.inference_adapters import PersonalResponsesAdapter
        from shelf.inference_transport import personal_permit
        from shelf.tests.test_portable_cabinet_store import candidate, result
        from shelf.disclosure import read_json
        import hashlib
        submitted = {'profile': {'signals': []}, 'candidates': [candidate()]}
        body = canonical_bytes({'input': canonical_bytes(submitted).decode()})
        adapter = PersonalResponsesAdapter(); now = datetime.now(timezone.utc)
        @contextmanager
        def credential(): yield bytearray(b'SYNTHETIC')
        permit = personal_permit(hashlib.sha256(body).hexdigest(), credential, now+timedelta(seconds=30), lambda:now)
        with patch.object(adapter.transport, 'send', return_value=canonical_bytes(response_document(read_json(result())))) as send:
            self.assertEqual(adapter.dispatch('reserved', body, permit=permit), result())
            send.assert_called_once_with(body, permit)
        with patch.object(adapter.transport, 'send') as send:
            with self.assertRaises(TransportFault):
                adapter.dispatch('reserved', body, permit=ProbePermit(hashlib.sha256(body).hexdigest(), lambda:b'KEY'))
            send.assert_not_called()
