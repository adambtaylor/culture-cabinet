"""Synthetic policy entries exercise real native authority; no source sends."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from shelf import grant_authority as ga
from shelf.enhance_contract import EnhanceFault
from shelf.native_source_policy import build_native_source_policy
from shelf.portable_authority import AuthorityFault
from shelf.source_authority import (SourceAuthority, SourcePolicyProvider, SourceRights,
                                    configure_source, current_binding)
from shelf.source_evidence import MOSHTIX_FACTS, MOSHTIX_POLICY_REF
from .test_grant_authority import AuthorityCase
from .test_source_evidence import policy


class NativeSourcePolicyTests(AuthorityCase):
    def setUp(self):
        super().setUp()
        self.profile_ref = self.bind()
        self.source_ref = configure_source(self.authority, self.ref, confirmed=True)
        self.rights = SourceRights(replace(policy(facts=tuple(sorted(MOSHTIX_FACTS))),
            ref=MOSHTIX_POLICY_REF), 1, 'synthetic-terms', 'synthetic-privacy',
            'synthetic-owner-VIC-consent', 'CultureCabinet/fixture (example.invalid)',
            'synthetic-processor')

    def build(self, entries=None):
        return build_native_source_policy(self.authority, approved_policies=
            {'moshtix_public': self.rights} if entries is None else entries)

    def current(self, provider, **changes):
        arguments = dict(profile_ref=self.profile_ref, connection_ref=self.source_ref,
                         now=self.authority.now())
        arguments.update(changes)
        return provider.current(self.authority, self.ref, **arguments)

    def denied(self, code, callback):
        with self.assertRaises((EnhanceFault, AuthorityFault)) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_explicit_entry_preserves_evidence_fields_and_original_deadline(self):
        provider = self.build()
        self.assertIsInstance(provider, SourcePolicyProvider)
        actual = self.current(provider)
        self.assertEqual(actual, self.rights)
        self.assertIsNot(actual, self.rights)
        self.assertIsNot(actual.policy, self.rights.policy)
        self.assertEqual(actual.policy.expires_at, self.rights.policy.expires_at)
        self.assertEqual(actual.policy.permissions, self.rights.policy.permissions)
        self.denied('AUTHORITY_DENIED', lambda: self.current(provider,
            now=self.rights.policy.expires_at))
        self.denied('AUTHORITY_DENIED', lambda: self.current(provider,
            now=self.rights.policy.checked_at - timedelta(microseconds=1)))

    def test_constructor_snapshot_does_not_follow_caller_mapping_changes(self):
        entries = {'moshtix_public': self.rights}
        provider = self.build(entries)
        entries['moshtix_public'] = replace(self.rights, revision=99,
            policy=replace(self.rights.policy, expires_at=self.rights.policy.expires_at + timedelta(days=1)))
        entries.clear()
        self.assertEqual(self.current(provider), self.rights)
        self.denied('AUTHORITY_DENIED', lambda: self.current(provider,
            now=self.rights.policy.expires_at + timedelta(seconds=1)))

    def test_missing_and_expired_entries_deny_without_extending_evidence(self):
        self.denied('CAPABILITY_UNAVAILABLE', lambda: self.current(self.build({})))
        expired = replace(self.rights, policy=replace(self.rights.policy,
            checked_at=self.authority.now() - timedelta(hours=2),
            expires_at=self.authority.now() - timedelta(hours=1)))
        provider = self.build({'moshtix_public': expired})
        self.denied('AUTHORITY_DENIED', lambda: self.current(provider))

    def test_actual_adapter_selects_only_its_approved_entry_and_requires_credentials(self):
        vault = self.authority._source_vault
        row = vault.configure(self.ref, {'adapter': 'hardcover', 'model': '', 'reasoning': ''})
        book = replace(self.rights, source='hardcover', terms_ref='synthetic-book-terms')
        provider = self.build({'moshtix_public': self.rights, 'hardcover': book})
        self.denied('CREDENTIAL_REQUIRED', lambda: self.current(provider, connection_ref=str(row.pk)))
        vault.replace(self.ref, row.pk, row.config_revision, b'SYNTHETIC-HARDCOVER')
        self.assertEqual(self.current(provider, connection_ref=str(row.pk)), book)
        self.denied('CAPABILITY_UNAVAILABLE', lambda: self.current(self.build(), connection_ref=str(row.pk)))
        self.assertEqual(self.current(provider), self.rights)

    def test_foreign_authority_profile_session_or_connection_cannot_select_evidence(self):
        provider = self.build()
        self.denied('AUTHORITY_DENIED', lambda: provider.current(object(), self.ref,
            profile_ref=self.profile_ref, connection_ref=self.source_ref, now=self.authority.now()))
        self.denied('AUTHORITY_DENIED', lambda: self.current(provider, profile_ref=str(uuid4())))
        self.denied('AUTHORITY_DENIED', lambda: self.current(provider, connection_ref=str(uuid4())))
        self.denied('UNAUTHENTICATED', lambda: provider.current(self.authority, 'missing-session',
            profile_ref=self.profile_ref, connection_ref=self.source_ref, now=self.authority.now()))

    def test_unknown_adapter_untyped_or_cross_adapter_policy_is_rejected(self):
        for entries in (None, [], {'openai_responses': self.rights},
                        {'moshtix_public': vars(self.rights)},
                        {'moshtix_public': SimpleNamespace(**vars(self.rights))}):
            self.denied('CONTRACT_INVALID', lambda: build_native_source_policy(
                self.authority, approved_policies=entries))
        self.denied('AUTHORITY_DENIED', lambda: self.build({'hardcover': self.rights}))

    def test_foreign_recovery_witness_cannot_return_approved_evidence(self):
        provider = self.build()
        with self.controlled_recovery_state('foreign_witness'):
            self.denied('CAPABILITY_UNAVAILABLE', lambda: self.current(provider))

    def test_typed_policy_cannot_replace_source_grant_or_inference_consent(self):
        provider = self.build()
        # Evidence selection is not a grant. The existing issuer still refuses
        # to build a current catalogue view until source consent exists.
        self.assertEqual(self.current(provider), self.rights)
        self.denied('AUTHORITY_DENIED', lambda: current_binding(self.authority, self.ref,
            profile_ref=self.profile_ref, connection_ref=self.source_ref))
        ga.change_grant(self.authority, self.ref, profile_ref=self.profile_ref,
            expected_revision=0, purpose='catalogue_lookup', connection_ref=self.source_ref,
            allowed_data_classes=['canonical_seed'], expires_at=self.expires, action='grant')
        self.assertEqual(current_binding(self.authority, self.ref,
            profile_ref=self.profile_ref, connection_ref=self.source_ref).connection_ref, self.source_ref)
        issuer = SourceAuthority(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=self.source_ref, policy_provider=provider)
        self.assertEqual(issuer._rights(self.authority.now()), self.rights)
        self.denied('AUTHORITY_DENIED', lambda: ga.current_view(self.authority, self.ref,
            profile_ref=self.profile_ref, connection_ref=self.source_ref, purpose='inference'))
        ga.change_grant(self.authority, self.ref, profile_ref=self.profile_ref,
            expected_revision=1, purpose='catalogue_lookup', connection_ref=self.source_ref,
            allowed_data_classes=['canonical_seed'], expires_at=self.expires, action='revoke')
        self.denied('AUTHORITY_DENIED', lambda: current_binding(self.authority, self.ref,
            profile_ref=self.profile_ref, connection_ref=self.source_ref))
