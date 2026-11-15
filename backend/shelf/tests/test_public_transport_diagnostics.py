"""Safe diagnostic contracts from fake streams only; no real process or source."""
import subprocess
from unittest import TestCase
from unittest.mock import Mock, patch

from shelf import discovery_transport as transport
from shelf.discovery_sources import SourceError

CANARY = 'SYNTHETIC_PRIVATE_COMMAND_OR_BODY'


class PublicTransportDiagnosticTests(TestCase):
    def send(self, chunks=None, *, exit_code=0, read_error=None, timeout=False, wait_error=False,
             max_bytes=131072):
        chunks = [b'ok\n200', b''] if chunks is None else chunks
        child = Mock(returncode=exit_code)
        child.poll.return_value = None
        child.stdout.fileno.return_value = 91
        if wait_error:
            child.wait.side_effect = [subprocess.TimeoutExpired(CANARY, 6), None]
        ready = Mock()
        ready.__enter__ = Mock(return_value=ready)
        ready.__exit__ = Mock(return_value=False)
        ready.get_map.side_effect = [True] * len(chunks) + [False]
        ready.select.return_value = [] if timeout else [(Mock(), 1)]
        with patch.object(transport.subprocess, 'Popen', return_value=child) as spawn, \
             patch.object(transport.selectors, 'DefaultSelector', return_value=ready), \
             patch.object(transport.os, 'set_blocking'), \
             patch.object(transport.os, 'read', side_effect=read_error or chunks):
            try:
                return transport._send([CANARY], max_bytes=max_bytes)
            finally:
                self.assertEqual(spawn.call_count, 1)
                self.assertEqual(spawn.call_args.args[0], [CANARY])
                self.assertEqual(spawn.call_args.kwargs['stderr'], subprocess.DEVNULL)
                self.assertEqual(spawn.call_args.kwargs['stdin'], subprocess.DEVNULL)
                self.assertEqual(spawn.call_args.kwargs['env'], {'LANG': 'C', 'LC_ALL': 'C'})
                child.kill.assert_called_once()
                child.stdout.close.assert_called_once()

    def assert_safe(self, error, reason, exit_code=None):
        self.assertIsInstance(error, SourceError)
        self.assertIs(type(error), transport.PublicTransportError)
        self.assertEqual((error.reason, error.exit_code), (reason, exit_code))
        self.assertEqual(error.args, (reason,))
        self.assertNotIn(CANARY, str(error))
        self.assertNotIn(CANARY, repr(error))
        self.assertEqual(vars(error), {'reason': reason, 'exit_code': exit_code})

    def test_success_and_http_failure_wire_envelopes_are_unchanged(self):
        self.assertEqual(self.send(), (200, b'ok'))
        # HTTP refusal is a received result, not a process failure or a guessed
        # child exit code. Existing callers retain their own status semantics.
        self.assertEqual(self.send([CANARY.encode()+b'\n403', b'']), (403, CANARY.encode()))
        self.assertEqual(self.send([b'\n200', b'']), (200, b''))

    def test_child_exit_is_observed_numeric_metadata_not_an_http_or_historical_cause(self):
        for code in (6, 7, 35, 60, -9):
            with self.subTest(exit_code=code), self.assertRaises(SourceError) as caught:
                self.send([CANARY.encode(), b''], exit_code=code)
            self.assert_safe(caught.exception, 'child_exit', code)
        with self.assertRaises(SourceError) as caught:
            self.send(exit_code=256)
        self.assert_safe(caught.exception, 'child_exit')

    def test_response_limit_and_invalid_envelope_are_distinct_without_raw_output(self):
        with self.assertRaises(SourceError) as caught:
            self.send([CANARY.encode()], max_bytes=8)
        self.assert_safe(caught.exception, 'response_limit')
        for output in (CANARY.encode(), b'ok\n20', b'ok\n200x', b'ok\nABC'):
            with self.subTest(output_length=len(output)), self.assertRaises(SourceError) as caught:
                self.send([output, b''])
            self.assert_safe(caught.exception, 'invalid_envelope')

    def test_process_start_and_stream_io_failure_do_not_copy_os_text(self):
        with patch.object(transport.subprocess, 'Popen', side_effect=OSError(CANARY)), \
             self.assertRaises(SourceError) as caught:
            transport._send([CANARY])
        self.assert_safe(caught.exception, 'process_start')
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        for error in (OSError(CANARY), subprocess.SubprocessError(CANARY)):
            with self.assertRaises(SourceError) as caught:
                self.send(read_error=error)
            self.assert_safe(caught.exception, 'process_io')
            self.assertTrue(caught.exception.__suppress_context__)

    def test_parent_child_and_wait_timeouts_keep_the_original_exception_contract(self):
        for options in ({'timeout': True}, {'exit_code': 28}, {'wait_error': True}):
            with self.subTest(options=options), self.assertRaises(TimeoutError) as caught:
                self.send(**options)
            self.assertNotIsInstance(caught.exception, transport.PublicTransportError)
            self.assertEqual(caught.exception.args, ())
        with patch.object(transport.time, 'monotonic', side_effect=[100, 107]), \
             self.assertRaises(TimeoutError):
            self.send()

    def test_diagnostic_fields_are_closed_bounded_and_never_accept_caller_text(self):
        for reason, code in [(CANARY, None), ([], None), ('child_exit', True), ('child_exit', 0),
                             ('child_exit', 256), ('child_exit', -256), ('process_io', 7)]:
            with self.assertRaises(ValueError):
                transport.PublicTransportError(reason, code)
        self.assertEqual(transport.PublicTransportError.REASONS,
            {'response_limit', 'invalid_envelope', 'child_exit', 'process_start', 'process_io'})

    def test_existing_event_driver_keeps_safe_outcomes_and_does_not_retry(self):
        from datetime import datetime, timezone
        from shelf.event_scraping import collect
        for error, expected in [(transport.PublicTransportError('child_exit', 60), 'invalid'),
                                (transport.PublicTransportError('process_start'), 'invalid'),
                                (TimeoutError(), 'timeout')]:
            with patch.object(transport, 'rrr', side_effect=error) as read:
                result = collect('rrr', now=datetime(2026, 11, 11, tzinfo=timezone.utc))
            self.assertEqual(result.status, expected)
            self.assertEqual(result.candidates, ())
            self.assertNotIn(CANARY, repr(result))
            read.assert_called_once_with()
