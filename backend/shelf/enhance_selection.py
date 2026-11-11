"""Deterministic grounded queues; model hypotheses never become source facts."""
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


class GroundedSelector:
    def build(self, output, submitted, outcomes, *, snapshot=None, local_context=None):
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
