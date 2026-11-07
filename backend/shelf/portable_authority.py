"""Portable owner/session authority, dormant until a trusted server selects it.

No Profile or Grant is created. Ownership is not another adult's disclosure
permission. The launcher/database integration and external wire gates remain
separate work; the existing synthetic/legacy entrypoints do not select this code.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import hmac
import secrets
import resource
import threading
import time
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import get_hasher, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .models import PortableConnection, PortableInstallation, Profile


class AuthorityFault(Exception):
    def __init__(self, code, status=403):
        self.code, self.status = code, status
        super().__init__(code)


def require_secret_protection():
    """Local process precondition, established before owner inputs are accepted.

    This does not promise Python byte erasure or protection from OS administrators.
    A future launcher must construct authority before exposing its request paths.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        if soft != 0:
            resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
        if resource.getrlimit(resource.RLIMIT_CORE)[0] != 0:
            raise OSError
    except (OSError, ValueError):
        raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503) from None


@dataclass
class OwnerSession:
    ref: str
    owner_id: int
    owner_revision: int
    created: float
    last_seen: float
    reauthenticated: float


class PortableAuthority:
    def __init__(self, boundary, *, monotonic=time.monotonic, now=timezone.now):
        require_secret_protection()
        self.boundary = boundary
        self.monotonic, self.now = monotonic, now
        self._lock = threading.RLock()
        self._sessions = {}
        self._revokers = []
        self._challenge = None
        self._challenge_issued = False
        self._challenge_failures = 0
        self._challenge_deadline = 0
        if (not boundary.active or boundary._closing
                or str(connection.settings_dict['NAME']) != boundary.database_name):
            raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503)
        with transaction.atomic():
            existing = PortableInstallation.objects.select_for_update().filter(pk=boundary.installation_id).first()
            if existing is None:
                if (PortableInstallation.objects.exists() or get_user_model().objects.exists()
                        or Profile.objects.exists()):
                    raise AuthorityFault('AUTHORITY_DENIED')
                existing = PortableInstallation.objects.create(id=boundary.installation_id,
                    root_identity=boundary.root_identity, process_generation=boundary.process_generation)
            elif existing.root_identity != boundary.root_identity or existing.data_schema_version != 1:
                raise AuthorityFault('AUTHORITY_DENIED')
            else:
                existing.process_generation = boundary.process_generation
                existing.save(update_fields=['process_generation'])
                PortableConnection.objects.filter(installation=existing).update(credential_generation=F('credential_generation') + 1)
        boundary._closing.append(self.close)

    def _installation(self):
        row = PortableInstallation.objects.select_related('owner').get(pk=self.boundary.installation_id)
        if not self.boundary.active or row.process_generation != self.boundary.process_generation:
            raise AuthorityFault('UNAUTHENTICATED', 401)
        return row

    def close(self):
        with self._lock:
            self._challenge = None
            for ref in list(self._sessions):
                self.logout(ref)

    @sensitive_variables()
    def issue_challenge(self, terminal):
        require_secret_protection()
        with self._lock:
            if self._installation().owner_id is not None or self._challenge_issued:
                raise AuthorityFault('AUTHORITY_DENIED')
            if not terminal.isatty():
                raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503)
            code = secrets.token_urlsafe(24)
            self._challenge = hashlib.sha256(code.encode()).digest()
            self._challenge_issued = True
            self._challenge_deadline = self.monotonic() + 300
            terminal.write(code + '\n')
            terminal.flush()

    @sensitive_variables()
    def _password(self, password):
        if type(password) is not str or not 12 <= len(password) <= 256:
            raise AuthorityFault('CONTRACT_INVALID', 422)
        try:
            password.encode('utf-8', 'strict')
            validate_password(password)
        except (ValidationError, UnicodeError):
            raise AuthorityFault('CONTRACT_INVALID', 422) from None

    def _new_session(self, row):
        moment = self.monotonic()
        ref = str(uuid.uuid4())
        self._sessions[ref] = OwnerSession(ref, row.owner_id, row.owner_revision, moment, moment, moment)
        return ref

    @sensitive_variables()
    def claim(self, code, password):
        require_secret_protection()
        with self._lock:
            if (self._challenge is None or self.monotonic() >= self._challenge_deadline
                    or self._challenge_failures >= 5):
                self._challenge = None
                raise AuthorityFault('AUTHORITY_DENIED')
            self._challenge_failures += 1
            if (type(code) is not str or len(code) != 32 or not code.isascii()):
                raise AuthorityFault('AUTHORITY_DENIED')
            if not hmac.compare_digest(
                    hashlib.sha256(code.encode()).digest(), self._challenge):
                raise AuthorityFault('AUTHORITY_DENIED')
            self._password(password)
            try:
                get_hasher()
                password_hash = make_password(password)
            except (ValueError, IndexError, ImportError, ImproperlyConfigured):
                raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503) from None
            try:
                with transaction.atomic():
                    row = self._installation()
                    if row.owner_id is not None:
                        raise AuthorityFault('AUTHORITY_DENIED')
                    user = get_user_model().objects.create(username='portable-' + uuid.uuid4().hex,
                                                           password=password_hash)
                    updated = PortableInstallation.objects.filter(pk=row.pk, owner__isnull=True,
                        owner_revision=0, process_generation=self.boundary.process_generation).update(
                        owner=user, owner_revision=1)
                    if updated != 1:
                        raise AuthorityFault('REVISION_CONFLICT', 409)
                    row.owner_id, row.owner_revision = user.pk, 1
            except (IntegrityError, OperationalError):
                raise AuthorityFault('REVISION_CONFLICT', 409) from None
            self._challenge = None
            return self._new_session(row)

    def authorize(self, ref, *, sensitive=False, touch=True):
        with self._lock:
            session = self._sessions.get(ref) if type(ref) is str else None
            try:
                row = self._installation()
            except AuthorityFault:
                self.logout(ref)
                raise
            moment = self.monotonic()
            if (session is None or row.owner_id != session.owner_id
                    or row.owner_revision != session.owner_revision or not row.owner.is_active
                    or moment < session.created or moment < session.last_seen
                    or moment - session.created >= 28800 or moment - session.last_seen >= 1800):
                self.logout(ref)
                raise AuthorityFault('UNAUTHENTICATED', 401)
            if sensitive and (moment < session.reauthenticated or moment - session.reauthenticated >= 300):
                raise AuthorityFault('UNAUTHENTICATED', 401)
            if touch:
                session.last_seen = moment
            return session

    def logout(self, ref):
        with self._lock:
            if type(ref) is str:
                self._sessions.pop(ref, None)
                for revoke in self._revokers:
                    revoke(ref)

    @sensitive_variables()
    def _verify_password(self, password):
        require_secret_protection()
        if type(password) is not str or not 12 <= len(password) <= 256:
            password = ''
        try:
            password.encode('utf-8', 'strict')
        except UnicodeError:
            password = ''
        with transaction.atomic():
            row = PortableInstallation.objects.select_for_update().select_related('owner').get(pk=self._installation().pk)
            now = self.now()
            if row.cooldown_until is not None and now < row.cooldown_until:
                raise AuthorityFault('RATE_LIMITED', 429)
            if row.cooldown_until is not None:
                row.failed_auth, row.cooldown_until = 0, None
            if row.owner_id is None or not row.owner.is_active:
                raise AuthorityFault('UNAUTHENTICATED', 401)
            matched = row.owner.check_password(password)
            if not matched:
                row.failed_auth += 1
                if row.failed_auth >= 5:
                    row.failed_auth = 5
                    row.cooldown_until = now + timedelta(minutes=15)
            else:
                row.failed_auth, row.cooldown_until = 0, None
            row.save(update_fields=['failed_auth', 'cooldown_until'])
        if not matched:
            raise AuthorityFault('UNAUTHENTICATED', 401)
        return row

    @sensitive_variables()
    def login(self, password):
        with self._lock:
            return self._new_session(self._verify_password(password))

    @sensitive_variables()
    def reauthenticate(self, ref, password):
        with self._lock:
            session = self.authorize(ref)
            self._verify_password(password)
            session.reauthenticated = self.monotonic()
