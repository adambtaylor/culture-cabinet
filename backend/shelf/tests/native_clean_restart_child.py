"""Owned real native processes with a private dummy Secret Service port.

This checks native lifecycle/callers, not OS collection encryption. No provider
adapter, fixture authority, synthetic restart or real credential is installed.
"""
import hashlib
import json
import os
from pathlib import Path
import pty
import signal
import struct
import sys
from urllib.parse import urlencode
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cabinet import portable_runtime as runtime

DUMMY = 'NONCREDENTIAL-NATIVE-CLEAN-RESTART-ONLY'
PASSWORD = 'Dummy native checkpoint password 884!'


def read_exact(fd, size):
    result = b''
    while len(result) < size:
        block = os.read(fd, size-len(result))
        if not block:
            raise ValueError('Private fixture IPC ended')
        result += block
    return result



def seed_selection_history(authority,ref,profile,connection):
    """Fictional old rows test restart retirement, not inference readiness."""
    from datetime import datetime,timezone
    from django.db import transaction
    from shelf import grant_authority as ga
    from shelf.models import SynthesisRun,_content_digest
    from shelf.enhance_contract import EnhanceBindings
    from shelf.enhance_selection import admission_fingerprint
    from shelf.category_registry import registry_view
    expected=[]
    with ga.current_writer(authority,ref,profile) as state,transaction.atomic(),ga._write_window():
        registry=registry_view()
        record=dict(bindings=vars(EnhanceBindings(str(state.installation_id),str(state.actor_ref),profile,
            'a'*64,state.policy_epoch,str(state.restriction_watermark),registry.revision,connection.config_revision,
            'b'*64,'c'*64)),categories=list(registry.categories),source_revision='fictional-restart-history')
        for noise in (None,75):
            refresh=uuid.uuid4()
            evidence=dict(code='OK',outcomes=[],phase='catalogue',expected_publication_revision=state.publication_revision)
            if noise is not None:evidence['selection_policy']={'version':'selection-policy/v1','noise':noise}
            run=SynthesisRun.objects.create(actor_id=state.actor_id,profile_id=state.profile_id,mode='portable',
                installation_id=state.installation_id,portable_connection=connection,contract_version='enhance/v1',
                client_key=uuid.uuid4(),refresh_nonce=refresh,input_snapshot={},
                input_digest=admission_fingerprint(record,refresh,evidence),portable_bindings=record,
                portable_stage='authorized',portable_expires_at=datetime(2026,1,1,tzinfo=timezone.utc),evidence=evidence)
            if noise is not None:
                SynthesisRun.objects.filter(pk=run.pk).update(revision=1,attempts=1,state='running',
                    portable_stage='sending',transport_request_id=uuid.uuid4(),collection_digest='c'*64,body_digest='d'*64)
                run.refresh_from_db()
            expected.append(dict(id=str(run.pk),policy=noise,input_digest=run.input_digest,
                content_digest=_content_digest(json.dumps(run.output),json.dumps(run.evidence)),
                attempts=run.attempts,request=str(run.transport_request_id) if run.transport_request_id else None))
    return expected


def main(root, phase, request_fd, response_fd):
    original = runtime.make_server
    observed = {}
    def no_provider(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'):
            raise ValueError('No provider/child work in native checkpoint fixture')
    sys.addaudithook(no_provider)
    def store(value):
        raw = json.dumps(value, separators=(',', ':')).encode()
        assert len(raw) <= 65536
        data = struct.pack('>I', len(raw))+raw
        while data:
            data = data[os.write(request_fd, data):]
        size = struct.unpack('>I', read_exact(response_fd, 4))[0]
        assert 0 < size <= 65536
        return json.loads(read_exact(response_fd, size))

    def make(boundary, application):
        server = original(boundary, application)
        try:
            from django.conf import settings
            from django.test import Client
            from shelf.models import ProfileCabinetState, PortableConnection, FeedbackEvent
            settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
            client = Client(enforce_csrf_checks=True)
            headers = {'HTTP_HOST': boundary.origin.removeprefix('http://'), 'HTTP_ORIGIN': boundary.origin}
            def post(path, fields):
                token = client.get('/setup/status', **headers).json()['csrf_token']
                return client.post(path, urlencode({'csrfmiddlewaretoken':token, **fields}),
                    content_type='application/x-www-form-urlencoded', HTTP_X_CSRFTOKEN=token, **headers)
            refs_path = Path(root).parent/'native-proof-refs.json'
            if phase.startswith('first'):
                master, slave = pty.openpty()
                try:
                    with os.fdopen(slave, 'w') as terminal:
                        settings.PORTABLE_AUTHORITY.issue_challenge(terminal)
                        code = os.read(master, 128).decode().strip()
                finally:
                    os.close(master)
                assert post('/setup/form/claim', {'code':code, 'passphrase':PASSWORD}).status_code == 303
                code = None
                ref = client.session['portable_session']
                created = post('/setup/cabinet/create', {'expected_owner_revision':
                    str(settings.PORTABLE_AUTHORITY.authorize(ref).owner_revision)})
                assert created.status_code == 303
                profile = str(ProfileCabinetState.objects.get().profile_ref)
                assert post('/setup/form/configure', {'adapter':'openai_responses',
                    'model':'gpt-6-astra', 'reasoning':'high'}).status_code == 303
                row = PortableConnection.objects.get(adapter='openai_responses')
                assert post('/setup/form/credential', {'connection_ref':str(row.pk), 'revision':'1',
                    'storage':'persistent', 'secret':DUMMY}).status_code == 303
                fields = {'profile_ref':profile, 'entry_ref':str(uuid.uuid4()), 'mutation_id':str(uuid.uuid4()),
                    'base_revision':'0','operation':'set','signal':'interest','entry_kind':'item',
                    'category':'book','title':'Dummy preference retained across native restart','value':'yes'}
                assert post('/cabinet/taste',fields).status_code == 303
                note = {**fields, 'entry_ref':str(uuid.uuid4()), 'mutation_id':str(uuid.uuid4()),
                    'signal':'note', 'entry_kind':'category_note','text':'Exact dummy note\n雪 preserved'}
                note.pop('title'); note.pop('value')
                assert post('/cabinet/taste',note).status_code == 303
                refs = {'installation_id':str(boundary.installation_id), 'generation':str(boundary.process_generation),
                    'profile_ref':profile, 'connection_ref':str(row.pk),
                    'events':[(str(r.pk),hashlib.sha256(json.dumps(r.body,sort_keys=True).encode()).hexdigest())
                              for r in FeedbackEvent.objects.order_by('id')]}
                if phase=='first-selection':refs['selection_runs']=seed_selection_history(settings.PORTABLE_AUTHORITY,ref,profile,row)
                refs_path.write_text(json.dumps(refs))
            else:
                refs = json.loads(refs_path.read_text())
                assert str(boundary.installation_id) == refs['installation_id']
                assert str(boundary.process_generation) != refs['generation']
                assert post('/setup/form/login', {'passphrase':PASSWORD}).status_code == 303
                ref = client.session['portable_session']
                row = PortableConnection.objects.get(pk=refs['connection_ref'])
                value = settings.PORTABLE_CONNECTIONS.vault.resolve(ref,row.pk,row.config_revision)
                assert value == DUMMY.encode()
                assert not settings.PORTABLE_CONNECTIONS.status(ref,row.pk,row.config_revision)['connection_ready']
                assert str(ProfileCabinetState.objects.get().profile_ref) == refs['profile_ref']
                events = [(str(r.pk),hashlib.sha256(json.dumps(r.body,sort_keys=True).encode()).hexdigest())
                          for r in FeedbackEvent.objects.order_by('id')]
                assert [list(r) for r in events] == refs['events']
            if phase=='second-selection':
                from shelf.models import SynthesisRun
                from shelf.recovery import semantic_check
                from django.db import connection
                for expected in refs['selection_runs']:
                    run=SynthesisRun.objects.get(pk=expected['id'])
                    assert run.evidence==({} if expected['policy'] is None else
                        {'selection_policy':{'version':'selection-policy/v1','noise':expected['policy']}})
                    assert run.output=={} and run.input_digest==expected['input_digest'] and run.content_digest==expected['content_digest']
                    assert run.attempts==expected['attempts']
                    assert (str(run.transport_request_id) if run.transport_request_id else None)==expected['request']
                    if run.attempts:assert run.state=='unknown'
                semantic_check(connection.connection)
                observed['selection_retention']='EXACT_POLICY_OR_LEGACY_EMPTY'
            observed.update(installation=str(boundary.installation_id), generation=str(boundary.process_generation),
                profile=str(ProfileCabinetState.objects.get().profile_ref), events=FeedbackEvent.objects.count())
            if phase=='first-pending':
                from shelf.recovery import installed_coordinator
                current=installed_coordinator()
                current.prepare(str(uuid.uuid4()),[{'kind':'revoke',
                    'profile_id':ProfileCabinetState.objects.get().profile_id,'target_id':None,
                    'signal':None,'scope':None,'subject_id':None,'restriction_code':'deny_export'}])
            if phase=='first-cleanup-failure':
                settings.PORTABLE_AUTHORITY._selected_supervisor.failed=True
            if phase == 'first-hold':
                print('NATIVE_CHECKPOINT_HOLD_READY',flush=True)
            else:
                def finish():
                    os.kill(os.getpid(),signal.SIGHUP)
                server.handle_request = finish
            return server
        except BaseException:
            server.server_close();server.error_stream.close()
            raise
    with patch.object(runtime,'make_server',make):
        result = runtime.run(root,saved_store=store)
    print(json.dumps({'state':'NATIVE_DUMMY_LIFECYCLE_CLOSED','phase':phase,'exit':result,
        'pid':os.getpid(),'observed':observed,'providerCalls':0,'keyReentry':phase.startswith('first')}),flush=True)
    return result


if __name__=='__main__':
    try:
        raise SystemExit(main(sys.argv[1],sys.argv[2],int(sys.argv[3]),int(sys.argv[4])))
    except Exception:
        print('NATIVE_DUMMY_LIFECYCLE_REFUSED',flush=True)
        raise SystemExit(2) from None
