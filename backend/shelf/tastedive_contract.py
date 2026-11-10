"""Legacy TasteDive public descriptor and invented-response contract only.

No transport, key ingress or native registration lives here. The fixture label
is a caller assertion, not proof that arbitrary bytes are nonpersonal. Real
retention/onward-use rights and supported identity resolution remain separate.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import unicodedata
from urllib.parse import urlencode

from .disclosure import DisclosureError, canonical_bytes, read_json


TYPES = ('movie', 'show', 'book')
FIXTURE_KIND = 'synthetic_fixture_not_live'
MAX_BODY = 128 * 1024
MAX_NAME = 200


class TasteDiveContractError(ValueError):
    """Fixed codes only; rejected text/bytes are never echoed."""


def _name(value):
    if (type(value) is not str or not 1 <= len(value) <= MAX_NAME or not value.strip()
            or any(unicodedata.category(ch) in ('Cc', 'Cs', 'Zl', 'Zp') for ch in value)):
        raise TasteDiveContractError('NAME_INVALID')
    return value


def _type(value):
    if type(value) is not str or value not in TYPES:
        raise TasteDiveContractError('TYPE_INVALID')
    return value


@dataclass(frozen=True)
class TasteDiveOperation:
    seed_type: str
    seed_title: str = field(repr=False)
    result_type: str

    def __post_init__(self):
        _type(self.seed_type)
        _type(self.result_type)
        _name(self.seed_title)
        # The documented q grammar uses comma and colon as structural syntax.
        # No literal escape is documented: reject ambiguous titles in this slice
        # rather than invent an escape that could add another seed/type.
        if any(ch in self.seed_title for ch in ',:'):
            raise TasteDiveContractError('SEED_SYNTAX_AMBIGUOUS')

    @property
    def _parameters(self):
        return (('q', self.seed_type + ':' + self.seed_title), ('type', self.result_type),
                ('limit', '5'), ('slimit', '1'), ('info', '0'))

    @property
    def public_query(self):
        """Only public query bytes; deliberately no complete URL or key slot value."""
        return urlencode(self._parameters)

    @property
    def material(self):
        # k is deliberately absent. A later admitted backend must separately bind
        # exact public bytes to current protected credential generation/authority.
        return {'method': 'GET', 'origin': 'https://tastedive.com', 'path': '/api/similar',
                'parameters': [list(pair) for pair in self._parameters],
                'body_digest': sha256(b'').hexdigest(), 'authentication_slot': 'private-query-k'}

    @property
    def operation_digest(self):
        return sha256(canonical_bytes(self.material)).hexdigest()


@dataclass(frozen=True, repr=False)
class SyntheticTasteDiveReply:
    body: bytes = field(repr=False)
    operation_digest: str
    observed_at: datetime
    evidence_kind: str = FIXTURE_KIND

    def __post_init__(self):
        if type(self.body) is not bytes or len(self.body) > MAX_BODY:
            raise TasteDiveContractError('REPLY_SIZE_INVALID')
        if (type(self.operation_digest) is not str or len(self.operation_digest) != 64
                or any(ch not in '0123456789abcdef' for ch in self.operation_digest)):
            raise TasteDiveContractError('OPERATION_DIGEST_INVALID')
        if (type(self.observed_at) is not datetime or self.observed_at.tzinfo is None
                or self.observed_at.utcoffset() is None):
            raise TasteDiveContractError('OBSERVATION_CLOCK_INVALID')
        if self.evidence_kind != FIXTURE_KIND:
            raise TasteDiveContractError('SYNTHETIC_REPLY_REQUIRED')


@dataclass(frozen=True, repr=False)
class TasteDiveMatchedInput:
    name: str
    item_type: str


@dataclass(frozen=True, repr=False)
class TasteDiveSuggestion:
    name: str
    item_type: str
    provider_position: int


@dataclass(frozen=True, repr=False)
class TasteDiveBatch:
    matched_inputs: tuple[TasteDiveMatchedInput, ...]
    suggestions: tuple[TasteDiveSuggestion, ...]
    operation_digest: str
    response_digest: str
    observed_at: datetime
    evidence_kind: str = FIXTURE_KIND

    @property
    def model_eligible(self):
        return False

    @property
    def native_eligible(self):
        return False


def _rows(value, bound):
    if type(value) is not list or len(value) > bound:
        raise TasteDiveContractError('REPLY_SHAPE_INVALID')
    for row in value:
        if type(row) is not dict or set(row) != {'Name', 'Type'}:
            raise TasteDiveContractError('REPLY_SHAPE_INVALID')
        _name(row['Name'])
        _type(row['Type'])
    return value


def normalize_synthetic_tastedive(reply, operation):
    """Unresolved names only, never SourceCards, identity or taste evidence.

    Info is the provider's seed interpretation. Results order is merely returned
    position, not a quality score. Duplicate names remain distinct and ambiguous.
    """
    if type(reply) is not SyntheticTasteDiveReply or type(operation) is not TasteDiveOperation:
        raise TasteDiveContractError('SYNTHETIC_REPLY_REQUIRED')
    if reply.operation_digest != operation.operation_digest:
        raise TasteDiveContractError('OPERATION_MISMATCH')
    try:
        value = read_json(reply.body)
    except DisclosureError:
        raise TasteDiveContractError('REPLY_JSON_INVALID') from None
    if type(value) is not dict or set(value) != {'Similar'}:
        raise TasteDiveContractError('REPLY_SHAPE_INVALID')
    similar = value['Similar']
    if type(similar) is not dict or set(similar) != {'Info', 'Results'}:
        raise TasteDiveContractError('REPLY_SHAPE_INVALID')
    inputs = _rows(similar['Info'], 1)
    results = _rows(similar['Results'], 5)
    if any(row['Type'] != operation.result_type for row in results):
        raise TasteDiveContractError('RESULT_TYPE_MISMATCH')
    return TasteDiveBatch(tuple(TasteDiveMatchedInput(row['Name'], row['Type']) for row in inputs),
        tuple(TasteDiveSuggestion(row['Name'], row['Type'], position)
              for position, row in enumerate(results, 1)),
        operation.operation_digest, sha256(reply.body).hexdigest(), reply.observed_at, reply.evidence_kind)


def synthetic_tastedive_fixture(operation=None, *, count=2):
    """Invented cross-category data with a fixed clock, never fetched content."""
    if operation is None:
        operation = TasteDiveOperation('movie', 'Synthetic Lantern', 'book')
    if type(operation) is not TasteDiveOperation or type(count) is not int or not 0 <= count <= 5:
        raise TasteDiveContractError('FIXTURE_INPUT_INVALID')
    body = {'Similar': {'Info': [{'Name': operation.seed_title, 'Type': operation.seed_type}],
        'Results': [{'Name': 'Synthetic suggestion ' + str(i + 1), 'Type': operation.result_type}
                    for i in range(count)]}}
    return SyntheticTasteDiveReply(canonical_bytes(body), operation.operation_digest,
                                  datetime(2026, 1, 1, tzinfo=timezone.utc))
