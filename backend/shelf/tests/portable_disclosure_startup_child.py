"""Fresh-interpreter normal startup proof with synthetic neutral wire only."""
import json
import os
from pathlib import Path
import pty
import sys
from urllib.parse import urlencode
import uuid
from unittest.mock import patch

# This entry is launched by the existing isolated child with the backend on path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from cabinet import portable_runtime as runtime


def main():
    root, mode = Path(sys.argv[1]), sys.argv[2]
    root.parent.mkdir(parents=True, exist_ok=True)
    challenge = []
    observed = {}
    real_make = runtime.make_server

    def private_challenge(authority, stream):
        master, slave = pty.openpty()
        try:
            with os.fdopen(slave, 'w') as terminal:
                observed['real_challenge'](authority, terminal)
                challenge.append(os.read(master, 256).decode().strip())
        finally:
            os.close(master)

    def make_server(boundary, application):
        from django.conf import settings
        from django.test import Client
        from shelf import grant_authority as ga
        from shelf.disclosure import canonical_bytes
        from shelf.inference_transport import CheckedTransport, ProbePermit
        from shelf.tests.test_inference_adapters import response_document
        server = real_make(boundary, application)
        observed.update(boundary=boundary, server=server, authority=settings.PORTABLE_AUTHORITY)

        def journey():
            authority = settings.PORTABLE_AUTHORITY
            validation = settings.PORTABLE_CONNECTIONS
            client = Client(enforce_csrf_checks=True)
            headers = {'HTTP_HOST': boundary.origin.removeprefix('http://'), 'HTTP_ORIGIN': boundary.origin}
            def post(path, fields, *, form=False):
                token = client.get('/setup/status', **headers).json()['csrf_token']
                return client.post(path, urlencode(fields) if form else json.dumps(fields),
                    content_type='application/x-www-form-urlencoded' if form else 'application/json',
                    HTTP_X_CSRFTOKEN=token, **headers)
            password = 'Only synthetic startup test credential 8!'
            assert post('/setup/claim', {'code': challenge.pop(), 'passphrase': password}).status_code == 200
            ref = client.session['portable_session']
            created = post('/setup/cabinet/create', dict(
                expected_owner_revision=str(authority.authorize(ref).owner_revision)), form=True)
            assert created.status_code == 303
            assert client.get(created['Location'], **headers).status_code == 200
            from shelf.models import ProfileCabinetState
            profile = str(ProfileCabinetState.objects.get().profile_ref)
            configured = post('/setup/connections/configure', dict(adapter='openai_responses',
                model='gpt-6-astra', reasoning='high'))
            assert configured.status_code == 200
            connection = configured.json()['connection_ref']
            binding = dict(connection_ref=connection, revision=1)
            # Missing credentials cannot authorize inference even though creation
            # of a consent record and transport readiness are separate contracts.
            try:
                ga.current_view(authority, ref, profile_ref=profile, connection_ref=connection, purpose='inference')
            except ga.AuthorityFault:
                pass
            else:
                raise AssertionError('Absent credential authorized inference')
            assert post('/setup/connections/credential', {**binding,
                'secret': 'SYNTHETIC-STARTUP-NOT-A-KEY'}).status_code == 200
            preview = post('/setup/connections/preview', binding)
            assert preview.status_code == 200
            def neutral_wire(transport, body, permit):
                assert type(permit) is ProbePermit
                permit.authorize(body)
                return canonical_bytes(response_document())
            with patch.object(CheckedTransport, 'send', neutral_wire):
                confirmed = post('/setup/connections/confirm', dict(
                    preview_ref=preview.json()['preview_ref'], intent_ref=str(uuid.uuid4()),
                    confirm_billable_test=True))
            assert confirmed.status_code == 200
            consent = post('/cabinet/consent/change', dict(profile_ref=profile,
                connection_ref=connection, expected_revision='0', action='grant', confirmed='yes',
                allow_canonical_seed='yes'), form=True)
            # On the old startup this exact normal caller returns503: no helper
            # constructs the missing service or repairs private registrations.
            print('NATIVE_INFERENCE_CONSENT_STATUS', consent.status_code, flush=True)
            assert consent.status_code == 303, ('native consent', consent.status_code)
            assert client.get(consent['Location'], **headers).status_code == 200
            services = authority._disclosure_services
            assert services.authority is authority and services.validation is validation
            assert authority._disclosure_validation is validation and validation.vault.authority is authority
            assert not hasattr(authority, '_disclosure_fixture_token')
            ga.current_view(authority, ref, profile_ref=profile, connection_ref=connection, purpose='inference')
            assert post('/setup/connections/credential', {**binding,
                'secret': 'SYNTHETIC-REPLACEMENT-NOT-A-KEY'}).status_code == 200
            try:
                ga.current_view(authority, ref, profile_ref=profile, connection_ref=connection, purpose='inference')
            except ga.AuthorityFault:
                pass
            else:
                raise AssertionError('Changed credential retained inference authority')
            observed['journey'] = True
            raise runtime.PortableStop()
        server.handle_request = journey
        return server

    # Constructor failure must abort before accepting requests. Patch its own
    # constructor only in this negative case, never install replacement authority.
    failure = mode == 'constructor_failure'
    original_configure = runtime.configure
    from contextlib import ExitStack
    with ExitStack() as patches:
        def configure(boundary):
            original_configure(boundary)
            from shelf.portable_authority import PortableAuthority
            observed['real_challenge'] = PortableAuthority.issue_challenge
            patches.enter_context(patch.object(PortableAuthority, 'issue_challenge', private_challenge))
            observed['boundary'] = boundary
            if failure:
                from shelf.portable_disclosure import DisclosureServices
                patches.enter_context(patch.object(DisclosureServices, '__init__',
                    side_effect=RuntimeError('synthetic constructor refusal')))
        patches.enter_context(patch.object(runtime, 'configure', configure))
        patches.enter_context(patch.object(runtime, 'make_server', make_server))
        result = runtime.run(root)
    boundary = observed['boundary']
    assert boundary._socket.fileno() == -1
    from django.db import connections
    assert all(c.connection is None for c in connections.all())
    print('NORMAL_STARTUP_CLEANUP socketClosed=True databaseClosed=True', flush=True)
    assert result == (1 if failure else 0), ('run result', result)
    if failure:
        assert 'server' not in observed
    else:
        assert observed['journey'] and observed['server'].error_stream.closed
        assert not observed['authority']._disclosure_services._collections
        assert not observed['authority']._disclosure_services._live
    print('NORMAL_STARTUP_DISCLOSURE_PASS '+mode)


if __name__ == '__main__':
    main()
