"""Optional owned browser fixture, over real discovery handlers and fake replies.

No listener is created by the ordinary seed test. The private browser method
requires an exact, unexpired candidate-bound admission and a single method label.
"""
from pathlib import Path
import hashlib
import json
import os
import stat
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
from shelf.tests.test_pbs_events import NOW, card as pbs_card, listing as pbs_listing
from shelf.tests.test_rrr_events import card as rrr_card, listing as rrr_listing

OUT = Path('/home/tang/.codex/visualizations/2026/11/01/'
    '01a19511-f462-7811-87bc-78f71fef99fb/pr91-browser')
RUNS = OUT / 'runs'
ADMISSION = OUT / 'admission.json'
METHOD = 'shelf.tests.test_event_reference_browser_qa.EventReferenceBrowserQA.test_private_browser_round'
PASSWORD = 'FICTIONAL-only-event-QA-2026!'
ACTUAL_TARGET = 'e4cc61949aff9d62effdff0b346e7a70f9e5a2d3'
COMPOSITION_COMMIT = 'c810b854f04cd3aaca71f927f74625d26772349c'
PACKAGE_SHA = '104b85853aade4064a24d573071e98f5561195bba40a4f8586861ac33c8cecd9'
RECEIVER = Path('/home/tang/.codex/visualizations/2026/11/01/01a194f6-72e2-7d32-b6f3-e00f7cae7bd6/PR91-E4-BROWSER/pr91-event-reference-browser.cjs')
RECEIVER_SHA = '32e0e2eeded996090509f357ff1bcdb7ca06043aa38fe31b992f10dd3d3155c7'


def _json_once(path, value):
    """Create a complete private receipt once; never replace an earlier run."""
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    return hashlib.sha256(raw).hexdigest()


def _private_directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            or path.resolve() != path.absolute()):
        raise RuntimeError('PR91_PRIVATE_RUN_DIRECTORY')


def _read_admission():
    """Read only the owner-routed single-method admission without following links."""
    fd = os.open(ADMISSION, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size <= 0 or info.st_size > 4096):
            raise SkipTest('Owned private single-method admission required.')
        return json.loads(os.read(fd, info.st_size + 1).decode('utf-8'))
    finally:
        os.close(fd)


def _process_start_ticks():
    raw = Path('/proc/self/stat').read_text()
    value = int(raw[raw.rfind(')') + 2:].split()[19])
    if value <= 0:
        raise RuntimeError('PR91_PROCESS_START_TICKS')
    return value


def _has_actual_main_ancestry(candidate, composition, target):
    """Require the pinned composition merge, target parent, and candidate ancestry."""
    try:
        parents = subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-list', '--parents', '-n', '1', composition],
            text=True).strip().split()
        if len(parents) != 3 or parents[2] != target:
            return False
        subprocess.check_call(['git', '-C', str(ROOT), 'merge-base', '--is-ancestor',
            composition, candidate], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


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
    from shelf.pbs_events import parse_listing as parse_pbs
    current_key = parse_pbs(current, retrieved_at=NOW.isoformat())[0].key
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
    case.assertNotIn(current_key, identities.values())
    return identities, acquire, current_key


@override_settings(ROOT_URLCONF='cabinet.provider_urls', ALLOWED_HOSTS=['testserver'])
class EventBrowserSeedTests(TestCase):
    form = PublicDiscoveryUITests.form

    def test_ready_rows_survive_normal_login_and_current_source_action(self):
        identities, acquire, current_key = seed(self)
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
        self.assertEqual(page.context['public_results'][0]['outcome'].candidates[0].key,current_key)
        self.assertNotIn(current_key,set(identities.values()))
        acquire.assert_called_once_with()
        self.assertEqual(len(DiscoveryState.objects.get().history), 8)


class EventBrowserCandidateBindingTests(TestCase):
    def test_composition_merge_retains_expected_actual_main(self):
        self.assertTrue(_has_actual_main_ancestry(
            'HEAD', COMPOSITION_COMMIT, ACTUAL_TARGET))

    def test_composition_merge_rejects_wrong_actual_main(self):
        self.assertFalse(_has_actual_main_ancestry(
            'HEAD', COMPOSITION_COMMIT, '98b0daadc71fc84abc8af771edfe3978bc0699b2'))


@override_settings(ROOT_URLCONF='cabinet.provider_urls',
    ALLOWED_HOSTS=['localhost', '127.0.0.1', 'testserver'])
class EventReferenceBrowserQA(StaticLiveServerTestCase):
    host = '127.0.0.1'
    form = PublicDiscoveryUITests.form

    @classmethod
    def setUpClass(cls):
        if not ADMISSION.is_file() or METHOD not in sys.argv:
            raise SkipTest('Explicit private event browser admission required.')
        _private_directory(OUT)
        admission = _read_admission()
        required = {'head', 'tree', 'expires_unix', 'timeout_seconds', 'run_id'}
        if (set(admission) != required or admission['timeout_seconds'] != 900
                or not 0 < admission['expires_unix']-time.time() <= 900
                or not isinstance(admission['run_id'], str)
                or len(admission['run_id']) != 32
                or not all(c in '0123456789abcdef' for c in admission['run_id'])
                or [arg for arg in sys.argv if arg.startswith('shelf.tests.')] != [METHOD]):
            raise SkipTest('Exact unexpired single-method admission required.')
        for revision, name in [('HEAD', 'head'), ('HEAD^{tree}', 'tree')]:
            if subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', revision],
                    text=True).strip() != admission[name]:
                raise SkipTest('Candidate changed before browser admission.')
        if not _has_actual_main_ancestry(
                admission['head'], COMPOSITION_COMMIT, ACTUAL_TARGET):
            raise SkipTest('Candidate must preserve the exact composed current-main parent.')
        if subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain'], text=True).strip():
            raise SkipTest('Clean candidate required.')
        if hashlib.sha256(RECEIVER.read_bytes()).hexdigest() != RECEIVER_SHA:
            raise SkipTest('Exact paired receiver required before listener setup.')
        cls.admission = admission
        super().setUpClass()

    def test_private_browser_round(self):
        identities, acquire, current_key = seed(self)
        blocked_effects = []
        def deny(event, args):
            if event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen',
                         'os.fork', 'os.forkpty', 'os.posix_spawn', 'os.exec'):
                blocked_effects.append(event)
                raise OSError('SYNTHETIC_EVENT_QA_EXTERNAL_EFFECT_DENIED')
        # This hook lasts until this exact single-method process exits. It does
        # not change product settings, authority or the browser's existing CSP.
        sys.addaudithook(deny)
        folder = RUNS / self.admission['run_id'];folder.mkdir(mode=0o700)
        _private_directory(folder)
        stop = folder / 'stop'
        started = time.time()
        deadline = min(self.admission['expires_unix'], started + self.admission['timeout_seconds'])
        if deadline <= started or time.time() >= deadline:
            raise RuntimeError('PR91_ORIGINAL_DEADLINE_EXPIRED')
        current_map = dict(source_id='pbs', key=current_key,
            title='FICTIONAL QA current source card', state='distinct-current-fictional-card')
        ready = dict(state='READY_FICTIONAL_EVENT_QA', origin=self.live_server_url,
            candidate=self.admission, username=self.actor.username, password=PASSWORD,
            login='/login/?next=/profiles/1/discovery/', discovery=self.url,
            profile_selection='After ordinary login, choose Synthetic owner on /profiles/, then open discovery.',
            identities=identities, stop_file=str(stop), network_calls=0,
            policy='PBS fixture positive; RRR actual production NULL; no source permission',
            current_card='Use registry PBS action: FICTIONAL QA current source card; '
                'distinct identity '+current_map['key'])
        ready_path=folder/'ready.json';ready_sha=_json_once(ready_path,ready)
        fixture_path=Path(__file__).resolve();fixture_sha=hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        receiver_sha=hashlib.sha256(RECEIVER.read_bytes()).hexdigest()
        if receiver_sha != RECEIVER_SHA:
            raise RuntimeError('PR91_PAIRED_RECEIVER_CHANGED')
        binding={'schema':'pr91-event-reference-browser/v1','packageSHA256':PACKAGE_SHA,
            'head':self.admission['head'],'tree':self.admission['tree'],'actualTarget':ACTUAL_TARGET,
            'fixture':{'path':str(fixture_path),'sha256':fixture_sha},
            'receiver':{'path':str(RECEIVER),'sha256':receiver_sha},
            'readyPath':str(ready_path),'readySHA256':ready_sha,
            'runtime':{'pid':os.getpid(),'startTicks':_process_start_ticks(),
                'started':started,'deadline':deadline}}
        binding_path=folder/'supervisor-binding.json';binding_sha=_json_once(binding_path,binding)
        print('EVENT_BROWSER_READY '+str(ready_path)+' '+ready_sha+' '+str(binding_path)+' '+binding_sha, flush=True)
        while not stop.exists() and time.time() < deadline:
            time.sleep(.2)
        _json_once(folder/'fixture-terminal.json',dict(
            schema='pr91-event-reference-fixture-terminal/v1',
            state='EXPLICIT_STOP' if stop.exists() else 'DEADLINE',
            candidate=self.admission, mocked_pbs_reads=acquire.call_count,
            blocked_effects=blocked_effects,
            history_rows=len(DiscoveryState.objects.get().history), network_calls=0,
            current_identity=current_map,runtime=binding['runtime'],
            listener_cleanup='PRE_TEARDOWN; outer supervisor must confirm process and listener closure'))
        self.assertTrue(stop.exists(), 'Original fixture deadline ended before explicit stop.')
        self.assertEqual(acquire.call_count,1,'Exactly one current PBS source action is permitted.')
        self.assertEqual(blocked_effects,[],'No blocked external effect may be treated as success.')
