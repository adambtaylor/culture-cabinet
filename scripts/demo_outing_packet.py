"""Render the complete outing rehearsal through actual HTTP views, no external I/O."""
import os,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
output=(root/sys.argv[1]).resolve() if len(sys.argv)==2 else root/'.work/Author/demo'
if not output.is_relative_to(root/'.work'):raise SystemExit('Output must stay in candidate scratch.')
sys.path.insert(0,str(root/'backend'))
from cabinet.boundary import prepare
prepare(['test'],os.environ)
os.environ['DJANGO_SETTINGS_MODULE']='cabinet.settings'
import django
django.setup()
from django.test.runner import DiscoverRunner
from shelf.tests import test_outing_packet
output.mkdir(parents=True,exist_ok=True)
test_outing_packet.DEMO_OUTPUT=output
raise SystemExit(bool(DiscoverRunner(verbosity=2).run_tests(['shelf.tests.test_outing_packet.OutingTests.test_review_demo_full_block_privacy_email_unknown_update_and_cancel'])))
