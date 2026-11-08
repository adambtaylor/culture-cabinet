"""Synthetic authority/HTTP ports and private limiter files; never call Apple."""
from dataclasses import replace
from datetime import timedelta
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

from shelf import apple_podcast_native as apple, discovery_transport as transport
from shelf.category_registry import KEYS
from shelf.disclosure import SCALARS, canonical_bytes
from shelf.discovery_sources import SourceError
from shelf.enhance_contract import CollectionBudget, EnhanceFault, digest
from shelf.source_adapters import (SourceAuthorityIssuer, SourcePermit, SourceReply,
                                   SourceRequest, normalize, registered_source)
from shelf.source_collection import SourceCollection
from shelf.source_evidence import FieldPolicy
from .test_source_collection import Clock, plan
from .test_source_evidence import NOW


def document():
    return {'resultCount': 1, 'results': [{'kind': 'podcast-episode',
        'trackId': 81001, 'collectionId': 91001, 'trackName': 'Synthetic episode',
        'collectionName': 'Synthetic show',
        'trackViewUrl': 'https://podcasts.apple.com/au/podcast/synthetic/id91001?i=81001&at=ignored',
        'artworkUrl600': 'https://example.invalid/MUST_NOT_FETCH',
        'episodeUrl': 'https://example.invalid/MUST_NOT_FETCH',
        'description': 'MUST_NOT_PROJECT', 'releaseDate': '2026-10-01T00:00:00Z'}]}


class Issuer(SourceAuthorityIssuer):
    """Test-only capability port; this is not native source/processor authority."""
    def __init__(self):
        self.rights = FieldPolicy('synthetic-apple-scalars', 1, NOW - timedelta(seconds=1),
            NOW + timedelta(seconds=300), (('*', ('fetch',)),) + tuple(
                (key, ('fetch', 'display', 'retain', 'model_context')) for key in sorted(SCALARS)))
        self.current, self.processor, self.generation = True, True, 0
        self.permits = {}

    def operation_for(self, request, query, *, now):
        return apple.apple_podcast_operation(request)

    def authorize(self, request, query, operation, *, now):
        key = (query.plan_ref, request.request_digest)
        if key not in self.permits:
            permit = SourcePermit(apple.SOURCE, apple.ORIGIN, 'catalogue_lookup',
                query.plan_ref, query.bindings, request.request_digest, 1, 1,
                self.rights.ref, self.rights.revision, min(query.expires_at, self.rights.expires_at),
                ('synthetic-title-parent',), self.generation)
            self.permits[key] = permit, operation, self.rights
        return self.permits[key][0]

    def revalidate(self, permit, request, query, operation, *, now):
        retained = self.permits.get((query.plan_ref, request.request_digest))
        return bool(self.current and retained and retained[0] is permit
                    and retained[1:] == (operation, self.rights) and self.rights.current(now))

    def policy(self, permit, operation, *, now):
        return self.rights

    def model_context_current(self, permit, operation, *, now):
        return self.processor


class FixtureBackend(apple.ApplePodcastBackend):
    isolated_fixture = True


class AppleOperationTests(TestCase):
    def test_one_literal_term_supplies_preview_digest_and_fixed_wire_url(self):
        term = ' Synthetic café & country=US?limit=999 '
        operation = apple.ApplePodcastOperation(term)
        url = urlsplit(operation.url)
        self.assertEqual((url.scheme, url.netloc, url.path), ('https', 'itunes.apple.com', '/search'))
        self.assertEqual(parse_qs(url.query), {'term': [term], 'media': ['podcast'],
            'entity': ['podcastEpisode'], 'country': ['AU'], 'limit': ['10']})
        self.assertEqual(operation.material['query'], url.query)
        self.assertEqual(operation.body, b'')
        self.assertEqual(operation.material['body_digest'], digest(b''))
        self.assertEqual(operation.operation_digest, digest(canonical_bytes(operation.material)))
        self.assertNotEqual(operation.operation_digest, apple.ApplePodcastOperation('other').operation_digest)
        self.assertNotIn(term, repr(operation))
        template = apple.apple_podcast_template()
        self.assertEqual((template['source'], template['operation'], template['path']),
                         (apple.SOURCE, apple.OPERATION, '/search'))
        self.assertEqual(template['fixed_parameters'], {'media': 'podcast',
            'entity': 'podcastEpisode', 'country': 'AU', 'limit': 10})

    def test_invalid_terms_foreign_source_and_nonrequest_never_form_operation(self):
        for term in ('', ' ', 'x' * 151, 'private\nnotes', '\ud800', None, ['one', 'two']):
            with self.subTest(term=repr(term)), self.assertRaises(SourceError):
                apple.ApplePodcastOperation(term)
        for request in (SimpleNamespace(source=apple.SOURCE),
                        SourceRequest('tmdb_movie', 'movie', 'search_movie', ('Synthetic',))):
            with self.subTest(request=type(request)), self.assertRaises(EnhanceFault):
                apple.apple_podcast_operation(request)

    def test_existing_parser_projects_episode_identity_not_show_audio_or_artwork(self):
        request = SourceRequest(apple.SOURCE, apple.CATEGORY, apple.OPERATION, ('Synthetic',))
        body = document()
        body['results'] += [dict(body['results'][0]), dict(body['results'][0], kind='podcast'),
                           dict(body['results'][0], trackId=81002,
                                trackViewUrl='https://example.invalid/?i=81002')]
        body['resultCount'] = len(body['results'])
        reply = SourceReply(request.request_digest, 200, canonical_bytes(body), NOW)
        cards = normalize(registered_source(apple.SOURCE), request, reply, now=NOW).cards
        self.assertEqual(len(cards), 1)
        self.assertEqual((cards[0].key, cards[0].title, cards[0].provider, cards[0].medium),
            ('apple:podcast_episode:81001', 'Synthetic episode', 'apple', apple.CATEGORY))
        self.assertEqual(cards[0].source_url,
                         'https://podcasts.apple.com/au/podcast/synthetic/id91001?i=81001')
        self.assertEqual(cards[0].facts, ())
        self.assertNotIn('MUST_NOT', repr(cards))
        for raw in (b'{', b'{"resultCount":0,"resultCount":0,"results":[]}',
                    canonical_bytes({'resultCount': 11, 'results': []})):
            with self.subTest(raw=raw), self.assertRaises((ValueError, SourceError, EnhanceFault)):
                normalize(registered_source(apple.SOURCE), request,
                    replace(reply, body=raw), now=NOW)


class AppleBackendTests(TestCase):
    def setUp(self):
        self.clock, self.issuer = Clock(), Issuer()
        self.backend = FixtureBackend(self.issuer, transport.MediaHostLimiter(apple.SOURCE), clock=self.clock)
        self.registry, self.query = plan((apple.CATEGORY,))
        self.request = SourceRequest(apple.SOURCE, apple.CATEGORY, apple.OPERATION,
                                     ('Synthetic podcast_episode',))
        self.wire = patch.object(apple, 'apple_podcast_authorized')
        self.send = self.wire.start()
        self.addCleanup(self.wire.stop)
        # Accidental production transport or legacy cache/network access fails the test.
        for name in ('shelf.podcast_sources._fetch', 'shelf.podcast_sources.search_cluster',
                     'shelf.credential_store.open_store'):
            guard = patch(name, side_effect=AssertionError('Unexpected legacy/credential access'))
            guard.start(); self.addCleanup(guard.stop)

    def authorize(self):
        return self.backend.authorize(self.request, self.query, now=NOW)

    def test_issuer_receives_its_exact_retained_preview_operation_not_an_equal_copy(self):
        retained = apple.apple_podcast_operation(self.request)
        original = self.issuer.authorize
        def authorize(request, query, operation, *, now):
            self.assertIs(operation, retained)
            return original(request, query, operation, now=now)
        with patch.object(self.issuer, 'operation_for', return_value=retained), \
             patch.object(self.issuer, 'authorize', side_effect=authorize):
            permit = self.authorize()
            self.assertTrue(self.backend.revalidate(permit, self.request, self.query, now=NOW))
        self.send.assert_not_called()

    def test_source_collection_keeps_original_observation_and_honest_all_domain_outcomes(self):
        self.registry, self.query = plan(KEYS)
        headers = (('date', NOW.strftime('%a, %d %b %Y %H:%M:%S GMT')), ('cache-control', 'no-store'))
        def wire(operation, **bounds):
            self.assertTrue(bounds['revalidate']())
            self.assertEqual(operation.term, 'Synthetic podcast_episode')
            self.clock.elapsed += 1
            return 200, canonical_bytes(document()), headers
        self.send.side_effect = wire
        collector = SourceCollection(((apple.CATEGORY, (apple.SOURCE,)),),
            {apple.SOURCE: self.backend}, clock=self.clock, isolated_fixture=True)
        result = collector.collect(self.query, self.registry, CollectionBudget())
        self.send.assert_called_once()
        self.assertEqual(tuple(row.category for row in result.outcomes), KEYS)
        outcome = next(row for row in result.outcomes if row.category == apple.CATEGORY)
        self.assertEqual((outcome.state, outcome.candidate_count), ('fresh', 1))
        self.assertTrue(all(row.state == 'unavailable' for row in result.outcomes
                            if row.category != apple.CATEGORY))
        self.assertEqual(collector.model_eligible_keys(result, self.query, self.registry),
                         frozenset({'apple:podcast_episode:81001'}))
        self.assertEqual(result.records[0]['candidate']['evidence_type'], 'fixture_not_live')
        self.assertNotIn('MUST_NOT', result._material.decode())
        self.assertEqual(collector.state.cache, {})

    def test_shorter_caller_bounds_and_original_http_headers_survive_backend(self):
        permit = self.authorize()
        def wire(operation, **bounds):
            self.assertTrue(bounds['revalidate']())
            self.assertEqual((bounds['response_bytes'], bounds['request_seconds'], bounds['parent_seconds']),
                             (4096, 0.5, 1))
            self.clock.elapsed = 2
            return 200, canonical_bytes(document()), (('age', '100'), ('cache-control', 'max-age=120'))
        self.send.side_effect = wire
        reply = self.backend.request(self.request, permit, response_bytes=4096,
                                     request_seconds=0.5, parent_seconds=1)
        self.assertEqual(reply.observed_at, NOW + timedelta(seconds=2))
        self.assertEqual(reply.headers, (('age', '100'), ('cache-control', 'max-age=120')))
        self.assertEqual(registered_source(apple.SOURCE).ttl_seconds, 900)

    def test_foreign_copied_and_wrong_generation_permits_fail_without_send(self):
        permit = self.authorize()
        with self.assertRaises(EnhanceFault):
            self.backend.policy(replace(permit), now=NOW)
        with self.assertRaises(EnhanceFault):
            self.backend.revalidate(permit, self.request, replace(self.query, plan_ref='foreign'), now=NOW)
        for change in ({'source': 'musicbrainz_recording'}, {'parent_ids': ()},
                       {'credential_generation': 1}, {'credential_generation': None}):
            issuer = Issuer()
            bad = replace(permit, **change)
            with patch.object(issuer, 'authorize', return_value=bad), self.assertRaises(EnhanceFault):
                FixtureBackend(issuer, transport.MediaHostLimiter(apple.SOURCE), clock=Clock()).authorize(
                    self.request, self.query, now=NOW)
        self.send.assert_not_called()

    def test_stale_operation_revocation_expiry_and_processor_refuse(self):
        with patch.object(self.issuer, 'operation_for', return_value=apple.ApplePodcastOperation('other')):
            with self.assertRaises(EnhanceFault):
                self.authorize()
        permit = self.authorize()
        self.issuer.current = False
        self.assertFalse(self.backend.revalidate(permit, self.request, self.query, now=NOW))
        with self.assertRaises(EnhanceFault):
            self.backend.policy(permit, now=NOW)
        self.issuer.current, self.issuer.processor = True, False
        with self.assertRaises(EnhanceFault):
            self.backend.policy(permit, now=NOW)
        with self.assertRaises(EnhanceFault):
            self.backend.policy(permit, now=permit.expires_at)
        self.send.assert_not_called()

    def test_local_only_policy_requires_no_processor_and_cannot_gain_model_permission(self):
        self.issuer.rights = replace(self.issuer.rights, permissions=tuple(
            (target, tuple(use for use in uses if use != 'model_context'))
            for target, uses in self.issuer.rights.permissions))
        self.issuer.processor = False
        permit = self.authorize()
        self.assertEqual(self.backend.policy(permit, now=NOW), self.issuer.rights)
        self.send.assert_not_called()

    def test_denied_policy_targets_or_missing_fetch_cannot_authorize(self):
        for permissions in ((('title', ('display',)),), (('*', ('fetch', 'model_context')),),
                            (('*', ('fetch',)), ('artwork', ('display',)))):
            issuer = Issuer(); issuer.rights = replace(issuer.rights, permissions=permissions)
            with self.subTest(permissions=permissions), self.assertRaises(EnhanceFault):
                FixtureBackend(issuer, transport.MediaHostLimiter(apple.SOURCE), clock=Clock()).authorize(
                    self.request, self.query, now=NOW)
        self.send.assert_not_called()

    def test_failure_is_one_shot_even_when_issuer_returns_same_permit(self):
        permit = self.authorize()
        self.send.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            self.backend.request(self.request, permit, response_bytes=131072, request_seconds=5, parent_seconds=6)
        self.assertIs(self.authorize(), permit)
        with self.assertRaises(EnhanceFault):
            self.backend.request(self.request, permit, response_bytes=131072, request_seconds=5, parent_seconds=6)
        self.send.assert_called_once()

    def test_rate_failure_maps_without_losing_http_auth_and_rate_statuses(self):
        for status in (401, 403, 429, 503):
            issuer = Issuer()
            backend = FixtureBackend(issuer, transport.MediaHostLimiter(apple.SOURCE), clock=Clock())
            permit = backend.authorize(self.request, self.query, now=NOW)
            self.send.return_value = status, b'{}', ()
            self.assertEqual(backend.request(self.request, permit, response_bytes=100,
                             request_seconds=1, parent_seconds=2).status, status)
        self.send.side_effect = transport.MoshtixRateLimited
        permit = self.authorize()
        with self.assertRaisesRegex(EnhanceFault, 'RATE_LIMITED'):
            self.backend.request(self.request, permit, response_bytes=100, request_seconds=1, parent_seconds=2)


class AppleTransportTests(TestCase):
    def setUp(self):
        self.slot = Mock(); self.slot.current.return_value = True
        self.reserve = patch.object(transport.MediaHostLimiter, 'reserve', return_value=self.slot)
        self.reservation = self.reserve.start(); self.addCleanup(self.reserve.stop)
        guard = patch.object(apple.subprocess, 'Popen', side_effect=AssertionError('Unmocked spawn'))
        self.spawn_guard = guard.start(); self.addCleanup(guard.stop)

    def send(self, *, status=200, raw=None, current=None, timeout=False, code=0,
             cleanup_failure=False, response_bytes=131072, request_seconds=5, parent_seconds=6):
        raw = raw if raw is not None else (
            f'HTTP/1.1 {status} Synthetic\r\nContent-Type: application/json\r\n\r\n{{}}\n{status}'.encode())
        child = Mock(returncode=code); child.poll.return_value = None
        child.stdout.fileno.return_value = 92
        if cleanup_failure:
            child.stdout.close.side_effect = OSError
        ready = MagicMock(); ready.__enter__.return_value = ready
        ready.get_map.side_effect = [True, True, False]
        ready.select.return_value = [] if timeout else [(SimpleNamespace(), 1)]
        with patch.object(apple.subprocess, 'Popen', return_value=child) as spawn, \
             patch.object(apple.selectors, 'DefaultSelector', return_value=ready), \
             patch.object(apple.os, 'set_blocking'), patch.object(apple.os, 'read', side_effect=[raw, b'']):
            try:
                result = apple.apple_podcast_authorized(apple.ApplePodcastOperation('Synthetic & café'),
                    revalidate=current or Mock(return_value=True), limiter=transport.MediaHostLimiter(apple.SOURCE),
                    response_bytes=response_bytes, request_seconds=request_seconds, parent_seconds=parent_seconds)
                return result, spawn.call_args
            finally:
                self.assertEqual(spawn.call_count, 1)
                child.kill.assert_called_once()
                child.stdout.close.assert_called_once()
                self.slot.finish.assert_called_once()

    def test_fixed_anonymous_get_and_smaller_bounds_match_preview_with_clean_environment(self):
        result, call = self.send(response_bytes=4096, request_seconds=2, parent_seconds=1)
        args = call.args[0]
        self.assertEqual(result[:2], (200, b'{}'))
        self.assertEqual(args[args.index('--url') + 1], apple.ApplePodcastOperation('Synthetic & café').url)
        for flag, value in (('--request', 'GET'), ('--max-time', '1'), ('--connect-timeout', '1'),
                            ('--max-filesize', '4096'), ('--proto', '=https'),
                            ('--max-redirs', '0'), ('--retry', '0'), ('--proxy', '')):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(call.kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(call.kwargs['env'], {'LANG': 'C', 'LC_ALL': 'C'})
        self.assertIn('--no-netrc', args)
        self.assertNotIn('@-', args)
        self.assertNotIn('--location', args)
        self.assertNotIn('--data-binary', args)
        self.assertNotIn('Authorization', repr(call))

    def test_untrusted_operation_limiter_or_invalid_bounds_never_reserve_or_spawn(self):
        changes = ({'operation': SimpleNamespace(url='https://example.invalid')},
                   {'limiter': transport.MediaHostLimiter('musicbrainz')},
                   {'response_bytes': True}, {'response_bytes': 131073}, {'response_bytes': 0},
                   {'request_seconds': float('nan')}, {'request_seconds': 6},
                   {'request_seconds': 10 ** 1000},
                   {'parent_seconds': float('inf')}, {'parent_seconds': 0}, {'parent_seconds': 7},
                   {'revalidate': lambda: False})
        for change in changes:
            arguments = dict(operation=apple.ApplePodcastOperation('Synthetic'),
                limiter=transport.MediaHostLimiter(apple.SOURCE), revalidate=lambda: True)
            arguments.update(change)
            with self.subTest(change=list(change)), self.assertRaises(SourceError):
                apple.apple_podcast_authorized(**arguments)
        self.reservation.assert_not_called()
        self.spawn_guard.assert_not_called()

    def test_redirect_compression_oversize_and_ambiguous_headers_refuse_without_retry(self):
        for raw in (b'HTTP/1.1 302 Found\r\nLocation: https://example.invalid\r\n\r\n{}\n302',
                    b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n{}\n200',
                    b'HTTP/1.1 200 OK\r\nAge: 1\r\nAge: 2\r\n\r\n{}\n200',
                    b'x' * 140000):
            self.slot.reset_mock(); self.slot.current.return_value = True
            with self.subTest(size=len(raw)), self.assertRaises(SourceError):
                self.send(raw=raw)

    def test_timeout_curl_failure_revocation_and_cleanup_failure_close(self):
        cases = [({'timeout': True}, TimeoutError), ({'code': 28}, TimeoutError),
                 ({'code': 60}, SourceError), ({'cleanup_failure': True}, SourceError),
                 ({'current': Mock(side_effect=[True, True, False])}, SourceError)]
        for kwargs, error in cases:
            self.slot.reset_mock(); self.slot.current.return_value = True
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(error):
                self.send(**kwargs)

    def test_auth_status_and_original_freshness_headers_pass_through_to_collector(self):
        for status in (401, 403, 429):
            self.slot.reset_mock(); self.slot.current.return_value = True
            raw = (f'HTTP/1.1 {status} Synthetic\r\nRetry-After: 90\r\nAge: 100\r\n'
                   'Cache-Control: max-age=120\r\nSet-Cookie: ignored=1\r\n\r\n{}\n' + str(status)).encode()
            result, _ = self.send(raw=raw)
            self.assertEqual(result, (status, b'{}',
                (('age', '100'), ('cache-control', 'max-age=120'), ('retry-after', '90'))))
            self.assertEqual(self.slot.finish.call_args.args, (status, result[2]))


def reserve_in_child(connection):
    """A separate process must see the same host-owner reservation/cooldown."""
    try:
        slot = transport.MediaHostLimiter(apple.SOURCE).reserve()
        slot.finish(200, (), now=NOW)
        connection.send('reserved')
    except (SourceError, OSError) as error:
        connection.send(type(error).__name__)
    finally:
        connection.close()


class AppleLimiterTests(TestCase):
    def test_cross_process_six_second_reservation_and_retry_after_are_shared(self):
        real_open = os.open
        expected = f'/tmp/culture-cabinet-media-{os.geteuid()}-apple_podcast.rate'
        with TemporaryDirectory() as directory:
            target = str(Path(directory) / 'apple.rate')
            def redirected(name, flags, mode=0o777):
                return real_open(target if name == expected else name, flags, mode)
            with patch.object(transport.os, 'open', side_effect=redirected), \
                 patch.object(transport.time, 'monotonic', return_value=100) as clock:
                slot = transport.MediaHostLimiter(apple.SOURCE).reserve()
                slot.finish(200, (), now=NOW)
                self.assertEqual(json.loads(Path(target).read_text())['not_before'], 106)
                context = multiprocessing.get_context('fork')
                receiver, sender = context.Pipe(duplex=False)
                child = context.Process(target=reserve_in_child, args=(sender,))
                child.start(); sender.close()
                try:
                    self.assertTrue(receiver.poll(3))
                    self.assertEqual(receiver.recv(), 'MoshtixRateLimited')
                    child.join(3)
                    self.assertEqual(child.exitcode, 0)
                finally:
                    if child.is_alive():
                        child.terminate(); child.join(3)
                    receiver.close()
                clock.return_value = 106
                second = transport.MediaHostLimiter(apple.SOURCE).reserve()
                second.finish(429, (('retry-after', '90'),), now=NOW)
                with self.assertRaises(transport.MoshtixRateLimited):
                    transport.MediaHostLimiter(apple.SOURCE).reserve()
                self.assertEqual(Path(target).stat().st_mode & 0o777, 0o600)

    def test_new_identity_does_not_admit_urls_or_arbitrary_provider_names(self):
        for provider in ('apple', 'itunes.apple.com', 'https://itunes.apple.com', '../apple_podcast'):
            with self.subTest(provider=provider), self.assertRaises(SourceError):
                transport.MediaHostLimiter(provider)
