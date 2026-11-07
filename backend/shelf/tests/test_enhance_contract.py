"""Frozen shared record regressions; these records never mint capabilities."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from shelf.disclosure import canonical_bytes
from shelf.enhance_contract import (CataloguePlan, CategoryOutcome, CollectionBudget,
    CollectionSeal, EnhanceBindings, EnhanceFault, RegistryView, StageEvent, digest)


class EnhanceContractTests(unittest.TestCase):
    def test_complete_seal_rejects_omitted_category_binding_count_and_expiry_changes(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        registry = RegistryView('1', ('books', 'films', 'podcasts'))
        bindings = EnhanceBindings('install', 'actor', 'profile', 'revision', 1, '1', '1', 1, 'grants', 'connection')
        body = canonical_bytes({'schema': 'catalogue-query/v1', 'categories': list(registry.categories),
            'queries': [{'category': key, 'terms': []} for key in registry.categories], 'profile': {}})
        plan = CataloguePlan('server-plan', bindings, registry.categories, body, digest(body), now + timedelta(minutes=5))
        outcomes = tuple(CategoryOutcome(key, 'unavailable', 0, 'CAPABILITY_UNAVAILABLE') for key in registry.categories)
        seal = CollectionSeal('server-seal', plan.plan_ref, bindings, outcomes,
            canonical_bytes({'records': [], 'observations': {}, 'policies': {}}), b'{}', plan.expires_at)
        seal.verify(plan, registry, now=now)
        for invalid in (replace(seal, outcomes=outcomes[:-1]), replace(seal, plan_ref='other-plan'),
                        replace(seal, bindings=replace(bindings, profile_ref='foreign-profile')),
                        replace(seal, expires_at=now)):
            with self.subTest(invalid=invalid):
                with self.assertRaises(EnhanceFault):
                    invalid.verify(plan, registry, now=now)
        with self.assertRaises(EnhanceFault):
            replace(seal, outcomes=(CategoryOutcome('books', 'fresh', 1),))

    def test_registry_budget_and_public_stages_never_invent_coverage_or_publication(self):
        for categories in (('all',), ('books', 'books'), ()):
            with self.assertRaises(EnhanceFault):
                RegistryView('1', categories)
        for overrides in ({'initial_sends': 13}, {'sources_per_category': 3}, {'candidates_total': 101},
                          {'response_bytes': 262145}, {'request_seconds': 9}):
            with self.assertRaises(EnhanceFault):
                CollectionBudget(**overrides)
        event = StageEvent('server-run', 'sealed', outcomes=(CategoryOutcome('events', 'unavailable', 0, 'CAPABILITY_UNAVAILABLE'),))
        public = event.public()
        self.assertEqual(public['category_outcomes'][0]['state'], 'unavailable')
        self.assertFalse(public['published'])
        self.assertFalse(public['run_ready'])
        self.assertNotIn('body', public)
        with self.assertRaises(EnhanceFault):
            StageEvent('server-run', 'published')
        self.assertTrue(StageEvent('server-run', 'published', publication_revision=2).public()['published'])
