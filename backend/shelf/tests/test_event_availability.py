"""Synthetic supplied observations, without live seller/notification operations."""
from dataclasses import replace
from datetime import timedelta
from unittest import TestCase

from shelf import event_availability as a
from shelf.discovery_sources import SourceError
from shelf.tests.test_event_sources import NOW, FETCHED, official, parse


def event(name='one', **changes):
    return parse(official(id=name, url=f'https://events.unimelb.edu.au/{name}', **changes))[0]


def seller(item, status=a.SellerStatus.LOW, **changes):
    return replace(a.SellerObservation(a.AvailabilityScope(item.key), status,
        item.evidence[0], item.starts_at, .9), **changes)


def risk(item, **changes):
    return replace(a.EstimatedRisk(a.AvailabilityScope(item.key), .6,
        'Synthetic bounded prior; no stock evidence', FETCHED, item.starts_at, .4), **changes)


class AvailabilityTests(TestCase):
    def test_saved_then_high_fit_then_other_with_a_bounded_output(self):
        ordinary, fit, saved = [event(name) for name in ('ordinary', 'fit', 'saved')]
        result = a.reduce_availability([ordinary, fit, saved], list(map(seller, [ordinary, fit, saved])),
            now=NOW, interests=[a.EventInterest(fit.key, fit=.95), a.EventInterest(saved.key, saved=True)], limit=2)
        self.assertEqual([w.scope.event_key for w in result.warnings], [saved.key, fit.key])
        # An unshown warning is not marked delivered when the output budget fills.
        again = a.reduce_availability([ordinary, fit, saved], list(map(seller, [ordinary, fit, saved])),
            now=NOW, previous=result.memory)
        self.assertEqual([w.scope.event_key for w in again.warnings], [ordinary.key])

    def test_repeat_and_timestamp_confidence_jitter_are_suppressed(self):
        item = event(); signal = seller(item)
        first = a.reduce_availability([item], [signal], now=NOW)
        newer = replace(signal, evidence=replace(signal.evidence,
            retrieved_at=(NOW + timedelta(minutes=1)).isoformat()), confidence=.8)
        again = a.reduce_availability([item], [newer], now=NOW + timedelta(minutes=1), previous=first.memory)
        self.assertEqual(again.warnings, ())
        self.assertEqual(again.memory[0].checked_at, newer.checked_at)

    def test_material_seller_change_and_stock_recovery_warn_once(self):
        item = event(); signal = seller(item)
        memory = ()
        for status in (a.SellerStatus.LOW, a.SellerStatus.ALMOST_SOLD_OUT,
                       a.SellerStatus.SOLD_OUT, a.SellerStatus.AVAILABLE):
            result = a.reduce_availability([item], [replace(signal, status=status)], now=NOW, previous=memory)
            self.assertEqual([w.kind for w in result.warnings], [status.value])
            memory = result.memory
        self.assertEqual(a.reduce_availability([item], [replace(signal, status=status)],
            now=NOW, previous=memory).warnings, ())

    def test_available_is_not_an_initial_scarcity_warning(self):
        item = event()
        result = a.reduce_availability([item], [seller(item, a.SellerStatus.AVAILABLE)], now=NOW)
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.views[0].signal.status, a.SellerStatus.AVAILABLE)

    def test_stale_or_future_checked_data_remain_visible_without_warning(self):
        item = event()
        for offset, expected in ((-901, 'stale'), (1, 'future-checked')):
            evidence = replace(item.evidence[0], retrieved_at=(NOW + timedelta(seconds=offset)).isoformat())
            result = a.reduce_availability([item], [seller(item, evidence=evidence)], now=NOW)
            self.assertEqual(result.views[0].freshness, expected)
            self.assertEqual(result.warnings, ())
            self.assertEqual(result.memory, ())

    def test_stale_event_context_cannot_make_new_seller_data_fresh(self):
        item = event()
        old = replace(item, retrieved_at=(NOW - timedelta(hours=1)).isoformat())
        result = a.reduce_availability([old], [seller(item)], now=NOW)
        self.assertEqual(result.views[0].freshness, 'stale')
        self.assertFalse(result.warnings)

    def test_stale_then_refreshed_same_status_does_not_repeat(self):
        item = event(); signal = seller(item)
        first = a.reduce_availability([item], [signal], now=NOW)
        later = NOW + timedelta(hours=1)
        stale = a.reduce_availability([item], [signal], now=later, previous=first.memory)
        self.assertEqual(stale.memory, first.memory)
        fresh = replace(item, retrieved_at=later.isoformat())
        signal = replace(signal, evidence=replace(signal.evidence, retrieved_at=later.isoformat()))
        self.assertFalse(a.reduce_availability([fresh], [signal], now=later, previous=stale.memory).warnings)

    def test_cancelled_and_postponed_replace_scarcity_with_one_event_notice(self):
        item = event()
        signals = [seller(item), seller(item, scope=a.AvailabilityScope(item.key, allocation_id='fixture-allocation'))]
        for status in ('cancelled', 'postponed'):
            result = a.reduce_availability([replace(item, status=status)], signals, now=NOW)
            self.assertEqual([w.kind for w in result.warnings], [status])
            self.assertEqual(result.warnings[0].scope.level, 'event')
            self.assertFalse(a.reduce_availability([replace(item, status=status)], signals,
                now=NOW, previous=result.memory).warnings)

    def test_changed_date_invalidates_old_stock_and_deduplicates_notice(self):
        item = event(); changed = replace(item, starts_at='2026-11-02T19:00:00+11:00')
        result = a.reduce_availability([changed], [seller(item)], now=NOW)
        self.assertEqual(result.views[0].freshness, 'event-date-changed')
        self.assertEqual([w.kind for w in result.warnings], ['event-date-changed'])
        self.assertFalse(a.reduce_availability([changed], [seller(item)], now=NOW, previous=result.memory).warnings)
        self.assertEqual(a.reduce_availability([changed], [seller(changed)], now=NOW,
            previous=result.memory).warnings[0].kind, 'seller-low')

    def test_equivalent_offset_is_not_a_date_change(self):
        item = event()
        result = a.reduce_availability([item], [seller(item, event_start='2026-11-01T08:00:00+00:00')], now=NOW)
        self.assertEqual(result.views[0].freshness, 'fresh')

    def test_allocation_sold_out_does_not_mean_whole_event_sold_out(self):
        item = event()
        scope = a.AvailabilityScope(item.key, 'session-a', 'tier-a', 'seller-allocation-a')
        result = a.reduce_availability([item], [seller(item, a.SellerStatus.SOLD_OUT, scope=scope),
            seller(item, a.SellerStatus.AVAILABLE)], now=NOW)
        warning, = result.warnings
        self.assertEqual(warning.scope.level, 'allocation')
        self.assertIn('seller-allocation-a', warning.message)
        self.assertIn('tier-a', warning.message)
        self.assertEqual(result.views[1].signal.status, a.SellerStatus.AVAILABLE)

    def test_sessions_tiers_and_resale_have_independent_warning_memory(self):
        item = event()
        scopes = [a.AvailabilityScope(item.key, session_id='a'), a.AvailabilityScope(item.key, session_id='b'),
                  a.AvailabilityScope(item.key, tier_id='a'), a.AvailabilityScope(item.key, market='resale')]
        result = a.reduce_availability([item], [seller(item, scope=s) for s in scopes], now=NOW)
        self.assertEqual({w.scope for w in result.warnings}, set(scopes))
        self.assertEqual(len(result.memory), 4)

    def test_seller_evidence_is_visible_and_never_a_stock_count(self):
        item = event(); result = a.reduce_availability([item], [seller(item)], now=NOW)
        warning, = result.warnings
        self.assertEqual((warning.source, warning.reference, warning.checked_at, warning.confidence),
                         (item.source_id, item.official_url, FETCHED, .9))
        self.assertIs(result.views[0].signal.evidence, item.evidence[0])
        self.assertNotIn('count', vars(result.views[0].signal))

    def test_estimate_never_becomes_seller_status_and_has_a_bound(self):
        item = event(); signal = risk(item)
        result = a.reduce_availability([item], [signal], now=NOW)
        self.assertEqual(result.warnings[0].kind, 'estimated-risk')
        self.assertIn('not seller stock', result.warnings[0].message)
        for changes in ({'score': .76}, {'score': float('nan')}, {'confidence': .51}, {'score': True}, {'score': -1}):
            with self.subTest(changes=changes), self.assertRaises(SourceError):
                replace(signal, **changes)

    def test_prior_jitter_does_not_accumulate_or_repeat(self):
        item = event(); signal = risk(item)
        first = a.reduce_availability([item], [signal], now=NOW)
        second = a.reduce_availability([item], [replace(signal, score=.7)], now=NOW, previous=first.memory)
        self.assertEqual(second.warnings, ())
        lower = a.reduce_availability([item], [replace(signal, score=.2)], now=NOW, previous=second.memory)
        self.assertFalse(lower.warnings)
        self.assertEqual(len(a.reduce_availability([item], [signal], now=NOW, previous=lower.memory).warnings), 1)

    def test_fresh_seller_status_takes_precedence_over_same_scope_prior(self):
        item = event()
        for status in a.SellerStatus:
            result = a.reduce_availability([item], [risk(item), seller(item, status)], now=NOW)
            self.assertNotIn('estimated-risk', [w.kind for w in result.warnings])
        resale = replace(risk(item), scope=a.AvailabilityScope(item.key, market='resale'))
        self.assertEqual(a.reduce_availability([item], [seller(item, a.SellerStatus.AVAILABLE), resale],
            now=NOW).warnings[0].kind, 'estimated-risk')

    def test_exact_source_duplicates_and_out_of_order_observations(self):
        item = event()
        old = seller(item, evidence=replace(item.evidence[0], retrieved_at=(NOW-timedelta(minutes=1)).isoformat()))
        current = seller(item, a.SellerStatus.ALMOST_SOLD_OUT)
        result = a.reduce_availability([item, item], [current, old, current], now=NOW)
        self.assertEqual([w.kind for w in result.warnings], ['almost-sold-out'])
        self.assertFalse(a.reduce_availability([item], [old], now=NOW, previous=result.memory).warnings)

    def test_conflicting_same_time_observations_require_resolution(self):
        item = event()
        with self.assertRaises(SourceError):
            a.reduce_availability([item], [seller(item), seller(item, a.SellerStatus.SOLD_OUT)], now=NOW)

    def test_past_and_unknown_dates_do_not_emit_scarcity(self):
        item = event()
        for changed in (replace(item, starts_at='2020-01-01T08:00:00+00:00'),
                        replace(item, starts_at=None, date_precision='unknown')):
            result = a.reduce_availability([changed], [seller(item)], now=NOW)
            self.assertFalse(result.warnings)
            self.assertNotEqual(result.views[0].freshness, 'fresh')

    def test_invalid_inputs_and_budgets_fail_before_output(self):
        item = event()
        for changes in ({'status': 'seller-low'}, {'confidence': float('inf')}, {'max_age_seconds': 86401},
                        {'event_start': None}, {'evidence': replace(item.evidence[0], retrieved_at='unknown')}):
            with self.subTest(changes=changes), self.assertRaises(SourceError):
                seller(item, **changes)
        for values in ([seller(item)] * 101, (s for s in [seller(item)]), [object()]):
            with self.assertRaises(SourceError):
                a.reduce_availability([item], values, now=NOW)
        with self.assertRaises(SourceError):
            a.reduce_availability([item], [seller(event('unknown'))], now=NOW)
        with self.assertRaises(SourceError):
            a.reduce_availability([item], [], now=NOW.replace(tzinfo=None))

    def test_wrong_event_evidence_and_fixture_to_source_mixing_rejected(self):
        item = event()
        for evidence in (event('different').evidence[0], replace(item.evidence[0], provenance='supplied-snapshot')):
            with self.assertRaises(SourceError):
                a.reduce_availability([item], [seller(item, evidence=evidence)], now=NOW)

    def test_fresh_cancellation_is_not_hidden_by_old_stock_observation(self):
        item = event()
        old = replace(item.evidence[0], retrieved_at=(NOW-timedelta(hours=1)).isoformat())
        result = a.reduce_availability([replace(item, status='cancelled')], [seller(item, evidence=old)], now=NOW)
        self.assertEqual([w.kind for w in result.warnings], ['cancelled'])
        self.assertIsNone(result.warnings[0].confidence)  # Event evidence has no confidence assertion.

    def test_memory_budget_fails_instead_of_eviction_and_repeat_warning(self):
        item = event()
        memory = [a.WarningMemory(a.AvailabilityScope(f'fixture:event:{i}'), 'seller', ('seller-low',), FETCHED)
                  for i in range(a.MAX_MEMORY)]
        with self.assertRaises(SourceError):
            a.reduce_availability([item], [seller(item)], now=NOW, previous=memory)


class OccurrenceSellerBindingTests(TestCase):
    def test_seller_for_other_occurrence_with_same_url_is_rejected(self):
        from shelf.event_sources import occurrence_event
        first = occurrence_event(event())
        second = occurrence_event(replace(event(), source_start='2026-11-01T21:00:00+11:00',
                                         starts_at='2026-11-01T21:00:00+11:00'))
        signal = seller(first, scope=a.AvailabilityScope(second.key))
        with self.assertRaises(SourceError):
            a.reduce_availability([first, second], [signal], now=NOW)

    def test_fresh_receipt_and_partial_mixed_source_mode_or_locator_mismatches(self):
        from shelf.event_sources import occurrence_event
        item = occurrence_event(event())
        evidence = replace(item.evidence[0], retrieved_at=(NOW + timedelta(seconds=1)).isoformat(),
                           snapshot_sha256='f'*64)
        valid = seller(item, evidence=evidence)
        self.assertEqual(len(a.reduce_availability([item], [valid], now=NOW + timedelta(seconds=1)).views), 1)
        for wrong in (replace(evidence, occurrence_ref='different'),
                      replace(evidence, occurrence_basis='explicit_date'),
                      replace(evidence, occurrence_ref=None), replace(evidence, occurrence_basis=None),
                      replace(evidence, occurrence_ref=None, occurrence_basis=None),
                      replace(evidence, source_id='other'), replace(evidence, provenance='live-automated'),
                      replace(evidence, official_url='https://events.unimelb.edu.au/else', event_id='else')):
            with self.subTest(wrong=wrong), self.assertRaises(SourceError):
                a.reduce_availability([item], [replace(valid, evidence=wrong)], now=NOW)
        legacy = event()
        with self.assertRaises(SourceError):
            a.reduce_availability([legacy], [seller(legacy, evidence=evidence)], now=NOW)
