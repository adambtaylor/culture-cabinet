"""Offline episode contract and network boundaries; never call Apple in tests."""
import io
import json
import subprocess
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from shelf import podcast_sources as p

STAMP = '2026-10-31T12:00:00+00:00'


def episode(**changes):
    row = {'kind': 'podcast-episode', 'trackId': 123, 'collectionId': 456,
           'trackName': 'A research discussion', 'collectionName': 'A show',
           'trackViewUrl': 'https://podcasts.apple.com/au/podcast/a-show/id456?i=123',
           'releaseDate': '2025-01-02T03:04:05Z', 'trackTimeMillis': 4567000,
           'episodeUrl': 'https://evil.test/audio.mp3', 'artworkUrl600': 'https://evil.test/image'}
    row.update(changes)
    return row


def document(*rows):
    return {'resultCount': len(rows), 'results': list(rows)}


def response(*rows):
    return 200, json.dumps(document(*rows)).encode()


class PodcastSourcesTests(TestCase):
    def setUp(self):
        p._cache.clear()
        p._calls.clear()
        self.fetch = p._fetch
        self.network = patch.object(p, '_fetch', side_effect=AssertionError('Unexpected network'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_episode_identity_lineage_and_unknowns(self):
        row = p.parse_results(document(episode()), STAMP)[0]
        self.assertEqual(row['key'], 'apple:podcast_episode:123')
        self.assertEqual((row['apple_episode_id'], row['apple_collection_id']), ('123', '456'))
        self.assertEqual(row['duration_seconds'], 4567)
        self.assertEqual(row['published_at'], '2025-01-02T03:04:05+00:00')
        self.assertEqual(row['source_url'], row['apple_url'])
        self.assertEqual(row['retrieved_at'], STAMP)
        self.assertEqual(row['evidence_status'], 'not_independently_appraised')
        self.assertEqual(row['content_kind'], 'unknown')
        self.assertNotIn('artworkUrl600', row)
        self.assertNotIn('episodeUrl', row)
        unknown = p.parse_results(document(episode(releaseDate=None, trackTimeMillis=None,
            evidence_status='strong', content_kind='research', artistName='Professor MD PhD')), STAMP)[0]
        self.assertIsNone(unknown['published_at'])
        self.assertIsNone(unknown['duration_seconds'])
        self.assertEqual(unknown['evidence_status'], 'not_independently_appraised')
        self.assertEqual(unknown['content_kind'], 'unknown')

    def test_show_only_invalid_id_link_and_duplicate_rows_cannot_masquerade(self):
        for change in ({'kind': 'podcast'}, {'trackId': True}, {'collectionId': 0},
                       {'trackId': '123'}, {'trackName': ''}, {'collectionName': 'x'*301}):
            self.assertEqual(p.parse_results(document(episode(**change)), STAMP), [])
        for url in ('https://podcasts.apple.com/au/podcast/a/id456',
                    'http://podcasts.apple.com/au/podcast/a/id456?i=123',
                    'https://evil.test/au/podcast/a/id456?i=123',
                    'https://podcasts.apple.com.evil.test/au/podcast/a/id456?i=123',
                    'https://podcasts.apple.com:443/au/podcast/a/id456?i=123',
                    'https://user@podcasts.apple.com/au/podcast/a/id456?i=123',
                    'https://podcasts.apple.com/au/podcast/a/id999?i=123',
                    'https://podcasts.apple.com/au/podcast/a/id456?i=999',
                    'https://podcasts.apple.com/au/podcast/a/id456?i=123&i=123'):
            self.assertEqual(p.parse_results(document(episode(trackViewUrl=url)), STAMP), [])
        self.assertEqual(len(p.parse_results(document(episode(), episode()), STAMP)), 1)

    def test_payload_bounds_and_strict_json(self):
        for payload in ([], {}, document(*[episode()]*11), {'resultCount': True, 'results': []},
                        {'resultCount': 0, 'results': [episode()]}):
            with self.assertRaises(p.PodcastSourceError):
                p.parse_results(payload, STAMP)
        for raw in (b'[] garbage', b'{"resultCount":NaN}', b'{"results":[],"results":[]}',
                    b'\xff', b'x'*(p.MAX_BODY+1)):
            with self.assertRaises(p.PodcastSourceError):
                p._document(200, raw)
        for value in ('2026-01-01', None, 'not a date'):
            with self.assertRaises(p.PodcastSourceError):
                p.parse_results(document(), value)

    def test_fixed_host_encoded_terms_no_redirects_or_credentials(self):
        command = p._command('https://evil.test/?q=x&entity=movie')
        url = urlsplit(command[-1])
        self.assertEqual((url.scheme, url.netloc, url.path), ('https', 'itunes.apple.com', '/search'))
        params = parse_qs(url.query)
        self.assertEqual(params['term'], ['https://evil.test/?q=x&entity=movie'])
        self.assertEqual(params['entity'], ['podcastEpisode'])
        self.assertEqual(params['media'], ['podcast'])
        self.assertEqual(params['country'], ['AU'])
        self.assertEqual(params['limit'], ['10'])
        self.assertNotIn('-L', command)
        self.assertNotIn('--location', command)
        self.assertIn('--no-netrc', command)
        for flag, value in (('--max-time', '8'), ('--max-redirs', '0'),
                            ('--retry', '0'), ('--max-filesize', str(p.MAX_BODY))):
            self.assertEqual(command[command.index(flag)+1], value)
        self.assertNotIn('Authorization', repr(command))

    def test_one_call_cache_copy_isolation_and_five_cluster_budget(self):
        transport = Mock(return_value=response(episode()))
        ids = ('deep_dives', 'macroeconomics', 'sleep_health', 'living_soil', 'unusual_history')
        for cluster in ids:
            rows = p.search_cluster(cluster, ['research', 'discussion'], transport=transport)
            self.assertEqual(rows[0]['cluster'], cluster)
        self.assertEqual(transport.call_count, 5)
        rows[0]['title'] = 'changed'
        cached = p.search_cluster(ids[-1], 'research discussion', transport=transport)
        self.assertEqual(cached[0]['title'], 'A research discussion')
        self.assertEqual(transport.call_count, 5)
        with patch.object(p, 'monotonic', return_value=100):
            p._calls.clear()
            for i in range(20):
                p.search_cluster('deep_dives', f'topic {i}', transport=transport)
            with self.assertRaises(p.PodcastSourceError) as caught:
                p.search_cluster('deep_dives', 'over budget', transport=transport)
            self.assertEqual(caught.exception.status, 'rate')
        with patch.object(p, 'monotonic', return_value=161):
            p.search_cluster('deep_dives', 'next minute', transport=transport)

    def test_invalid_input_and_failures_make_no_retry(self):
        transport = Mock(return_value=response())
        for terms in ('', 'x'*151, 'line\nbreak', ['x']*6, [None], None):
            with self.assertRaises(p.PodcastSourceError):
                p.search_cluster('deep_dives', terms, transport=transport)
        for cluster in ('', '../deep_dives', 'https://evil.test', None):
            with self.assertRaises(p.PodcastSourceError):
                p.search_cluster(cluster, 'research', transport=transport)
        transport.assert_not_called()
        for status, expected in ((302, 'bad_response'), (500, 'bad_response'), (429, 'rate')):
            transport = Mock(return_value=(status, b'private provider body'))
            with self.assertRaises(p.PodcastSourceError) as caught:
                p.search_cluster('deep_dives', 'research', transport=transport)
            self.assertEqual(str(caught.exception), expected)
            self.assertEqual(transport.call_count, 1)
        transport = Mock(side_effect=TimeoutError)
        with self.assertRaises(p.PodcastSourceError) as caught:
            p.search_cluster('deep_dives', 'research', transport=transport)
        self.assertEqual(caught.exception.status, 'timeout')
        self.assertEqual(transport.call_count, 1)

    def test_cache_expires_after_six_hours_including_empty_results(self):
        transport = Mock(return_value=response())
        with patch('django.core.cache.backends.locmem.time.time', return_value=100):
            self.assertEqual(p.search_cluster('deep_dives', 'research', transport=transport), [])
        with patch('django.core.cache.backends.locmem.time.time', return_value=100+p.CACHE_SECONDS-1):
            self.assertEqual(p.search_cluster('deep_dives', 'research', transport=transport), [])
        self.assertEqual(transport.call_count, 1)
        with patch('django.core.cache.backends.locmem.time.time', return_value=101+p.CACHE_SECONDS):
            p.search_cluster('deep_dives', 'research', transport=transport)
        self.assertEqual(transport.call_count, 2)

    def test_real_transport_reads_only_bounded_metadata(self):
        # Exercise the production pipe/status boundary with an in-memory child.
        child = Mock()
        child.stdout = io.BytesIO(b'{}\n302')
        child.wait.return_value = 0
        process = Mock()
        process.__enter__ = Mock(return_value=child)
        process.__exit__ = Mock(return_value=False)
        with patch.object(p.subprocess, 'Popen', return_value=process) as popen:
            self.assertEqual(self.fetch('research'), (302, b'{}'))
            self.assertEqual(popen.call_count, 1)
        child.stdout = io.BytesIO(b'x'*(p.MAX_BODY+5))
        with patch.object(p.subprocess, 'Popen', return_value=process):
            with self.assertRaises(p.PodcastSourceError):
                self.fetch('research')
        child.kill.assert_called_once()
        child.stdout = io.BytesIO(b'{}\n200')
        child.wait.side_effect = [subprocess.TimeoutExpired('curl', 9), 0]
        with patch.object(p.subprocess, 'Popen', return_value=process):
            with self.assertRaises(p.PodcastSourceError) as caught:
                self.fetch('research')
        self.assertEqual(caught.exception.status, 'timeout')
        self.assertEqual(child.kill.call_count, 2)
