"""Replay the actual HTTP journey in an ephemeral database; no server or network.

Run from this candidate: python -B scripts/demo_restaurant_plan.py .work/Author/demo
"""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
output = (root / sys.argv[1]).resolve() if len(sys.argv) == 2 else root / '.work/Author/demo'
if not output.is_relative_to(root / '.work'):
    raise SystemExit('Demo output must stay under this candidate .work directory.')
sys.path.insert(0, str(root / 'backend'))
from cabinet.boundary import prepare
prepare(['test'], os.environ)
os.environ['DJANGO_SETTINGS_MODULE'] = 'cabinet.settings'
import django
django.setup()
from django.test.runner import DiscoverRunner
from shelf.tests import test_restaurant_plans
output.mkdir(parents=True, exist_ok=True)
test_restaurant_plans.DEMO_OUTPUT = output
failed = DiscoverRunner(verbosity=2).run_tests([
    'shelf.tests.test_restaurant_plans.RestaurantPlanTests.test_review_demo_availability_alternate_evening_and_uncertain_reservation'])
print('Fictional journey artifacts:', output)
raise SystemExit(bool(failed))
