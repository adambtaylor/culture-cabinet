"""Eight actual CLI starts total, all isolated/no-egress, no billable send."""
from contextlib import closing
import http.client
import os
import signal
import threading
import time
from http.cookies import SimpleCookie
import json
from pathlib import Path
import secrets
import socket
import sqlite3
import tempfile
from urllib.parse import urlencode
import uuid

from django.test import SimpleTestCase
from .portable_fixture import PortableFixture, PortableFixtureFailure

SOURCE = Path(__file__).resolve().parents[3]


class LocalHTTP:
    def __init__(self, origin):
        self.origin = origin
        self.port = int(origin.rsplit(':', 1)[1])
        self.cookies = SimpleCookie()
        self.csrf = None

    def request(self, path, *, method='GET', data=None, form=False, headers=None, csrf=True):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('fixed local paths only')
        extra = {'Host': '127.0.0.1:'+str(self.port), 'Cookie': '; '.join(k+'='+v.coded_value for k,v in self.cookies.items())}
        body = None
        if method == 'POST':
            extra.update({'Origin':self.origin, 'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json'})
            if csrf and self.csrf:
                extra['X-CSRFToken'] = self.csrf
            body = urlencode(data or {}) if form else json.dumps(data or {})
        extra.update(headers or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=extra)
            response = conn.getresponse()
            raw = response.read()
            pairs = response.getheaders()
            for name, value in pairs:
                if name.lower() == 'set-cookie':
                    self.cookies.load(value)
            if response.status == 303:
                # Follow only canonical local GET; never resend a body or follow
                # an arbitrary redirect. Existing semantic assertions inspect GET.
                location = dict(pairs)['Location']
                if not (location == '/setup' or location.startswith('/setup?connection_ref=')):
                    raise AssertionError('unexpected redirect target')
                return self.request(location)
            return response.status, dict(pairs), raw
        finally:
            conn.close()

    def status(self):
        status, _, body = self.request('/setup/status')
        if status != 200:
            raise AssertionError('owner status unavailable')
        value = json.loads(body)
        self.csrf = value['csrf_token']
        return value


class PortableLaunchTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.password = secrets.token_urlsafe(24)+'T9!'
        self.key = secrets.token_urlsafe(32)

    def fixture(self, name, **kwargs):
        f = PortableFixture(SOURCE, self.root/name, self.root/(name+'-'+secrets.token_hex(4)+'.audit'), **kwargs)
        self.addCleanup(self.check_stop, f)
        return f

    def check_stop(self, f):
        self.assertEqual(f.stop(), dict(remaining_owned_pids=[],listener_closed=True,lock_released=True))
        self.assertEqual(f.audit_count, 0)
        self.assertFalse(f.emergency_kill_used)
        self.assertEqual(f.teardown_errors, [])
        if f.origin:
            self.assertEqual(f.graceful_exit_code, 0)

    def db(self, f, sql):
        with closing(sqlite3.connect(Path(f.data_root/'portable.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
            return db.execute(sql).fetchall()

    def claim_json(self, f):
        client = LocalHTTP(f.origin)
        client.status()
        status, _, _ = client.request('/setup/claim', method='POST', data={'code':f.claim_code,'passphrase':self.password})
        self.assertEqual(status, 200)
        self.assertTrue(client.status()['authenticated'])
        return client

    def configure_json(self, client):
        status, _, raw = client.request('/setup/connections/configure',method='POST',data={'adapter':'openai_responses','model':'gpt-6-astra','reasoning':'high'})
        self.assertEqual(status,200)
        return json.loads(raw)

    def test_actual_html_commands_and_security_guards(self):
        f = self.fixture('flow').start()
        client = LocalHTTP(f.origin)
        status, headers, body = client.request('/setup')
        self.assertEqual(status, 200)
        self.assertIn(b'data-state="claim"', body)
        self.assertNotIn(f.claim_code.encode(), body)
        self.assertIn("style-src 'self'", headers['Content-Security-Policy'])
        self.assertIn("form-action 'self'", headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        client.status()
        status, _, body = client.request('/setup/form/claim',method='POST',form=True,data={'code':'x'*32,'passphrase':self.password})
        self.assertEqual(status,403)
        self.assertNotIn(self.password.encode(),body)
        status, _, body = client.request('/setup/form/claim',method='POST',form=True,data={'code':f.claim_code,'passphrase':self.password})
        self.assertEqual(status,200)
        self.assertIn(b'data-state="model"',body)
        client.status()
        self.assertEqual(self.db(f,'SELECT COUNT(*) FROM shelf_feedbacktarget'),[(0,)])
        self.assertEqual(self.db(f,'SELECT COUNT(*) FROM shelf_recoveryanchor'),[(1,)])
        self.assertEqual(self.db(f,'SELECT COUNT(*) FROM shelf_profile'),[(0,)])
        self.assertEqual(self.db(f,'SELECT COUNT(*) FROM shelf_grant'),[(0,)])
        status, _, _ = client.request('/setup/style.css')
        self.assertEqual(status,200)
        status, _, body = client.request('/setup/form/configure',method='POST',form=True,data={'adapter':'openai_responses','model':'gpt-6-astra','reasoning':'high'})
        self.assertEqual(status,200)
        self.assertIn(b'data-configured="true"',body)
        ref = str(uuid.UUID(self.db(f,'SELECT id FROM shelf_portableconnection')[0][0]))
        fields = {'connection_ref':ref,'revision':'1'}
        status, _, body = client.request('/setup/form/credential',method='POST',form=True,data={**fields,'secret':self.key})
        self.assertEqual(status,200)
        self.assertIn(b'data-credential-configured="true"',body)
        self.assertNotIn(self.key.encode(),body)
        status, _, body = client.request('/setup/form/preview',method='POST',form=True,data=fields)
        self.assertEqual(status,200)
        self.assertIn(b'api.openai.com',body)
        self.assertIn(b'2048',body)
        self.assertIn(b'data-form="confirm"',body)
        self.assertNotIn(self.key.encode(),body)
        self.assertEqual(self.db(f,'SELECT COUNT(*) FROM shelf_portableprobeattempt'),[(0,)])
        status, _, _ = client.request('/setup/form/confirm',method='POST',form=True,data={'preview_ref':str(uuid.uuid4()),'intent_ref':str(uuid.uuid4()),'confirm_billable_test':'no'})
        self.assertEqual(status,422)
        self.assertEqual(self.db(f,'SELECT COUNT(*) FROM shelf_portableprobeattempt'),[(0,)])
        # Same origin, CSRF and raw Host are independent guards, not alternatives.
        for forged in [{'Host':'localhost:'+str(client.port)}, {'Origin':'https://foreign.invalid'},
                       {'X-Forwarded-For':'127.0.0.1'}, {'Sec-Fetch-Site':'cross-site'}]:
            self.assertEqual(client.request('/setup/form/remove',method='POST',form=True,data=fields,headers=forged)[0],403)
        self.assertEqual(client.request('/setup/form/remove',method='POST',form=True,data=fields,csrf=False)[0],403)
        self.assertEqual(client.request('/setup/form/remove',method='POST',data=fields)[0],422)
        status, _, body = client.request('/setup/form/remove',method='POST',form=True,data=fields)
        self.assertEqual(status,200)
        self.assertIn(b'data-credential-configured="false"',body)
        self.assertIn(b'gpt-6-astra',body)
        for cookie in client.cookies.values():
            self.assertTrue(cookie['httponly'])
            self.assertEqual(cookie['samesite'],'Strict')
        self.assertEqual(client.request('/setup/form/logout',method='POST',form=True)[0],200)
        self.assertFalse(client.status()['authenticated'])
        disk=(f.data_root/'portable.sqlite3').read_bytes()
        for canary in [self.password,self.key,f.claim_code]:
            self.assertNotIn(canary.encode(),disk)
        self.assertEqual(f.audit_count,0)
        # Reuse this child: keep an accepted header incomplete with activity
        # faster than its inactivity timeout, then demand genuine graceful exit.
        slow = socket.create_connection(('127.0.0.1', client.port), timeout=1)
        done = threading.Event()
        def drip():
            while not done.wait(.1):
                try:
                    slow.sendall(b'x')
                except OSError:
                    return
        thread = threading.Thread(target=drip, daemon=True)
        try:
            slow.sendall(b'GET /setup HTTP/1.1\r\nX-Slow: ')
            thread.start()
            time.sleep(.25)
            started = time.monotonic()
            os.kill(f.pid, signal.SIGTERM)
            self.assertEqual(f.process.wait(timeout=3), 0)
            self.assertLess(time.monotonic()-started, 3)
            self.check_stop(f)
            self.assertFalse(f.emergency_kill_used)
        finally:
            done.set()
            if thread.ident is not None:
                thread.join(timeout=1)
            slow.close()

    def test_restart_restores_metadata_not_key_or_session(self):
        first=self.fixture('restart').start()
        client=self.claim_json(first)
        config=self.configure_json(client)
        self.assertEqual(client.request('/setup/connections/credential',method='POST',data={**{k:config[k] for k in ['connection_ref','revision']},'secret':self.key})[0],200)
        old_cookies='; '.join(k+'='+v.coded_value for k,v in client.cookies.items())
        self.check_stop(first)
        second=self.fixture('restart').start()
        self.assertIsNone(second.claim_code)
        resumed=LocalHTTP(second.origin)
        self.assertFalse(resumed.status()['authenticated'])
        self.assertEqual(resumed.request('/setup',headers={'Cookie':old_cookies})[0],200)
        self.assertFalse(resumed.status()['authenticated'])
        self.assertEqual(resumed.request('/setup/login',method='POST',data={'passphrase':self.password})[0],200)
        resumed.status()
        status,_,raw=resumed.request('/setup/connections/status?'+urlencode({k:config[k] for k in ['connection_ref','revision']}))
        self.assertEqual(status,200)
        value=json.loads(raw)
        self.assertFalse(value['credential_configured'])
        self.assertFalse(value['connection_ready'])
        self.assertFalse(value['run_ready'])
        self.assertEqual(value['config']['model'],'gpt-6-astra')
        self.assertEqual(resumed.request('/setup/claim',method='POST',data={'code':'x'*32,'passphrase':self.password})[0],403)
        self.assertEqual(self.db(second,'SELECT COUNT(*) FROM auth_user'),[(1,)])

    def test_foreign_store_not_adopted(self):
        root=self.root/'foreign';root.mkdir(mode=0o700)
        original=root/'unrelated';original.write_bytes(b'synthetic-foreign-content')
        f=self.fixture('foreign')
        with self.assertRaises(PortableFixtureFailure):f.start()
        self.assertEqual(f.exit_code,1)
        self.assertEqual(original.read_bytes(),b'synthetic-foreign-content')
        self.assertFalse((root/'.portable-installation.json').exists())
        self.assertFalse((root/'portable.sqlite3').exists())

    def test_occupied_port_refused_without_fallback(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1',0));occupied.listen()
            f=self.fixture('occupied',port=occupied.getsockname()[1])
            with self.assertRaises(PortableFixtureFailure):f.start()
            self.assertEqual(f.exit_code,1)
            self.assertIsNone(f.origin)
            self.assertGreater(occupied.fileno(),0)

    def test_inherited_settings_refused_before_root_creation(self):
        f=self.fixture('environment',environment={'DJANGO_SETTINGS_MODULE':'cabinet.settings'})
        with self.assertRaises(PortableFixtureFailure):f.start()
        self.assertEqual(f.exit_code,1)
        self.assertFalse(f.data_root.exists())

    def test_two_installations_do_not_share_owner_or_cookies(self):
        first=self.fixture('one').start();second=self.fixture('two').start()
        client=self.claim_json(first)
        other=LocalHTTP(second.origin)
        self.assertFalse(other.status()['claimed'])
        self.assertFalse(other.status()['authenticated'])
        status,_,body=other.request('/setup',headers={'Cookie':'; '.join(k+'='+v.coded_value for k,v in client.cookies.items())})
        self.assertEqual(status,200)
        self.assertIn(b'data-state="claim"',body)
        self.assertFalse(other.status()['authenticated'])
        self.assertEqual(other.request('/setup',headers={'Host':first.origin.removeprefix('http://')})[0],403)
        self.assertNotEqual(json.loads((first.data_root/'.portable-installation.json').read_text())['installation_id'],json.loads((second.data_root/'.portable-installation.json').read_text())['installation_id'])
