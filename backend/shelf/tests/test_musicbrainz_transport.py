"""Bounded process doubles and private temporary limiter state; no HTTP sends."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch
import json
import os
import subprocess

from shelf import discovery_transport as transport
from shelf.discovery_sources import SourceError
from shelf.musicbrainz_source import MusicBrainzOperation, ARTIST_SOURCE
from .test_musicbrainz_source import IDENTITY
from .test_source_evidence import NOW


class MusicBrainzTransportTests(TestCase):
    def setUp(self):
        self.guard = patch.object(transport.subprocess, 'Popen', side_effect=AssertionError('Unmocked spawn'))
        self.guard.start(); self.addCleanup(self.guard.stop)
        self.slot = Mock(); self.slot.current.return_value = True
        self.reserve = patch.object(transport.MediaHostLimiter, 'reserve', return_value=self.slot)
        self.reserve_mock = self.reserve.start(); self.addCleanup(self.reserve.stop)

    def send(self, *, raw=None, current=None, timeout=False, cleanup_failure=False, operation=None):
        raw = raw if raw is not None else b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"recordings":[]}\n200'
        child = Mock(returncode=0); child.poll.return_value = None; child.stdout.fileno.return_value = 92
        if cleanup_failure:
            child.stdout.close.side_effect = OSError
        ready = MagicMock(); ready.__enter__.return_value = ready
        ready.get_map.side_effect = [True, True, False]
        ready.select.return_value = [] if timeout else [(SimpleNamespace(), 1)]
        with patch.object(transport.subprocess, 'Popen', return_value=child) as spawn, \
             patch.object(transport.selectors, 'DefaultSelector', return_value=ready), \
             patch.object(transport.os, 'set_blocking'), \
             patch.object(transport.os, 'read', side_effect=[raw, b'']):
            try:
                result = transport.musicbrainz_authorized(operation or MusicBrainzOperation('Synthetic artist', IDENTITY),
                    revalidate=current or Mock(return_value=True), limiter=transport.MediaHostLimiter('musicbrainz'))
                return result, spawn.call_args
            finally:
                self.assertEqual(spawn.call_count, 1)
                child.kill.assert_called_once()
                child.stdout.close.assert_called_once()
                self.slot.finish.assert_called_once()

    def test_fixed_get_has_no_bearer_or_credential_channel_and_closes(self):
        result, call = self.send()
        args = call.args[0]
        self.assertEqual(result[:2], (200, b'{"recordings":[]}'))
        self.assertEqual(args[args.index('--request') + 1], 'GET')
        self.assertEqual(args[args.index('--url') + 1],
            'https://musicbrainz.org/ws/2/recording/?query=artistname%3A%22Synthetic+artist%22&fmt=json&limit=9&offset=0')
        self.assertEqual(args[args.index('--user-agent') + 1], IDENTITY)
        self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(call.kwargs['env'], {'LANG': 'C', 'LC_ALL': 'C'})
        self.assertNotIn('Authorization', repr(call))
        self.assertNotIn('--data-binary', args)
        for flag, value in (('--proto', '=https'), ('--retry', '0'), ('--max-redirs', '0')):
            self.assertEqual(args[args.index(flag) + 1], value)

    def test_artist_uses_same_bounded_wire_with_distinct_fixed_path_and_query(self):
        result, call = self.send(operation=MusicBrainzOperation('Synthetic artist', IDENTITY, ARTIST_SOURCE),
            raw=b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"artists":[]}\n200')
        self.assertEqual(result[:2], (200, b'{"artists":[]}'))
        args = call.args[0]
        self.assertEqual(args[args.index('--url') + 1],
            'https://musicbrainz.org/ws/2/artist/?query=artist%3A%22Synthetic+artist%22&fmt=json&limit=9&offset=0')
        self.assertEqual(args[args.index('--max-time') + 1], '5')
        self.assertEqual(args[args.index('--max-redirs') + 1], '0')
        self.assertEqual(args[args.index('--retry') + 1], '0')
        self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)
        self.assertNotIn('Authorization', repr(call))

    def test_wrong_operation_limiter_bounds_or_stale_precheck_never_spawn(self):
        op = MusicBrainzOperation('Synthetic artist', IDENTITY)
        for changes in ({'operation': SimpleNamespace(url='https://example.invalid')},
                        {'limiter': transport.MediaHostLimiter('tmdb')}, {'response_bytes': True},
                        {'response_bytes': 131073}, {'request_seconds': 6}, {'parent_seconds': 7},
                        {'revalidate': lambda: False}):
            arguments = dict(operation=op, limiter=transport.MediaHostLimiter('musicbrainz'), revalidate=lambda: True)
            arguments.update(changes)
            with self.subTest(changes=list(changes)), self.assertRaises(SourceError):
                transport.musicbrainz_authorized(**arguments)
        self.reserve_mock.assert_not_called()

    def test_redirect_compression_oversize_and_cleanup_failure_never_retry(self):
        for raw in (b'HTTP/1.1 302 Found\r\nLocation: https://example.invalid\r\n\r\n{}\n302',
                    b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n{}\n200',
                    b'x' * 140000):
            self.slot.reset_mock(); self.slot.current.return_value = True
            with self.subTest(size=len(raw)), self.assertRaises(SourceError):
                self.send(raw=raw)
        self.slot.reset_mock(); self.slot.current.return_value = True
        with self.assertRaises(SourceError):
            self.send(cleanup_failure=True)

    def test_timeout_and_revocation_after_io_close_without_returning_body(self):
        with self.assertRaises(TimeoutError):
            self.send(timeout=True)
        self.slot.reset_mock(); self.slot.current.return_value = True
        with self.assertRaises(SourceError):
            self.send(current=Mock(side_effect=[True, True, False]))

    def test_rate_status_and_only_allowlisted_headers_reach_cooldown(self):
        result, _ = self.send(raw=b'HTTP/1.1 429 Too Many Requests\r\nRetry-After: 90\r\nSet-Cookie: private=ignored\r\n\r\n{}\n429')
        self.assertEqual(result, (429, b'{}', (('retry-after', '90'),)))
        self.slot.finish.assert_called_once()
        self.assertEqual(self.slot.finish.call_args.args, (429, (('retry-after', '90'),)))


class MusicBrainzLimiterTests(TestCase):
    def test_distinct_instances_share_private_six_second_reservation(self):
        from .test_musicbrainz_source import FixtureBackend, Issuer
        from .test_source_collection import Clock
        recording = FixtureBackend(Issuer(), transport.MediaHostLimiter('musicbrainz'), clock=Clock())
        artist = FixtureBackend(Issuer(ARTIST_SOURCE), transport.MediaHostLimiter('musicbrainz'),
            clock=Clock(), source=ARTIST_SOURCE)
        real_open = os.open
        expected = f'/tmp/culture-cabinet-media-{os.geteuid()}-musicbrainz.rate'
        with TemporaryDirectory() as directory:
            target = str(Path(directory) / 'musicbrainz.rate')
            def redirected(name, flags, mode=0o777):
                self.assertEqual(name, expected)
                return real_open(target, flags, mode)
            with patch.object(transport.os, 'open', side_effect=redirected), \
                 patch.object(transport.time, 'monotonic', return_value=100) as clock:
                first = recording.limiter.reserve()
                try:
                    with self.assertRaises(transport.MoshtixRateLimited):
                        artist.limiter.reserve()
                finally:
                    first.finish(200, (), now=NOW)
                self.assertIsNone(first.fd)
                self.assertEqual(json.loads(Path(target).read_text())['not_before'], 106)
                with self.assertRaises(transport.MoshtixRateLimited):
                    artist.limiter.reserve()
                clock.return_value = 106
                second = artist.limiter.reserve()
                second.finish(429, (('retry-after', '90'),), now=NOW)
                self.assertIsNone(second.fd)
                # Artist throttling holds recording calls too; no new per-kind
                # file or quota can double the provider's host-owner rate.
                with self.assertRaises(transport.MoshtixRateLimited):
                    recording.limiter.reserve()
