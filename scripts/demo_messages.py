"""Exercise real request handlers in an ephemeral DB; export inert fixture screens."""
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
from shelf.tests import test_messages
output.mkdir(parents=True, exist_ok=True)
test_messages.DEMO_OUTPUT = output
failed = DiscoverRunner(verbosity=2).run_tests([
    'shelf.tests.test_messages.MessageTests.test_demo_invitation_unknown_reconciliation_and_selected_reply'])
print('Inert fictional message journey:', output)
raise SystemExit(bool(failed))
