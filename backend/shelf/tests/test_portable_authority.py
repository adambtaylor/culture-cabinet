"""Synthetic owner/session/locked-root behavior; no launcher readiness claim."""
from datetime import datetime, timedelta, timezone
import io
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import tempfile
from unittest.mock import PropertyMock, patch
import uuid

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from cabinet.portable_boundary import BoundRoot, BoundaryFailure, open_boundary
from shelf.models import Grant, PortableConnection, PortableInstallation, Profile
from shelf.portable_authority import AuthorityFault, PortableAuthority

PASSWORD = 'Synthetic-owner-only-42!'


class Clock:
    def __init__(self):
        self.tick = 100.0
        self.wall = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def advance(self, seconds):
        self.tick += seconds
        self.wall += timedelta(seconds=seconds)


class Terminal(io.StringIO):
    def isatty(self):
        return True


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class BoundaryCase(TransactionTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'owner'
        from .portable_fixture import BoundSocketLease
        lease = BoundSocketLease(self.id())
        self.addCleanup(lease.close)  # Registered before acquisition/partial setup.
        self.boundary = open_boundary(self.root)
        lease.attach(self.boundary)
        self.database = patch.object(BoundRoot, 'database_name', new_callable=PropertyMock,
                                     return_value=str(connection.settings_dict['NAME']))
        self.database.start()
        self.addCleanup(self.database.stop)
        self.clock = Clock()
        self.authority = PortableAuthority(self.boundary, monotonic=lambda: self.clock.tick, now=lambda: self.clock.wall)

    def challenge(self):
        terminal = Terminal()
        self.authority.issue_challenge(terminal)
        return terminal.getvalue().strip()

    def claim_owner(self, password=PASSWORD):
        return self.authority.claim(self.challenge(), password)

    def fault(self, code, function, *args, **kwargs):
        with self.assertRaises(AuthorityFault) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def restart(self):
        self.boundary.close()
        self.boundary = open_boundary(self.root)
        self.addCleanup(self.boundary.close)
        self.authority = PortableAuthority(self.boundary, monotonic=lambda: self.clock.tick, now=lambda: self.clock.wall)


class OwnerTests(BoundaryCase):
    def test_claim_durable_hashed_owner_without_profile_grants(self):
        ref = self.claim_owner()
        row = PortableInstallation.objects.get()
        self.assertEqual(self.authority.authorize(ref).owner_id, row.owner_id)
        self.assertNotEqual(row.owner.password, PASSWORD)
        self.assertTrue(row.owner.check_password(PASSWORD))
        self.assertEqual(row.owner_revision, 1)
        self.assertFalse(Profile.objects.exists())
        self.assertFalse(Grant.objects.exists())

    def test_claim_replay_and_competing_submission_create_one_owner(self):
        code = self.challenge()
        self.authority.claim(code, PASSWORD)
        self.fault('AUTHORITY_DENIED', self.authority.claim, code, PASSWORD)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_expired_challenge_stays_locked(self):
        code = self.challenge()
        self.clock.advance(300)
        self.fault('AUTHORITY_DENIED', self.authority.claim, code, PASSWORD)
        self.fault('AUTHORITY_DENIED', self.authority.issue_challenge, Terminal())
        self.assertFalse(get_user_model().objects.exists())

    def test_five_failures_lock_challenge(self):
        code = self.challenge()
        for _ in range(5):
            self.fault('AUTHORITY_DENIED', self.authority.claim, 'x' * 32, PASSWORD)
        self.fault('AUTHORITY_DENIED', self.authority.claim, code, PASSWORD)
        self.assertFalse(get_user_model().objects.exists())

    def test_redirected_terminal_never_gets_a_code(self):
        sink = io.StringIO()
        self.fault('CAPABILITY_UNAVAILABLE', self.authority.issue_challenge, sink)
        self.assertEqual(sink.getvalue(), '')
        self.assertIsNone(self.authority._challenge)

    def test_exact_unicode_password_survives_restart(self):
        password = '  Synthetic Café e\u0301-42!  '
        self.claim_owner(password)
        self.restart()
        self.authority.login(password)
        self.fault('UNAUTHENTICATED', self.authority.login, password.strip())
        self.fault('AUTHORITY_DENIED', self.authority.issue_challenge, Terminal())

    def test_short_long_or_surrogate_passwords_fail_closed(self):
        code = self.challenge()
        for password in ['short', 'x' * 257, 'synthetic-password' + chr(0xD800)]:
            self.fault('CONTRACT_INVALID', self.authority.claim, code, password)
        self.assertFalse(get_user_model().objects.exists())

    def test_missing_supported_hasher_has_no_fallback(self):
        code = self.challenge()
        with override_settings(PASSWORD_HASHERS=[]):
            self.fault('CAPABILITY_UNAVAILABLE', self.authority.claim, code, PASSWORD)
        self.assertFalse(get_user_model().objects.exists())

    def test_five_bad_passwords_create_durable_cooldown(self):
        self.claim_owner()
        for _ in range(5):
            self.fault('UNAUTHENTICATED', self.authority.login, 'synthetic-wrong-42!')
        row = PortableInstallation.objects.get()
        self.assertEqual(row.failed_auth, 5)
        self.assertEqual(row.cooldown_until, self.clock.wall + timedelta(minutes=15))
        self.restart()
        self.fault('RATE_LIMITED', self.authority.login, PASSWORD)
        self.clock.advance(900)
        self.authority.login(PASSWORD)
        row.refresh_from_db()
        self.assertEqual(row.failed_auth, 0)
        self.assertIsNone(row.cooldown_until)

    def test_idle_session_expires_at_thirty_minutes(self):
        ref = self.claim_owner()
        self.clock.advance(1800)
        self.fault('UNAUTHENTICATED', self.authority.authorize, ref)

    def test_touch_does_not_extend_absolute_eight_hour_limit(self):
        ref = self.claim_owner()
        for _ in range(31):
            self.clock.advance(900)
            self.authority.authorize(ref)
        self.clock.advance(900)
        self.fault('UNAUTHENTICATED', self.authority.authorize, ref)

    def test_sensitive_operation_requires_recent_reauthentication(self):
        ref = self.claim_owner()
        self.clock.advance(300)
        self.fault('UNAUTHENTICATED', self.authority.authorize, ref, sensitive=True)
        self.authority.reauthenticate(ref, PASSWORD)
        self.authority.authorize(ref, sensitive=True)

    def test_logout_and_unknown_reference_are_unauthenticated(self):
        ref = self.claim_owner()
        self.authority.logout(ref)
        for value in [ref, str(uuid.uuid4()), None, []]:
            self.fault('UNAUTHENTICATED', self.authority.authorize, value)

    def test_owner_revocation_invalidates_session(self):
        ref = self.claim_owner()
        PortableInstallation.objects.update(owner_revision=2)
        self.fault('UNAUTHENTICATED', self.authority.authorize, ref)

    def test_disabled_owner_invalidates_session(self):
        ref = self.claim_owner()
        get_user_model().objects.update(is_active=False)
        self.fault('UNAUTHENTICATED', self.authority.authorize, ref)

    def test_restart_invalidates_session_and_advances_credentials(self):
        ref = self.claim_owner()
        row = PortableConnection.objects.create(id=uuid.uuid4(), installation=PortableInstallation.objects.get(),
            adapter='openai_responses', model='gpt-6-astra', reasoning='high')
        generation = self.boundary.process_generation
        self.restart()
        self.assertNotEqual(self.boundary.process_generation, generation)
        row.refresh_from_db()
        self.assertEqual(row.credential_generation, 1)
        self.fault('UNAUTHENTICATED', self.authority.authorize, ref)

    def test_second_authority_cannot_share_one_bound_process(self):
        self.fault('CAPABILITY_UNAVAILABLE', PortableAuthority, self.boundary)

    def test_foreign_root_cannot_adopt_database(self):
        with open_boundary(Path(self.directory.name) / 'foreign') as other:
            self.fault('AUTHORITY_DENIED', PortableAuthority, other)

    def test_root_lock_and_explicit_occupied_port_refuse(self):
        with self.assertRaises(BoundaryFailure):
            open_boundary(self.root)
        with self.assertRaises(BoundaryFailure):
            open_boundary(Path(self.directory.name) / 'second', port=self.boundary._socket.getsockname()[1])

    def test_nonempty_foreign_root_is_not_claimed(self):
        root = Path(self.directory.name) / 'foreign-data'
        root.mkdir(mode=0o700)
        (root / 'private-data').write_text('synthetic only')
        with self.assertRaises(BoundaryFailure):
            open_boundary(root)
        self.assertFalse((root / '.portable-installation.json').exists())

    def test_symlink_root_and_permissive_root_are_rejected(self):
        alias = Path(self.directory.name) / 'alias'
        alias.symlink_to(self.root)
        with self.assertRaises(BoundaryFailure):
            open_boundary(alias)
        public = Path(self.directory.name) / 'public'
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        with self.assertRaises(BoundaryFailure):
            open_boundary(public)

    def test_cookie_names_are_process_specific_and_origin_is_actual_bound_port(self):
        before = self.boundary.namespace
        self.assertEqual(self.boundary.origin, 'http://127.0.0.1:' + str(self.boundary._socket.getsockname()[1]))
        self.restart()
        self.assertNotEqual(self.boundary.namespace, before)
        self.assertNotEqual(self.boundary.session_cookie, self.boundary.csrf_cookie)

    def test_root_database_selection_cannot_be_supplied_by_http(self):
        self.database.stop()
        self.fault('CAPABILITY_UNAVAILABLE', PortableAuthority, self.boundary)
        self.database.start()


    def test_simultaneous_claims_create_exactly_one_owner(self):
        code = self.challenge()
        def submit():
            try:
                self.authority.claim(code, PASSWORD)
                return 'CLAIMED'
            except AuthorityFault as fault:
                return fault.code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda unused: submit(), range(2)))
        self.assertEqual(sorted(outcomes), ['AUTHORITY_DENIED', 'CLAIMED'])
        self.assertEqual(get_user_model().objects.count(), 1)


    def test_core_protection_failure_blocks_authority_startup(self):
        with patch('shelf.portable_authority.resource.getrlimit', return_value=(1, 1)), patch(
                'shelf.portable_authority.resource.setrlimit', side_effect=OSError):
            self.fault('CAPABILITY_UNAVAILABLE', PortableAuthority, self.boundary)
        self.assertFalse(get_user_model().objects.exists())
        self.assertIsNone(self.authority._challenge)

    def test_core_protection_failure_emits_no_bootstrap_code(self):
        terminal = Terminal()
        with patch('shelf.portable_authority.resource.getrlimit', return_value=(1, 1)), patch(
                'shelf.portable_authority.resource.setrlimit', side_effect=OSError), patch(
                'shelf.portable_authority.secrets.token_urlsafe') as random_code:
            self.fault('CAPABILITY_UNAVAILABLE', self.authority.issue_challenge, terminal)
        random_code.assert_not_called()
        self.assertEqual(terminal.getvalue(), '')
        self.assertIsNone(self.authority._challenge)

    def test_core_protection_failure_prevents_password_hash_or_verification(self):
        code = self.challenge()
        with patch('shelf.portable_authority.resource.getrlimit', return_value=(1, 1)), patch(
                'shelf.portable_authority.resource.setrlimit', side_effect=OSError), patch(
                'shelf.portable_authority.make_password') as hashing:
            self.fault('CAPABILITY_UNAVAILABLE', self.authority.claim, code, PASSWORD)
        hashing.assert_not_called()
        self.assertFalse(get_user_model().objects.exists())
        self.authority.claim(code, PASSWORD)
        with patch('shelf.portable_authority.resource.getrlimit', return_value=(1, 1)), patch(
                'shelf.portable_authority.resource.setrlimit', side_effect=OSError), patch.object(
                get_user_model(), 'check_password') as verify:
            self.fault('CAPABILITY_UNAVAILABLE', self.authority.login, PASSWORD)
        verify.assert_not_called()

    def test_core_guard_is_established_before_challenge_generation(self):
        order = []
        def disable(*args):
            order.append('core_off')
        def code(size):
            order.append('code')
            return 'A' * 32
        with patch('shelf.portable_authority.resource.getrlimit', side_effect=[(1, 1), (0, 1)]), patch(
                'shelf.portable_authority.resource.setrlimit', side_effect=disable), patch(
                'shelf.portable_authority.secrets.token_urlsafe', side_effect=code):
            self.authority.issue_challenge(Terminal())
        self.assertEqual(order, ['core_off', 'code'])

    def test_malformed_marker_refuses_and_releases_lock_for_corrected_reopen(self):
        marker = self.root / '.portable-installation.json'
        correct = marker.read_bytes()
        self.boundary.close()
        for value in [[], {}, 5, None, True, 'not-a-uuid']:
            marker.write_text(json.dumps({'schema': 1, 'installation_id': value}))
            with self.assertRaises(BoundaryFailure) as caught:
                open_boundary(self.root)
            self.assertEqual(str(caught.exception), 'CAPABILITY_UNAVAILABLE')
            marker.write_bytes(correct)
            with open_boundary(self.root) as reopened:
                self.assertTrue(reopened.active)

    def test_unexpected_boundary_constructor_failure_also_releases_lock(self):
        self.boundary.close()
        with patch('cabinet.portable_boundary.BoundRoot', side_effect=RuntimeError('synthetic-constructor-fault')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic-constructor-fault'):
                open_boundary(self.root)
        with open_boundary(self.root) as reopened:
            self.assertTrue(reopened.active)
