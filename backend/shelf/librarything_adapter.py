"""Synthetic-only LibraryThing producer seam; no registration or live transport.

The accepted diagnostic discards actual response content. Its restrictions are
not relaxed here: callers supply invented fixture bytes, never fetched content.
Work identifiers/co-occurrence ranks are not book titles, verified identities,
taste evidence, SourceCards or model permissions.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json

from .disclosure import canonical_bytes
from .librarything_smoke import _response, MAX_BODY

SOURCE = 'librarything'
OPERATION = 'multirecommendations_v2_diagnostic'
ORIGIN = 'https://www.librarything.com'
PATH = '/api/multirecommendations.php'
DOCUMENTATION_ISBN = '0300178727'
FIXTURE_KIND = 'synthetic_fixture_not_live'


class LibraryThingAdapterError(ValueError):
    """Fixed nonsecret codes only; never echo response text or exception values."""


@dataclass(frozen=True)
class LibraryThingOperation:
    """Closed public operation descriptor, not an executable URL or authority."""
    @property
    def material(self):
        return {'source': SOURCE, 'operation': OPERATION, 'method': 'GET',
                'origin': ORIGIN, 'path': PATH,
                'parameters': [['v', '2'], ['isbns', DOCUMENTATION_ISBN], ['maxItems', '5'],
                               ['maxPerAuthor', '2'], ['showtitles', 'false']]}

    @property
    def operation_digest(self):
        return sha256(canonical_bytes(self.material)).hexdigest()


@dataclass(frozen=True)
class LibraryThingUseDecision:
    use: str
    allowed: bool
    code: str


def librarything_use_decision(use):
    """Describe this slice's exact use boundary without minting provider rights.

    A future native adapter needs its own admitted operation, current custody,
    issuer/consent and use-specific evidence. A boolean or SSH report cannot
    supply those; AI uses retain the documented written-permission restriction.
    """
    if type(use) is not str:
        raise LibraryThingAdapterError('UNSUPPORTED_USE')
    if use == 'synthetic_contract':
        return LibraryThingUseDecision(use, True, 'FIXTURE_CONTRACT_ONLY')
    if use in ('model_context', 'prompt', 'embedding', 'grounding'):
        return LibraryThingUseDecision(use, False, 'LIBRARYTHING_AI_WRITTEN_PERMISSION_REQUIRED')
    if use == 'diagnostic':
        return LibraryThingUseDecision(use, False, 'ACTUAL_DIAGNOSTIC_CUSTODY_AND_EXECUTION_UNPROVED')
    if use in ('native_collection', 'fetch', 'display', 'retain', 'cache', 'export', 'redistribute'):
        return LibraryThingUseDecision(use, False, 'NATIVE_OPERATION_RIGHTS_AND_BINDING_UNAVAILABLE')
    raise LibraryThingAdapterError('UNSUPPORTED_USE')


@dataclass(frozen=True, repr=False)
class SyntheticLibraryThingReply:
    body: bytes = field(repr=False)
    operation_digest: str
    observed_at: datetime
    evidence_kind: str = FIXTURE_KIND

    def __post_init__(self):
        if (type(self.body) is not bytes or len(self.body) > MAX_BODY
                or self.evidence_kind != FIXTURE_KIND):
            raise LibraryThingAdapterError('SYNTHETIC_REPLY_REQUIRED')
        if (type(self.observed_at) is not datetime or self.observed_at.tzinfo is None
                or self.observed_at.utcoffset() != timezone.utc.utcoffset(self.observed_at)):
            raise LibraryThingAdapterError('FIXTURE_CLOCK_INVALID')


@dataclass(frozen=True, repr=False)
class LibraryThingSuggestion:
    work_id: str
    reported_rank: int
    from_work_ids: tuple[str, ...]
    reported_isbns: tuple[str, ...]
    reported_upcs: tuple[str, ...]


@dataclass(frozen=True, repr=False)
class LibraryThingNormalizedReply:
    status: str
    reason: str
    suggestions: tuple[LibraryThingSuggestion, ...]
    operation_digest: str
    response_digest: str
    observed_at: datetime
    evidence_kind: str = FIXTURE_KIND

    @property
    def model_eligible(self):
        return False


def _work_id(value):
    # The accepted shape validator precedes this conversion; canonical strings
    # avoid merging integer and zero-padded representations as different works.
    number = int(value)
    if number <= 0:
        raise LibraryThingAdapterError('WORK_ID_INVALID')
    return str(number)


def normalize_synthetic_librarything(reply):
    """Normalize explicit invented bytes only; output has no raw-content field.

    The fixture label is a caller's test-data assertion, not cryptographic source
    provenance. No real reply ingress is exposed, and this output is deliberately
    not a registered SourceReply/CollectionSeal or a live diagnostic receipt.
    """
    if type(reply) is not SyntheticLibraryThingReply or reply.evidence_kind != FIXTURE_KIND:
        raise LibraryThingAdapterError('SYNTHETIC_REPLY_REQUIRED')
    operation = LibraryThingOperation()
    if reply.operation_digest != operation.operation_digest:
        raise LibraryThingAdapterError('OPERATION_MISMATCH')
    # Reuse the accepted duplicate-key/type/size/error-envelope validator. The
    # fixed trailer is synthetic test context, never an actual HTTP observation.
    status, _, count, reason, _ = _response(0, reply.body + b'\n200\napplication/json')
    suggestions = []
    if status == 'ok':
        value = json.loads(reply.body)
        if value['request'][0].get('isbn') != DOCUMENTATION_ISBN:
            raise LibraryThingAdapterError('REQUEST_PROVENANCE_MISMATCH')
        seen = set()
        for item in value['recommendations']:
            work_id = _work_id(item['work'])
            if work_id in seen:
                raise LibraryThingAdapterError('DUPLICATE_WORK')
            seen.add(work_id)
            suggestions.append(LibraryThingSuggestion(work_id, item['rank'],
                tuple(sorted({_work_id(x) for x in item['fromworks']})),
                tuple(item['isbns'] or ()), tuple(item['upcs'] or ())))
        suggestions.sort(key=lambda row: row.work_id)
        if len(suggestions) != count:
            raise LibraryThingAdapterError('SHAPE_INVALID')
    return LibraryThingNormalizedReply(status, reason, tuple(suggestions),
        operation.operation_digest, sha256(reply.body).hexdigest(), reply.observed_at)


def synthetic_librarything_fixture(count=2):
    """Owned invented recommendations; documentation seed is not an owner taste."""
    if type(count) is not int or not 0 <= count <= 5:
        raise LibraryThingAdapterError('FIXTURE_COUNT_INVALID')
    payload = {'request': [{'isbn': DOCUMENTATION_ISBN}], 'recommendations': [
        {'rank': i + 1, 'work': 700 + i, 'fromworks': [999],
         'isbns': ['SYNTHETIC-ISBN-' + str(i)], 'upcs': []} for i in range(count)]}
    return SyntheticLibraryThingReply(canonical_bytes(payload), LibraryThingOperation().operation_digest,
        datetime(2026, 1, 1, tzinfo=timezone.utc))
