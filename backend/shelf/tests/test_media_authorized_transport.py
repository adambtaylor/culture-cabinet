"""Fixture-free, synthetic operations and fake process streams; no provider I/O."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, MagicMock, patch
import subprocess

from shelf import credential_store, discovery_transport as transport, provider_catalogue as catalogue
from shelf.disclosure import SCALARS, canonical_bytes, read_json
from shelf.enhance_contract import CataloguePlan, CollectionBudget, EnhanceBindings, EnhanceFault, digest
from shelf.category_registry import registry_view
from shelf.source_adapters import SourceBackend, SourcePermit, SourceReply, SourceRequest, normalize, registered_source
from shelf.source_collection import SourceCollection
from shelf.source_evidence import FieldPolicy, build_evidence

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class MediaAuthorizedTests(TestCase):
    def setUp(self):
        # An accidental real spawn or real reservation fails the test before I/O.
        self.spawn_guard = patch.object(transport.subprocess, 'Popen', side_effect=AssertionError('Unmocked spawn'))
        self.spawn_guard.start()
        self.addCleanup(self.spawn_guard.stop)
        self.slot = Mock()
        self.slot.current.return_value = True
        self.reserve = patch.object(transport.MediaHostLimiter, 'reserve', return_value=self.slot)
        self.reserve.start()
        self.addCleanup(self.reserve.stop)

    def request(self, source='tmdb_movie', operation='search_movie'):
        return SourceRequest(source, 'book' if source == 'hardcover' else 'tv' if source == 'tmdb_tv' else 'movie',
            operation, ('Synthetic title',), (81001, 81002) if operation == 'book_details' else ())

    def fake_send(self, *, operation=None, raw=None, current=None, writes=None, timeout=False, wait_error=False):
        raw = raw if raw is not None else b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}\n200'
        operation = operation or catalogue.media_operation(self.request())
        secret = bytearray(b'SYNTHETIC-ONLY-CANARY')
        child = Mock(returncode=0)
        child.poll.return_value = None
        child.stdin.fileno.return_value = 91
        child.stdout.fileno.return_value = 92
        if wait_error:
            child.wait.side_effect = subprocess.TimeoutExpired('synthetic', 1)
        ready = MagicMock()
        ready.__enter__.return_value = ready
        write_events = 2 if writes == 'partial' else 1
        events = [[(SimpleNamespace(data='input'), 1)]] * write_events + [
            [(SimpleNamespace(data='output'), 1)], [(SimpleNamespace(data='output'), 1)]]
        ready.get_map.side_effect = [True] * len(events) + [False]
        ready.select.side_effect = [[]] if timeout else events
        captured = []

        def write(fd, value):
            captured.append(bytes(value))
            return 0 if writes == 'zero' else 5 if writes == 'partial' and len(captured) == 1 else len(value)

        with patch.object(transport.subprocess, 'Popen', return_value=child) as spawn, \
             patch.object(transport.selectors, 'DefaultSelector', return_value=ready), \
             patch.object(transport.os, 'set_blocking'), \
             patch.object(transport.os, 'write', side_effect=write), \
             patch.object(transport.os, 'read', side_effect=[raw, b'']):
            try:
                result = transport.media_authorized(operation, secret=secret,
                    revalidate=current or Mock(return_value=True), limiter=transport.MediaHostLimiter(operation.provider))
                return result, spawn.call_args, captured
            finally:
                self.assertEqual(secret, bytearray(len(secret)))
                self.assertEqual(spawn.call_count, 1)
                child.kill.assert_called_once()
                child.stdout.close.assert_called_once()
                self.assertNotIn('SYNTHETIC-ONLY-CANARY', repr(spawn.call_args))

    def test_four_fixed_operations_preserve_legacy_bytes_and_select_native_minimal_details(self):
        for source, name in [('tmdb_movie', 'search_movie'), ('tmdb_tv', 'search_tv'),
                             ('hardcover', 'book_search'), ('hardcover', 'book_details')]:
            request = self.request(source, name)
            operation = catalogue.media_operation(request)
            value = list(request.ids) if request.ids else request.terms[0]
            provider, args = catalogue._command(name, value)
            legacy = catalogue.MediaOperation(source, name, ids=request.ids) if request.ids else \
                catalogue.MediaOperation(source, name, request.terms[0])
            self.assertEqual(provider, operation.provider)
            self.assertEqual(args[args.index('--url') + 1], operation.origin + operation.path +
                ('?' + operation.query if operation.query else ''))
            if operation.body:
                self.assertEqual(args[args.index('--data-binary') + 1].encode(), legacy.body)
                if name == 'book_details':
                    self.assertNotEqual(operation.body, legacy.body)
                    self.assertEqual(operation.projection, 'native_minimal')
                else:
                    self.assertEqual(operation.body, legacy.body)
            self.assertEqual(len(operation.operation_digest), 64)
            self.assertNotIn('Synthetic title', repr(operation))
            with self.assertRaises(catalogue.CatalogueError):
                replace(operation, source='https://example.invalid')
        for value in ['', 'x' * 101, 'bad\nheader']:
            with self.assertRaises(catalogue.CatalogueError):
                catalogue.MediaOperation('tmdb_movie', 'search_movie', value)
        for ids in [(True,), (-1,), tuple(range(1, 11))]:
            with self.assertRaises(catalogue.CatalogueError):
                catalogue.MediaOperation('hardcover', 'book_details', ids=ids)

    def test_independent_pinned_wires_and_digests_reach_authorized_process(self):
        import hashlib
        import json
        # Independently pin both the unchanged legacy document and the new
        # closed native document; preview and transport must bind selected bytes.
        fields = ('id title slug description book_category_id canonical_id image{url width height} '
            'book_series(limit:12){book_id series_id position details compilation series{id name slug}} '
            'editions(limit:12){id book_id canonical_id edition_format edition_information}')
        search = 'query Search($q:String!){search(query:$q,per_page:9,page:1){ids error}}'
        details = 'query Details($ids:[Int!]!){books(where:{id:{_in:$ids}},limit:9){id title slug}}'
        legacy_details = 'query Details($ids:[Int!]!){books(where:{id:{_in:$ids}},limit:9){' + fields + '}}'
        legacy = catalogue.MediaOperation('hardcover', 'book_details', ids=(81001, 81002))
        self.assertEqual(legacy.body, json.dumps({'query': legacy_details,
            'variables': {'ids': [81001, 81002]}}, ensure_ascii=True).encode())
        query = 'include_adult=false&language=en-US&page=1&query=Synthetic+title'
        expected = [
            ('tmdb_movie', 'search_movie', 'GET', 'https://api.themoviedb.org', '/3/search/movie', query, b''),
            ('tmdb_tv', 'search_tv', 'GET', 'https://api.themoviedb.org', '/3/search/tv', query, b''),
            ('hardcover', 'book_search', 'POST', 'https://api.hardcover.app', '/v1/graphql', '',
             json.dumps({'query': search, 'variables': {'q': 'Synthetic title'}}, ensure_ascii=True).encode()),
            ('hardcover', 'book_details', 'POST', 'https://api.hardcover.app', '/v1/graphql', '',
             json.dumps({'query': details, 'variables': {'ids': [81001, 81002]}}, ensure_ascii=True).encode())]
        for source, name, method, origin, path, query, body in expected:
            with self.subTest(source=source, operation=name):
                operation = catalogue.media_operation(self.request(source, name))
                material = {'method': method, 'origin': origin, 'path': path, 'query': query,
                            'body_digest': hashlib.sha256(body).hexdigest()}
                expected_digest = hashlib.sha256(json.dumps(material, sort_keys=True,
                    separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
                self.assertEqual(operation.material, material)
                self.assertEqual(operation.body, body)
                self.assertEqual(operation.operation_digest, expected_digest)
                _, spawn, _ = self.fake_send(operation=operation)
                args = spawn.args[0]
                self.assertEqual(args[args.index('--request') + 1], method)
                self.assertEqual(args[args.index('--url') + 1], origin + path + ('?' + query if query else ''))
                if body:
                    self.assertEqual(args[args.index('--data-binary') + 1].encode(), body)
                else:
                    self.assertNotIn('--data-binary', args)

    def test_expired_original_observation_cannot_borrow_current_policy_or_permit(self):
        request = self.request()
        registry = registry_view(('movie',))
        bindings = EnhanceBindings('synthetic-install', 'synthetic-actor', 'synthetic-profile',
            'synthetic-snapshot', 1, 'synthetic-watermark', registry.revision, 1, 'synthetic-grants', 'synthetic-connection')
        body = canonical_bytes({'schema': 'catalogue-query/v1', 'categories': ['movie'],
            'queries': [{'category': 'movie', 'terms': list(request.terms)}], 'profile': {'signals': []}})
        plan = CataloguePlan('synthetic-plan', bindings, ('movie',), body, digest(body), NOW + timedelta(seconds=600))
        policy = FieldPolicy('synthetic-policy', 1, NOW - timedelta(seconds=1), NOW + timedelta(seconds=300),
            (('*', ('fetch',)),) + tuple((key, ('fetch', 'display', 'model_context', 'retain', 'cache'))
                                        for key in sorted(SCALARS)))
        permit = SourcePermit(request.source, 'https://api.themoviedb.org', 'catalogue_lookup',
            plan.plan_ref, bindings, request.request_digest, 1, 1, policy.ref, policy.revision,
            plan.expires_at, ('synthetic-query',), 1)
        permit.verify(request, plan, NOW)
        self.assertTrue(policy.current(NOW))
        observed = NOW - timedelta(hours=1)
        reply = SourceReply(request.request_digest, 200,
            canonical_bytes({'results': [{'id': 81001, 'title': 'Synthetic expired movie'}]}), observed)
        cards = normalize(registered_source(request.source), request, reply, now=NOW).cards
        records = []
        with self.assertRaises(EnhanceFault) as denied:
            batch = build_evidence(cards, category='movie', observation_ref='synthetic-expired',
                source_revision=1, response_digest=digest(reply.body), observed_at=reply.observed_at,
                expires_at=observed + timedelta(minutes=5), parents=permit.parent_ids,
                policy=policy, now=NOW, fixture=True)
            records.extend(read_json(batch.material)['records'])
        self.assertEqual(denied.exception.code, 'PREVIEW_STALE')
        self.assertEqual(records, [])
        self.assertEqual(reply.observed_at, observed)

    def test_secret_only_in_stdin_partial_write_and_fixed_process_bounds(self):
        result, spawn, written = self.fake_send(writes='partial')
        command = spawn.args[0]
        self.assertEqual(result, (200, b'{}', (('content-type', 'application/json'),)))
        self.assertTrue(any(b'SYNTHETIC-ONLY-CANARY' in chunk for chunk in written))
        self.assertIn('@-', command)
        self.assertNotIn('--location', command)
        for flag, value in [('--max-redirs', '0'), ('--retry', '0'), ('--max-time', '5'),
                            ('--proxy', ''), ('--max-filesize', '262144')]:
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(spawn.kwargs['env'], {'LANG': 'C', 'LC_ALL': 'C'})
        self.slot.finish.assert_called_once()

    def test_missing_wrong_limiter_and_stale_before_send_refuse_without_spawn(self):
        operation = catalogue.media_operation(self.request())
        for limiter, callback in [(None, Mock(return_value=True)),
                                  (transport.MediaHostLimiter('hardcover'), Mock(return_value=True)),
                                  (transport.MediaHostLimiter('tmdb'), Mock(return_value=False))]:
            secret = bytearray(b'SYNTHETIC-CANARY')
            with self.assertRaises(transport.SourceError), patch.object(transport.subprocess, 'Popen') as spawn:
                transport.media_authorized(operation, secret=secret, revalidate=callback, limiter=limiter)
            spawn.assert_not_called()
            self.assertFalse(any(secret))
        with self.assertRaises(transport.SourceError):
            self.fake_send(current=Mock(side_effect=[True, True, False]))

    def test_redirect_compression_header_body_and_write_failures_never_resend(self):
        cases = [b'HTTP/1.1 302 Found\r\nLocation: https://example.invalid\r\n\r\n{}\n302',
            b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n{}\n200',
            b'HTTP/1.1 200 OK\r\nX: ' + b'x' * 8193 + b'\r\n\r\n{}\n200',
            b'HTTP/1.1 200 OK\r\n\r\n' + b'x' * 262145 + b'\n200']
        for raw in cases:
            with self.subTest(size=len(raw)), self.assertRaises(transport.SourceError):
                self.fake_send(raw=raw)
        with self.assertRaises(transport.SourceError):
            self.fake_send(writes='zero')
        with self.assertRaises(TimeoutError):
            self.fake_send(timeout=True)
        with self.assertRaises(transport.SourceError):
            self.fake_send(wait_error=True)

    def test_rate_headers_are_allowlisted_and_extend_provider_cooldown(self):
        raw = b'HTTP/1.1 429 Limited\r\nRetry-After: 120\r\nSet-Cookie: SYNTHETIC\r\n\r\n{}\n429'
        result, _, _ = self.fake_send(raw=raw)
        self.assertEqual(result[0], 429)
        self.assertEqual(dict(result[2]), {'retry-after': '120'})
        self.assertEqual(transport.moshtix_cooldown(result[0], result[2], now=NOW), 120)
        self.assertEqual(transport.MediaHostLimiter('tmdb').provider,
                         catalogue.media_operation(self.request('tmdb_tv', 'search_tv')).provider)

    def test_search_detail_lineage_expiry_and_denied_model_rights_stay_distinct(self):
        registry = registry_view(('book',))
        bindings = EnhanceBindings('synthetic-install', 'synthetic-actor', 'synthetic-profile',
            'synthetic-snapshot', 1, 'synthetic-watermark', registry.revision, 1, 'synthetic-grants', 'synthetic-connection')
        body = canonical_bytes({'schema': 'catalogue-query/v1', 'categories': ['book'],
            'queries': [{'category': 'book', 'terms': ['Synthetic book']}], 'profile': {'signals': []}})
        plan = CataloguePlan('synthetic-plan', bindings, ('book',), body, digest(body), NOW + timedelta(seconds=600))
        uses = ('fetch', 'display', 'model_context', 'retain', 'cache')
        policy = FieldPolicy('synthetic-policy', 1, NOW - timedelta(seconds=1), NOW + timedelta(seconds=300),
            (('*', ('fetch',)),) + tuple((key, uses) for key in sorted(SCALARS)))
        backend = MagicMock(spec=SourceBackend)
        backend.isolated_fixture = True
        backend.authorize.side_effect = lambda request, plan, now: SourcePermit('hardcover',
            'https://api.hardcover.app', 'catalogue_lookup', plan.plan_ref, plan.bindings,
            request.request_digest, 1, 1, policy.ref, policy.revision, plan.expires_at, ('synthetic-query',), 1)
        backend.revalidate.return_value = True
        backend.policy.return_value = policy

        def reply(request, permit, **bounds):
            value = {'data': {'search': {'ids': [81001], 'error': None}}} if request.operation == 'book_search' else {
                'data': {'books': [{'id': 81001, 'title': 'Synthetic book', 'slug': 'synthetic-book'}]}}
            return SourceReply(request.request_digest, 200, canonical_bytes(value), NOW)

        backend.request.side_effect = reply
        clock = SimpleNamespace(now=lambda: NOW, monotonic=lambda: 0, sleep=lambda _: None)
        collector = SourceCollection((('book', ('hardcover',)),), {'hardcover': backend}, clock=clock, isolated_fixture=True)
        seal = collector.collect(plan, registry, CollectionBudget())
        self.assertEqual(seal.records[0]['candidate']['key'], 'hardcover:book:81001')
        self.assertEqual(seal.records[0]['direct_parent_ids'], ['synthetic-query'])
        self.assertEqual(len(read_json(seal.source_revisions)['book:hardcover']['response_chain']), 2)
        self.assertEqual(seal.expires_at, policy.expires_at)
        denied = replace(policy, permissions=tuple((key, tuple(u for u in value if u != 'model_context'))
                                                  for key, value in policy.permissions))
        backend.policy.return_value = denied
        local_collector = SourceCollection((('book', ('hardcover',)),), {'hardcover': backend},
            clock=clock, isolated_fixture=True)
        result = local_collector.collect(plan, registry, CollectionBudget())
        # API-first collection retains locally permitted facts; the separate
        # model boundary must still exclude this complete search/detail chain.
        self.assertEqual(result.outcomes[0].state, 'fresh')
        self.assertEqual(result.records[0]['candidate']['key'], 'hardcover:book:81001')
        self.assertEqual(local_collector.model_eligible_keys(result, plan, registry), frozenset())
