"""Deterministic grounded queues; model hypotheses never become source facts."""
from dataclasses import dataclass
import hashlib

from .disclosure import canonical_bytes
from .enhance_contract import require
from .inference_contract import validate_result


def preference_strength(signal):
    value = signal.get('value')
    if signal.get('basis') == 'favourite' and value == 'explicit_positive':
        return 2
    if signal.get('basis') == 'typed_feedback' and type(value) is dict:
        if value.get('signal') == 'saved' and value.get('value') == {'value': True}:
            return 2
        if value.get('signal') == 'interest':
            if value.get('value') == {'value': 'yes', 'basis': 'anticipated'}:return 1
            if value.get('value') == {'value': 'no', 'basis': 'anticipated'}:return -1
    return 0


@dataclass(frozen=True)
class RunSelectionPolicy:
    noise: int
    version: str='selection-policy/v1'

    def __post_init__(self):
        require(self.version=='selection-policy/v1' and type(self.noise) is int and 0<=self.noise<=100)

    def material(self):return {'version':self.version,'noise':self.noise}

    @property
    def digest(self):return hashlib.sha256(canonical_bytes(self.material())).hexdigest()


def selection_seed(policy,run_ref,taste_digest,collection_digest):
    require(type(policy) is RunSelectionPolicy)
    return hashlib.sha256(canonical_bytes(dict(policy_version=policy.version,run_ref=run_ref,
        frozen_taste_digest=taste_digest,collection_digest=collection_digest))).hexdigest()


def _experimental(ordered,selected,policy,seed):
    """Keep slot0 and the uncertain tail; vary only proved retained slots.

    The same permitted pool is already validated. Noise changes local selection,
    never the submitted pool, model parameters or a source request. A weaker
    positive is variety, while a direct-dislike-only candidate is ineligible.
    """
    retained=[row for row in selected if row[0]==-2]
    count=len(retained);quota=0 if policy.noise==0 or count<2 else min(count-1,(count*policy.noise+249)//250)
    positions={j*count//(quota+1) for j in range(1,quota+1)}
    pool=[row for row in ordered if row[0]==-2]
    chosen=[];keys=set();exploratory=[];providers={}
    for index in range(count):
        baseline=next(row for row in pool if row[5] not in keys)
        candidate=baseline
        if index in positions:
            alternatives=[row for row in pool if row[5] not in keys and row[5]!=baseline[5]
                          and row[1]<=0 and row[2]==baseline[2]]
            if alternatives:
                candidate=min(alternatives,key=lambda row:(-row[1],row[3],providers.get((-row[1],row[2],row[3],row[4]),0),
                    hashlib.sha256((seed+':'+row[5]).encode()).hexdigest(),row[5]))
                exploratory.append(candidate[5])
        chosen.append(candidate);keys.add(candidate[5])
        # Diversity belongs to the same affinity/local-rank tier, matching the
        # precise selector. A stronger prior favourite cannot penalize neutrals.
        tier=(-candidate[1],candidate[2],candidate[3],candidate[4])
        providers[tier]=providers.get(tier,0)+1
    reason=('Precise baseline selected.' if quota==0 else
            'No additional eligible variety in the same radius band.' if len(exploratory)<quota else '')
    return chosen+[row for row in selected if row[0]!=-2],dict(requested=quota,used=len(exploratory),
        reason=reason,exploratory_keys=exploratory)


class GroundedSelector:
    def build(self, output, submitted, outcomes, *, snapshot=None, local_context=None,
              selection_policy=None,selection_seed=None,selection_report=None):
        if selection_policy is not None:
            require(type(selection_policy) is RunSelectionPolicy and type(selection_seed) is str
                and len(selection_seed)==64 and all(c in '0123456789abcdef' for c in selection_seed))
        if selection_report is not None:require(type(selection_report) is list and not selection_report)
        candidates = submitted['candidates']
        signals = [{key: row[key] for key in ('key', 'medium')} for row in submitted['profile']['signals']]
        references = [{'key': row['key'], 'evidence_refs': sorted(set(row['source_roots']) |
                       {fact['id'] for fact in row['facts']})} for row in candidates]
        valid = validate_result(canonical_bytes(output), signals=signals, candidates=references)
        excluded = set()
        if snapshot is not None:
            excluded.update(snapshot['hard_exclusions'])
            excluded.update(snapshot['seen_keys'])
            # Free-text access/age/availability rules need an authenticated
            # eligibility producer; never reinterpret a model guess as proof.
            require(not snapshot['hard_constraints'], 'CAPABILITY_UNAVAILABLE')
        assessments = {row['candidate_key']: row for row in valid['candidate_assessments']}
        strengths = {row['key']: preference_strength(row) for row in submitted['profile']['signals']}
        categories, explanations = [], []
        for outcome in outcomes:
            ranked = []
            for card in candidates:
                assessment = assessments.get(card['key'])
                if (card['category'] != outcome.category or card['key'] in excluded
                        or assessment is None or assessment['decision'] == 'reject'
                        or not assessment['evidence_refs']):
                    continue
                score = 2 if assessment['decision'] == 'retain' else 1
                weights = [strengths[key] for key in assessment['signal_keys']]
                positives = [weight for weight in weights if weight > 0]
                strength = max(positives) if positives else -1 if -1 in weights else 0
                # Evidence/decision tier remains first. Permission-filtered,
                # cited preference uses max, so duplicates and Obtain cannot stack.
                from .local_event_ranking import rank_key
                band,distance=rank_key(local_context,card)
                ranked.append((-score,-strength,band,distance,card['provider'],card['key'],card,assessment))
            ranked.sort(key=lambda row: row[:6])
            # Equal evidence/taste tiers use local radius bands and known distance.
            # Provider diversity/stable keys still resolve equal local ranks.
            # The source/model pool has already been validated and is unchanged.
            ordered = []
            for score in sorted({row[:4] for row in ranked}):
                buckets = {}
                for row in ranked:
                    if row[:4] == score:
                        buckets.setdefault(row[4], []).append(row)
                while any(buckets.values()):
                    for provider in sorted(buckets):
                        if buckets[provider]:
                            ordered.append(buckets[provider].pop(0))
            selected = ordered[:20] if outcome.state == 'fresh' else []
            if selection_policy is not None:
                selected,report=_experimental(ordered,selected,selection_policy,selection_seed)
                if selection_report is not None:
                    selection_report.append(dict(category=outcome.category,**report))
            cards = [row[6] for row in selected]
            state = 'fresh' if cards else 'empty_bounded' if outcome.state == 'fresh' else outcome.state
            categories.append(dict(category=outcome.category, state=state, code=outcome.code, cards=cards))
            for _,negative_strength,_,_,_,_,card,assessment in selected:
                inferred = bool(assessment['signal_keys'])
                explanations.append(dict(candidate_key=card['key'],
                    kind='inferred' if inferred else 'source',
                    text=('Tentative association with an explicitly stated favourite.' if negative_strength == -2 else
                          'Tentative association with an explicitly liked item.' if negative_strength == -1 else
                          'Tentative association with submitted signals.' if inferred else 'Selected from cited catalogue evidence.'),
                    evidence_refs=assessment['evidence_refs']))
        return {'schema': 'cabinet-manifest/v1', 'categories': categories, 'for_you': explanations}


def checked_selection_report(report,categories,*,policy_digest,seed,selected_keys=None):
    require(all(type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value)
                for value in (policy_digest,seed)))
    require(type(report) is dict and set(report)=={'schema','policy_digest','seed','categories'}
        and report['schema']=='selection-report/v1' and report['policy_digest']==policy_digest and report['seed']==seed)
    require(type(report['categories']) is list and len(report['categories'])==len(categories))
    seen=set()
    for category,row in zip(categories,report['categories']):
        require(type(row) is dict and set(row)=={'category','requested','used','reason','exploratory_keys'}
            and row['category']==category and type(row['requested']) is int and type(row['used']) is int
            and 0<=row['used']<=row['requested']<=19 and type(row['reason']) is str and len(row['reason'])<=500
            and (row['used']==row['requested'] or bool(row['reason'])))
        keys=row['exploratory_keys']
        require(type(keys) is list and len(keys)==row['used'] and all(type(key) is str and 0<len(key)<=300 for key in keys)
            and len(set(keys))==len(keys) and not seen.intersection(keys))
        if selected_keys is not None:require(set(keys)<=selected_keys)
        seen.update(keys)
    return report


def parse_noise(value):
    import re
    from . import grant_authority as ga
    ga._require(type(value) is str and re.fullmatch(r'0|[1-9][0-9]?|100',value) is not None,'CONTRACT_INVALID',422)
    return int(value)


def redacted_selection_evidence(evidence):
    """Keep only the original user-associated policy in the existing tombstone.

    This setting verifies admission lineage; it grants no display/send authority.
    Missing legacy policy stays missing. Malformed/orphan metadata never becomes
    an empty legacy record, and no current default participates in this function.
    """
    require(type(evidence) is dict)
    if 'selection_policy' not in evidence:
        require(not {'selection_seed','selection_policy_digest','selection_report'} & set(evidence))
        return {}
    policy=evidence['selection_policy']
    require(type(policy) is dict and set(policy)=={'version','noise'})
    return {'selection_policy':RunSelectionPolicy(**policy).material()}


def admission_fingerprint(record,refresh_nonce,evidence):
    """The original admission codec, shared by live and raw historical callers."""
    import uuid
    from .enhance_contract import EnhanceBindings
    require(type(record) is dict and set(record) in (
        {'bindings','categories','source_revision'},
        {'bindings','categories','source_revision','local_selection_digest'}))
    require(type(record['bindings']) is dict)
    bindings=EnhanceBindings(**record['bindings'])
    refresh=str(uuid.UUID(str(refresh_nonce)))
    material=dict(schema='enhance/v1',bindings=vars(bindings),refresh_nonce=refresh)
    if 'local_selection_digest' in record:
        value=record['local_selection_digest']
        require(type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value))
        material['local_selection_digest']=value
    projected=redacted_selection_evidence(evidence)
    if projected:material.update(projected)
    return hashlib.sha256(canonical_bytes(material)).hexdigest()


def validate_selection_history(record,evidence,*,input_digest,refresh_nonce,run_ref,collection_digest,redacted):
    """Validate supplied immutable history only; never consult live authority."""
    import uuid
    projected=redacted_selection_evidence(evidence)
    require(type(redacted) is bool and input_digest==admission_fingerprint(record,refresh_nonce,evidence))
    if redacted:
        require(evidence==projected)
        return projected
    if projected:
        policy=RunSelectionPolicy(**projected['selection_policy'])
        has_seed='selection_seed' in evidence or 'selection_policy_digest' in evidence
        if has_seed:
            require(type(collection_digest) is str and len(collection_digest)==64
                and all(c in '0123456789abcdef' for c in collection_digest))
            require(evidence.get('selection_policy_digest')==policy.digest and evidence.get('selection_seed')==
                selection_seed(policy,str(uuid.UUID(str(run_ref))),record['bindings']['snapshot_revision'],collection_digest))
        if 'selection_report' in evidence:
            require(has_seed)
            checked_selection_report(evidence['selection_report'],record['categories'],
                policy_digest=policy.digest,seed=evidence['selection_seed'])
    return projected
