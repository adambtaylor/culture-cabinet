"""Explicit, bounded Apple episode metadata reads; never fetch audio or artwork.

Contract: https://performance-partners.apple.com/search-api documents episode
search, UTF-8 JSON, caching and approximately 20 calls/minute. The rate guard and
cache are process local, suitable for this application's single worker deployment.
Callers own explicit-action authorization and the five-cluster action budget.
"""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import subprocess
from threading import Lock
from time import monotonic
from urllib.parse import parse_qs, urlencode, urlsplit

from django.core.cache.backends.locmem import LocMemCache

from shelf.provider_smoke import CA, CURL

MAX_BODY = 128 * 1024
MAX_RESULTS = 10
CACHE_SECONDS = 6 * 60 * 60
_cache = LocMemCache('podcast-episodes', {'TIMEOUT': CACHE_SECONDS,
                                       'OPTIONS': {'MAX_ENTRIES': 128}})
_calls = deque()
_lock = Lock()


class PodcastSourceError(ValueError):
    """Public status only: provider content and caller queries never reach errors."""

    def __init__(self, status):
        self.status = status
        super().__init__(status)


def _positive_id(value):
    return type(value) is int and 0 < value < 2**63


def _date(value):
    if type(value) is not str or len(value) > 50:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.isoformat() if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _text(value):
    return type(value) is str and 0 < len(value.strip()) <= 300


def _episode_url(value, episode_id, collection_id):
    if type(value) is not str or len(value) > 2000:
        return None
    try:
        url = urlsplit(value)
        query = parse_qs(url.query, max_num_fields=10)
        if (url.scheme != 'https' or url.netloc != 'podcasts.apple.com'
                or url.fragment or not re.fullmatch(r'/[a-z]{2}/podcast/(?:[^/?#]+/)?id' + str(collection_id), url.path)
                or query.get('i') != [str(episode_id)]):
            return None
    except ValueError:
        return None
    # Retain the verified identity; discard affiliate and other tracking values.
    return f'https://podcasts.apple.com{url.path}?i={episode_id}'


def parse_results(payload, retrieved_at):
    """Project episode identity only; untrusted show-only/invalid rows are skipped.

    A source's title, guest credentials or popularity do not establish evidence
    quality. Independent appraisal and content classification belong to curation.
    """
    retrieved_at = _date(retrieved_at)
    if (retrieved_at is None or type(payload) is not dict
            or type(payload.get('resultCount')) is not int
            or not 0 <= payload['resultCount'] <= MAX_RESULTS
            or type(payload.get('results')) is not list
            or len(payload['results']) != payload['resultCount']):
        raise PodcastSourceError('bad_response')
    output, seen = [], set()
    for row in payload['results']:
        if type(row) is not dict or row.get('kind') != 'podcast-episode':
            continue
        episode_id, collection_id = row.get('trackId'), row.get('collectionId')
        if not _positive_id(episode_id) or not _positive_id(collection_id):
            continue
        url = _episode_url(row.get('trackViewUrl'), episode_id, collection_id)
        if (url is None or not _text(row.get('trackName'))
                or not _text(row.get('collectionName')) or episode_id in seen):
            continue
        milliseconds = row.get('trackTimeMillis')
        duration = (milliseconds // 1000 if type(milliseconds) is int
                    and 0 < milliseconds <= 7 * 24 * 60 * 60 * 1000 else None)
        output.append({
            'key': f'apple:podcast_episode:{episode_id}',
            'apple_episode_id': str(episode_id), 'apple_collection_id': str(collection_id),
            'title': row['trackName'].strip(), 'show': row['collectionName'].strip(),
            'apple_url': url, 'published_at': _date(row.get('releaseDate')),
            'duration_seconds': duration, 'source_url': url,
            'retrieved_at': retrieved_at, 'evidence_status': 'not_independently_appraised',
            'content_kind': 'unknown', 'cluster': None,
        })
        seen.add(episode_id)
    return output


def _query(terms):
    if type(terms) in (list, tuple):
        if not 1 <= len(terms) <= 5 or any(type(term) is not str for term in terms):
            raise PodcastSourceError('invalid')
        terms = ' '.join(terms)
    if (type(terms) is not str or not 1 <= len(terms.strip()) <= 150
            or len(terms) > 150 or any(ord(c) < 32 or ord(c) == 127 for c in terms)):
        raise PodcastSourceError('invalid')
    return terms.strip()


def _command(term):
    url = 'https://itunes.apple.com/search?' + urlencode({
        'term': _query(term), 'media': 'podcast', 'entity': 'podcastEpisode',
        'country': 'AU', 'limit': MAX_RESULTS,
    })
    return [CURL, '-q', '--silent', '--proto', '=https', '--proto-redir', '=https',
            '--cacert', CA, '--proxy', '', '--noproxy', '*', '--no-netrc',
            '--max-redirs', '0', '--retry', '0', '--connect-timeout', '5',
            '--max-time', '8', '--max-filesize', str(MAX_BODY),
            '--header', 'Accept: application/json', '--write-out', '\n%{http_code}',
            '--url', url]


def _fetch(term):
    """Cap pipe reads as well as curl's transfer size, with no redirects/retries."""
    try:
        with subprocess.Popen(_command(term), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env={'LANG': 'C', 'LC_ALL': 'C'}, close_fds=True) as child:
            try:
                raw = child.stdout.read(MAX_BODY + 5)
                if len(raw) > MAX_BODY + 4:
                    child.kill()
                    raise PodcastSourceError('bad_response')
                code = child.wait(timeout=9)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                raise PodcastSourceError('timeout') from None
        if code:
            raise PodcastSourceError('timeout' if code == 28 else 'unavailable')
    except OSError:
        raise PodcastSourceError('unavailable') from None
    body, separator, status = raw.rpartition(b'\n')
    if not separator or len(status) != 3 or not status.isdigit():
        raise PodcastSourceError('bad_response')
    return int(status), body


def _document(status, raw):
    if status == 429:
        raise PodcastSourceError('rate')
    if status != 200:
        raise PodcastSourceError('bad_response')
    if type(raw) is not bytes or len(raw) > MAX_BODY:
        raise PodcastSourceError('bad_response')
    try:
        return json.loads(raw.decode('utf-8'), parse_constant=_reject_constant,
                          object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise PodcastSourceError('bad_response') from None


def _reject_constant(value):
    raise ValueError('Invalid JSON constant')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def search_cluster(cluster_id, terms, *, transport=None):
    """One explicit cluster search, at most one call; no automatic fallback/retry.

    The injectable transport receives a validated term and returns (status, bytes).
    Tests and offline callers must inject it rather than use a real HTTP request.
    """
    if type(cluster_id) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', cluster_id):
        raise PodcastSourceError('invalid')
    term = _query(terms)
    cache_key = sha256(f'{cluster_id}:{term}'.encode('utf-8')).hexdigest()
    with _lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            return deepcopy(cached)
        now = monotonic()
        while _calls and now - _calls[0] >= 60:
            _calls.popleft()
        if len(_calls) >= 20:
            raise PodcastSourceError('rate')
        _calls.append(now)
    try:
        status, raw = (transport or _fetch)(term)
    except TimeoutError:
        raise PodcastSourceError('timeout') from None
    rows = parse_results(_document(status, raw), datetime.now(timezone.utc).isoformat())
    for row in rows:
        row['cluster'] = cluster_id
    with _lock:
        _cache.set(cache_key, rows, timeout=CACHE_SECONDS)
    return deepcopy(rows)
