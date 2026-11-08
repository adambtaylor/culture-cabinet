"""One owned file-backed normal launcher; synthetic providers, actual loopback HTTP.

The driver owns no authority/ORM access. Only the normal serving thread enters
native/recovery scopes; the cancellation port only touches an Event.
"""
import html
import http.client
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cabinet import portable_runtime as runtime


def main(folder):
    owner_thread = threading.get_ident()
    state = {'origin': None, 'observations': [], 'cancel_threads': [], 'errors': [], 'turns': 0}
    blocked = threading.Event(); cancelled = threading.Event(); finished = threading.Event()
    driver = None
    stack = ExitStack()
    original_make = runtime.make_server

    def audit(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo'):
            target = urlsplit(state['origin']) if state['origin'] else None
            host, port = (args[1][:2] if event == 'socket.connect' else args[:2])
            if target is None or (host, port) != ('127.0.0.1', target.port):
                raise AssertionError('No external transport')
        if event in ('subprocess.Popen', 'os.fork', 'os.forkpty', 'os.posix_spawn', 'os.exec'):
            raise AssertionError('No child provider/process')
    sys.addaudithook(audit)

    def make(boundary, application):
        nonlocal driver
        from django.conf import settings
        from django.db import connection, transaction
        from django.test import Client
        from contextvars import Context
        from shelf import grant_authority as ga, recovery, obtain_preview as preview, credential_store as store
        from shelf import obtain_runtime, completion_registry
        from shelf.models import ProfileCabinetState, ObtainTransfer
        from shelf.portable_saved_authority import (portable_saved_scope, selected_context,
                                                   active_portable_access)
        from shelf.portable_owner_tv import get_owner_tv, owner_backend, OwnerLookupForm
        from shelf.disclosure import canonical_bytes
        from shelf.tests.test_owner_tv_entry import OwnerPolicy, FixtureTVProvider
        from shelf.tests.test_portable_authority import Terminal, PASSWORD
        from shelf.tests.test_obtain_preview import payload, item
        from shelf.tests.test_obtain_acquisition import NZB
        from shelf.tests.test_acquisition_contract import CAPS
        from shelf.tests.test_acquisition_backend import grant
        from shelf.tests.fixtures_credentials import FakeTree
        from shelf.obtain_tv_flow import DeliveryBinding, DestinationBinding, ServiceGeneration
        from shelf.jellyfin_native import TV_ROOTS
        from shelf.acquisition_contract import COMPLETION_SCRIPT
        from shelf.tests.portable_fixture import BoundSocketLease
        lease = BoundSocketLease('native-selected-supervisor-file-launcher')
        lease.attach(boundary); stack.callback(lease.close)
        assert not str(connection.settings_dict['NAME']).startswith('file:memorydb_')
        authority = settings.PORTABLE_AUTHORITY
        supervisor = authority._selected_supervisor
        witness = recovery.installed_coordinator()
        assert supervisor.coordinator is witness and supervisor.owner_thread == owner_thread
        settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
        settings.PORTABLE_OBTAIN_SUPERVISE = True
        state['origin'] = boundary.origin
        client = Client(enforce_csrf_checks=True)
        headers = {'HTTP_HOST': boundary.origin.removeprefix('http://'), 'HTTP_ORIGIN': boundary.origin}
        def csrf():
            response = client.get('/setup/status', **headers)
            assert response.status_code == 200
            return response.json()['csrf_token']
        terminal = Terminal(); authority.issue_challenge(terminal)
        response = client.post('/setup/claim', json.dumps({'code': terminal.getvalue().strip(), 'passphrase': PASSWORD}),
            content_type='application/json', HTTP_X_CSRFTOKEN=csrf(), **headers)
        assert response.status_code == 200, response.content
        ref = client.session['portable_session']
        response = client.post('/setup/cabinet/create',
            urlencode({'expected_owner_revision': authority.authorize(ref).owner_revision}),
            content_type='application/x-www-form-urlencoded', HTTP_X_CSRFTOKEN=csrf(), **headers)
        assert response.status_code == 303, response.content
        cabinet = ProfileCabinetState.objects.get(); profile = str(cabinet.profile_ref)
        response = client.post('/cabinet/saved/select/', {'profile_ref': profile, 'return_to': 'owner_tv'},
            HTTP_X_CSRFTOKEN=csrf(), **headers)
        assert response.status_code == 303
        selection = client.session['portable_saved_selection']
        token = selected_context(authority, ref, selection)
        def scope():
            return portable_saved_scope(authority, ref, profile_ref=profile, selection_token=token, mutate=True)
        vault = authority._source_vault
        source = vault.configure(ref, {'adapter': 'tmdb_tv', 'model': '', 'reasoning': ''})
        vault.replace(ref, source.pk, source.config_revision, b'SYNTHETIC-TV-SOURCE')
        end = authority.now() + timedelta(seconds=600)
        ga.change_grant(authority, ref, profile_ref=profile, connection_ref=str(source.pk), expected_revision=0,
            purpose='catalogue_lookup', allowed_data_classes=['canonical_seed'], expires_at=end, action='grant')
        settings.PORTABLE_OWNER_TV_POLICY_PROVIDER = OwnerPolicy(authority.now())
        def source_send(operation, *, revalidate, **kwargs):
            assert threading.get_ident() == owner_thread and revalidate()
            return 200, canonical_bytes({'results': [{'id': 81001, 'name': 'Fiction Series',
                'first_air_date': '2024-05-08'}]}), (('cache-control', 'max-age=300'),)
        with scope() as access, patch('shelf.discovery_transport.media_authorized', side_effect=source_send):
            owner = get_owner_tv(access)
            value = owner.preview(title='Fiction Series', series_year=2024, season=2, episode=1)
            fields = {key: value[key] for key in OwnerLookupForm.base_fields if key != 'confirmed'}
            matches = owner.lookup({**fields, 'confirmed': 'on'}, owner_backend(access))
            selected = owner.select({key: matches[0][key] for key in ('intent_ref', 'match_ref', 'match_digest')}
                                    | {'confirmed': 'on'})
            origin, target = owner.resolve(selected, now=authority.now())
        # Actual scoped credential-store/Backend/reviewed-descriptor paths with
        # disposable obvious NONcredentials; only transport bytes are synthetic.
        trees = {}
        with patch('tempfile.tempdir', str(Path(folder))):
            for provider in ('hydra', 'sab'):
                tree = FakeTree(store, provider=provider); trees[provider] = tree
                stack.callback(tree.close)
                metadata = grant(provider)
                metadata = replace(metadata, binding=replace(metadata.binding,
                    actor_id=str(cabinet.actor_id), profile_id=str(cabinet.profile_id)))
                assert store.commit_entry(tree.store, metadata, bytearray(b'SYNTHETIC-NOT-A-KEY')).outcome == 'stored'
                tree.store.close()
        stack.enter_context(patch.object(store, 'open_store', side_effect=lambda *, provider:
            store._fixture_store(trees[provider].path, provider=provider)))
        def send(request, **kwargs):
            assert threading.get_ident() == owner_thread
            if request.operation == 'caps': return CAPS
            if request.operation == 'search':
                return payload(item(title='Fiction Series 2024 S02E01 1080p', category='5000'))
            if request.operation == 'get': return NZB
            assert request.operation == 'addfile'
            state['sends'] = state.get('sends', 0) + 1
            return b'{"status":true,"nzo_ids":["SABnzbd_nzo_synthetic"]}'
        transport = Mock(); transport.send.side_effect = send
        stack.enter_context(patch('shelf.acquisition_backend.LoopbackTransport', return_value=transport))
        with scope() as access:
            review = preview.issue_search_review(access=access, origin=origin, scope=target.scope)
            receipt = preview.search_origin(access=access, origin=origin, scope=target.scope,
                                           search_review_ref=review['search_review_ref'])
            result = receipt.results.get()
            job, _ = preview.confirm(actor=access.actor, profile_id=cabinet.profile_id, preview_id=receipt.pk,
                result_id=result.pk, data={'review_token': preview.review_token(receipt, result), 'scope': target.scope,
                    'edition': result.metadata['title'], 'storage': 'local_library',
                    'authorized_content': 'on', 'storage_check': 'on'})
        complete = Path(folder) / 'complete'; complete.mkdir(mode=0o700)
        policy = obtain_runtime.SubmissionPolicy(True, str(complete), 20000, 0, True,
            str(Path(folder) / 'registry'), True, COMPLETION_SCRIPT)
        settings.OBTAIN_SUBMISSION_POLICY = asdict(policy)
        delivery = DeliveryBinding(DestinationBinding('synthetic-destination', 'synthetic-host', TV_ROOTS[0],
            ServiceGeneration('destination_file', 'd'*64, 'synthetic-config', 'synthetic-key')),
            'Fiction Series 2024', False)
        fixture = FixtureTVProvider(delivery, cabinet.actor_id, cabinet.profile_id, str(cabinet.installation_id),
            end, SimpleNamespace(now=authority.now, monotonic=authority.monotonic))
        def cancel():
            state['cancel_threads'].append(threading.get_ident())
            assert not witness.closed
            cancelled.set()
        def services_for(context):
            assert threading.get_ident() == owner_thread and recovery.installed_coordinator() is witness
            value = fixture.current(context)
            def history(nzo):
                assert threading.get_ident() == owner_thread and recovery.installed_coordinator() is witness
                assert connection.connection.execute('SELECT 1').fetchone() == (1,)
                assert active_portable_access() is not None and recovery._BARRIER_STACK.get()
                state['observations'].append(time.monotonic())
                if state.get('interrupt'):
                    blocked.set()
                    try:
                        cancelled.wait(3)
                    finally:
                        state['interrupted_port_unwound'] = True
                return None
            def registration(nzo):
                return completion_registry.registry_entry(json.loads(Path(policy.registry_path).read_text()), nzo)
            return replace(value, observer=replace(value.observer, read_registration=registration,
                read_history=history, read_hook=lambda request: None), stop_owned_children=cancel)
        settings.PORTABLE_OBTAIN_TV_SERVICES = obtain_runtime.CurrentTVServiceProvider(authority,
            delivery_for=fixture.delivery_binding, services_for=services_for)
        server = original_make(boundary, application)
        original_handle = server.handle_request
        def handle():
            original_handle()
            assert active_portable_access() is None and not recovery._BARRIER_STACK.get()
            assert ga._CURRENT_WRITER.get() is None and not connection.in_atomic_block
            state['turns'] += 1
        server.handle_request = handle
        # Wrong-thread and absent-witness ticks are denied before ORM/ports.
        refusals = []
        def wrong_thread():
            try: supervisor.tick_once()
            except Exception as error: refusals.append(type(error).__name__)
        probe = threading.Thread(target=wrong_thread); probe.start(); probe.join(timeout=2)
        assert not probe.is_alive() and refusals == ['Blocked']
        try: Context().run(supervisor.tick_once)
        except Exception as error: assert type(error).__name__ == 'Blocked'
        else: raise AssertionError('Missing witness accepted')
        with scope():
            try: supervisor.tick_once()
            except Exception as error: assert type(error).__name__ == 'Blocked'
            else: raise AssertionError('Request scope accepted')
        with ga.recovery_guard(authority, ref):
            try: supervisor.tick_once()
            except Exception as error: assert type(error).__name__ == 'Blocked'
            else: raise AssertionError('Recovery barrier accepted')
        with transaction.atomic():
            try: supervisor.tick_once()
            except Exception as error: assert type(error).__name__ == 'Blocked'
            else: raise AssertionError('SQL scope accepted')
        boundary._closing.append(lambda: state.update(supervisor_closed_before_witness=
            supervisor.closed and witness.closed and not authority._obtain_workers))
        cookies = '; '.join(key + '=' + value.value for key, value in client.cookies.items())
        csrf_value = csrf()
        def drive():
            try:
                target = urlsplit(boundary.origin)
                cookie = cookies
                def request(method, path, fields=None):
                    nonlocal cookie
                    conn = http.client.HTTPConnection('127.0.0.1', target.port, timeout=5)
                    try:
                        body = urlencode(fields or {}) if method == 'POST' else None
                        conn.request(method, path, body=body, headers={'Host': target.netloc, 'Origin': boundary.origin,
                            'Cookie': cookie, 'X-CSRFToken': csrf_value,
                            'Content-Type': 'application/x-www-form-urlencoded'})
                        response = conn.getresponse(); data = response.read()
                        changes = response.getheaders()
                        jar = dict(part.split('=', 1) for part in cookie.split('; '))
                        for key, value in changes:
                            if key.lower() == 'set-cookie':
                                name, val = value.split(';', 1)[0].split('=', 1); jar[name] = val
                        cookie = '; '.join(key + '=' + value for key, value in jar.items())
                        return response.status, data
                    finally:
                        conn.close()
                base = {'profile_ref': profile, 'selection_token': token}
                status, body = request('POST', f'/cabinet/saved/jobs/{job.pk}/submit/review/', base)
                assert status == 200, (status, body)
                submission = html.unescape(re.search(rb'name="token" value="([^"]+)"', body)[1].decode())
                fields = {**base, 'token': submission,
                    **{key: 'on' for key in ('exact_selection', 'rights', 'scope', 'size', 'duplicates')}}
                status, body = request('POST', f'/cabinet/saved/jobs/{job.pk}/submit/', fields)
                assert status == 302, (status, body)
                worker = authority._obtain_workers[job.pk]
                original_deadlines = (worker.deadline, worker.monotonic_end)
                # The next automatic observation needs no browser/HTTP turn.
                until = time.monotonic() + 8
                while len(state['observations']) < 2 and time.monotonic() < until:
                    time.sleep(0.05)
                assert len(state['observations']) >= 2
                assert state['observations'][1] - state['observations'][0] >= 5
                status, body = request('GET', f'/cabinet/obtain/jobs/{job.pk}/status/')
                current = json.loads(body); assert status == 200 and current['state'] != 'done'
                status, body = request('POST', f'/cabinet/obtain/jobs/{job.pk}/advance/',
                    {**base, 'expected_revision': current['revision']})
                assert status == 302
                assert authority._obtain_workers[job.pk] is worker
                assert (worker.deadline, worker.monotonic_end) == original_deadlines
                # Real selection rotation stops custody through the monitor;
                # this thread performs HTTP only, never recovery/SQL authority.
                status, body = request('POST', '/cabinet/saved/select/', {'profile_ref': profile, 'return_to': 'owner_tv'})
                assert status == 303
                until = time.monotonic() + 2
                while not worker.done.is_set() and time.monotonic() < until: time.sleep(0.02)
                assert worker.done.is_set() and not worker.monitor.is_alive()
                status, body = request('GET', f'/cabinet/obtain/jobs/{job.pk}/')
                assert status == 200, (status, body)
                new_token = html.unescape(re.search(rb'name="selection_token" value="([^"]+)"', body)[1].decode())
                status, body = request('GET', f'/cabinet/obtain/jobs/{job.pk}/status/')
                current = json.loads(body)
                state['interrupt'] = True; cancelled.clear()
                status, body = request('POST', f'/cabinet/obtain/jobs/{job.pk}/advance/',
                    {'profile_ref': profile, 'selection_token': new_token, 'expected_revision': current['revision']})
                assert status == 302
                assert blocked.wait(2)
                os.kill(os.getpid(), signal.SIGTERM)
            except BaseException as error:
                state['errors'].append(type(error).__name__ + ': ' + str(error))
                os.kill(os.getpid(), signal.SIGTERM)
            finally:
                finished.set()
        driver = threading.Thread(target=drive, name='synthetic-http-driver')
        driver.start()
        state.update(authority=authority, witness=witness, server=server)
        return server

    try:
        with patch.object(runtime, 'make_server', side_effect=make):
            result = runtime.run(Path(folder) / 'owner')
        if driver is not None:
            driver.join(timeout=3)
        assert driver is not None and not driver.is_alive() and finished.is_set()
        assert result == 0, result
        assert not state['errors'], state['errors']
        assert state['sends'] == 1 and state['turns'] > 2
        assert state['interrupted_port_unwound']
        assert state['supervisor_closed_before_witness']
        assert state['witness'].closed and state['server'].socket.fileno() == -1
        assert not [thread for thread in threading.enumerate() if thread.name.startswith('obtain-bound-')]
        print('NATIVE_SUPERVISOR_FILE_LAUNCHER_PASS ' + json.dumps({'sends': state['sends'],
            'observations': len(state['observations']), 'minimum_spacing': state['observations'][1] - state['observations'][0],
            'serving_thread': owner_thread, 'cancel_threads': state['cancel_threads'], 'all_closed': True}), flush=True)
    finally:
        if driver is not None and driver.is_alive():
            cancelled.set(); driver.join(timeout=3)
        stack.close()


if __name__ == '__main__':
    main(sys.argv[1])
