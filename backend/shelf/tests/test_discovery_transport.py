"""Synthetic streams and process mocks only; never execute curl."""
from datetime import datetime, timedelta, timezone
import json
import subprocess
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from shelf import discovery_sources as s, discovery_transport as t
from shelf.tests.test_discovery_sources import ARTIST, OTHER, RECORDING, radio

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def event_document(venue_id=2362):
    return {'data': {'viewer': {'getEvents': {'totalCount': 1, 'items': [
        {'id': 123, 'name': 'SYNTHETIC event', 'startDate': '2026-11-01T10:00:00Z',
         'venue': {'id': venue_id, 'name': 'Synthetic Forum-like venue',
                   'address': {'locality': 'Synthetic locality', 'region': 'VIC'}}}
    ]}}}}


class PublicTransportTests(TestCase):
    def test_fixed_radio_url_enumerated_mode_and_bounds(self):
        for mode in ('easy', 'medium', 'hard'):
            args = t.radio_command(ARTIST, mode)
            url = urlsplit(args[-1])
            self.assertEqual(url.scheme + '://' + url.netloc + url.path,
                             'https://api.listenbrainz.org/1/lb-radio/artist/' + ARTIST)
            query = parse_qs(url.query)
            self.assertEqual(query['mode'], [mode])
            self.assertEqual(query['max_similar_artists'], ['5'])
            self.assertEqual(query['max_recordings_per_artist'], ['3'])
        with patch.object(t.subprocess, 'Popen') as process:
            for seed, mode in [('https://example.invalid', 'medium'), (ARTIST, 'all')]:
                with self.assertRaises(s.SourceError):
                    t.radio(seed, mode)
            process.assert_not_called()

    def test_exact_moshtix_contract_and_future_window(self):
        args = t.events_command(NOW)
        body = json.loads(args[args.index('--data-binary') + 1])
        # Independently verified https://api.moshtix.com/v1/graphql snapshot, 2026-10-28.
        expected_query = (
            'query CultureCabinetBoundedContract($from:Date,$to:Date){viewer{getEvents('
            'pageIndex:0,pageSize:1,sortBy:STARTDATE,sortByDirection:ASC,region:[VIC],'
            'eventStartDateFrom:$from,eventStartDateTo:$to,includePastEvents:false,'
            'publicOnly:true,searchableOnly:true){totalCount items{id name startDate '
            'venue{id name address{locality region}}}}}}'
        )
        self.assertEqual(body['query'], expected_query)
        self.assertEqual(args[-1], 'https://api.moshtix.com/v1/graphql')
        self.assertEqual(args[args.index('--request') + 1], 'POST')
        self.assertEqual(s.timestamp(body['variables']['from']), NOW)
        self.assertEqual(s.timestamp(body['variables']['to']) - NOW, timedelta(weeks=8))
        self.assertNotIn('authorization', repr(args).lower())

    def send_mock(self, chunks, *, code=0, timeout=False, wait_error=False):
        child = Mock(returncode=code)
        child.poll.return_value = None
        if wait_error:
            child.wait.side_effect = subprocess.TimeoutExpired('synthetic', 6)
        ready = Mock()
        ready.__enter__ = Mock(return_value=ready)
        ready.__exit__ = Mock(return_value=False)
        ready.get_map.side_effect = [True] * len(chunks) + [False]
        ready.select.return_value = [] if timeout else [(Mock(), 1)]
        with patch.object(t.subprocess, 'Popen', return_value=child) as process, \
             patch.object(t.selectors, 'DefaultSelector', return_value=ready), \
             patch.object(t.os, 'set_blocking'), patch.object(t.os, 'read', side_effect=chunks) as read:
            result = s.fetch_public('radio', ARTIST, 'medium', NOW)
        self.assertEqual(process.call_count, 1)
        command = process.call_args.args[0]
        self.assertEqual(command[:2], [t.CURL, '-q'])
        for flag, value in [('--cacert', t.CA), ('--proxy', ''), ('--noproxy', '*'),
                            ('--max-redirs', '0'), ('--retry', '0'), ('--max-time', '5'),
                            ('--max-filesize', str(s.MAX_BYTES))]:
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertNotIn('--location', command)
        self.assertIn('--no-netrc', command)
        self.assertEqual(process.call_args.kwargs['env'], {'LANG': 'C', 'LC_ALL': 'C'})
        self.assertEqual(process.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(process.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertTrue(all(call.args[1] <= 8192 for call in read.call_args_list))
        child.kill.assert_called_once()
        child.stdout.close.assert_called_once()
        return result

    def test_parent_stream_bounds_and_minimum_process_environment(self):
        self.assertEqual(self.send_mock([json.dumps(radio()).encode() + b'\n200', b'']).status, 'ready')
        self.assertEqual(self.send_mock([b'x' * 8192] * 17).status, 'invalid')
        self.assertEqual(self.send_mock([b'\n200', b''], timeout=True).status, 'timeout')
        self.assertEqual(self.send_mock([b'\n200', b''], code=28).status, 'timeout')
        self.assertEqual(self.send_mock([b'\n200', b''], wait_error=True).status, 'timeout')
        self.assertEqual(self.send_mock([b'\n200', b''], code=60).status, 'invalid')

    def test_strict_json_empty_http_errors_timeout_and_no_source_text(self):
        cases = [(200, b'{}', 'empty'), (429, b'CANARY', 'rate-limited'),
                 (503, b'CANARY', 'error'), (302, b'CANARY', 'error'),
                 (200, b'{', 'invalid'), (200, b'{"a":1,"a":2}', 'invalid'),
                 (200, b'{"a":NaN}', 'invalid'), (200, b'{"a":1e999}', 'invalid'),
                 (200, b'\xff', 'invalid'), (200, b'x' * (s.MAX_BYTES + 1), 'invalid')]
        for code, body, expected in cases:
            with self.subTest(expected=expected, body=body[:20]), patch.object(t, 'radio', return_value=(code, body)) as send:
                result = s.fetch_public('radio', ARTIST, 'medium', NOW)
                self.assertEqual(result.status, expected)
                self.assertNotIn('CANARY', repr(result))
                send.assert_called_once()
        with patch.object(t, 'radio', side_effect=TimeoutError('CANARY')):
            self.assertEqual(s.fetch_public('radio', ARTIST, 'medium', NOW).status, 'timeout')
        with patch.object(t.subprocess, 'Popen', side_effect=OSError('CANARY')):
            self.assertNotIn('CANARY', repr(s.fetch_public('radio', ARTIST, 'medium', NOW)))

    def test_provider_lineage_position_and_no_invented_recording_title(self):
        with patch.object(t, 'radio', return_value=(200, json.dumps(radio()).encode())):
            result = s.fetch_public('radio', ARTIST, 'medium', NOW)
        card, = result.candidates
        self.assertEqual(card.roots, ('musicbrainz:artist:' + ARTIST,))
        self.assertEqual(card.artist_key, 'musicbrainz:artist:' + OTHER)
        self.assertEqual(card.lineage, ((card.roots[0], 1),))
        self.assertEqual(card.key, 'musicbrainz:recording:' + RECORDING)
        self.assertIn('title unavailable', card.title)
        self.assertEqual(card.provenance, 'live-automated')
        fixture = s.fetch_radio_fixture(Mock(return_value=(200, json.dumps(radio()).encode())), ARTIST, NOW.isoformat())
        self.assertNotIn('musicbrainz:', repr(fixture))

    def test_minimal_event_fields_exact_venue_and_unknown_price(self):
        for venue_id, expected in [(2362, 'official:venue:thornbury-theatre'), (999, None)]:
            with patch.object(t, 'events', return_value=(200, json.dumps(event_document(venue_id)).encode())):
                card, = s.fetch_public('events', '', '', NOW).candidates
                self.assertEqual(card.venue_key, expected)
                self.assertEqual(card.artwork_label(), 'Artwork price unknown; check official source')
                self.assertIsNone(card.entry_fee)
        for mutation in ('missing', 'past', 'distant', 'region', 'graphql', 'count'):
            doc = event_document(); row = doc['data']['viewer']['getEvents']['items'][0]
            if mutation == 'missing': del row['venue']['id']
            if mutation == 'past': row['startDate'] = '2020-01-01T00:00:00Z'
            if mutation == 'distant': row['startDate'] = '2030-01-01T00:00:00Z'
            if mutation == 'region': row['venue']['address']['region'] = 'NSW'
            if mutation == 'graphql': doc['errors'] = [{'message': 'CANARY'}]
            if mutation == 'count': doc['data']['viewer']['getEvents']['totalCount'] = 0
            with self.subTest(mutation=mutation), patch.object(t, 'events', return_value=(200, json.dumps(doc).encode())):
                result = s.fetch_public('events', '', '', NOW)
                self.assertEqual(result.status, 'invalid')
                self.assertNotIn('CANARY', repr(result))
        doc = {'data': {'viewer': {'getEvents': {'totalCount': 0, 'items': []}}}}
        with patch.object(t, 'events', return_value=(200, json.dumps(doc).encode())):
            self.assertEqual(s.fetch_public('events', '', '', NOW).status, 'empty')
