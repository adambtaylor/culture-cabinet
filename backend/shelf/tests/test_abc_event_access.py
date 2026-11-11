"""ABC's shared-origin allowance is not an AI-session source permission."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from django.test import TestCase as DjangoCase,override_settings
from shelf import event_listing_transport as transport,event_scraping
from shelf.models import DiscoveryState
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests
from shelf.tests.test_discovery_transport import NOW


class ABCEventAccessTests(TestCase):
    def test_stricter_robots_delay_is_shared_across_both_sources(self):
        with TemporaryDirectory() as directory,patch.object(transport,'_RATE_DIRECTORY',directory),\
             patch.object(transport,'_DELAYS',dict(transport._DELAYS)),\
             patch.object(transport,'LISTING_ACCESS',dict(transport.LISTING_ACCESS,triple_j=True,double_j=True)),\
             patch.object(transport,'_send',return_value=(200,b'User-agent: *\nCrawl-delay: 25\nAllow: /\n')) as send:
            transport.observe_robots('triple_j')
            for source in ('triple_j','double_j'):
                with self.assertRaises(transport.ListingIntervalPending):transport._read(source)
            paths=list(Path(directory).iterdir())
            self.assertEqual(len(paths),1);self.assertTrue(paths[0].name.endswith('-abc.rate'))
            self.assertEqual(json.loads(paths[0].read_text())['delay'],25)
            send.assert_called_once()

    def test_fixed_source_targets_and_existing_receipt_names_remain_distinct(self):
        with TemporaryDirectory() as directory,patch.object(transport,'_RATE_DIRECTORY',directory),\
             patch.object(transport,'LISTING_ACCESS',dict(transport.LISTING_ACCESS,triple_j=True,double_j=True)),\
             patch.object(transport,'_send',return_value=(200,b'HTML')) as send:
            transport.triple_j()
            with self.assertRaises(transport.ListingIntervalPending):transport.double_j()
            transport.venue303()
            self.assertEqual(send.call_count,2)
            self.assertEqual({path.name.rsplit('-',1)[-1] for path in Path(directory).iterdir()},
                {'abc.rate','venue303.rate'})
        with patch.object(transport,'_reserve'),\
             patch.object(transport,'LISTING_ACCESS',dict(transport.LISTING_ACCESS,triple_j=True,double_j=True)),\
             patch.object(transport,'_send',return_value=(200,b'HTML')) as send:
            transport.double_j()
            self.assertEqual(send.call_args.args[0][-1],'https://www.abc.net.au/listen/doublej/touring')

    def test_current_access_gates_refuse_before_reservation_or_network(self):
        with patch.object(transport,'_reserve') as reserve,patch.object(transport,'_send') as send:
            for source in ('triple_j','double_j'):
                with self.assertRaises(transport.ListingPolicyUnavailable):transport._read(source)
                outcome=event_scraping.collect(source,now=NOW)
                self.assertEqual(outcome.status,'permission-required');self.assertEqual(outcome.candidates,())
            reserve.assert_not_called();send.assert_not_called()


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class ABCEventAccessUITests(DjangoCase):
    setUp=PublicDiscoveryUITests.setUp
    form=PublicDiscoveryUITests.form

    def test_normal_source_actions_replay_render_without_acquisition(self):
        with patch.object(transport,'_send') as send:
            for source in ('triple_j','double_j'):
                data=self.form('event_source',source_id=source)
                self.assertEqual(self.client.post(self.action,data).status_code,302)
                self.assertEqual(self.client.post(self.action,data).status_code,302)
            page=self.client.get(self.url)
            self.assertContains(page,'AI-agent access restrictions')
            self.assertEqual([row['outcome'].status for row in page.context['public_results']],
                ['permission-required','permission-required'])
            self.assertEqual(DiscoveryState.objects.get().history,[]);send.assert_not_called()
