"""Offline provider contracts; no credential opener or real curl is allowed."""
from dataclasses import replace
from datetime import timedelta
import json
import subprocess
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

from shelf import credential_store as store, provider_catalogue as p
from shelf.tests.fixtures_credentials import CANARY


class ProviderCatalogueTests(TestCase):
    def test_native_minimal_projection_is_closed_and_legacy_callers_keep_wider_bytes(self):
        from shelf.source_adapters import SourceRequest
        document = 'query Details($ids:[Int!]!){books(where:{id:{_in:$ids}},limit:9){id title slug}}'
        request = SourceRequest('hardcover', 'book', 'book_details', ('Synthetic book',), (7, 9))
        native = p.media_operation(request)
        self.assertEqual(p.BOOK_DETAILS_MINIMAL, document)
        self.assertEqual(native.projection, 'native_minimal')
        self.assertEqual(native.body, json.dumps({'query': document,
            'variables': {'ids': [7, 9]}}, ensure_ascii=True).encode('ascii'))
        legacy = p.MediaOperation('hardcover', 'book_details', ids=(7, 9))
        self.assertEqual(legacy.projection, 'legacy')
        _, command = p._command('book_details', [7, 9])
        self.assertEqual(command[command.index('--data-binary') + 1].encode(), legacy.body)
        self.assertIn(b'description', legacy.body)
        self.assertNotIn(b'description', native.body)
        for name in ('BOOK_DETAILS', 'BOOK_DECK', 'BOOK_CATEGORY'):
            self.assertIn(p.BOOK_FIELDS, getattr(p, name))
        self.assertNotEqual(native.operation_digest, legacy.operation_digest)
        for projection in ('minimal', '', None, True, {'fields': ['id']}):
            with self.subTest(projection=projection), self.assertRaises(p.CatalogueError):
                replace(native, projection=projection)
        with self.assertRaises(p.CatalogueError):
            p.MediaOperation('tmdb_movie', 'search_movie', 'Synthetic', projection='native_minimal')
        for ids in ((), (7, 7), (True,), ('7',)):
            with self.subTest(ids=ids), self.assertRaises(p.CatalogueError):
                p.MediaOperation('hardcover', 'book_details', ids=ids, projection='native_minimal')

    def test_fixed_operations_no_url_or_graphql_escape(self):
        for operation in list(p.LANES) + ['search_movie', 'search_tv', 'neighbors_movie',
                                        'neighbors_tv', 'book_search', 'book_details', 'book_deck']:
            value = ([1, 2] if operation == 'book_details' else 1 if operation.startswith('neighbors') else 'a " \\ title')
            provider, command = p._command(operation, value)
            self.assertEqual(command[:2], ['/usr/bin/curl', '-q'])
            for flag, value in (('--proxy', ''), ('--noproxy', '*'), ('--retry', '0'),
                                ('--max-redirs', '0'), ('--max-time', '10'),
                                ('--max-filesize', '524288'), ('--cacert', p.CA)):
                self.assertEqual(command[command.index(flag) + 1], value)
            self.assertIn('--no-netrc', command)
            self.assertEqual(command[command.index('--header') + 1], '@-')
            self.assertNotIn('-L', command)
            endpoint = command[command.index('--url') + 1]
            self.assertIn(urlsplit(endpoint).hostname, ('api.themoviedb.org', 'api.hardcover.app'))
            self.assertNotIn(CANARY.decode(), repr(command))
        for operation in ('https://evil.test/', 'query { users { id }}', 'book_library', 'mutation', ''):
            with self.assertRaises(p.CatalogueError):
                p._command(operation)

    def test_query_variables_and_search_params(self):
        query = 'a " \\ ) { private }'
        _, cmd = p._command('book_search', query)
        body = json.loads(cmd[cmd.index('--data-binary') + 1])
        self.assertEqual(body['variables'], {'q': query})
        self.assertEqual(body['query'], p.BOOK_SEARCH)
        _, cmd = p._command('search_movie', query)
        params = parse_qs(urlsplit(cmd[-1]).query)
        self.assertEqual(params['query'], [query])
        self.assertEqual(params['include_adult'], ['false'])
        self.assertEqual(params['page'], ['1'])
        for bad in ('', ' '*3, 'x'*101, 'a\nb', None):
            with self.assertRaises(p.CatalogueError):
                p._command('book_search', bad)
        for bad in (True, '1', 0, -1, 2147483648):
            with self.assertRaises(p.CatalogueError):
                p._command('neighbors_movie', bad)

    def test_typed_response_failures_never_include_provider_text(self):
        cases = [(0, b'{}\n401', p.Status.AUTH), (0, b'{}\n403', p.Status.AUTH),
                 (0, b'{}\n429', p.Status.RATE), (28, CANARY, p.Status.TIMEOUT),
                 (60, CANARY, p.Status.TLS), (6, CANARY, p.Status.BAD_RESPONSE),
                 (0, b'{}\n302', p.Status.BAD_RESPONSE), (0, b'[]\n200', p.Status.BAD_RESPONSE),
                 (0, b'x'*(p.MAX_BODY+1)+b'\n200', p.Status.BAD_RESPONSE)]
        for code, body, status in cases:
            with self.assertRaises(p.CatalogueError) as caught:
                p._response('tmdb', code, body)
            self.assertEqual(caught.exception.status, status)
            self.assertNotIn(CANARY.decode(), str(caught.exception))
        for errors in ([], None, [{'message': CANARY.decode()}]):
            with self.assertRaises(p.CatalogueError) as caught:
                p._response('hardcover', 0, json.dumps({'data': {'books': []}, 'errors': errors}).encode()+b'\n200')
            self.assertEqual(caught.exception.status, p.Status.GRAPHQL)

    def test_typed_parser_safe_links_and_plain_bounded_description(self):
        rows = [{'id': 13, 'title': '<b>Title</b>', 'overview': '<script>x</script>'+'x'*3000,
                 'homepage': 'javascript:bad', 'poster_path': 'https://evil.test'}]
        card = p.parse_candidates('tmdb', 'movie', rows, lane='a', lane_label='A')[0]
        self.assertEqual(card.url, 'https://www.themoviedb.org/movie/13')
        self.assertEqual(len(card.description), 2000)
        self.assertTrue(card.description.startswith('<script>'))
        self.assertIn('unavailable', card.trailer_status)
        self.assertTrue(card.fetched_at)
        for slug in ('https://evil.test', '../escape', 'bad?x=y', 'bad#x', '//evil', 'bad%2fpath', ''):
            with self.assertRaises(p.CatalogueError):
                p.parse_candidates('hardcover', 'book', [{'id': 1, 'title': 'T', 'slug': slug}], lane='b', lane_label='B')
        book = p.parse_candidates('hardcover', 'book', [{'id': 1, 'title': 'T', 'slug': 'real-book-1'}], lane='b', lane_label='B')[0]
        self.assertEqual(book.url, 'https://hardcover.app/books/real-book-1')
        for bad in (True, '1', -1, None):
            with self.assertRaises(p.CatalogueError):
                p.parse_candidates('tmdb', 'movie', [{'id': bad, 'title': 'T'}], lane='a', lane_label='A')

    def test_book_search_bounded_two_requests_and_graphql_error_soft_failure(self):
        with patch.object(p, '_request', side_effect=[{'search': {'ids': [7], 'error': None}},
                {'books': [{'id': 7, 'title': 'A', 'slug': 'a'}]}]) as send:
            cards = p.search('book', 'A', actor_id=1, profile_id=1)
        self.assertEqual(len(cards), 1)
        self.assertEqual([c.args[0] for c in send.call_args_list], ['book_search', 'book_details'])
        for response in ({'search': {'ids': [], 'error': 'provider text'}}, {'search': {'ids': ['7']}}):
            with patch.object(p, '_request', return_value=response) as send:
                with self.assertRaises(p.CatalogueError):
                    p.search('book', 'A', actor_id=1, profile_id=1)
                self.assertEqual(send.call_count, 1)

    def test_initial_budget_five_calls_and_partial_failures(self):
        def reply(op, **kwargs):
            if op == 'book_deck':
                raise p.CatalogueError(p.Status.GRAPHQL)
            return {'results': [{'id': 1, 'title': 'Movie', 'name': 'Series'}]}
        with patch.object(p, '_request', side_effect=reply) as send:
            cards, failures = p.initial_deck(actor_id=1, profile_id=1)
        self.assertEqual(send.call_count, 5)
        self.assertEqual(len(cards), 4)
        self.assertEqual(failures, [{'lane': 'books_popular', 'status': 'graphql'}])

    def _metadata(self):
        now = store.utc_now()
        return store.EntryMetadata(store.Binding('FICTION_ACCOUNT', '1', '1', 'tmdb',
            'culture-cabinet-tmdb-backend'), now, now+timedelta(days=1), now+timedelta(days=2), store.TMDB_SCOPES)

    def _request_fixture(self, metadata, *, failure=None):
        handle = SimpleNamespace(context=store._provider_context('tmdb'), uid=1000, gid=1000, close=Mock())
        record = store.PrivateRecord(metadata, bytearray(CANARY), 'FICTION', None)
        def send(*args):
            handle.close.assert_called_once()
            self.assertEqual(bytes(args[2]), CANARY)
            if failure:
                raise failure
            return {'results': []}
        with patch.object(store, 'open_store', return_value=handle), patch.object(store, 'lock_store'), \
                patch.object(store, 'read_record', return_value=record), patch.object(p, '_send', side_effect=send) as sender:
            try:
                result = p._request('search_movie', 'fiction', actor_id=1, profile_id=1)
            except p.CatalogueError as error:
                result = error.status
        self.assertEqual(record.secret, bytearray(len(CANARY)))
        handle.close.assert_called_once()
        return result, sender

    def test_store_lock_released_before_io_and_secret_cleared(self):
        result, sender = self._request_fixture(self._metadata())
        self.assertEqual(result, {'results': []})
        sender.assert_called_once()
        result, _ = self._request_fixture(self._metadata(), failure=OSError(CANARY.decode()))
        self.assertEqual(result, p.Status.BAD_RESPONSE)

    def test_metadata_purpose_scopes_expiry_binding_before_io(self):
        base = self._metadata()
        bad = [replace(base, binding=replace(base.binding, actor_id='2')),
               replace(base, binding=replace(base.binding, profile_id='2')),
               replace(base, binding=replace(base.binding, purpose='wrong')),
               replace(base, scopes=()), replace(base, entered_at=base.entered_at-timedelta(days=2),
                                                expires_at=base.entered_at-timedelta(days=1))]
        for metadata in bad:
            result, sender = self._request_fixture(metadata)
            self.assertEqual(result, p.Status.METADATA)
            sender.assert_not_called()
        with patch.object(store, 'open_store') as opener:
            for actor, profile in ((2, 1), (1, 2), (True, 1)):
                with self.assertRaises(p.CatalogueError):
                    p._request('book_deck', actor_id=actor, profile_id=profile)
            opener.assert_not_called()

    def test_token_only_in_stdin_env_and_argv_clean(self):
        child = MagicMock(returncode=0)
        child.__enter__.return_value = child
        headers = []
        def communicate(**kwargs):
            headers.append(bytes(kwargs['input']))
            return b'{"results":[]}\n200', None
        child.communicate.side_effect = communicate
        _, cmd = p._command('search_movie', 'fiction')
        with patch.object(p.subprocess, 'Popen', return_value=child) as process:
            self.assertEqual(p._send(cmd, 'tmdb', bytearray(CANARY)), {'results': []})
        self.assertEqual(headers, [b'Authorization: Bearer '+CANARY+b'\n'])
        self.assertNotIn(CANARY.decode(), repr(process.call_args))
        self.assertEqual(process.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(process.call_args.kwargs['env'], {'LANG': 'C', 'LC_ALL': 'C'})
        self.assertEqual(child.communicate.call_args.kwargs['input'], bytearray(len(headers[0])))

    def test_timeout_kills_and_reaps_without_raw_error(self):
        child = MagicMock()
        child.__enter__.return_value = child
        child.communicate.side_effect = [subprocess.TimeoutExpired('fake', 11, stderr=CANARY), (b'', None)]
        with patch.object(p.subprocess, 'Popen', return_value=child):
            with self.assertRaises(p.CatalogueError) as caught:
                p._send(['fake'], 'tmdb', bytearray(CANARY))
        self.assertEqual(caught.exception.status, p.Status.TIMEOUT)
        child.kill.assert_called_once()
        self.assertEqual(child.communicate.call_count, 2)
