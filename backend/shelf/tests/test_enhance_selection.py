"""Pure selection regressions; authored only, never provider or runtime evidence."""
from copy import deepcopy
from unittest import TestCase
from shelf.enhance_selection import GroundedSelector
from shelf.enhance_contract import CategoryOutcome, EnhanceFault
from shelf.inference_contract import ContractError
from .test_portable_cabinet_store import candidate


class GroundedSelectionTests(TestCase):
    def material(self):
        cards = [dict(candidate(), key=key, provider=provider) for key, provider in
                 [('source:c', 'beta'), ('source:b', 'alpha'), ('source:a', 'alpha')]]
        submitted = {'profile': {'signals': []}, 'candidates': cards}
        output = {'schema': 'cabinet-inference-result/v1', 'associations': None,
            'candidate_assessments': [dict(candidate_key=c['key'], decision='retain',
                signal_keys=[], evidence_refs=['evidence-1'], hypothesis='UNTRUSTED MODEL TEXT') for c in cards]}
        outcomes = (CategoryOutcome('books', 'fresh', 3), CategoryOutcome('events', 'unavailable', 0, 'CAPABILITY_UNAVAILABLE'))
        return submitted, output, outcomes

    def test_order_is_stable_diverse_and_all_categories_remain_explicit(self):
        submitted, output, outcomes = self.material()
        first = GroundedSelector().build(output, submitted, outcomes)
        submitted['candidates'].reverse(); output['candidate_assessments'].reverse()
        self.assertEqual(first, GroundedSelector().build(output, submitted, outcomes))
        self.assertEqual([c['key'] for c in first['categories'][0]['cards']], ['source:a', 'source:c', 'source:b'])
        self.assertEqual(first['categories'][1]['state'], 'unavailable')
        self.assertTrue(all(reason['text'] != 'UNTRUSTED MODEL TEXT' for reason in first['for_you']))

    def test_hard_exclusions_and_seen_keys_precede_model_retain(self):
        submitted, output, outcomes = self.material()
        snapshot = dict(hard_exclusions=['source:a'], seen_keys=['source:b'], hard_constraints=[])
        value = GroundedSelector().build(output, submitted, outcomes, snapshot=snapshot)
        self.assertEqual([c['key'] for c in value['categories'][0]['cards']], ['source:c'])
        snapshot['hard_constraints'] = [{'verification_required': True}]
        with self.assertRaises(EnhanceFault):
            GroundedSelector().build(output, submitted, outcomes, snapshot=snapshot)

    def test_invented_refs_and_rejected_or_uncited_candidates_are_not_recommended(self):
        submitted, output, outcomes = self.material()
        output['candidate_assessments'][0]['evidence_refs'] = ['invented']
        with self.assertRaises(ContractError):
            GroundedSelector().build(output, submitted, outcomes)
        output['candidate_assessments'][0].update(evidence_refs=['evidence-1'], decision='reject')
        output['candidate_assessments'][1]['evidence_refs'] = []
        value = GroundedSelector().build(output, submitted, outcomes)
        self.assertEqual([c['key'] for c in value['categories'][0]['cards']], ['source:a'])
