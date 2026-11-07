"""Portable result validation only: no inference, transport or authority capture.

Submitted views are lists of {key, medium} signals and {key, evidence_refs}
candidates. They must come from an admitted input, not model-created context.
A validated assessment is a hypothesis, not a grounded selection or preference.
"""
from django.views.decorators.debug import sensitive_variables

from shelf.acquisition_contract import Blocked
from shelf.disclosure import DisclosureError, canonical_bytes, read_json


VERSION = 'cabinet-inference-result/v1'
MAX_RESULT_BYTES = 128 * 1024
MAX_SIGNALS = 500
MAX_CANDIDATES = 100


class ContractError(ValueError):
    """Fixed codes only; provider content and private input never become errors."""
    def __init__(self, code='CONTRACT_INVALID'):
        self.code = code if code in ('CONTRACT_INVALID', 'LIMIT_EXCEEDED') else 'CONTRACT_INVALID'
        super().__init__(self.code)


def _require(condition, code='CONTRACT_INVALID'):
    if not condition:
        raise ContractError(code)


@sensitive_variables()
def _key(value, maximum=300):
    _require(type(value) is str and 1 <= len(value) <= maximum)
    return value


@sensitive_variables()
def _references(values, maximum):
    _require(type(values) is list)
    _require(len(values) <= maximum, 'LIMIT_EXCEEDED')
    for value in values:
        _key(value)
    _require(len(set(values)) == len(values))
    return set(values)


@sensitive_variables()
def _submitted(signals, candidates):
    _require(type(signals) is list and type(candidates) is list)
    _require(len(signals) <= MAX_SIGNALS and len(candidates) <= MAX_CANDIDATES, 'LIMIT_EXCEEDED')
    try:
        # Reuse the existing 256 KiB/depth/Unicode/JSON input guard as well.
        canonical_bytes([signals, candidates])
    except DisclosureError as error:
        raise ContractError(error.code) from None
    signal_keys, candidate_refs = set(), {}
    for signal in signals:
        _require(type(signal) is dict and set(signal) == {'key', 'medium'})
        key = _key(signal['key'])
        _key(signal['medium'], 40)
        _require(key not in signal_keys)
        signal_keys.add(key)
    for candidate in candidates:
        _require(type(candidate) is dict and set(candidate) == {'key', 'evidence_refs'})
        key = _key(candidate['key'])
        _require(key not in candidate_refs)
        # This is an input allowlist, not the assessment's 100-citation limit.
        # The existing bounded JSON encoder already caps any array at 2000.
        candidate_refs[key] = _references(candidate['evidence_refs'], 2000)
    return signal_keys, candidate_refs


@sensitive_variables()
def validate_result(raw, *, signals, candidates):
    """Decode bounded UTF8 JSON and bind every citation to the submitted views.

    A partial or empty assessment list is valid; unassessed candidates gain no
    recommendation status. Evidence refs must belong to the assessed candidate.
    Legacy association cardinality and cross-media rules are delegated unchanged.
    """
    _require(type(raw) is bytes)
    _require(len(raw) <= MAX_RESULT_BYTES, 'LIMIT_EXCEEDED')
    signal_keys, candidate_refs = _submitted(signals, candidates)
    try:
        value = read_json(raw)
    except DisclosureError as error:
        raise ContractError(error.code) from None
    _require(type(value) is dict and set(value) == {'schema', 'associations', 'candidate_assessments'})
    _require(value['schema'] == VERSION)
    if not signals:
        _require(value['associations'] is None)
    else:
        _require(value['associations'] is not None)
        from shelf.synthesis import validate_output
        try:
            value['associations'] = validate_output(value['associations'], signals)
        except Blocked:
            raise ContractError() from None
    assessments = value['candidate_assessments']
    _require(type(assessments) is list)
    _require(len(assessments) <= MAX_CANDIDATES, 'LIMIT_EXCEEDED')
    seen = set()
    for assessment in assessments:
        _require(type(assessment) is dict and set(assessment) == {
            'candidate_key', 'decision', 'signal_keys', 'evidence_refs', 'hypothesis'})
        key = _key(assessment['candidate_key'])
        _require(key in candidate_refs and key not in seen)
        seen.add(key)
        _require(assessment['decision'] in ('retain', 'reject', 'uncertain'))
        cited_signals = _references(assessment['signal_keys'], MAX_SIGNALS)
        cited_evidence = _references(assessment['evidence_refs'], 100)
        _require(cited_signals <= signal_keys and cited_evidence <= candidate_refs[key])
        _require(type(assessment['hypothesis']) is str and len(assessment['hypothesis']) <= 1200)
    return value


def output_schema():
    """Fresh closed portable JSON schema; contextual ID rules remain runtime checks."""
    key = {'type': 'string', 'minLength': 1, 'maxLength': 300}
    explanation = {'type': 'string', 'minLength': 1, 'maxLength': 1200}
    cluster = {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'label': {'type': 'string', 'minLength': 1, 'maxLength': 120},
            'members': {'type': 'array', 'minItems': 1, 'maxItems': 100,
                        'uniqueItems': True, 'items': dict(key)},
            'explanation': dict(explanation),
        },
        'required': ['label', 'members', 'explanation'],
    }
    link = {
        'type': 'object', 'additionalProperties': False,
        'properties': {'from': dict(key), 'to': dict(key), 'explanation': dict(explanation)},
        'required': ['from', 'to', 'explanation'],
    }
    graph = {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'basis': {'const': 'inferred_taste_association'},
            'clusters': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': cluster},
            'links': {'type': 'array', 'maxItems': 40, 'items': link},
        },
        'required': ['basis', 'clusters', 'links'],
    }
    assessment = {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'candidate_key': dict(key),
            'decision': {'enum': ['retain', 'reject', 'uncertain']},
            'signal_keys': {'type': 'array', 'maxItems': MAX_SIGNALS, 'uniqueItems': True, 'items': dict(key)},
            'evidence_refs': {'type': 'array', 'maxItems': 100, 'uniqueItems': True, 'items': dict(key)},
            'hypothesis': {'type': 'string', 'maxLength': 1200},
        },
        'required': ['candidate_key', 'decision', 'signal_keys', 'evidence_refs', 'hypothesis'],
    }
    return {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'schema': {'const': VERSION},
            'associations': {'anyOf': [{'$ref': '#/$defs/AssociationGraph'}, {'type': 'null'}]},
            'candidate_assessments': {'type': 'array', 'maxItems': MAX_CANDIDATES,
                                      'items': {'$ref': '#/$defs/CandidateAssessment'}},
        },
        'required': ['schema', 'associations', 'candidate_assessments'],
        '$defs': {'AssociationGraph': graph, 'CandidateAssessment': assessment},
    }
