"""Preparatory D3 projection over caller-supplied, trusted synthetic views.

This module captures no authority, observations or rights. Backend producers must
supply their current scoped versions. Local provenance stays separate until the
permitted portable_input projection maps it to body-local aliases. Only the
inference_body codec wraps that projection; neither function grants a send.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import unicodedata
from uuid import UUID


MAX_BYTES = 262144
MAX_REVISION = 2**53 - 1
REACTIONS = {'love_it', 'intrigued', 'mildly_interested', 'sounds_boring', 'hate_it'}
LEGACY = {'enjoyed', 'interested', 'not_for_me', 'unknown', 'skipped'}
CLASSES = {'canonical_seed', 'explicit_favourites', 'reaction', 'anticipated_reactions',
           'experienced_enjoyment', 'legacy_choices', 'raw_notes', 'inferred_traits',
           'free_text_entry', 'unresolved_personal_text', 'exploration_requests',
           'coarse_area', 'coarse_location', 'exact_location', 'names', 'sensitive'}
SCALARS = {'key', 'category', 'title', 'medium', 'source_url', 'provider', 'source_id',
           'identity_status', 'evidence_type', 'observed_at', 'valid_until'}
CANDIDATE_FIELDS = SCALARS | {'rights_ref', 'source_roots', 'facts'}
NODE_META = {'author_ref', 'applicable_subjects', 'origin', 'sensitivity',
             'purposes', 'direct_parent_ids'}
EVIDENCE_FIELDS = {'id', 'subject_ref', 'key', 'title', 'medium', 'basis', 'value',
                   'data_classes', 'source_ref', 'identity_status'} | NODE_META
TRAIT_FIELDS = {'id', 'subject_ref', 'text', 'evidence_ids', 'lineage_complete'} | NODE_META


class DisclosureError(ValueError):
    """Only fixed codes escape validation; private input is never echoed."""
    def __init__(self, code='CONTRACT_INVALID'):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Preparation:
    ok: bool
    code: str
    profile_bytes: bytes | None = field(repr=False)
    candidate_projection: tuple = field(repr=False)
    omitted_classes: tuple[str, ...]
    _source_slots: tuple = field(default=(), repr=False, compare=False)


def _require(condition, code='CONTRACT_INVALID'):
    if not condition:
        raise DisclosureError(code)


def _json(value, depth=0):
    _require(depth <= 16, 'LIMIT_EXCEEDED')
    if type(value) is dict:
        _require(len(value) <= 2000, 'LIMIT_EXCEEDED')
        for key, item in value.items():
            _require(type(key) is str)
            _json(key, depth + 1)
            _json(item, depth + 1)
    elif type(value) is list:
        _require(len(value) <= 2000, 'LIMIT_EXCEEDED')
        for item in value:
            _json(item, depth + 1)
    elif type(value) is str:
        _require(len(value) <= MAX_BYTES, 'LIMIT_EXCEEDED')
        _require(not any(0xD800 <= ord(c) <= 0xDFFF for c in value))
    else:
        _require(value is None or type(value) in (bool, int, float))
        _require(type(value) is not float or math.isfinite(value))


def canonical_bytes(value):
    """D7 canonical JSON only; encoding does not confer disclosure permission."""
    _json(value)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DisclosureError() from None
    _require(len(encoded) <= MAX_BYTES, 'LIMIT_EXCEEDED')
    return encoded


def read_json(raw):
    """Bounded preparatory record parsing, including duplicate object keys."""
    _require(type(raw) is bytes)
    _require(len(raw) <= MAX_BYTES, 'LIMIT_EXCEEDED')
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result
    def constant(_):
        raise DisclosureError()
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise DisclosureError() from None
    canonical_bytes(result)
    return result


def _record(value, keys, schema=None):
    _require(type(value) is dict and set(value) == set(keys))
    if schema:
        _require(value['schema'] == schema)


def _text(value, limit=300, empty=False):
    _require(type(value) is str and len(value) <= limit and (empty or bool(value)))
    return value


def _refs(values, limit=500):
    _require(type(values) is list)
    _require(len(values) <= limit, 'LIMIT_EXCEEDED')
    for value in values:
        _text(value)
    _require(len(set(values)) == len(values))
    return values


def _uuid(value):
    _text(value)
    try:
        _require(str(UUID(value)) == value)
    except ValueError:
        raise DisclosureError() from None


def _revision(value):
    _require(type(value) is int and 0 <= value <= MAX_REVISION)


def _sha(value):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None)


def _time(value):
    _text(value, 40)
    _require(re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z', value) is not None)
    try:
        return datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError:
        raise DisclosureError() from None


def _note(value):
    _text(value, 500, empty=True)
    _require(len(value.encode('utf-8')) <= 2000, 'LIMIT_EXCEEDED')
    _require(not any(unicodedata.category(c).startswith('C') and c not in '\n\t'
                     and not (c == '\r' and i + 1 < len(value) and value[i + 1] == '\n')
                     for i, c in enumerate(value)))


def _context(snapshot, context, now):
    _record(context, {'schema', 'installation_id', 'actor_ref', 'profile_ref', 'applicable_subjects',
        'purpose', 'adapter_id', 'adapter_version', 'destination_origin', 'config_revision',
        'policy_epoch', 'grant_revisions', 'session_expires_at', 'isolated_fixture'}, 'disclosure-context/v1')
    _require(type(now) is datetime and now.tzinfo is not None and now.utcoffset() == timezone.utc.utcoffset(now))
    _require(context['purpose'] in ('inference', 'catalogue_lookup'))
    _require(type(context['isolated_fixture']) is bool)
    for name in ('installation_id', 'actor_ref', 'profile_ref'):
        _uuid(context[name])
        _require(snapshot[name] == context[name], 'AUTHORITY_DENIED')
    for name in ('adapter_id', 'adapter_version', 'destination_origin'):
        _text(context[name])
    for name in ('config_revision', 'policy_epoch'):
        _revision(context[name])
    _require(snapshot['policy_epoch'] == context['policy_epoch'], 'PREVIEW_STALE')
    subjects = _refs(context['applicable_subjects'])
    for subject in subjects:
        _uuid(subject)
    _require(bool(subjects) and now < _time(context['session_expires_at']), 'AUTHORITY_DENIED')
    _require(type(context['grant_revisions']) is dict)
    for key, revision in context['grant_revisions'].items():
        _uuid(key)
        _revision(revision)


def _grant_permissions(grants, context, now):
    _require(type(grants) is list and len(grants) <= 500, 'LIMIT_EXCEEDED')
    permissions, seen = set(), set()
    for grant in grants:
        _record(grant, {'schema', 'grant_id', 'granting_subject', 'applicable_subjects', 'installation_id',
            'actor_ref', 'profile_ref', 'purpose', 'adapter_id', 'adapter_version', 'destination_origin',
            'config_revision', 'allowed_data_classes', 'grant_revision', 'policy_epoch', 'issued_at',
            'expires_at', 'state'}, 'grant-authority/v1')
        for name in ('grant_id', 'granting_subject', 'installation_id', 'actor_ref', 'profile_ref'):
            _uuid(grant[name])
        _require(grant['grant_id'] not in seen)
        seen.add(grant['grant_id'])
        for name in ('installation_id', 'actor_ref', 'profile_ref', 'purpose', 'adapter_id',
                     'adapter_version', 'destination_origin'):
            _require(grant[name] == context[name], 'AUTHORITY_DENIED')
        for name in ('config_revision', 'policy_epoch', 'grant_revision'):
            _revision(grant[name])
        _require(grant['config_revision'] == context['config_revision']
                 and grant['policy_epoch'] == context['policy_epoch']
                 and context['grant_revisions'].get(grant['grant_id']) == grant['grant_revision'], 'PREVIEW_STALE')
        subjects = _refs(grant['applicable_subjects'])
        _require(set(subjects) <= set(context['applicable_subjects']), 'AUTHORITY_DENIED')
        _require(grant['granting_subject'] in context['applicable_subjects'], 'AUTHORITY_DENIED')
        classes = _refs(grant['allowed_data_classes'], len(CLASSES))
        _require(set(classes) <= CLASSES and grant['state'] in ('active', 'revoked'))
        issued, expires = _time(grant['issued_at']), _time(grant['expires_at'])
        _require(issued < expires <= _time(context['session_expires_at']))
        if grant['state'] == 'active' and issued <= now < expires:
            # The issuer grants only their OWN contribution, even in a Family view.
            if grant['granting_subject'] in subjects:
                permissions.update((grant['granting_subject'], value) for value in classes)
    return permissions


def _nodes(snapshot, context):
    version2 = snapshot.get('schema') == 'profile-snapshot/v2'
    fields = {'schema', 'installation_id', 'actor_ref', 'profile_ref', 'profile_kind',
        'applicability_ref', 'profile_revision_digest', 'source_revisions', 'policy_epoch',
        'restriction_watermark', 'evidence', 'derived_traits', 'hard_exclusions', 'seen_keys',
        'category_registry_revision', 'hard_constraints'}
    _record(snapshot, fields | ({'excluded_history'} if version2 else set()),
            'profile-snapshot/v2' if version2 else 'profile-snapshot/v1')
    if version2:
        excluded = snapshot['excluded_history']
        _require(type(excluded) is list and len(excluded) <= 500)
        for item in excluded:
            _record(item, {'reason', 'count', 'blocks_use'})
            _require(item['reason'] in ('unattributed_feedback', 'unsupported_history',
                'unsupported_derived_history', 'unattributed_discovery', 'foreign_history'))
            _revision(item['count'])
            _require(type(item['blocks_use']) is bool)
        # Unsafe unknown constraints cannot be treated as a no-history cold start.
        # Exclusion details remain local; denied/private counts never seed bytes.
        _require(not any(item['blocks_use'] for item in excluded), 'CAPABILITY_UNAVAILABLE')
        _require(not excluded or bool(snapshot['evidence']), 'CAPABILITY_UNAVAILABLE')
    _require(snapshot['profile_kind'] in ('owner', 'partner', 'family'))
    _sha(snapshot['profile_revision_digest'])
    _text(snapshot['applicability_ref'])
    _text(snapshot['restriction_watermark'])
    _require(type(snapshot['source_revisions']) is dict and bool(snapshot['source_revisions']))
    _revision(snapshot['policy_epoch'])
    _revision(snapshot['category_registry_revision'])
    _refs(snapshot['hard_exclusions'])
    _refs(snapshot['seen_keys'], 2000)
    _require(type(snapshot['hard_constraints']) is list and len(snapshot['hard_constraints']) <= 100)
    for constraint in snapshot['hard_constraints']:
        _record(constraint, {'id', 'subject_ref', 'categories', 'kind', 'rule',
                             'source_ref', 'verification_required'})
        for name in ('id', 'subject_ref', 'rule', 'source_ref'):
            _text(constraint[name])
        _require(bool(_refs(constraint['categories'])))
        _require(constraint['kind'] in ('accessibility', 'age', 'availability', 'explicit_exclusion'))
        _require(type(constraint['verification_required']) is bool)
    _require(type(snapshot['evidence']) is list and len(snapshot['evidence']) <= 500, 'LIMIT_EXCEEDED')
    _require(type(snapshot['derived_traits']) is list and len(snapshot['derived_traits']) <= 100, 'LIMIT_EXCEEDED')
    nodes = {}
    for trait, rows in ((False, snapshot['evidence']), (True, snapshot['derived_traits'])):
        for node in rows:
            typed = not trait and node.get('basis') == 'typed_feedback'
            _require(not typed or version2)
            _record(node, TRAIT_FIELDS if trait else EVIDENCE_FIELDS | ({'event_lineage'} if typed else set()))
            _uuid(node['id'])
            _uuid(node['author_ref'])
            _require(node['id'] not in nodes)
            _refs(node['direct_parent_ids'])
            subjects = _refs(node['applicable_subjects'])
            _require(bool(subjects) and set(subjects) <= set(context['applicable_subjects']), 'AUTHORITY_DENIED')
            _text(node['subject_ref'])
            _require(node.get('subject_ref', snapshot['profile_ref']) in
                     set(context['applicable_subjects']) | {snapshot['profile_ref']}, 'AUTHORITY_DENIED')
            _require(node['subject_ref'] == snapshot['profile_ref'] or node['subject_ref'] in subjects,
                     'AUTHORITY_DENIED')
            _require(node['sensitivity'] in ('ordinary', 'sensitive', 'unknown'))
            _require(set(_refs(node['purposes'], 2)) <= {'inference', 'catalogue_lookup'})
            _require(node['origin'] in ('owner', 'authenticated-human-post', 'assistant', 'unknown', 'inferred_hypothesis'))
            if trait:
                _text(node['text'], 1200, empty=True)
                _require(bool(_refs(node['evidence_ids'])))
                _require(node['origin'] == 'inferred_hypothesis')
                _require(type(node['lineage_complete']) is bool)
            else:
                _text(node['key'])
                _text(node['source_ref'])
                _text(node['title'], 300, empty=True)
                _text(node['medium'], 40)
                _require(node['identity_status'] in ('confirmed', 'unresolved_personal'))
                _require(set(_refs(node['data_classes'], 4)) <= CLASSES)
                basis, value = node['basis'], node['value']
                if basis == 'anticipated_reaction':
                    values = value if type(value) is list else [value]
                    _require(1 <= len(values) <= 5 and all(type(v) is str and v in REACTIONS for v in values)
                             and len(set(values)) == len(values))
                elif basis in ('interest', 'enjoyment'):
                    _require(type(value) is int and 1 <= value <= 5)
                elif basis == 'favourite':
                    _require(value is True or type(value) is str and value == 'explicit_positive')
                elif basis == 'legacy_choice':
                    _require(type(value) is str and (value in LEGACY or
                             value.startswith('Conflicting historical responses: ') and len(value) <= 100))
                elif basis == 'feedback_note':
                    _record(value, {'reaction', 'text'})
                    _require(type(value['reaction']) is str and value['reaction'] in REACTIONS | LEGACY)
                    _note(value['text'])
                elif basis == 'typed_feedback':
                    from .feedback import validate_body, FeedbackError
                    _record(value, {'signal', 'scope', 'value'})
                    _require(value['scope'] in ('item', 'session'))
                    try:
                        validate_body(value['signal'], value['value'], imported=True)
                    except FeedbackError:
                        raise DisclosureError() from None
                    lineage = node['event_lineage']
                    _require(type(lineage) is list and 0 < len(lineage) <= 2000)
                    known, revision = set(), 0
                    for event in lineage:
                        _record(event, {'event_ref', 'revision', 'operation', 'reverses_ref', 'corrects_ref',
                                        'author_ref', 'subject_ref', 'origin', 'recorded_at', 'source_ref'})
                        for name in ('event_ref', 'author_ref', 'subject_ref'):
                            _uuid(event[name])
                        _require(event['event_ref'] not in known and event['revision'] == revision + 1)
                        _require(event['operation'] in ('set', 'reverse') and event['origin'] == 'direct')
                        _time(event['recorded_at']); _text(event['source_ref'])
                        _require(event['author_ref'] == node['author_ref'] and event['subject_ref'] == node['subject_ref'])
                        for name in ('reverses_ref', 'corrects_ref'):
                            _require(event[name] is None or event[name] in known)
                        _require((event['operation'] == 'reverse') == (event['reverses_ref'] is not None))
                        known.add(event['event_ref']); revision = event['revision']
                elif basis in ('discovery_seed', 'authored_observation'):
                    _text(value, 1000)
                else:
                    raise DisclosureError()
            nodes[node['id']] = node
    return nodes


def _classifications(node):
    if 'basis' not in node:
        return {'inferred_traits'}
    required = {'favourite': 'explicit_favourites', 'anticipated_reaction': 'anticipated_reactions',
        'interest': 'anticipated_reactions', 'enjoyment': 'experienced_enjoyment',
        'legacy_choice': 'legacy_choices', 'feedback_note': 'raw_notes',
        'discovery_seed': 'exploration_requests', 'authored_observation': 'unresolved_personal_text'}
    if node['basis'] == 'typed_feedback':
        signal = node['value']['signal']
        groups = {'enjoyment': {'experienced_enjoyment'}, 'interest': {'anticipated_reactions'},
            'saved': {'explicit_favourites'}, 'legacy_rating': {'legacy_choices'}, 'legacy_state': {'legacy_choices'},
            'constraint': {'raw_notes', 'free_text_entry'},
            'seed_statement': {'free_text_entry', 'unresolved_personal_text'}}.get(signal, {'reaction'})
        return set(node['data_classes']) | groups
    classes = set(node['data_classes']) | {required[node['basis']]}
    if node['basis'] == 'feedback_note':
        classes.add('anticipated_reactions' if node['value']['reaction'] in REACTIONS else 'legacy_choices')
    if node['identity_status'] == 'unresolved_personal':
        classes.add('unresolved_personal_text')
    return classes


def _lineage(nodes, permissions, purpose):
    parents = {key: set(node['direct_parent_ids']) | set(node.get('evidence_ids', [])) for key, node in nodes.items()}
    known = {}
    def allowed(start):
        stack, visiting = [(start, False)], set()
        while stack:
            key, finished = stack.pop()
            if key in known:
                continue
            node = nodes.get(key)
            if node is None:
                known[key] = False
                continue
            if not finished:
                if key in visiting:
                    known[key] = False
                    continue
                visiting.add(key)
                stack.append((key, True))
                stack.extend((parent, False) for parent in sorted(parents[key], reverse=True))
                continue
            classes = _classifications(node)
            if node['sensitivity'] == 'sensitive':
                classes = classes | {'sensitive'}
            own = (node['origin'] in ('owner', 'authenticated-human-post', 'inferred_hypothesis')
                   and node['sensitivity'] != 'unknown' and purpose in node['purposes']
                   and node.get('lineage_complete', True) and
                   ('basis' in node or bool(parents[key])) and
                   all((subject, group) in permissions for subject in node['applicable_subjects'] for group in classes))
            # Inferred content can never be presented as direct human evidence.
            if 'basis' in node and node['origin'] == 'inferred_hypothesis':
                own = False
            known[key] = bool(own and all(known.get(parent, False) for parent in parents[key]))
            visiting.discard(key)
        return known.get(start, False)
    return allowed, parents


def _profile(nodes, allowed, parents):
    signals, notes, traits, omitted = [], [], [], set()
    for key, node in nodes.items():
        if not allowed(key):
            omitted.update(_classifications(node))
        elif 'basis' not in node:
            traits.append(node)
        elif node['basis'] == 'feedback_note':
            notes.append(node)
        else:
            signals.append(node)
    # Equal disclosed content shares an alias: private IDs and input order cannot
    # break ties or change a trait's evidence references.
    aliases = {}
    profile = {'schema': 'disclosed-profile/v1', 'mode': 'seeded' if signals else 'broad', 'signals': []}
    for rows, prefix, output_name in ((signals, 'signal', 'signals'), (notes, 'note', 'raw_notes')):
        grouped = {}
        for node in rows:
            value = ('explicit_positive' if node.get('basis') == 'favourite' else node['value'])
            content = value if prefix == 'note' else {
                'title': node['title'], 'medium': node['medium'], 'source': 'authenticated_human',
                'basis': node['basis'], 'value': value}
            grouped.setdefault(canonical_bytes(content), (content, []))[1].append(node['id'])
        output = []
        for i, (_, (content, ids)) in enumerate(sorted(grouped.items())):
            alias = f'{prefix}-{i + 1}'
            aliases.update({key: alias for key in ids})
            output.append({'id': alias, 'key': alias, **content} if prefix == 'note' else
                          {'key': alias, **content})
        if output:
            profile[output_name] = output
    if traits:
        grouped = {}
        for node in traits:
            ancestors, pending = set(), list(parents[node['id']])
            while pending:
                parent = pending.pop()
                if parent not in ancestors:
                    ancestors.add(parent)
                    pending.extend(parents.get(parent, ()))
            refs = sorted({aliases[p] for p in ancestors if p in aliases})
            if refs:
                content = {'text': node['text'], 'evidence_keys': refs}
                grouped[canonical_bytes(content)] = content
        output = [{'id': f'trait-{i + 1}', **content}
                  for i, (_, content) in enumerate(sorted(grouped.items()))]
        if output:
            profile['inferred_traits'] = output
    return profile, tuple(sorted(omitted))


def _views(observations, policies):
    _require(type(observations) is dict and type(policies) is dict)
    for name, view in observations.items():
        _text(name)
        _record(view, {'revision', 'digest'})
        _revision(view['revision'])
        _sha(view['digest'])
    for name, view in policies.items():
        _text(name)
        _record(view, {'revision'})
        _revision(view['revision'])


def _binding(binding, value, observations, policies, now):
    _record(binding, {'target', 'fact_id', 'classification', 'direct_parent_ids', 'observation', 'rights'})
    _require(binding['classification'] in ('public_source', 'personal_or_derived'))
    _refs(binding['direct_parent_ids'])
    if binding['classification'] == 'personal_or_derived':
        _require(bool(binding['direct_parent_ids']))
    observation, rights = binding['observation'], binding['rights']
    _record(observation, {'ref', 'revision', 'digest', 'locator', 'observed_at', 'expires_at'})
    _text(observation['ref'])
    _text(observation['locator'])
    _revision(observation['revision'])
    _sha(observation['digest'])
    _require(observations.get(observation['ref']) == {k: observation[k] for k in ('revision', 'digest')}, 'PREVIEW_STALE')
    _record(rights, {'policy_ref', 'revision', 'disposition', 'checked_at', 'expires_at', 'binding_digest'})
    _text(rights['policy_ref'])
    _revision(rights['revision'])
    _sha(rights['binding_digest'])
    _require(policies.get(rights['policy_ref']) == {'revision': rights['revision']}, 'PREVIEW_STALE')
    _require(rights['disposition'] in ('allowed', 'denied', 'unknown'))
    material = {k: binding[k] for k in ('target', 'fact_id', 'classification', 'direct_parent_ids', 'observation')}
    material['value'] = value
    _require(hashlib.sha256(canonical_bytes(material)).hexdigest() == rights['binding_digest'])
    observed, checked = _time(observation['observed_at']), _time(rights['checked_at'])
    observation_end, rights_end = _time(observation['expires_at']), _time(rights['expires_at'])
    _require(observed < observation_end and checked < rights_end)
    return rights['disposition'] == 'allowed' and observed <= now < observation_end and checked <= now < rights_end, min(observation_end, rights_end)


def _sources(records, allowed, context, observations, policies, now):
    _require(type(records) is list and len(records) <= 100, 'LIMIT_EXCEEDED')
    _views(observations, policies)
    prepared, counts, seen, record_refs, blocked = [], {}, set(), set(), False
    source_slots = {}
    for record in records:
        _record(record, {'schema', 'record_ref', 'candidate', 'direct_parent_ids', 'fields'}, 'candidate-source-rights/v1')
        _text(record['record_ref'])
        _require(record['record_ref'] not in record_refs)
        record_refs.add(record['record_ref'])
        _refs(record['direct_parent_ids'])
        candidate = record['candidate']
        _record(candidate, CANDIDATE_FIELDS)
        _require(candidate['rights_ref'] == record['record_ref'])
        for name in CANDIDATE_FIELDS - {'facts', 'source_roots'}:
            _text(candidate[name], 300, empty=name == 'title')
        _require(candidate['key'] not in seen)
        seen.add(candidate['key'])
        counts[candidate['category']] = counts.get(candidate['category'], 0) + 1
        _require(counts[candidate['category']] <= 20, 'LIMIT_EXCEEDED')
        _require(candidate['identity_status'] in ('confirmed', 'fixture_identity_only'))
        kinds = {'live_lookup', 'cached_lookup', 'release_bundled'}
        fixture = candidate['identity_status'] == 'fixture_identity_only' or candidate['evidence_type'] == 'fixture_not_live' or candidate['key'].startswith(('synthetic:', 'fixture:'))
        _require(not fixture or context['isolated_fixture'], 'AUTHORITY_DENIED')
        _require(candidate['evidence_type'] in kinds or fixture and candidate['evidence_type'] == 'fixture_not_live')
        _refs(candidate['source_roots'], 64)
        observed, deadline = _time(candidate['observed_at']), _time(candidate['valid_until'])
        _require(observed < deadline)
        facts = candidate['facts']
        _require(type(facts) is list and len(facts) <= 40, 'LIMIT_EXCEEDED')
        by_id = {}
        for fact in facts:
            _record(fact, {'id', 'property', 'value', 'observation_ref'})
            for name in ('id', 'property', 'observation_ref'):
                _text(fact[name])
            _require(type(fact['value']) in (str, int, bool))
            if type(fact['value']) is str:
                _text(fact['value'], 1200, empty=True)
            _require(fact['id'] not in by_id)
            by_id[fact['id']] = fact
        _require(type(record['fields']) is list and len(record['fields']) == 11 + len(facts))
        targets, retained, roots, valid = set(), [], set(), observed <= now < deadline
        slots = []
        for binding in record['fields']:
            _require(type(binding) is dict and 'target' in binding and 'fact_id' in binding)
            target, fact_id = binding['target'], binding['fact_id']
            _require(type(target) is str and target in SCALARS | {'fact'})
            if target == 'fact':
                _text(fact_id)
                _require(fact_id in by_id)
                value = by_id[fact_id]
            else:
                _require(fact_id is None)
                value = candidate[target]
            _require((target, fact_id) not in targets)
            targets.add((target, fact_id))
            permitted, expires = _binding(binding, value, observations, policies, now)
            if target == 'fact':
                _require(value['observation_ref'] == binding['observation']['ref'])
            dependencies_ok = all(allowed(parent) for parent in binding['direct_parent_ids'])
            # Record parents separately cover ALL membership/query/order inputs.
            # An optional evidence fact can be omitted only from that independent pool.
            if permitted and dependencies_ok:
                if target == 'fact':
                    retained.append(value)
                roots.add(binding['observation']['ref'])
                slots.append((target, fact_id or '', binding['observation']['ref']))
                deadline = min(deadline, expires)
            elif target != 'fact':
                valid = False
        _require(targets == {(name, None) for name in SCALARS} | {('fact', name) for name in by_id})
        _require(all(ref in observations for ref in candidate['source_roots']))
        if not all(allowed(parent) for parent in record['direct_parent_ids']):
            blocked = True
        if valid and candidate['title']:
            _require(len(roots) <= 64, 'LIMIT_EXCEEDED')
            prepared.append({**candidate, 'facts': sorted(retained, key=lambda f: f['id']),
                             'source_roots': sorted(roots), 'valid_until': deadline.isoformat().replace('+00:00', 'Z')})
            source_slots[candidate['key']] = tuple(sorted(slots))
    prepared = tuple(sorted(prepared, key=lambda c: c['key']))
    return prepared, blocked, tuple(source_slots[c['key']] for c in prepared)


def prepare(snapshot, grants, context, candidates, observations, policies, *, now):
    """Project fixtures only; caller views are not authenticated by this function.

    profile_bytes encode the portable input with NO source candidates. The local
    candidate_projection retains REQ-010 local provenance refs. portable_input
    maps only retained bindings for the admitted codec. Never send this object.
    """
    canonical_bytes([snapshot, grants, context, candidates, observations, policies])
    _require(type(snapshot) is dict and {'installation_id', 'actor_ref', 'profile_ref', 'policy_epoch'} <= snapshot.keys())
    _context(snapshot, context, now)
    nodes = _nodes(snapshot, context)
    permissions = _grant_permissions(grants, context, now)
    allowed, parents = _lineage(nodes, permissions, context['purpose'])
    source_projection, blocked, source_slots = _sources(candidates, allowed, context, observations, policies, now)
    if blocked:
        return Preparation(False, 'AUTHORITY_DENIED', None, (), ())
    profile, omitted = _profile(nodes, allowed, parents)
    version2 = snapshot['schema'] == 'profile-snapshot/v2'
    if version2:
        profile['schema'] = 'disclosed-profile/v2'
    data = canonical_bytes({'schema': 'cabinet-inference-input/v2' if version2 else 'cabinet-inference-input/v1',
                            'profile': profile, 'candidates': []})
    return Preparation(True, 'OK', data, source_projection, omitted, source_slots)


def portable_input(preparation):
    """Map the ONE permitted projection to body-local provenance references.

    Only retained field slots establish aliases. Private observation IDs, policy
    hashes and denied bindings never seed identifiers or break ordering ties.
    The legacy preparatory local projection remains unchanged for local callers.
    """
    _require(type(preparation) is Preparation and preparation.ok, 'AUTHORITY_DENIED')
    value = read_json(preparation.profile_bytes)
    _require(len(preparation.candidate_projection) == len(preparation._source_slots))
    aliases, candidates = {}, []
    for index, (candidate, slots) in enumerate(zip(preparation.candidate_projection,
                                                 preparation._source_slots), 1):
        roots = set()
        fact_refs = {}
        for target, fact_id, ref in slots:
            if ref not in aliases:
                aliases[ref] = f'evidence-{len(aliases) + 1}'
            roots.add(aliases[ref])
            if target == 'fact':
                fact_refs[fact_id] = aliases[ref]
        facts = [{**fact, 'id': f'fact-{index}-{i}', 'observation_ref': fact_refs[fact['id']]}
                 for i, fact in enumerate(candidate['facts'], 1)]
        candidates.append({**candidate, 'rights_ref': f'rights-{index}',
                           'source_roots': sorted(roots), 'facts': facts})
    value['candidates'] = candidates
    canonical_bytes(value)
    return value


def catalogue_input(snapshot, grants, context, categories, *, now):
    """Derive exact catalogue terms only AFTER the existing permission traversal.

    No complete-state digest, denied note or private node reference can seed a
    query or its order. The source owner must separately authorize its actual
    registered endpoint/config; this pure projection does not confer that right.
    """
    _require(type(context) is dict and context.get('purpose') == 'catalogue_lookup', 'AUTHORITY_DENIED')
    _require(type(categories) is tuple and 0 < len(categories) <= 100)
    for category in categories:
        _text(category, 80)
    _require('all' not in categories and len(set(categories)) == len(categories))
    prepared = prepare(snapshot, grants, context, [], {}, {}, now=now)
    _require(prepared.ok, prepared.code)
    profile = read_json(prepared.profile_bytes)['profile']
    terms = sorted({signal['title'] for signal in profile['signals'] if signal['title']})
    # Recording search accepts an artist name, not an arbitrary music title.
    # Derive it only after consent/lineage filtering, never from raw notes.
    artists = sorted({signal['title'] for signal in profile['signals']
                      if signal.get('medium') == 'artist' and signal['title']})
    value = {'schema': 'catalogue-query/v1', 'categories': list(categories), 'profile': profile,
             'queries': [{'category': category, 'terms': list(artists if
                context.get('adapter_id') == 'musicbrainz_recording' and category == 'recording' else terms)}
                for category in categories]}
    return canonical_bytes(value), prepared.omitted_classes
