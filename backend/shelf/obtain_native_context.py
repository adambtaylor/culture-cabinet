"""Live native job authority; operational receipt values never recreate access."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib

from . import grant_authority as ga
from .acquisition_contract import Blocked
from .disclosure import canonical_bytes
from .models import AcquisitionJob, ObtainTransfer
from .portable_saved_authority import PortableSavedAccess, active_portable_access

OPERATIONS = frozenset({'view', 'search', 'review', 'submit', 'observe', 'import', 'verify_library'})
_ISSUER = object()


def operational_digest(value):
    return hashlib.sha256(b'obtain-native-binding/v1\x00' + canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class _Frame:
    identity: object
    request_id: str
    job_status: str
    transfer_revision: int | None
    binding_digest: str | None


class NativeJobContext:
    """Exact trusted type consumed by selected ports, never a legacy operator.

    This fence owns current native selection/job/CAS authority. Each service
    port must additionally revalidate its own actual configuration/credential
    generations immediately before its effect; a stored binding is not a key.
    """
    def __init__(self, access, job_id, operation, frame, *, _issuer=None):
        if _issuer is not _ISSUER:
            raise Blocked('Live native context factory required')
        self._access, self.job_id, self.operation, self._frame = access, job_id, operation, frame
        self._deadline = None

    @property
    def actor_id(self):
        return self._frame.identity.actor_id

    @property
    def profile_id(self):
        return self._frame.identity.profile_id

    @property
    def request_id(self):
        return self._frame.request_id

    @property
    def transfer_revision(self):
        return self._frame.transfer_revision

    @property
    def binding_digest(self):
        return self._frame.binding_digest

    def revalidate(self):
        """Check the same live selection and exact job before accepting a result."""
        if (type(self) is not NativeJobContext or active_portable_access() is not self._access
                or _read_frame(self._access, self.job_id, self.operation) != self._frame):
            raise Blocked('Native job authority or revision changed')

    def authority_deadline(self):
        """Actual sensitive-session/runtime minimum, frozen against renewal.

        A continuing owner interaction can change idle/reauthentication times.
        It cannot extend a worker or file intent already issued by this context.
        """
        from django.conf import settings
        if self.operation == 'view':
            raise Blocked('Read-only context cannot issue an effect deadline')
        self.revalidate()
        authority, ref = self._access.authority, self._access.session_ref
        with ga.recovery_guard(authority, ref), authority._lock:
            _, session = ga._bound(authority, ref, self._access.profile_ref, sensitive=True)
            cache = getattr(authority, '_obtain_sensitive_ends', {})
            key = (ref, session.reauthenticated)
            if key not in cache:
                cache = {pair: value for pair, value in cache.items()
                         if pair[0] in authority._sessions and pair[0] != ref}
                cache[key] = authority.now() + timedelta(seconds=session.reauthenticated + 300 - authority.monotonic())
                authority._obtain_sensitive_ends = cache
            end = min(ga._end(authority, session), cache[key])
            runtime_end = getattr(settings, 'PORTABLE_OBTAIN_POLL_UNTIL', None)
            if runtime_end is not None:
                if type(runtime_end) is not datetime or runtime_end.utcoffset() is None:
                    raise Blocked('Actual runtime deadline required')
                end = min(end, runtime_end)
            worker = getattr(authority, '_obtain_workers', {}).get(self.job_id)
            if (worker is not None and not worker.done.is_set() and worker.session_ref == ref
                    and worker.selection == self._access._signed):
                end = min(end, worker.deadline)
            self._deadline = min(end, self._deadline) if self._deadline is not None else end
            if not authority.now() < self._deadline:
                raise Blocked('Original native effect window expired')
            return self._deadline

    def require(self, actor, profile_id):
        if (actor.pk, profile_id) != (self.actor_id, self.profile_id):
            raise Blocked('Native job ownership changed')
        self.revalidate()

    @contextmanager
    def effect_window(self):
        if self.operation == 'view':
            raise Blocked('Read-only context cannot perform work')
        # Keep the existing recovery/owner fence across work, without wrapping
        # network or file effects in a transaction. A new revision needs a fresh
        # context, so stale workers cannot publish over a newer continuation.
        with self._access.effect_window(action='mutate'):
            self.revalidate()
            yield self
            self.revalidate()


def _read_frame(access, job_id, operation):
    if (type(access) is not PortableSavedAccess or active_portable_access() is not access
            or type(job_id) is not int or job_id <= 0
            or type(operation) is not str or operation not in OPERATIONS):
        raise Blocked('Current native job access required')
    with ga.recovery_guard(access.authority, access.session_ref), access.authority._lock:
        identity = access.require(actor_id=access.identity.actor_id,
                                  profile_id=access.identity.profile_id,
                                  action='view' if operation == 'view' else 'mutate')
        try:
            job = AcquisitionJob.objects.get(pk=job_id, actor_id=identity.actor_id,
                                             profile_id=identity.profile_id)
        except AcquisitionJob.DoesNotExist:
            raise Blocked('Owned native job unavailable') from None
        if operation != 'view' and job.status != 'blocked_configuration':
            raise Blocked('Cancelled native job cannot perform work')
        transfer = ObtainTransfer.objects.filter(job=job).first()
        return _Frame(identity, str(job.request_id), job.status, transfer.revision if transfer else None,
                      operational_digest(transfer.binding) if transfer else None)


def native_job_context(access, *, job_id, operation):
    frame = _read_frame(access, job_id, operation)
    return NativeJobContext(access, job_id, operation, frame, _issuer=_ISSUER)
