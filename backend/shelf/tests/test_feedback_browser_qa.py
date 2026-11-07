"""Optional private LiveServer rehearsal, enabled only inside the supported test run."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from unittest import skipUnless
from unittest.mock import patch
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from cabinet.boundary import ROOT
from shelf import durable_intake, discovery_sources
from shelf.models import Grant, IntakeRun, Profile
from shelf.tests.test_durable_intake import fiction_cards

DIRECTORY = ROOT / '.work/Author'
ENABLE = DIRECTORY / 'browser-qa-enable.json'
READY = DIRECTORY / 'browser-qa-ready.json'
STOP = DIRECTORY / 'browser-qa-stop'


class FixtureBanner:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.get('Content-Type', '').startswith('text/html'):
            body = response.content
            marker = b'<main id="main"'
            banner = b'<aside role="note" class="paper-panel">FICTIONAL QA ONLY - disposable test data, no production owner feedback.</aside>'
            response.content = body.replace(marker, banner + marker, 1)
        return response


@skipUnless(ENABLE.is_file(), 'Private browser QA fixture not enabled for this aggregate run.')
@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['localhost', '127.0.0.1', 'testserver'],
    MIDDLEWARE=[*settings.MIDDLEWARE, 'shelf.tests.test_feedback_browser_qa.FixtureBanner'])
class FeedbackBrowserQA(StaticLiveServerTestCase):
    host = '127.0.0.1'

    def test_private_browser_round(self):
        admission = json.loads(ENABLE.read_text())
        self.assertEqual(admission, {'approved_fixture_only': True, 'timeout_seconds': 900})
        self.assertNotEqual(ROOT, Path('/home/tang/Projects/culture-cabinet'))
        actor = get_user_model().objects.create_user(pk=1, username='fictional-card-qa',
                                                     password='FICTIONAL-only-card-QA-2026!')
        profile = Profile.objects.create(pk=1, kind='owner', active=True, label='FICTIONAL QA - disposable owner')
        Grant.objects.create(user=actor, profile=profile, can_view=True, can_select=True)
        cards = fiction_cards()
        now = datetime.now(timezone.utc).isoformat()
        run = durable_intake.new_run(actor, 1)
        run.history = [{'card': durable_intake.snapshot(cards[0].data()), 'choice': 'interested',
                        'operation': 'choose', 'responded_at': now, 'recorded_at': now,
                        'provenance': 'SYNTHETIC browser QA fixture, not a production owner statement'}]
        run.save()
        artists = [discovery_sources.Candidate('musicbrainz:artist:' + str(UUID(int=n + 1)),
            'Fictional QA band ' + str(n + 1), 'artist', 'FICTIONAL QA registry',
            'manual-attributed', n, now, url='https://musicbrainz.org/artist/' + str(UUID(int=n + 1)))
            for n in range(20)]
        artists.append(discovery_sources.Candidate('official:recording:qa', 'FICTIONAL track - excluded from bands',
            'recording', 'FICTIONAL QA registry', 'manual-attributed', 1, now, url='https://example.org/qa-track'))
        self.enterContext(patch('shelf.provider_catalogue.initial_deck', return_value=(cards, [])))
        self.enterContext(patch('shelf.provider_catalogue.search', side_effect=lambda medium, query, **kw: [c for c in cards if c.medium == medium][:9]))
        self.enterContext(patch('shelf.provider_catalogue.neighbors', return_value=[]))
        self.enterContext(patch('shelf.provider_catalogue._request', side_effect=AssertionError('QA cannot fetch provider data')))
        self.enterContext(patch('shelf.discovery_sources.household_candidates', return_value=artists))
        self.enterContext(patch('shelf.discovery_sources.fetch_public', side_effect=ValueError('FICTIONAL QA: source reads disabled')))
        DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        READY.write_text(json.dumps({'status': 'READY_FICTIONAL_QA_ONLY', 'url': self.live_server_url,
            'source_digest': json.loads((ROOT / '.review-input/manifest.json').read_text())['manifest_digest'],
            'username': actor.username, 'password': 'FICTIONAL-only-card-QA-2026!',
            'production_data': False, 'model_worker': 'Not connected; canonical deployed CLI rejects candidate paths.',
            'stop_file': str(STOP), 'timeout_seconds': 900}, indent=2) + '\n')
        started = time.monotonic()
        while not STOP.exists() and time.monotonic() - started < 900:
            time.sleep(0.2)
        READY.write_text(json.dumps({'status': 'STOPPED_FICTIONAL_QA_ONLY', 'production_data': False}) + '\n')
        self.assertTrue(STOP.exists(), 'Browser QA timed out without an explicit test-owner stop.')
