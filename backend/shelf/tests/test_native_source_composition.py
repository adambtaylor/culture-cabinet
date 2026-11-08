"""Actual startup composition through the existing issuer, with synthetic input."""
from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
from unittest.mock import patch

from django.test import SimpleTestCase

from cabinet import portable_runtime as runtime
from shelf.enhance_contract import EnhanceFault
from shelf.discovery_sources import SourceError
from shelf.source_authority import SourceRights
from .test_grant_authority import AuthorityCase
from .test_source_evidence import policy


class NativeSourceCompositionTests(AuthorityCase):
    def test_factory_rejects_ambiguous_invalid_and_failed_inputs_without_fallback(self):
        calls = []
        def factory():
            calls.append('called')
            return {}
        for inputs in ({'approved_policies': {}, 'approved_policies_factory': factory},
                       {'approved_policies_factory': 'untrusted.module.name'}):
            with self.subTest(inputs=list(inputs)), self.assertRaises(runtime.RuntimeFailure):
                runtime._compose_enhance(self.authority, self.service, **inputs)
            # run must reject the same invalid inputs before opening a root.
            with patch('cabinet.portable_boundary.open_boundary',
                       side_effect=AssertionError('No runtime setup')) as opened:
                self.assertEqual(runtime.run('unused-synthetic-root', **inputs), 1)
                opened.assert_not_called()
        self.assertEqual(calls, [])
        for value in (None, [], {'musicbrainz_recording': SourceRights(policy(), 1,
                'synthetic-terms', 'synthetic-privacy', 'synthetic-locale',
                'CultureCabinet/fixture (example.invalid)', None, source='tmdb_movie')},
                {'tmdb_movie': SourceRights(replace(policy(), expires_at=self.authority.now()), 1,
                    'synthetic-terms', 'synthetic-privacy', 'synthetic-locale',
                    'CultureCabinet/fixture (example.invalid)', None, source='tmdb_movie')},
                {'musicbrainz_recording': SourceRights(policy(), 1,
                    'synthetic-terms', 'synthetic-privacy', 'synthetic-locale',
                    'CultureCabinet/fixture (not-a-contact)', None, source='musicbrainz_recording')}):
            before = len(calls)
            def invalid():
                calls.append('invalid')
                return value
            with self.subTest(kind=type(value).__name__), self.assertRaises(
                    (runtime.RuntimeFailure, EnhanceFault, SourceError)):
                runtime._compose_enhance(self.authority, self.service, approved_policies_factory=invalid)
            self.assertEqual(len(calls), before + 1)
            self.assertFalse(hasattr(self.authority, '_enhance_service'))
        def failed():
            calls.append('failed')
            raise RuntimeError('synthetic factory failure')
        with self.assertRaisesRegex(RuntimeError, 'synthetic factory failure'):
            runtime._compose_enhance(self.authority, self.service, approved_policies_factory=failed)
        self.assertEqual(calls.count('failed'), 1)
        self.assertFalse(hasattr(self.authority, '_enhance_service'))

    def test_explicit_empty_factory_is_consumed_once_and_remains_default_denial(self):
        calls = []
        def empty():
            calls.append('empty')
            return {}
        provider = runtime._compose_enhance(self.authority, self.service, approved_policies_factory=empty)
        self.assertEqual(calls, ['empty'])
        self.assertEqual(dict(provider._entries), {})
        self.assertIs(self.authority._enhance_service._policy_provider, provider)

    def test_startup_provider_reaches_actual_source_issuer_and_preserves_deadline(self):
        profile = self.bind()
        vault = self.authority._source_vault
        row = vault.configure(self.ref, {'adapter': 'tmdb_movie', 'model': '', 'reasoning': ''})
        vault.replace(self.ref, row.pk, row.config_revision, b'SYNTHETIC-SOURCE')
        now = self.authority.now()
        rights = SourceRights(replace(policy(), checked_at=now,
            expires_at=now + timedelta(minutes=5)), 1, 'synthetic-terms', 'synthetic-privacy',
            'synthetic-locale', 'CultureCabinet/fixture (example.invalid)',
            'synthetic-processor', source='tmdb_movie')
        with patch('socket.socket.connect', side_effect=AssertionError('No provider calls')):
            provider = runtime._compose_enhance(self.authority, self.service,
                approved_policies={'tmdb_movie': rights})
            service = self.authority._enhance_service
            current, _ = service._normal_context(self.ref, profile)
            issuer, = current.sources
            self.assertIs(issuer.provider, provider)
            self.assertEqual(issuer._rights(now), rights)
            with self.assertRaises(EnhanceFault) as expired:
                issuer._rights(rights.policy.expires_at)
            self.assertEqual(expired.exception.code, 'AUTHORITY_DENIED')

    def test_missing_host_entries_keep_real_composition_denied(self):
        profile = self.bind()
        vault = self.authority._source_vault
        row = vault.configure(self.ref, {'adapter': 'hardcover', 'model': '', 'reasoning': ''})
        vault.replace(self.ref, row.pk, row.config_revision, b'SYNTHETIC-SOURCE')
        with patch('socket.socket.connect', side_effect=AssertionError('No provider calls')):
            provider = runtime._compose_enhance(self.authority, self.service, approved_policies=None)
            current, _ = self.authority._enhance_service._normal_context(self.ref, profile)
            issuer, = current.sources
            self.assertIs(issuer.provider, provider)
            with self.assertRaises(EnhanceFault) as missing:
                issuer._rights(self.authority.now())
            self.assertEqual(missing.exception.code, 'CAPABILITY_UNAVAILABLE')


class ColdStartPolicyFactoryTests(SimpleTestCase):
    def test_fresh_interpreter_constructs_typed_policy_once_after_normal_configuration(self):
        backend = str(Path(__file__).resolve().parents[2])
        code = 'import sys\nsys.path.insert(0, ' + repr(backend) + ')\n' + textwrap.dedent('''
            import tempfile
            from pathlib import Path
            from cabinet import portable_runtime as runtime
            from django.conf import settings
            assert not settings.configured
            calls = []
            captured = []
            def approved():
                from django.apps import apps
                assert settings.configured and apps.ready
                from datetime import datetime, timedelta, timezone
                from shelf.source_authority import SourceRights
                from shelf.source_evidence import FieldPolicy
                now = datetime.now(timezone.utc)
                rights = SourceRights(FieldPolicy('synthetic-cold-policy', 1,
                    now-timedelta(seconds=1), now+timedelta(seconds=300),
                    (('*', ('fetch',)),)), 1, 'synthetic-terms', 'synthetic-privacy',
                    'synthetic-locale', 'CultureCabinet/fixture (test@example.invalid)',
                    None, source='musicbrainz_recording')
                calls.append('after-apps-ready')
                captured.append(rights)
                return {'musicbrainz_recording': rights}
            # Audit denies provider I/O; the actual boundary only binds a socket,
            # never listens. The existing lease records ownership and closure.
            def deny(event, args):
                if event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'):
                    raise AssertionError('No provider/process effect')
            sys.addaudithook(deny)
            from shelf.tests.portable_fixture import BoundSocketLease
            from cabinet.portable_boundary import open_boundary
            lease = BoundSocketLease('musicbrainz-policy-cold-start')
            with tempfile.TemporaryDirectory(prefix='policy-cold-start-') as folder:
                try:
                    boundary = open_boundary(Path(folder)/'owner')
                    lease.attach(boundary)
                    assert runtime.prepare_database(boundary)
                    runtime.configure(boundary)
                    from django.core.management import call_command
                    call_command('migrate', interactive=False, verbosity=0)
                    runtime.remove_fresh_fixtures()
                    from shelf.portable_authority import PortableAuthority
                    from shelf.secret_leases import MemoryCredentials
                    from shelf.connection_validation import ConnectionValidation
                    authority = PortableAuthority(boundary)
                    connection = ConnectionValidation(authority, MemoryCredentials(authority))
                    provider = runtime._compose_enhance(authority, connection,
                        approved_policies_factory=approved)
                    assert calls == ['after-apps-ready']
                    actual = provider._entries['musicbrainz_recording']
                    assert actual == captured[0]
                    assert actual.policy.checked_at == captured[0].policy.checked_at
                    assert actual.policy.expires_at == captured[0].policy.expires_at
                    assert actual.policy.permissions == (('*', ('fetch',)),)
                    assert actual.application_identity == 'CultureCabinet/fixture (test@example.invalid)'
                    assert authority._enhance_service._normal
                    assert authority._enhance_service._policy_provider is provider
                    print('COLD_START_FACTORY_ONCE_AFTER_CONFIGURATION_PASS')
                finally:
                    from django.db import connections
                    connections.close_all()
                    lease.close()
        ''')
        with tempfile.TemporaryDirectory(prefix='policy-cold-parent-') as temporary:
            env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
                   'TMPDIR': temporary}
            if os.environ.get('PR001_RESOURCE_LOG'):
                env['PR001_RESOURCE_LOG'] = os.environ['PR001_RESOURCE_LOG']
            result = subprocess.run([sys.executable, '-I', '-B', '-c', code],
                env=env, capture_output=True, text=True, timeout=24)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('COLD_START_FACTORY_ONCE_AFTER_CONFIGURATION_PASS', result.stdout)
