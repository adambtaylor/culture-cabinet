"""Local invented stimuli exercise production guards, with no simulated IO stack."""
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from unittest import TestCase
from unittest.mock import patch

from shelf.tmdb_contract import (
    AdapterResult, Clock, Coverage, Cursor, Decision, DiscoverQuery,
    PolicyEvidence, SourceIdentity, TmdbBudget, TmdbPolicy, VideosRequest,
)
from shelf.tmdb_transport import (
    NoFetch, fetch_tmdb, guard_response, safe_diagnostics,
    validate_destination_addresses, validate_request,
)

NOW = datetime(2026, 10, 27, tzinfo=UTC)
JSON_HEADERS = (b'Content-Type: application/json\r\n', b'\r\n')


class TmdbTransportTests(TestCase):
    def setUp(self):
        self.seconds = 0.0
        self.utc = NOW
        self.clock = Clock(lambda: self.seconds, lambda: self.utc)
        self.budget = TmdbBudget(clock=self.clock, fictional=True)
        allow = PolicyEvidence('allow', NOW, 'invented-only')
        self.policy = TmdbPolicy('fictional-v1', operations={'fetch': allow, 'transient_processing': allow})

    def fixed(self, media='movie', budget=None, cursor=None):
        return validate_request(DiscoverQuery(media, cursor), budget=budget or self.budget)

    def guard(self, status=200, headers=JSON_HEADERS, chunks=(b'{}',), *, request=None, budget=None):
        budget = budget or self.budget
        request = request or self.fixed(budget=budget)
        attempt = budget.admit(request, clock=self.clock)
        self.assertNotIsInstance(attempt, Decision)
        return guard_response(status, headers, chunks, request=request, attempt=attempt,
            clock=self.clock, budget=budget, fictional=True)

    def fresh_guard(self, status=200, headers=JSON_HEADERS, chunks=(b'{}',)):
        return self.guard(status, headers, chunks, budget=TmdbBudget(clock=self.clock, fictional=True))

    def assert_reason(self, result, state, reason, complete=False):
        self.assertEqual((result.state, result.reason), (state, reason))
        self.assertEqual(result.checked_at is not None, complete)
        if state != 'ready':
            self.assertEqual(result.body_bytes, b'')

    def test_fixed_discovery_templates_and_query_vocabulary(self):
        for media in ('movie', 'tv'):
            fixed = self.fixed(media)
            self.assertEqual(fixed.url, 'https://api.themoviedb.org:443/3/discover/' + media
                + '?include_adult=false&language=en-US&page=1&sort_by=popularity.desc')
            self.assertNotIn(fixed.url, repr(fixed))
        for request in (DiscoverQuery('film'), DiscoverQuery('movie', language='xx'),
                        DiscoverQuery('movie', include_adult=0), DiscoverQuery('movie', sort_by='other'),
                        'https://attacker.invalid', {'media': 'movie'}):
            self.assertEqual(validate_request(request, budget=self.budget), Decision('blocked', 'invalid_request'))

    def test_same_job_released_parent_required_for_videos(self):
        identity = SourceIdentity('movie', 900000001, True)
        self.assertEqual(validate_request(VideosRequest(identity), budget=self.budget), Decision('blocked', 'invalid_request'))
        fixed = self.fixed()
        self.budget.admit(fixed, clock=self.clock)
        self.budget.record_page(fixed, (identity,), page_valid=False)
        self.assertEqual(validate_request(VideosRequest(identity), budget=self.budget), Decision('blocked', 'invalid_request'))
        self.budget.record_page(fixed, (identity,), page_valid=True)
        video = validate_request(VideosRequest(identity), budget=self.budget)
        self.assertEqual(video.url, 'https://api.themoviedb.org:443/3/movie/900000001/videos?language=en-US')
        other = TmdbBudget(clock=self.clock, fictional=True)
        self.assertEqual(validate_request(VideosRequest(identity), budget=other), Decision('blocked', 'invalid_request'))
        self.assertEqual(validate_request(VideosRequest(SourceIdentity('tv', identity.id, True)), budget=self.budget),
                         Decision('blocked', 'invalid_request'))

    def test_cursor_job_media_query_single_use_and_expiry(self):
        first = self.fixed()
        self.budget.admit(first, clock=self.clock)
        self.assertIsNone(self.budget.issue_cursor(first))
        self.budget.record_page(first, (), page_valid=True)
        cursor = self.budget.issue_cursor(first)
        self.assertIs(cursor, self.budget.issue_cursor(first))
        for supplied, media, budget in (
                (Cursor(cursor.job_id, cursor.query_sha256, cursor.media, 2), 'movie', self.budget),
                (cursor, 'tv', self.budget),
                (cursor, 'movie', TmdbBudget(clock=self.clock, fictional=True))):
            self.assertEqual(validate_request(DiscoverQuery(media, supplied), budget=budget), Decision('blocked', 'invalid_request'))
        second = self.fixed(cursor=cursor)
        self.assertEqual(second.page, 2)
        self.assertEqual(second.query_sha256, first.query_sha256)
        self.seconds = 1
        self.budget.admit(second, clock=self.clock)
        self.assertEqual(self.fixed(cursor=cursor), Decision('blocked', 'invalid_request'))
        self.budget.record_page(second, (), page_valid=True)
        self.assertIsNone(self.budget.issue_cursor(second))
        self.assertEqual(self.budget.admit(first, clock=self.clock), Decision('blocked', 'invalid_request'))
        expiry_budget = TmdbBudget(clock=self.clock, fictional=True)
        initial = self.fixed(budget=expiry_budget)
        expiry_budget.admit(initial, clock=self.clock)
        expiry_budget.record_page(initial, (), page_valid=True)
        expiry_cursor = expiry_budget.issue_cursor(initial)
        self.seconds += 30
        self.assertEqual(validate_request(DiscoverQuery('movie', expiry_cursor), budget=expiry_budget),
                         Decision('blocked', 'invalid_request'))

    def test_address_classification_rejects_entire_mixed_set(self):
        valid = ('8.8.8.8', '2606:4700:4700::1111')
        self.assertEqual(validate_destination_addresses(valid + valid), valid)
        self.assertEqual(validate_destination_addresses(()), Decision('blocked', 'destination_unavailable'))
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '100.100.100.200',
                        '100.64.0.1', '192.0.2.1', '192.0.0.9', '192.0.0.10', '192.88.99.1',
                        '192.31.196.1', '192.52.193.1', '192.175.48.1', '2001:20::1',
                        '2620:4f:8000::1', '224.0.0.1', '0.0.0.0', '::', '::1',
                        'fe80::1%eth0', '::ffff:8.8.8.8', '2001:db8::1', 'fc00::1',
                        '2130706433', '0177.0.0.1', '0x7f000001', '8.008.8.8',
                        '2606:4700:4700:0:0:0:0:1111', 'API.themoviedb.org', '8.8.8.8 '):
            with self.subTest(address=address):
                self.assertEqual(validate_destination_addresses(valid + (address,)), Decision('quarantined', 'unsafe_destination'))

    def test_nofetch_denial_sentinels_and_zero_budget_consumption(self):
        fixed = self.fixed()
        with ExitStack() as stack:
            sentinels = [stack.enter_context(patch(name, side_effect=AssertionError('forbidden interaction')))
                for name in ('socket.getaddrinfo', 'socket.socket', 'subprocess.Popen',
                             'os.getenv', 'os._Environ.__getitem__', 'builtins.open')]
            self.assertEqual(NoFetch().send(fixed), Decision('blocked', 'transport_unavailable'))
            result = fetch_tmdb(DiscoverQuery('movie'), policy=self.policy, clock=self.clock, budget=self.budget)
            self.assert_reason(result, 'blocked', 'transport_unavailable')
            for sentinel in sentinels:
                sentinel.assert_not_called()
        self.assertEqual(self.budget.attempts, ())
        self.assertEqual(self.budget.body_bytes_count, 0)

    def test_preflight_policy_precedes_nofetch_and_admission(self):
        for policy, reason in ((replace(self.policy, source_off=True, paused=True), 'source_off'),
                               (replace(self.policy, paused=True), 'paused'),
                               (TmdbPolicy('unknown'), 'rights_unknown'),
                               (replace(self.policy, operations={'fetch': PolicyEvidence('deny')}), 'rights_denied'),
                               (replace(self.policy, operations={'fetch': PolicyEvidence('allow')}), 'rights_unknown')):
            result = fetch_tmdb(DiscoverQuery('movie'), policy=policy, clock=self.clock, budget=self.budget)
            self.assert_reason(result, 'blocked', reason)
        invalid = fetch_tmdb(DiscoverQuery('invalid'), policy=replace(self.policy, source_off=True),
                             clock=self.clock, budget=self.budget)
        self.assert_reason(invalid, 'blocked', 'invalid_request')
        self.assertTrue(invalid.fictional)
        self.assertEqual(self.budget.attempts, ())
        class ForbiddenSupplier:
            def send(inner, request):
                self.fail('A fictional stimulus seam cannot activate live transport')
        live_budget = TmdbBudget(clock=self.clock)
        result = fetch_tmdb(DiscoverQuery('movie'), policy=replace(self.policy, fictional_only=False),
                            clock=self.clock, budget=live_budget, transport=ForbiddenSupplier())
        self.assert_reason(result, 'blocked', 'transport_unavailable')
        self.assertEqual(live_budget.attempts, ())

    def test_injected_supplier_calls_production_guard_once(self):
        calls = []
        class Stimulus:
            def __init__(inner, status=200): inner.status = status
            def send(inner, request):
                calls.append(request)
                return inner.status, JSON_HEADERS, (b'{', b'}')
        result = fetch_tmdb(DiscoverQuery('movie'), policy=self.policy, clock=self.clock,
                            budget=self.budget, transport=Stimulus())
        self.assert_reason(result, 'ready', 'response_ready', True)
        self.assertEqual(result.body_bytes, b'{}')
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.body_bytes_count, 2)
        self.assertEqual(result.attempted_at, NOW)
        self.seconds = 1
        redirect = fetch_tmdb(DiscoverQuery('movie'), policy=self.policy, clock=self.clock,
                              budget=self.budget, transport=Stimulus(302))
        self.assert_reason(redirect, 'blocked', 'redirect_disallowed', True)
        self.assertEqual(len(calls), 2)

    def test_reported_failures_are_fixed_mapping_and_arbitrary_exceptions_are_opaque(self):
        class Report:
            def __init__(inner, supplied): inner.supplied = supplied
            def send(inner, request):
                if isinstance(inner.supplied, Exception): raise inner.supplied
                return inner.supplied
        for supplied, expected in ((Decision('failed', 'request_timeout'), 'request_timeout'),
                (Decision('failed', 'tls_failure'), 'tls_failure'),
                (Decision('failed', 'transport_failure'), 'transport_failure'),
                (RuntimeError('FICTIONAL_SECRET_CANARY'), 'transport_failure'),
                (TimeoutError('FICTIONAL_SECRET_CANARY'), 'transport_failure'),
                (Decision('failed', 'FICTIONAL_SECRET_CANARY'), 'transport_failure')):
            self.seconds += 1
            result = fetch_tmdb(DiscoverQuery('movie'), policy=self.policy, clock=self.clock,
                                budget=self.budget, transport=Report(supplied))
            self.assert_reason(result, 'failed', expected)
            self.assertNotIn('FICTIONAL_SECRET_CANARY', repr(result) + json.dumps(safe_diagnostics(result)))
        self.assertEqual(len(self.budget.attempts), 6)

    def test_spacing_quota_failed_attempts_and_admission_precedence(self):
        first = self.fixed()
        self.budget.admit(first, clock=self.clock)
        self.seconds = .999
        self.assertEqual(self.budget.admit(self.fixed('tv'), clock=self.clock), Decision('limited', 'pacing'))
        for second in range(1, 6):
            self.seconds = second
            self.assertNotIsInstance(self.budget.admit(self.fixed('tv' if second % 2 else 'movie'), clock=self.clock), Decision)
        self.assertEqual(self.budget.admit(self.fixed(), clock=self.clock), Decision('limited', 'request_budget'))
        self.seconds = 6
        self.assertEqual(self.budget.admit(self.fixed(), clock=self.clock), Decision('limited', 'request_budget'))
        self.seconds = 30
        self.assertEqual(self.budget.admit(replace(first, page=3), clock=self.clock), Decision('limited', 'job_deadline'))

    def test_pagination_and_video_request_caps(self):
        first = self.fixed()
        self.assertEqual(self.budget.admit(replace(first, page=3), clock=self.clock), Decision('limited', 'pagination_limit'))
        self.budget.admit(first, clock=self.clock)
        identity = SourceIdentity('movie', 900000001, True)
        self.budget.record_page(first, (identity,), page_valid=True)
        video = validate_request(VideosRequest(identity), budget=self.budget)
        for second in (1, 2):
            self.seconds = second
            video = validate_request(VideosRequest(identity), budget=self.budget)
            self.assertNotIsInstance(self.budget.admit(video, clock=self.clock), Decision)
            self.budget.record_page(video, (), page_valid=True)
        self.assertEqual(self.budget.admit(video, clock=self.clock), Decision('limited', 'video_request_limit'))
        self.budget.record_page(video, (), page_valid=True)
        self.budget.record_page(video, (), page_valid=True)
        other_video = validate_request(VideosRequest(identity), budget=self.budget)
        # record_page accepts only requests actually admitted by this job.
        with self.assertRaises(ValueError):
            self.budget.record_page(other_video, (), page_valid=True)
        self.assertEqual(self.budget.pages_completed(video), 2)

    def test_request_job_deadline_equality_and_utc_independence(self):
        attempt = self.budget.admit(self.fixed(), clock=self.clock)
        self.utc += timedelta(days=200)
        self.seconds = 9.999
        self.assertIsNone(self.budget.check_deadline(attempt=attempt, clock=self.clock))
        self.seconds = 10
        self.assertEqual(self.budget.check_deadline(attempt=attempt, clock=self.clock), Decision('failed', 'request_timeout'))
        self.seconds = 30
        self.assertEqual(self.budget.check_deadline(attempt=attempt, clock=self.clock), Decision('limited', 'job_deadline'))
        other = TmdbBudget(clock=self.clock, fictional=True)
        self.assertEqual(other.check_deadline(attempt=attempt, clock=self.clock), Decision('blocked', 'invalid_request'))

    def test_header_byte_and_field_inclusive_boundaries(self):
        base = b'Content-Type: application/json\r\n'
        header = b'X: ' + b'a' * (16384 - len(base) - 7) + b'\r\n'
        self.assertEqual(len(base + header + b'\r\n'), 16384)
        self.assert_reason(self.fresh_guard(headers=(base, header, b'\r\n')), 'ready', 'response_ready', True)
        self.assert_reason(self.fresh_guard(headers=(base, header[:-2] + b'a\r\n', b'\r\n')), 'failed', 'header_limit')
        fields = (base,) + (b'X: a\r\n',) * 63 + (b'\r\n',)
        self.assert_reason(self.fresh_guard(headers=fields), 'ready', 'response_ready', True)
        self.assert_reason(self.fresh_guard(headers=(base,) + (b'X: a\r\n',)*64 + (b'\r\n',)), 'failed', 'header_limit')

    def test_header_syntax_critical_duplicates_status_and_termination(self):
        variants = [(b'Bad Header: x\r\n', b'\r\n'), (b' folded: x\r\n', b'\r\n'),
                    (b'X: a\rb\r\n', b'\r\n'), (b'X: a\tb\r\n', b'\r\n'),
                    (b'X: \xff\r\n', b'\r\n'), (b'X: x\n', b'\r\n'),
                    (b'X: x\r\n',), (b'\r\n', b'X: x\r\n'), ('X: x',)]
        for name in (b'Content-Type', b'Content-Encoding', b'Content-Length', b'Location', b'Retry-After'):
            variants.append((name+b': x\r\n', name.lower()+b': y\r\n', b'\r\n'))
        for headers in variants:
            self.assert_reason(self.fresh_guard(headers=headers), 'failed', 'malformed_headers')
        for status in (True, '200', 99, 600):
            self.assert_reason(self.fresh_guard(status=status), 'failed', 'malformed_headers')

    def test_body_and_job_byte_inclusive_limits_count_overflow(self):
        result = self.guard(chunks=(b'', b'a'*131072, b'', b'b'*131072, b''))
        self.assert_reason(result, 'ready', 'response_ready', True)
        self.assertEqual(result.body_bytes_count, 262144)
        over = self.fresh_guard(chunks=(b'a'*262144, b'b'))
        self.assert_reason(over, 'limited', 'response_byte_limit')
        self.assertEqual(over.body_bytes_count, 262145)
        for second in (1, 2, 3):
            self.seconds = second
            self.assert_reason(self.guard(chunks=(b'a'*262144,)), 'ready', 'response_ready', True)
        self.assertEqual(self.budget.body_bytes_count, 1048576)
        self.seconds = 4
        aggregate = self.guard(chunks=(b'a'*262145,))
        self.assert_reason(aggregate, 'limited', 'job_byte_limit')
        self.assertEqual(self.budget.body_bytes_count, 1048576 + 262145)

    def test_no_read_past_first_overflow_or_malformed_chunk(self):
        def chunks():
            yield b'a'*262145
            self.fail('Read after overflow')
        self.assert_reason(self.fresh_guard(chunks=chunks()), 'limited', 'response_byte_limit')
        self.assert_reason(self.fresh_guard(chunks=(b'a', 'bad')), 'failed', 'malformed_headers')

    def test_missing_lying_and_invalid_content_length(self):
        self.assert_reason(self.fresh_guard(), 'ready', 'response_ready', True)
        for length, valid in ((b'2', True), (b'0002', True), (b'1', False), (b'3', False),
                              (b'-1', False), (b'+2', False), (b'2,2', False), (b'', False)):
            headers = (JSON_HEADERS[0], b'Content-Length: '+length+b'\r\n', b'\r\n')
            self.assert_reason(self.fresh_guard(headers=headers), 'ready' if valid else 'failed',
                               'response_ready' if valid else 'malformed_headers', valid)
        self.assert_reason(self.fresh_guard(headers=(JSON_HEADERS[0], b'Content-Length: 0\r\n', b'\r\n'),
                                           chunks=(b'a'*262145,)), 'limited', 'response_byte_limit')

    def test_encoding_type_and_resource_precedence(self):
        for value in (b'gzip', b'br', b'identity,gzip', b''):
            headers = (JSON_HEADERS[0], b'Content-Encoding: '+value+b'\r\n', b'\r\n')
            self.assert_reason(self.fresh_guard(headers=headers), 'failed', 'unsupported_content_encoding')
            self.assert_reason(self.fresh_guard(headers=headers, chunks=(b'x'*262145,)), 'limited', 'response_byte_limit')
        for value in (b'application/xml', b'text/json', b'application/json; charset=latin1', b'application/json; other=x'):
            self.assert_reason(self.fresh_guard(headers=(b'Content-Type: '+value+b'\r\n', b'\r\n')),
                               'failed', 'unsupported_content_type')
        self.assert_reason(self.fresh_guard(headers=(b'Content-Type: Application/JSON; charset=UTF-8\r\n', b'\r\n')),
                           'ready', 'response_ready', True)

    def test_304_redirect_location_and_envelope_precedence_no_follow(self):
        fixed = self.fixed()
        for location, unsafe in ((fixed.url, False),
                ('https://api.themoviedb.org/3/discover/movie?language=en-US&sort_by=popularity.desc&page=1&include_adult=false', False),
                ('http://127.0.0.1', True), ('', True),
                ('https://api.themoviedb.org.evil/3/discover/movie', True),
                (fixed.url+'#x', True), (fixed.url.replace('/3/', '/%33/'), True),
                (fixed.url+'&token=x', True), (fixed.url.replace(':443', ':444'), True)):
            headers = (JSON_HEADERS[0], ('Location: '+location+'\r\n').encode(), b'\r\n')
            self.assert_reason(self.fresh_guard(304, headers, ()), 'quarantined', 'orphan_304', True)
            for status in (200, 301, 302, 303, 307, 308, 401, 429, 500):
                self.assert_reason(self.fresh_guard(status, headers, ()),
                    'quarantined' if unsafe else 'blocked', 'unsafe_redirect' if unsafe else 'redirect_disallowed', True)
            self.assert_reason(self.fresh_guard(304, headers, (b'x'*262145,)), 'limited', 'response_byte_limit')
        for status in (301, 302, 303, 307, 308):
            self.assert_reason(self.fresh_guard(status, (b'\r\n',), ()), 'blocked', 'redirect_disallowed', True)

    def test_http_status_and_retry_after_fixed_results(self):
        cases = {401: ('blocked', 'authentication_required'), 403: ('blocked', 'access_denied'),
                 451: ('blocked', 'terms_or_legal_block'), 429: ('limited', 'provider_rate_limit'),
                 500: ('failed', 'provider_server_error'), 599: ('failed', 'provider_server_error'),
                 300: ('failed', 'unexpected_http_status'), 305: ('failed', 'unexpected_http_status'),
                 306: ('failed', 'unexpected_http_status'), 204: ('failed', 'unexpected_http_status')}
        for status, (state, reason) in cases.items():
            self.assert_reason(self.fresh_guard(status, (b'\r\n',), ()), state, reason, True)
        for value, seconds in ((b'3', 3), (b'9999999999999999999', 3600), (b'0000001', 1),
                               (b'-1', None), (b'Tue, 27 Oct 2026 00:00:00 GMT', None)):
            result = self.fresh_guard(429, (b'Retry-After: '+value+b'\r\n', b'\r\n'), ())
            self.assertEqual(result.next_allowed_at, None if seconds is None else NOW+timedelta(seconds=seconds))

    def test_deadline_on_chunk_and_after_empty_eof(self):
        def delayed():
            yield b'a'
            self.seconds = 10
            yield b'b'
        self.assert_reason(self.guard(chunks=delayed()), 'failed', 'request_timeout')
        budget = TmdbBudget(clock=self.clock, fictional=True)
        def empty():
            self.seconds += 10
            return
            yield b''
        self.assert_reason(self.guard(chunks=empty(), budget=budget), 'failed', 'request_timeout')

    def test_supplier_header_body_exceptions_have_no_details_or_checked_time(self):
        def broken():
            raise RuntimeError('FICTIONAL_SECRET_CANARY')
            yield b''
        for kwargs in ({'headers': broken()}, {'chunks': broken()}):
            result = self.fresh_guard(**kwargs)
            self.assert_reason(result, 'failed', 'transport_failure')
            self.assertNotIn('FICTIONAL_SECRET_CANARY', repr(result)+json.dumps(safe_diagnostics(result)))

    def test_raw_body_headers_and_request_internals_excluded_from_diagnostics(self):
        canary = 'FICTIONAL_SECRET_CANARY'
        result = self.fresh_guard(headers=(JSON_HEADERS[0], ('X: '+canary+'\r\n').encode(), b'\r\n'),
                                  chunks=(canary.encode(),))
        diagnostics = safe_diagnostics(result)
        for text in (repr(result), json.dumps(diagnostics)):
            self.assertNotIn(canary, text)
            self.assertNotIn('https://', text)
            self.assertNotIn('query_sha256', text)
        self.assertEqual(result.body_bytes, canary.encode())
        poisoned = replace(result, reason=canary, route_id=canary)
        self.assertNotIn(canary, json.dumps(safe_diagnostics(poisoned)))
        adapter = AdapterResult(outcome='failed', reason='malformed_json', attempted_at=NOW,
            checked_at=NOW, coverage=Coverage('discover/movie', 'movie'), rights_snapshot=self.policy)
        self.assertEqual(safe_diagnostics(adapter)['observations_count'], 0)
        self.assertNotIn('rights_snapshot', safe_diagnostics(adapter))
