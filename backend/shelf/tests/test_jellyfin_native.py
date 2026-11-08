"""Synthetic scoped credentials and injected connections; no socket or media access."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from shelf import jellyfin_native as jf


class Clock:
    def __init__(self):
        self.elapsed = 0
        self.started = datetime(2026, 11, 8, tzinfo=timezone.utc)

    def now(self):
        return self.started + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed


class Response:
    status = 200

    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers if headers is not None else [
            ('Content-Length', str(len(payload))), ('Content-Type', 'application/json')]

    def getheaders(self):
        return self.headers

    def getheader(self, name, default=None):
        return next((value for key, value in self.headers if key.lower() == name.lower()), default)

    def read(self, maximum):
        return self.payload[:maximum]


class Connection:
    def __init__(self, response):
        self.response, self.calls, self.closed = response, [], False

    def connect(self):
        self.calls.append('connect')

    def request(self, method, path, *, headers):
        self.calls.append((method, path, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class JellyfinSelectedReadTests(TestCase):
    def setUp(self):
        self.clock = Clock()
        self.binding = jf.ServiceBinding(jf.ORIGIN, '1' * 32, 'fixture-version',
            '2' * 32, '3' * 32, '4' * 32, 11, 22, '5' * 32, '6' * 32,
            jf.TV_ROOTS[0], 'selected_tv_read', self.clock.now() + timedelta(seconds=60))
        self.selection = jf.SelectedEpisode('7' * 32, 'episode_s01e02')
        self.item = {'Id': '7' * 32, 'ServerId': '1' * 32, 'Type': 'Episode',
            'ParentId': '8' * 32, 'SeriesId': '9' * 32, 'ParentIndexNumber': 1,
            'IndexNumber': 2, 'Path': jf.TV_ROOTS[0] + '/Fixture/S01E02.mkv',
            'MediaSources': [{'Id': 'a' * 32, 'Path': jf.TV_ROOTS[0] + '/Fixture/S01E02.mkv',
                              'Size': 4096, 'Protocol': 'File', 'IsRemote': False}],
            'Overview': 'PRIVATE_UNRELATED_TEXT', 'UserData': {'Played': True},
            'ImageTags': {'Primary': 'PRIVATE_IMAGE'}, 'Name': 'PRIVATE_TITLE'}
        self.order = []

    @contextmanager
    def credential(self, binding):
        self.order.append('credential')
        yield jf.ScopedCredential(jf.selected_operation(binding, self.selection).binding_digest,
                                  'SYNTHETIC_TOKEN_NOT_A_REAL_KEY')
        self.order.append('credential_closed')

    def current(self, binding, selection):
        self.order.append('current')
        return binding, selection

    def run_read(self, *, value=None, payload=None, response=None, **overrides):
        if payload is None:
            payload = json.dumps({'Items': [self.item]} if value is None else value).encode()
        self.connection = Connection(response or Response(payload))
        args = dict(credential_scope=self.credential, revalidate=self.current, clock=self.clock)
        args.update(overrides)
        with patch.object(jf, '_Connection', return_value=self.connection) as factory:
            result = jf.read_selected(self.binding, self.selection, **args)
            self.factory_calls = factory.call_args_list
        return result

    def test_exact_selected_wire_and_minimal_metadata_are_not_library_arrival(self):
        result = self.run_read()
        self.assertEqual(result.status, 'matched')
        wire = self.connection.calls[1]
        self.assertEqual(wire[0], 'GET')
        self.assertEqual(urlsplit(wire[1]).path, '/Items')
        query = parse_qs(urlsplit(wire[1]).query)
        self.assertEqual(query, {'ids': ['7' * 32], 'userId': ['2' * 32],
            'includeItemTypes': ['Episode'], 'recursive': ['false'], 'startIndex': ['0'],
            'limit': ['1'], 'fields': ['Path,MediaSources,ProviderIds,ParentId'],
            'enableUserData': ['false'], 'enableImages': ['false'],
            'enableTotalRecordCount': ['false']})
        self.assertEqual(self.order[0:2], ['current', 'credential'])
        self.assertTrue(self.connection.closed)
        self.assertEqual(result.media_sources[0].size, 4096)
        self.assertEqual(result.missing_evidence, jf.MISSING_EVIDENCE)
        self.assertNotIn('sha256', repr(result))
        self.assertNotIn('PRIVATE_', repr(result))
        self.assertNotIn('SYNTHETIC_TOKEN', repr(result))
        self.assertFalse(hasattr(result, 'snapshot'))
        self.assertFalse(hasattr(result, 'available'))

    def test_missing_or_boolean_authority_has_no_credential_or_connection(self):
        for args in ({'credential_scope': None}, {'revalidate': None},
                     {'revalidate': lambda *_: True}):
            with self.subTest(args=args):
                self.order.clear()
                self.assertEqual(self.run_read(**args).status, 'unavailable')
                self.assertEqual(self.factory_calls, [])
                self.assertNotIn('credential', self.order)

    def test_revocation_before_send_and_after_response_discards_observation(self):
        for after in (1, 3, 4, 5, 6, 7):
            count = [0]
            def validate(binding, selection):
                count[0] += 1
                return (binding, selection) if count[0] < after else None
            with self.subTest(after=after):
                result = self.run_read(revalidate=validate)
                self.assertEqual(result.status, 'unavailable')
                self.assertEqual(result.media_sources, ())
                if after <= 4:
                    self.assertFalse(any(type(c) is tuple for c in self.connection.calls))

    def test_foreign_expired_changed_or_invalid_bindings_never_send(self):
        for changes in ({'origin': 'http://elsewhere'}, {'server_id': '0' * 32},
                        {'actor_id': True}, {'tv_root': '/'},
                        {'expires_at': self.clock.now()}, {'purpose': 'enqueue'}):
            original = self.binding
            self.binding = replace(original, **changes)
            with self.subTest(changes=changes):
                self.assertEqual(self.run_read().status, 'unavailable')
                self.assertEqual(self.factory_calls, [])
            self.binding = original
        result = self.run_read(revalidate=lambda b, s: (replace(b, credential_generation='b' * 32), s))
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(self.factory_calls, [])
        for identity in ('0' * 32, '7' * 32 + ',x', '', '7' * 32 + '?searchTerm=private'):
            self.selection = replace(self.selection, item_id=identity)
            self.assertEqual(self.run_read().status, 'unavailable')
            self.assertEqual(self.factory_calls, [])

    def test_empty_wrong_duplicate_and_malformed_items(self):
        self.assertEqual(self.run_read(value={'Items': []}).status, 'not_observed')
        values = ({}, {'Items': None}, {'Items': [self.item] * 2}, {'Items': [None]})
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(self.run_read(value=value).status, 'unavailable')
        for changes in ({'Id': 'b' * 32}, {'ServerId': 'b' * 32}, {'Type': 'Person'},
                        {'IndexNumber': True}, {'ParentIndexNumber': 2},
                        {'IndexNumberEnd': 3}, {'SeriesId': '0' * 32}):
            with self.subTest(changes=changes):
                self.assertEqual(self.run_read(value={'Items': [{**self.item, **changes}]}).status,
                                 'unavailable')
        for payload in (b'{"Items":[],"Items":[]}', b'{', b'{"Items":NaN}', b'\xff'):
            self.assertEqual(self.run_read(payload=payload).status, 'unavailable')

    def test_paths_are_data_but_traversal_prefix_collisions_and_remote_sources_refuse(self):
        for path in ('relative', self.binding.tv_root + '/../elsewhere.mkv',
                     self.binding.tv_root + '-other/episode.mkv', 'https://elsewhere/file',
                     self.binding.tv_root + '//file.mkv', self.binding.tv_root):
            item = deepcopy(self.item)
            item['Path'] = item['MediaSources'][0]['Path'] = path
            with self.subTest(path=path):
                self.assertEqual(self.run_read(value={'Items': [item]}).status, 'unavailable')
        for changes in ({'Size': True}, {'Size': 0}, {'Size': 128 * 1024**3 + 1},
                        {'Protocol': 'Http'}, {'IsRemote': True}):
            item = deepcopy(self.item)
            item['MediaSources'][0].update(changes)
            self.assertEqual(self.run_read(value={'Items': [item]}).status, 'unavailable')
        for sources in ([], [self.item['MediaSources'][0]] * 2,
                        [self.item['MediaSources'][0]] * 9):
            self.assertEqual(self.run_read(value={'Items': [{**self.item, 'MediaSources': sources}]}).status,
                             'unavailable')

    def test_redirect_encoding_incomplete_and_oversize_refuse_without_retry(self):
        for status in (301, 302, 401, 429, 500):
            response = Response(b'{}')
            response.status = status
            self.assertEqual(self.run_read(response=response).status, 'unavailable')
            self.assertEqual(len(self.factory_calls), 1)
            self.assertTrue(self.connection.closed)
        headers = ([('Content-Length', str(jf.MAX_BYTES + 1))],
            [('Content-Encoding', 'gzip')], [('Content-Length', '999')],
            [('Content-Length', '2'), ('Content-Length', '2')],
            [('Content-Length', '2'), ('Transfer-Encoding', 'chunked')])
        for values in headers:
            response = Response(b'{}', values + [('Content-Type', 'application/json')])
            self.assertEqual(self.run_read(response=response).status, 'unavailable')
            self.assertEqual(len(self.factory_calls), 1)
        self.assertEqual(self.run_read(payload=b'x' * (jf.MAX_BYTES + 1)).status, 'unavailable')

    def test_absolute_deadline_shorter_expiry_and_connection_failure(self):
        self.binding = replace(self.binding, expires_at=self.clock.now() + timedelta(seconds=1))
        response = Response(json.dumps({'Items': [self.item]}).encode())
        read = response.read
        def slow(maximum):
            self.clock.elapsed += 2
            return read(maximum)
        response.read = slow
        self.assertEqual(self.run_read(response=response).status, 'unavailable')
        args = self.factory_calls[0].kwargs
        self.assertEqual(args['deadline'], 1)
        self.assertEqual(args['connect_timeout'], 1)
        self.clock.elapsed = 0
        with patch.object(jf, '_Connection', side_effect=TimeoutError('PRIVATE_ERROR')) as factory:
            result = jf.read_selected(self.binding, self.selection, credential_scope=self.credential,
                                      revalidate=self.current, clock=self.clock)
        self.assertEqual(factory.call_count, 1)
        self.assertNotIn('PRIVATE_ERROR', repr(result))
        self.assertEqual(result.status, 'unavailable')

    def test_shorter_caller_limits_win_and_increased_limits_are_refused(self):
        original = self.binding
        self.binding = replace(original, response_bytes=2)
        self.assertEqual(self.run_read().status, 'unavailable')
        self.binding = replace(original, connect_seconds=0.5, request_seconds=1.5)
        self.assertEqual(self.run_read().status, 'matched')
        self.assertEqual(self.factory_calls[0].kwargs['connect_timeout'], 0.5)
        self.assertEqual(self.factory_calls[0].kwargs['deadline'], 1.5)
        for changes in ({'request_seconds': 11}, {'connect_seconds': 3},
                        {'response_bytes': jf.MAX_BYTES + 1}, {'request_seconds': float('nan')}):
            self.binding = replace(original, **changes)
            with self.subTest(changes=changes):
                self.assertEqual(self.run_read().status, 'unavailable')
                self.assertEqual(self.factory_calls, [])
