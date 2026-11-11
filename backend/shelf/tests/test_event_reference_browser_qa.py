"""Optional owned browser fixture, over real discovery handlers and fake replies.

No listener is created by the ordinary seed test. The private browser method
requires an exact, unexpired candidate-bound admission and a single method label.
"""
from pathlib import Path
import json
import subprocess
import sys
import time
from unittest import SkipTest
from unittest.mock import patch

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, TestCase, override_settings

from cabinet.boundary import ROOT
from shelf import discovery_views as views, discovery_transport as transport
from shelf import event_listing_transport
from shelf.models import DiscoveryState
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests
from shelf.tests.test_event_saved_references import policies
from shelf.tests.test_pbs_events import card as pbs_card, listing as pbs_listing
from shelf.tests.test_rrr_events import card as rrr_card, listing as rrr_listing

OUT = Path('/home/tang/.codex/visualizations/2026/11/10/'
    '01a1c4fa-ceef-74a3-a7b1-77c5cd1b8651/e4/browser-qa')
ADMISSION = OUT / 'admission.json'
METHOD = 'shelf.tests.test_event_reference_browser_qa.EventReferenceBrowserQA.test_private_browser_round'
PASSWORD = 'FICTIONAL-only-event-QA-2026!'


def seed(case):
    """Build saved-only and corrected rows using normal authenticated POSTs."""
    PublicDiscoveryUITests.setUp(case)
    case.actor.set_password(PASSWORD);case.actor.save()
    case.client.force_login(case.actor)
    session = case.client.session;session['selected_profile_id'] = 1;session.save()
    descriptors = dict(views.EVENT_SOURCES)
    descriptors['pbs'] = policies()['pbs']  # Fictional policy only; RRR stays NULL.
    case.enterContext(patch('shelf.event_source_registry.SOURCES', descriptors))
    case.enterContext(patch.object(views, 'EVENT_SOURCES', descriptors))
    case.enterContext(patch.object(event_listing_transport, '_send',
        side_effect=OSError('SYNTHETIC_QA_NETWORK_DISABLED')))
    current = pbs_listing([pbs_card('fictional-current', title='FICTIONAL QA current source card')])
    acquire = case.enterContext(patch.object(transport, 'pbs', return_value=(200, current)))
    identities = {}
    for source_id, raw in (
        ('pbs', pbs_listing([pbs_card('fictional-saved'), pbs_card('fictional-corrected')])),
        ('rrr', rrr_listing(rrr_card(701), rrr_card(702))),
    ):
        with patch.object(transport, source_id, return_value=(200, raw)):
            case.assertEqual(case.client.post(case.action,
                case.form('event_source', source_id=source_id)).status_code, 302)
        rows = next(result['outcome'].candidates for result in
            case.client.get(case.url).context['public_results']
            if result['source'] == descriptors[source_id].label)
        for index, row in enumerate(rows):
            case.assertEqual(case.client.post(case.action,
                case.form('save', key=row.key)).status_code, 302)
            if index:
                for choice in ('mildly_interested', 'intrigued'):
                    case.assertEqual(case.client.post(case.action,
                        case.form('respond', key=row.key, choice=choice)).status_code, 302)
            identities[f'{source_id}_'+('corrected' if index else 'saved_only')] = row.key
    views.PUBLIC_CACHE.clear()
    page = case.client.get(case.url)
    for row in page.context['responses']:
        case.assertTrue(row['reference_only'] and row['saved'])
        case.assertEqual(row['saved_reference'] is not None, row['card'].key in
            {identities['pbs_saved_only'], identities['pbs_corrected']})
    case.assertEqual(len(page.context['responses']), 4)
    # Browser source controls can now perform one mocked normal read of a
    # distinct current card; it cannot turn either saved reference current.
    case.assertEqual(acquire.call_count, 0)
    return identities, acquire


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class EventBrowserSeedTests(TestCase):
    form = PublicDiscoveryUITests.form

    def test_ready_rows_survive_normal_login_and_current_source_action(self):
        identities, acquire = seed(self)
        fresh = Client(enforce_csrf_checks=True)
        fresh.get('/login/?next=/profiles/1/discovery/')
        from django.conf import settings
        token = fresh.cookies[settings.CSRF_COOKIE_NAME].value
        result = fresh.post('/login/?next=/profiles/1/discovery/',
            {'username': self.actor.username, 'password': PASSWORD,
             'next': '/profiles/1/discovery/', 'csrfmiddlewaretoken': token})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(fresh.get('/profiles/').status_code, 200)
        self.assertEqual(fresh.post('/profiles/select/', {'profile_id': '1'},
            HTTP_X_CSRFTOKEN=fresh.cookies[settings.CSRF_COOKIE_NAME].value).status_code, 302)
        page = fresh.get(self.url)
        self.assertEqual(len(page.context['responses']), 4)
        self.client = fresh
        self.assertEqual(fresh.post(self.action, self.form('event_source', source_id='pbs'),
            HTTP_X_CSRFTOKEN=fresh.cookies[settings.CSRF_COOKIE_NAME].value).status_code, 302)
        page = fresh.get(self.url)
        self.assertEqual(len(page.context['responses']), 4)
        self.assertNotIn(page.context['public_results'][0]['outcome'].candidates[0].key,
                         set(identities.values()))
        acquire.assert_called_once_with()
        self.assertEqual(len(DiscoveryState.objects.get().history), 8)


@override_settings(ROOT_URLCONF='cabinet.provider_urls',
    ALLOWED_HOSTS=['localhost', '127.0.0.1', 'testserver'])
class EventReferenceBrowserQA(StaticLiveServerTestCase):
    host = '127.0.0.1'
    form = PublicDiscoveryUITests.form

    @classmethod
    def setUpClass(cls):
        if not ADMISSION.is_file() or METHOD not in sys.argv:
            raise SkipTest('Explicit private event browser admission required.')
        admission = json.loads(ADMISSION.read_text())
        required = {'head', 'tree', 'expires_unix', 'timeout_seconds', 'run_id'}
        if (set(admission) != required or admission['timeout_seconds'] != 900
                or not 0 < admission['expires_unix']-time.time() <= 900
                or not isinstance(admission['run_id'], str)
                or not admission['run_id'].isalnum()
                or [arg for arg in sys.argv if arg.startswith('shelf.tests.')] != [METHOD]):
            raise SkipTest('Exact unexpired single-method admission required.')
        for revision, name in [('HEAD', 'head'), ('HEAD^{tree}', 'tree')]:
            if subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', revision],
                    text=True).strip() != admission[name]:
                raise SkipTest('Candidate changed before browser admission.')
        if subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain'], text=True).strip():
            raise SkipTest('Clean candidate required.')
        cls.admission = admission
        super().setUpClass()

    def test_private_browser_round(self):
        identities, acquire = seed(self)
        blocked_effects = []
        def deny(event, args):
            if event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen',
                         'os.fork', 'os.forkpty', 'os.posix_spawn', 'os.exec'):
                blocked_effects.append(event)
                raise OSError('SYNTHETIC_EVENT_QA_EXTERNAL_EFFECT_DENIED')
        # This hook lasts until this exact single-method process exits. It does
        # not change product settings, authority or the browser's existing CSP.
        sys.addaudithook(deny)
        folder = OUT / self.admission['run_id'];folder.mkdir(mode=0o700)
        stop = folder / 'stop'
        ready = dict(state='READY_FICTIONAL_EVENT_QA', origin=self.live_server_url,
            candidate=self.admission, username=self.actor.username, password=PASSWORD,
            login='/login/?next=/profiles/1/discovery/', discovery=self.url,
            profile_selection='After ordinary login, choose Synthetic owner on /profiles/, then open discovery.',
            identities=identities, stop_file=str(stop), network_calls=0,
            policy='PBS fixture positive; RRR actual production NULL; no source permission',
            current_card='Use registry PBS action: FICTIONAL QA current source card')
        (folder/'ready.json').write_text(json.dumps(ready, indent=2)+'\n')
        print('EVENT_BROWSER_READY '+str(folder/'ready.json'), flush=True)
        deadline = min(time.monotonic()+900,
                       time.monotonic()+self.admission['expires_unix']-time.time())
        while not stop.exists() and time.monotonic() < deadline:
            time.sleep(.2)
        (folder/'fixture-terminal.json').write_text(json.dumps(dict(
            state='EXPLICIT_STOP' if stop.exists() else 'DEADLINE',
            candidate=self.admission, mocked_pbs_reads=acquire.call_count,
            blocked_effects=blocked_effects,
            history_rows=len(DiscoveryState.objects.get().history), network_calls=0,
            listener_cleanup='Django tearDownClass and outer supervisor must confirm closure'),
            indent=2)+'\n')
        self.assertTrue(stop.exists(), 'Original fixture deadline ended before explicit stop.')
