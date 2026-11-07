"""Invented local envelope stimuli only; never a network implementation."""
from datetime import UTC, datetime
import json

from shelf.tmdb_contract import PolicyEvidence, TmdbPolicy

NOW = datetime(2026, 10, 27, tzinfo=UTC)
ALLOW = PolicyEvidence('allow', NOW, 'invented-evidence-only', 'Invented fixture data')
DENY = PolicyEvidence('deny', NOW, 'invented-denial')
DISPLAY = 'display_catalog_fields'
FIELDS = ('source_identity', 'title', 'field_provenance', 'overview', 'release', 'genre_ids',
          'key', 'site', 'type', 'official', 'provenance', 'iso_639_1', 'iso_3166_1',
          'published_at', 'published_precision')


def fixture_policy():
    return TmdbPolicy('fictional-v1', operations={
        'fetch': ALLOW, 'transient_processing': ALLOW, DISPLAY: ALLOW,
        'provider_content_export': DENY, 'external_AI_model_input': DENY},
        field_permissions={DISPLAY: {name: ALLOW for name in FIELDS}},
        attribution='Invented fixture data; no provider permission')


def movie(identity=900000001):
    return dict(id=identity, title='The Tin Orchard', release_date='2024-02-29',
                overview='An invented orchard grows small clocks.', genre_ids=[878],
                vote_average=9.9, vote_count=999999)


def television(identity=900000001):
    return dict(id=identity, name='Lantern Weather', first_air_date='2025-03-01',
                overview='An invented series about paper clouds.', genre_ids=[])


def discovery(rows=None, *, page=1, total=None):
    rows = [movie()] if rows is None else rows
    total = len(rows) if total is None else total
    return dict(page=page, total_pages=(total + 19)//20, total_results=total, results=rows)


def video(**changes):
    return dict(dict(key='SYNTHETIC01', site='YouTube', type='Trailer', official=True,
                     iso_639_1='en', iso_3166_1='US', published_at='2024-01-02T03:04:05.000Z'), **changes)


class FixtureTransport:
    """Supply bytes/status/headers/reported failures to the SAME H guards."""
    def __init__(self, payload=None, *, body=None, status=200, headers=None, chunks=None, failure=None):
        self.body = body if body is not None else json.dumps(payload, ensure_ascii=True).encode('utf-8')
        self.status = status
        self.headers = headers if headers is not None else (b'Content-Type: application/json\r\n', b'\r\n')
        self.chunks = chunks if chunks is not None else (self.body,)
        self.failure = failure
        self.calls = []

    def send(self, request):
        self.calls.append(request)
        return self.failure if self.failure is not None else (self.status, self.headers, self.chunks)
