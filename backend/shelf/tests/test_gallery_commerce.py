"""Exact delivered-price regressions; no seller requests or purchase actions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from unittest import TestCase

from shelf.gallery_commerce import COMPONENTS, evaluate_offer
from shelf.discovery_sources import Candidate, artwork_affordability

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def offer():
    components = {name: {'status': 'not_required', 'source_basis': 'Synthetic exact offer: not required'}
                  for name in COMPONENTS}
    components['artwork'] = {'status': 'known', 'amount': '2400', 'source_basis': 'Synthetic seller quote'}
    return {'offer_id': 'synthetic-edition-1', 'offer_url': 'https://example.test/exact-edition',
            'availability': 'available', 'currency': 'AUD', 'checked_at': NOW.isoformat(),
            'components': components}


class GalleryCommerceTests(TestCase):
    def test_decimal_context_cannot_round_total_under_the_ceiling(self):
        row = offer()
        row['components']['fees'] = {'status': 'known', 'amount': '100.01', 'source_basis': 'Synthetic quote'}
        with localcontext() as context:
            context.prec = 2
            gate = evaluate_offer(row, now=NOW)
        self.assertFalse(gate.qualified)
        self.assertEqual(gate.total_aud, '2500.01')

    def test_all_in_strict_boundary_without_rounding_or_purchase_authority(self):
        for extras, qualifies, total in [('99.99', True, '2499.99'), ('100', False, '2500.00'),
                                         ('100.01', False, '2500.01'), ('99.999', False, None),
                                         ('100.0000000000000000000000000001', False, None)]:
            row = offer()
            row['components']['fees'] = {'status': 'known', 'amount': extras, 'source_basis': 'Synthetic fee quote'}
            with self.subTest(extras=extras):
                gate = evaluate_offer(row, now=NOW)
                self.assertEqual(gate.qualified, qualifies)
                self.assertEqual(gate.total_aud, total)
                self.assertFalse(gate.purchase_authorized)

    def test_every_component_needs_a_status_and_explicit_basis(self):
        for name in COMPONENTS:
            for defect in ('missing', 'unknown', 'no_basis'):
                row = offer()
                if defect == 'missing':
                    del row['components'][name]
                elif defect == 'unknown':
                    row['components'][name] = {'status': 'unknown', 'amount': '0', 'source_basis': 'Not quoted'}
                else:
                    row['components'][name].pop('source_basis')
                with self.subTest(component=name, defect=defect):
                    gate = evaluate_offer(row, now=NOW)
                    self.assertFalse(gate.qualified)
                    self.assertIsNone(gate.total_aud)
                    self.assertTrue(any(name in reason.lower() for reason in gate.reasons))

    def test_included_tax_is_not_added_twice_but_needs_explicit_basis(self):
        row = offer()
        row['components']['tax'] = {'status': 'included', 'source_basis': 'Synthetic artwork price includes GST'}
        self.assertEqual(evaluate_offer(row, now=NOW).total_aud, '2400.00')
        row['components']['tax']['amount'] = '100'
        self.assertFalse(evaluate_offer(row, now=NOW).qualified)

    def test_invalid_costs_and_hidden_components_fail_closed(self):
        for value in (True, False, '-1', 'NaN', 'Infinity', '-Infinity', None, 'bad', 2.5,
                      Decimal('NaN'), '1e999999', '1e-999999'):
            row = offer()
            row['components']['artwork']['amount'] = value
            with self.subTest(value=value):
                self.assertFalse(evaluate_offer(row, now=NOW).qualified)
        for currency in ('USD', None, 'aud'):
            row = offer()
            row['currency'] = currency
            self.assertFalse(evaluate_offer(row, now=NOW).qualified)
        row = offer()
        row['components']['tax']['currency'] = 'USD'
        self.assertFalse(evaluate_offer(row, now=NOW).qualified)
        row = offer()
        row['components']['surcharge'] = {'status': 'unknown'}
        self.assertFalse(evaluate_offer(row, now=NOW).qualified)

    def test_exact_available_current_offer_required_and_inputs_unchanged(self):
        for key, value in [('availability', 'sold_out'), ('availability', 'unknown'),
                           ('offer_id', ''), ('offer_url', None), ('checked_at', None),
                           ('checked_at', (NOW - timedelta(minutes=16)).isoformat()),
                           ('checked_at', (NOW + timedelta(seconds=1)).isoformat()),
                           ('checked_at', '2026-10-31T12:00:00')]:
            row = offer()
            row[key] = value
            before = deepcopy(row)
            with self.subTest(key=key, value=value):
                self.assertFalse(evaluate_offer(row, now=NOW).qualified)
                self.assertEqual(row, before)
        self.assertFalse(evaluate_offer(None, now=NOW).qualified)

    def test_item_price_does_not_imply_all_in_qualification(self):
        card = Candidate('synthetic', 'Synthetic print', 'event', 'fixture', 'synthetic', 1, None,
                         artwork_price='2499.99', artwork_currency='AUD', price_per='individual artwork')
        self.assertEqual(artwork_affordability('2499.99', 'AUD', per='individual artwork'), 'within budget')
        self.assertIn('item price only', card.artwork_label())
        self.assertIn('all-in cost not qualified', card.artwork_label())
