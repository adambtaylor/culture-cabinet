"""Actual startup composition through the existing issuer, with synthetic input."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from cabinet import portable_runtime as runtime
from shelf.enhance_contract import EnhanceFault
from shelf.source_authority import SourceRights
from .test_grant_authority import AuthorityCase
from .test_source_evidence import policy


class NativeSourceCompositionTests(AuthorityCase):
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
