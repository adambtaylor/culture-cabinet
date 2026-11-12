"""Fixed destinations and real private reservation state; all HTTP is mocked."""
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from shelf import event_listing_transport as transport
from shelf.discovery_sources import SourceError


class ListingTransportTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.setting = patch.object(transport, '_RATE_DIRECTORY', self.directory.name)
        self.setting.start(); self.addCleanup(self.setting.stop)

    def test_fixed_bounds_same_origin_refusal_and_other_origin_independence(self):
        with patch.object(transport, '_send', return_value=(200, b'HTML')) as send:
            transport.venue303()
            with self.assertRaises(transport.ListingIntervalPending):
                transport.venue303()
            transport._read('forum', robots=True)
        self.assertEqual(send.call_count, 2)
        command = send.call_args_list[0].args[0]
        self.assertEqual(command[-1], 'https://303.net.au/gigs-events')
        for flag, value in (('--max-filesize', '524288'), ('--max-time', '5'),
                            ('--retry', '0'), ('--max-redirs', '0'), ('--proxy', '')):
            self.assertEqual(command[command.index(flag)+1], value)
        self.assertIn('--no-netrc', command)
        self.assertNotIn('--location', command)

    def test_bar_open_uses_only_fixed_miuc_path_and_shared_origin_slot(self):
        with patch.object(transport,'_send',return_value=(200,b'User-agent: *\nAllow: /\n')) as send:
            self.assertTrue(transport.observe_robots('bar_open')['allowed'])
            with self.assertRaises(transport.ListingIntervalPending):transport.bar_open()
            send.assert_called_once()
        with patch.object(transport,'_reserve'),patch.object(transport,'_send',return_value=(200,b'HTML')) as send:
            transport.bar_open()
            self.assertEqual(send.call_args.args[0][-1],'https://baropen.com.au/make-it-up-club/')


    def test_moons24_uses_fixed_target_and_shares_robots_listing_reservation(self):
        with patch.object(transport,'_send',return_value=(200,b'User-agent: *\nAllow: /\n')) as send:
            self.assertTrue(transport.observe_robots('moons24')['allowed'])
            self.assertEqual(send.call_args.args[0][-1],'https://24moons.com.au/robots.txt')
            with self.assertRaises(transport.ListingIntervalPending):transport.moons24()
            send.assert_called_once()
        with patch.object(transport,'_reserve'),patch.object(transport,'_send',return_value=(200,b'HTML')) as send:
            transport.moons24()
            self.assertEqual(send.call_args.args[0][-1],'https://24moons.com.au/events')

    def test_refused_send_still_reserves_and_unsafe_state_never_fetches(self):
        with patch.object(transport, '_send', return_value=(403, b'CANARY')) as send:
            transport.venue303()
            with self.assertRaises(transport.ListingIntervalPending):
                transport.venue303()
        send.assert_called_once()
        file, = Path(self.directory.name).iterdir()
        file.chmod(0o644)
        with patch.object(transport, '_send') as send, self.assertRaises(SourceError):
            transport.venue303()
        send.assert_not_called()

    def test_robots_and_listing_share_interval_and_stricter_delay_is_kept(self):
        with patch.dict(transport._DELAYS, {'venue303': 10}), \
             patch.object(transport, '_send', return_value=(200, b'User-agent: *\nCrawl-delay: 20\nDisallow: /private\n')):
            receipt = transport.observe_robots('venue303')
            self.assertTrue(receipt['allowed'])
            self.assertEqual(receipt['delay'], 20)
            with self.assertRaises(transport.ListingIntervalPending):
                transport.venue303()

    def test_robots_denial_and_unrecognized_html_are_not_permission(self):
        with patch.object(transport, '_reserve'), \
             patch.object(transport, '_send', return_value=(200, b'User-agent: *\nDisallow: /gigs-events\n')):
            self.assertFalse(transport.observe_robots('venue303')['allowed'])
        with patch.object(transport, '_reserve'), \
             patch.object(transport, '_send', return_value=(200, b'<html>challenge</html>')):
            with self.assertRaises(SourceError): transport.observe_robots('venue303')

    def test_stricter_policy_is_visible_to_a_fresh_process(self):
        import subprocess, sys
        with patch.dict(transport._DELAYS, {'venue303': 10}), patch.object(transport, '_send',
                return_value=(200, b'User-agent: *\nCrawl-delay: 20\n')):
            transport.observe_robots('venue303')
        code = ('import sys\nsys.path.insert(0,sys.argv[1])\n'
                'from shelf import event_listing_transport as t\nt._RATE_DIRECTORY=sys.argv[2]\n'
                'try:t._reserve("venue303")\nexcept t.ListingIntervalPending:sys.exit(0)\n'
                'else:sys.exit(9)\n')
        run = subprocess.run([sys.executable, '-B', '-c', code,
            str(Path(transport.__file__).resolve().parents[1]), self.directory.name],
            capture_output=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr.decode())

    def test_unverified_forum_policy_refuses_before_reservation_or_network(self):
        from shelf.event_scraping import collect
        from shelf.tests.test_event_sources import NOW
        with patch.object(transport, '_send') as send, patch.object(transport, '_reserve') as reserve:
            self.assertEqual(collect('forum', now=NOW).status, 'permission-required')
        send.assert_not_called()
        reserve.assert_not_called()
