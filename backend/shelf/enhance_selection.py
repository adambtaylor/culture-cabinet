"""Deterministic grounded queues; model hypotheses never become source facts."""
from .disclosure import canonical_bytes
from .enhance_contract import require
from .inference_contract import validate_result


class GroundedSelector:
    def build(self, output, submitted, outcomes, *, snapshot=None):
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
                ranked.append((-score, card['provider'], card['key'], card, assessment))
            ranked.sort(key=lambda row: row[:3])
            # Bounded round-robin diversity within equal evidence/decision scores;
            # stable keys break ties, and private provenance never supplies ranks.
            ordered = []
            for score in sorted({row[0] for row in ranked}):
                buckets = {}
                for row in ranked:
                    if row[0] == score:
                        buckets.setdefault(row[1], []).append(row)
                while any(buckets.values()):
                    for provider in sorted(buckets):
                        if buckets[provider]:
                            ordered.append(buckets[provider].pop(0))
            selected = ordered[:20] if outcome.state == 'fresh' else []
            cards = [row[3] for row in selected]
            state = 'fresh' if cards else 'empty_bounded' if outcome.state == 'fresh' else outcome.state
            categories.append(dict(category=outcome.category, state=state, code=outcome.code, cards=cards))
            for _, _, _, card, assessment in selected:
                inferred = bool(assessment['signal_keys'])
                explanations.append(dict(candidate_key=card['key'],
                    kind='inferred' if inferred else 'source',
                    text='Tentative association with submitted signals.' if inferred else 'Selected from cited catalogue evidence.',
                    evidence_refs=assessment['evidence_refs']))
        return {'schema': 'cabinet-manifest/v1', 'categories': categories, 'for_you': explanations}
