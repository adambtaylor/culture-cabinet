"""Fixed neutral capability probe; never accepts personal inference content.

The synthetic technical IDs are not sourced catalogue records, preferences or
recommendations. A successful result tests this output contract on this route;
it does not establish provider account capacity for subsequent work.
"""
from django.views.decorators.debug import sensitive_variables

from .disclosure import DisclosureError, canonical_bytes, read_json
from .inference_contract import ContractError, output_schema, validate_result
from .inference_transport import CheckedTransport, TransportFault

MODEL = 'gpt-6-astra'
REASONING = 'high'
BUILD = 'responses-neutral-v1'
OUTPUT_CONTRACT = 'cabinet-inference-result/v1'
CAPABILITY_CONTRACT = 'neutral-candidate-assessment/v1'
CANDIDATE = {'key': 'technical-probe-candidate', 'evidence_refs': ['technical-probe-evidence']}


def _wire_schema(value):
    """Use documented schema forms; local validation retains all domain rules.

    Uniqueness and text lengths remain enforced by the result validator. Their
    less certain provider keywords are omitted; constants use singleton enums.
    This translation never changes the canonical local result contract.
    """
    if type(value) is list:
        return [_wire_schema(item) for item in value]
    if type(value) is not dict:
        return value
    result = {key: _wire_schema(item) for key, item in value.items() if key not in {'uniqueItems', 'const', 'minLength', 'maxLength'}}
    if 'const' in value:
        result.update(type='string', enum=[value['const']])
    if 'enum' in value and all(type(item) is str for item in value['enum']):
        result['type'] = 'string'
    return result


def probe_body():
    body = canonical_bytes({'model': MODEL, 'reasoning': {'effort': REASONING},
        'store': False, 'stream': False, 'background': False, 'tools': [],
        'max_output_tokens': 2048,
        'instructions': 'Technical output-contract check only. Return the strict schema, associations null, one uncertain assessment for technical-probe-candidate citing technical-probe-evidence, signal_keys empty. No tools or personal data.',
        'input': 'There are zero personal signals. This is an artificial format check, not a recommendation.',
        'text': {'format': {'type': 'json_schema', 'name': 'cabinet_inference_result',
                            'strict': True, 'schema': _wire_schema(output_schema())}}})
    if len(body) > 16384:
        raise TransportFault('incompatible')
    return body


class HostedResponsesAdapter:
    def __init__(self):
        self.transport = CheckedTransport()

    @sensitive_variables()
    def generate(self, body, permit):
        if body != probe_body():
            raise TransportFault('stale')
        raw = self.transport.send(body, permit)
        try:
            value = read_json(raw)
            if (type(value) is not dict or value.get('status') != 'completed'
                    or value.get('error') is not None or value.get('incomplete_details') is not None
                    or value.get('model') != MODEL or type(value.get('output')) is not list
                    or len(value['output']) > 10):
                raise TransportFault('incompatible')
            texts = []
            for item in value['output']:
                if type(item) is not dict:
                    raise TransportFault('incompatible')
                if item.get('type') == 'reasoning':
                    continue
                if (item.get('type') != 'message' or item.get('role') != 'assistant'
                        or item.get('status') != 'completed' or type(item.get('content')) is not list):
                    raise TransportFault('incompatible')
                for content in item['content']:
                    if type(content) is not dict or content.get('type') != 'output_text' or type(content.get('text')) is not str:
                        raise TransportFault('incompatible')
                    texts.append(content['text'])
            if len(texts) != 1:
                raise TransportFault('incompatible')
            result = validate_result(texts[0].encode('utf-8'), signals=[], candidates=[CANDIDATE])
            rows = result['candidate_assessments']
            if (len(rows) != 1 or rows[0]['candidate_key'] != CANDIDATE['key']
                    or rows[0]['decision'] != 'uncertain' or rows[0]['signal_keys']
                    or rows[0]['evidence_refs'] != CANDIDATE['evidence_refs']):
                raise TransportFault('incompatible')
            return {'observed_model': value['model'], 'observation_limits': [
                'Only a zero-personal-signal technical result was generated; no preference quality tested.',
                'Model identifier is provider-reported, not independently attested.']}
        except (DisclosureError, ContractError, UnicodeError, TypeError, ValueError):
            raise TransportFault('incompatible') from None
