"""REQ-010 field evidence, with independent server policy and exact ancestry.

    Source content supplies facts, never authority. Neither a response hash nor
    constructing these records grants permission to use a field externally.
"""
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from .disclosure import SCALARS, canonical_bytes, read_json
from .enhance_contract import digest, require, text, utc


USES = frozenset(('fetch', 'display', 'model_context', 'cache', 'retain', 'export', 'redistribute'))

# These are returned facts, not parser guesses about status, tickets or prices.
MOSHTIX_FACTS = frozenset(('event.id', 'event.startDate', 'venue.id', 'venue.name',
                         'venue.address.locality', 'venue.address.region'))
MOSHTIX_POLICY_REF = 'moshtix-factual-use/2026-11-06'


@dataclass(frozen=True)
class FieldPolicy:
    ref: str
    revision: int
    checked_at: datetime
    expires_at: datetime
    permissions: tuple[tuple[str, tuple[str, ...]], ...]

    def __post_init__(self):
        text(self.ref)
        require(type(self.revision) is int and 0 <= self.revision < 2**53)
        require(utc(self.checked_at) < utc(self.expires_at))
        require(type(self.permissions) is tuple)
        names = []
        for target, uses in self.permissions:
            text(target)
            require(type(uses) is tuple and len(set(uses)) == len(uses) and set(uses) <= USES)
            names.append(target)
        require(len(set(names)) == len(names))

    def allows(self, target, use):
        # Missing and unknown rights deny. A fetch grant is deliberately unable
        # to substitute for model, display, retention or redistribution rights.
        return use in dict(self.permissions).get(target, ())

    def current(self, now):
        return self.checked_at <= utc(now) < self.expires_at


@dataclass(frozen=True, repr=False)
class SourceCard:
    key: str
    title: str
    medium: str
    provider: str
    source_id: str
    source_url: str
    locator: str
    facts: tuple[tuple[str, str | int | bool], ...] = ()

    def __post_init__(self):
        for name in ('key', 'title', 'medium', 'provider', 'source_id', 'source_url', 'locator'):
            text(getattr(self, name))
        try:
            link = urlsplit(self.source_url)
            safe = (link.scheme == 'https' and link.hostname and link.username is None
                    and link.password is None and link.port in (None, 443)
                    and '\\' not in self.source_url)
        except ValueError:
            safe = False
        require(safe)
        require(type(self.facts) is tuple and len(self.facts) <= 40)
        names = []
        for prop, value in self.facts:
            text(prop)
            require(type(value) in (str, int, bool))
            if type(value) is str:
                require(len(value) <= 1200)
            names.append(prop)
        require(len(set(names)) == len(names))


@dataclass(frozen=True, repr=False)
class EvidenceBatch:
    material: bytes = field(repr=False)
    expires_at: datetime
    cacheable: bool


def validate_moshtix_policy(policy, *, permit, now, model_context_current):
    """Check a current issuer policy; never turn a grant into source rights.

    The dated conditional disposition permits only necessary factual processing.
    Its external processor conditions require fresh issuer evidence independently
    of source consent. An absent model condition cannot become an allowed binding.
    """
    require(type(policy) is FieldPolicy and policy.current(now), 'AUTHORITY_DENIED')
    require(policy.ref == permit.policy_ref == MOSHTIX_POLICY_REF
            and policy.revision == permit.policy_revision, 'PREVIEW_STALE')
    require(type(model_context_current) is bool and policy.allows('*', 'fetch'), 'AUTHORITY_DENIED')
    for target, uses in policy.permissions:
        require(target in SCALARS | MOSHTIX_FACTS | {'*'}
                and set(uses) <= {'fetch', 'display', 'cache', 'retain', 'model_context'},
                'AUTHORITY_DENIED')
        require(target != '*' or uses == ('fetch',), 'AUTHORITY_DENIED')
        require('model_context' not in uses or model_context_current, 'AUTHORITY_DENIED')
    require(all(policy.allows(target, use) for target in SCALARS
                for use in ('fetch', 'display', 'retain')), 'AUTHORITY_DENIED')


def build_evidence(cards, *, category, observation_ref, source_revision, response_digest,
                   observed_at, expires_at, parents, policy, now, fixture=False):
    """Project bounded facts into the existing sole disclosure codec's shape.

    Record parents cover membership, query and ordering dependencies, not just
    displayed prose. Dropping a denied note fact cannot launder a note-derived
    candidate pool into an apparently public independent recommendation.
    """
    require(type(cards) is tuple and all(type(card) is SourceCard for card in cards))
    require(type(policy) is FieldPolicy and policy.current(now), 'AUTHORITY_DENIED')
    require(policy.allows('*', 'fetch'), 'AUTHORITY_DENIED')
    require(type(source_revision) is int and 0 <= source_revision < 2**53)
    text(category, 80)
    text(observation_ref)
    require(type(response_digest) is str and len(response_digest) == 64
            and all(c in '0123456789abcdef' for c in response_digest))
    require(type(parents) is tuple and len(parents) <= 500 and len(set(parents)) == len(parents))
    for parent in parents:
        text(parent)
    end = min(utc(expires_at), policy.expires_at)
    if any(card.provider == 'moshtix' for card in cards):
        from datetime import timedelta
        require(all(card.provider == 'moshtix' for card in cards))
        # Re-parsing and cache reuse cannot renew the original source clock.
        end = min(end, utc(observed_at) + timedelta(minutes=15))
        require(all(set(name for name, _ in card.facts) <= MOSHTIX_FACTS for card in cards))
        require(fixture or policy.ref == MOSHTIX_POLICY_REF, 'AUTHORITY_DENIED')
        require(not any(set(uses) & {'export', 'redistribute'} for _, uses in policy.permissions),
                'AUTHORITY_DENIED')
    require(utc(observed_at) <= utc(now) < end, 'PREVIEW_STALE')
    records, seen, cacheable = [], set(), True
    observations = {observation_ref: {'revision': source_revision, 'digest': response_digest}}
    policies = {policy.ref: {'revision': policy.revision}}
    for card in cards:
        require(card.key not in seen)
        seen.add(card.key)
        # Collection needs local fetch/display/retention rights. External model
        # permission remains a separate per-binding gate in the sole serializer;
        # it cannot erase useful, locally authorized collection progress.
        if not all(policy.allows(target, use) for target in SCALARS
                   for use in ('fetch', 'display', 'retain')):
            continue
        record_ref = 'source:' + digest(canonical_bytes([observation_ref, category, card.key]))
        candidate = {'key': card.key, 'category': category, 'title': card.title,
            'medium': card.medium, 'source_url': card.source_url,
            'provider': card.provider, 'source_id': card.source_id,
            'identity_status': 'fixture_identity_only' if fixture else 'confirmed',
            'evidence_type': 'fixture_not_live' if fixture else 'live_lookup',
            'observed_at': observed_at.isoformat().replace('+00:00', 'Z'),
            'valid_until': end.isoformat().replace('+00:00', 'Z'),
            'rights_ref': record_ref, 'source_roots': [observation_ref], 'facts': []}
        fields = []

        def bind(target, value, fact_id=None, permission_target=None):
            nonlocal cacheable
            locator = card.locator + ':' + (fact_id or target)
            if card.provider == 'moshtix':
                source_field = permission_target or {'title': 'name', 'source_id': 'id'}.get(target)
                locator = (card.locator + '.' + source_field.removeprefix('event.') if source_field
                           else card.locator + ':derived:' + target)
            binding = {'target': target, 'fact_id': fact_id,
                'classification': 'personal_or_derived' if parents else 'public_source',
                'direct_parent_ids': list(parents),
                'observation': {'ref': observation_ref, 'revision': source_revision,
                    'digest': response_digest, 'locator': locator,
                    'observed_at': candidate['observed_at'], 'expires_at': candidate['valid_until']}}
            material = {**binding, 'value': value}
            binding['rights'] = {'policy_ref': policy.ref, 'revision': policy.revision,
                # In this existing wire shape disposition describes external
                # model use. Unknown includes absent/denied model permission;
                # local collection never turns it into an implicit allowance.
                'disposition': ('allowed' if policy.allows(permission_target or target,
                                                          'model_context') else 'unknown'),
                'checked_at': policy.checked_at.isoformat().replace('+00:00', 'Z'),
                'expires_at': (end if card.provider == 'moshtix' else policy.expires_at).isoformat().replace('+00:00', 'Z'),
                'binding_digest': digest(canonical_bytes(material))}
            fields.append(binding)
            cacheable = cacheable and policy.allows(permission_target or target, 'cache')

        for target in sorted(SCALARS):
            bind(target, candidate[target])
        for prop, value in card.facts:
            if all(policy.allows(prop, use) for use in ('fetch', 'display', 'retain')):
                fact_id = 'fact:' + digest(canonical_bytes([card.key, prop]))
                fact = {'id': fact_id, 'property': prop, 'value': value,
                        'observation_ref': observation_ref}
                candidate['facts'].append(fact)
                bind('fact', fact, fact_id, permission_target=prop)
        records.append({'schema': 'candidate-source-rights/v1', 'record_ref': record_ref,
                        'candidate': candidate, 'direct_parent_ids': list(parents), 'fields': fields})
    return EvidenceBatch(canonical_bytes({'records': records, 'observations': observations,
                                         'policies': policies}), end, cacheable)


def cached_evidence(batch):
    """Retag reuse without moving original observation, rights or expiry clocks."""
    require(type(batch) is EvidenceBatch)
    material = read_json(batch.material)
    for record in material['records']:
        candidate = record['candidate']
        if candidate['evidence_type'] == 'fixture_not_live':
            continue
        candidate['evidence_type'] = 'cached_lookup'
        for binding in record['fields']:
            if binding['target'] == 'evidence_type':
                bound = {name: binding[name] for name in ('target', 'fact_id',
                    'classification', 'direct_parent_ids', 'observation')}
                binding['rights']['binding_digest'] = digest(canonical_bytes({**bound,
                    'value': 'cached_lookup'}))
    return EvidenceBatch(canonical_bytes(material), batch.expires_at, batch.cacheable)
