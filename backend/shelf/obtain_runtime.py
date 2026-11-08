"""Server-only composition using existing scoped records; disabled by default."""
from dataclasses import dataclass, replace
from contextlib import contextmanager
import os
from pathlib import Path
from uuid import UUID

from django.conf import settings

from shelf import credential_store as store, saved
from shelf.acquisition_backend import Backend
from shelf.acquisition_contract import Blocked, ConfirmedSelection, SABJobOptions, sab_name
from shelf import completion_registry as registry
from shelf.acquisition_pipeline import Evidence, scope_family
from shelf import obtain_acquisition as acquire
from shelf.models import ObtainTransfer
from shelf.obtain_tv_flow import DeliveryBinding, TVServiceProvider, SelectedTVServices


class CurrentTVServiceProvider(TVServiceProvider):
    """Bind legitimate runtime constructors to this actual native authority."""
    def __init__(self, authority, *, delivery_for, services_for):
        from . import grant_authority as ga
        if type(authority) is not ga.PortableAuthority or not callable(delivery_for) or not callable(services_for):
            raise Blocked('Trusted current TV runtime constructors required')
        self.authority, self._delivery_for, self._services_for = authority, delivery_for, services_for

    def delivery_binding(self, access, job):
        from .portable_saved_authority import active_portable_access
        if access.authority is not self.authority or active_portable_access() is not access:
            raise Blocked('Current native TV runtime authority required')
        access.require(actor_id=job.actor_id, profile_id=job.profile_id, action='mutate')
        value = self._delivery_for(access, job)
        if type(value) is not DeliveryBinding:
            raise Blocked('Current configured TV mapping required')
        value.__post_init__()
        return value

    def current(self, context):
        from .obtain_native_context import NativeJobContext
        if type(context) is not NativeJobContext or context._access.authority is not self.authority:
            raise Blocked('Current native selected job required')
        context.revalidate()
        value = self._services_for(context)
        if type(value) is not SelectedTVServices:
            raise Blocked('Current selected TV ports required')
        value.generations(context)
        job = acquire.owned_job(context._access.actor, context.profile_id, context.job_id)
        if value.delivery != self.delivery_binding(context._access, job):
            raise Blocked('Current delivery mapping changed')
        return replace(value, target_for=lambda current, selected, destination, evidence:
            selected_library_target(current, selected, destination, evidence, library=value.library))


def selected_library_target(context, job, destination, series_evidence, *, library):
    """Use current source facts or reread an independently validated Series.

    An expired bootstrap receipt supplies only the operational Series selector.
    The unchanged library binding and actual native deadline bound the new read;
    the resolver must independently prove the content identity again.
    """
    from . import jellyfin_native as jf
    from .obtain_native_context import NativeJobContext
    from .obtain_tv_flow import tv_content_identity_digest, SelectedJobBinding
    from .jellyfin_tv_import import DestinationFileReceipt
    if (type(context) is not NativeJobContext or context.operation != 'verify_library'
            or type(job) is not SelectedJobBinding or type(destination) is not DestinationFileReceipt):
        raise Blocked('Current native library context required')
    context.revalidate()
    if (type(library) is not jf.ServiceBinding or context._access.authority.now() >= library.expires_at
            or (job.actor_id, job.profile_id, job.job_id) !=
               (context.actor_id, context.profile_id, context.job_id)):
        raise Blocked('Current selected library binding required')
    if series_evidence is not None:
        if (type(series_evidence) is not jf.ValidatedSeriesBinding
                or (series_evidence.request_id, series_evidence.job_binding_digest,
                    series_evidence.content_identity_digest, series_evidence.destination_binding_digest,
                    series_evidence.scope, series_evidence.binding) !=
                   (job.request_id, job.binding_digest, job.content_identity_digest,
                    destination.destination_binding_digest, job.episode_scope, library)):
            raise Blocked('Exact validated Series evidence required')
        jf.series_evidence_material(series_evidence)
        show_id = show_name = None
        series_id = series_evidence.series_id
        end = min(library.expires_at, context.authority_deadline())
    else:
        from .portable_obtain_origin import resolve_obtain_origin_again
        from .episode_scope import parse_scope
        row = acquire.owned_job(context._access.actor, context.profile_id, context.job_id)
        source = resolve_obtain_origin_again(context._access, row.publication_origin,
            row.scope, context._access.authority.now())
        source.require_title_export()
        if type(source.source_id) is not int or source.source_id <= 0:
            raise Blocked('Current exact Series identity required')
        show_id, show_name, series_id = source.source_id, source.title, None
        season, episode = parse_scope(row.scope)
        if tv_content_identity_digest(tmdb_show_id=show_id, season=season, episode=episode) != job.content_identity_digest:
            raise Blocked('Current source content identity changed')
        end = min(source.valid_until, library.expires_at, context.authority_deadline())
    return jf.EpisodeResolutionTarget(job.request_id, job.binding_digest, job.content_identity_digest,
        destination.destination_binding_digest, show_id, job.episode_scope, show_name, series_id, end)


@dataclass(frozen=True)
class SubmissionServices:
    context: object
    evidence: object
    max_bytes: int
    retriever: object
    backend: object
    complete_root: str | None = None
    test_only: bool = True
    completion_intent: dict | None = None
    delivery_binding: DeliveryBinding | None = None


@dataclass(frozen=True)
class SubmissionPolicy:
    enabled: bool = False
    complete_root: str = ''
    max_bytes: int = 0
    reserve_bytes: int = 0
    test_only: bool = False
    registry_path: str = ''
    fixed_layout: bool = False
    script: str = ''
    pp: int = 2
    category: str = '*'

    def check(self):
        if (self.enabled is not True or type(self.complete_root) is not str
                or not self.complete_root.startswith('/') or self.complete_root == '/'
                or str(Path(self.complete_root)) != self.complete_root
                or '..' in Path(self.complete_root).parts or '\x00' in self.complete_root
                or type(self.max_bytes) is not int or not 0 < self.max_bytes <= 64 * 1024**3
                or type(self.reserve_bytes) is not int or not 0 <= self.reserve_bytes <= 64 * 1024**3
                or type(self.test_only) is not bool):
            raise Blocked('Submission policy unavailable')
        if self.fixed_layout is not True:
            raise Blocked('Approved fixed SAB layout required')
        SABJobOptions(self.script, self.pp, self.category)
        try:
            registry.path_parts(self.registry_path, absolute=True)
            if (len(Path(self.registry_path).parts) < 3
                    or self.registry_path.startswith(self.complete_root + '/')):
                raise ValueError
        except ValueError:
            raise Blocked('Private registry policy unavailable') from None

    def intent(self, request_id):
        self.check()
        if str(UUID(request_id)) != request_id:
            raise Blocked('Canonical request UUID required')
        name = sab_name(request_id)
        return {'nzbname': name, 'root': self.complete_root + '/' + name,
                'registry_path': self.registry_path, 'fixed_layout': True,
                'script': self.script, 'pp': self.pp, 'category': self.category}


def operator_policy():
    # No environment lookup or request-selected import, path, provider or grant.
    raw = getattr(settings, 'OBTAIN_SUBMISSION_POLICY', None)
    if raw is None:
        raise Blocked('Submission policy unavailable')
    if type(raw) is not dict or set(raw) != set(SubmissionPolicy.__dataclass_fields__):
        raise Blocked('Submission policy unavailable')
    policy = SubmissionPolicy(**raw)
    policy.check()
    return policy


def check_capacity(policy):
    """Check the operator's Complete filesystem without following any symlinks."""
    fd = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open('/', flags)
        for part in Path(policy.complete_root).parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        usage = os.fstatvfs(fd)
        if usage.f_bavail * usage.f_frsize < policy.max_bytes + policy.reserve_bytes:
            raise Blocked('Insufficient approved storage')
    except OSError:
        raise Blocked('Approved storage unavailable') from None
    finally:
        if fd is not None:
            os.close(fd)


def existing_grant(provider, actor, profile_id):
    handle = record = None
    try:
        handle = store.open_store(provider=provider)
        if handle.context != store._provider_context(provider):
            raise store.Refused()
        store.lock_store(handle)
        record = store.read_record(handle)
        metadata = record.metadata
        store.validate_metadata(metadata, now=store.utc_now(), provider=provider)
        if (metadata.binding.actor_id, metadata.binding.profile_id) != (str(actor.pk), str(profile_id)):
            raise store.Refused()
        return metadata
    except (store.Refused, OSError):
        raise Blocked('Acquisition authorization unavailable') from None
    finally:
        if record is not None:
            record.close()
        if handle is not None:
            handle.close()


@dataclass(frozen=True)
class SubmissionContext:
    actor_id: int
    profile_id: int
    job_id: object
    policy: SubmissionPolicy
    service_generations: tuple | None = None

    def current_delivery_binding(self, actor, profile_id):
        from .portable_saved_authority import active_portable_access
        access = active_portable_access()
        if access is None:
            raise Blocked('Current native destination authority required')
        self.require(actor, profile_id)
        provider = getattr(settings, 'PORTABLE_OBTAIN_TV_SERVICES', None)
        if not isinstance(provider, TVServiceProvider):
            raise Blocked('Current TV destination mapping unavailable')
        job = acquire.owned_job(actor, profile_id, self.job_id)
        with access.effect_window(action='mutate'):
            value = provider.delivery_binding(access, job)
            if type(value) is not DeliveryBinding:
                raise Blocked('Current typed TV destination required')
            value.__post_init__()
            return value

    def require(self, actor, profile_id):
        saved.authority(actor, profile_id)
        from .portable_saved_authority import active_portable_access
        access = active_portable_access()
        if access is not None:
            from .obtain_native_context import native_job_context
            native_job_context(access, job_id=self.job_id, operation='submit').require(actor, profile_id)
        if ((actor.pk, profile_id) != (self.actor_id, self.profile_id)
                or operator_policy() != self.policy):
            raise Blocked('Submission policy or authority changed')
        job = acquire.owned_job(actor, profile_id, self.job_id)
        if job.publication_origin is not None:
            from .obtain_native_context import native_job_context
            provider = getattr(settings, 'PORTABLE_OBTAIN_TV_SERVICES', None)
            if access is None or not isinstance(provider, TVServiceProvider) or self.service_generations is None:
                raise Blocked('Current complete native TV service bindings required')
            current = native_job_context(access, job_id=job.pk, operation='submit')
            services = provider.current(current)
            if type(services) is not SelectedTVServices or services.generations(current) != self.service_generations:
                raise Blocked('Native TV service generations changed; review again')
        exact, _ = acquire.exact_result(job, fresh=False)
        if exact.size > self.policy.max_bytes:
            raise Blocked('Selection exceeds approved size')
        check_capacity(self.policy)
        # Known Cabinet duplicates only. The final form also asks the owner to
        # check external downloads/library; titles cannot prove their absence.
        others = ObtainTransfer.objects.exclude(job=job)
        duplicate = (others.filter(job__active_key=job.active_key).exists() if job.publication_origin is not None else
            others.filter(job__provider=job.provider, job__medium=job.medium,
                          job__source_id=job.source_id, job__scope=job.scope).exists())
        if others.filter(result_guid=exact.guid).exists() or duplicate:
            raise Blocked('Existing acquisition requires review')


def submission_context(*, actor, profile_id, job, policy):
    if job.publication_origin is None:
        return SubmissionContext(actor.pk, profile_id, job.pk, policy)
    from .portable_saved_authority import active_portable_access
    from .obtain_native_context import native_job_context
    access = active_portable_access()
    provider = getattr(settings, 'PORTABLE_OBTAIN_TV_SERVICES', None)
    if access is None or not isinstance(provider, TVServiceProvider):
        raise Blocked('Current native TV service factory required')
    context = native_job_context(access, job_id=job.pk, operation='submit')
    with context.effect_window():
        services = provider.current(context)
        if type(services) is not SelectedTVServices:
            raise Blocked('Current typed TV services required')
        signature = services.generations(context)
    return SubmissionContext(actor.pk, profile_id, job.pk, policy, signature)


def submission_factory(*, actor, profile_id, job):
    saved.authority(actor, profile_id)
    policy = operator_policy()  # Disabled means no credential or storage reads.
    context = submission_context(actor=actor, profile_id=profile_id, job=job, policy=policy)
    context.require(actor, profile_id)
    job = acquire.owned_job(actor, profile_id, job.pk)
    exact, _ = acquire.exact_result(job, fresh=False)
    hydra = existing_grant('hydra', actor, profile_id)
    sab = existing_grant('sab', actor, profile_id)
    backend = Backend(hydra_grant=hydra, sab_grant=sab, _open_store=store.open_store,
                      job_options=SABJobOptions(policy.script, policy.pp, policy.category))
    from shelf.reviewed_nzb import ReviewedRetriever
    snapshot = exact.snapshot
    media, episode = scope_family(snapshot.provider, snapshot.medium, snapshot.scope)
    selection = ConfirmedSelection(
        item_id=f'{snapshot.provider}:{snapshot.medium}:{snapshot.source_id}',
        media=media, edition=snapshot.edition, storage=snapshot.storage,
        authorized_content=snapshot.authorized_content, user_confirmed=True,
        storage_checked=True, duplicate_checked=True, episode=episode)
    delivery = context.current_delivery_binding(actor, profile_id) if job.publication_origin is not None else None
    return SubmissionServices(context, Evidence(snapshot, selection), policy.max_bytes,
                              ReviewedRetriever(backend), backend, policy.complete_root,
                              policy.test_only, policy.intent(str(job.request_id)), delivery)


def selected_services(context):
    provider = getattr(settings, 'PORTABLE_OBTAIN_TV_SERVICES', None)
    if not isinstance(provider, TVServiceProvider):
        raise Blocked('Current selected TV runtime unavailable')
    with context.effect_window():
        value = provider.current(context)
        if type(value) is not SelectedTVServices:
            raise Blocked('Current typed selected TV ports required')
        value.generations(context)
        return value


class _SelectedWorker:
    """One owner-thread registration; its monitor can only cancel custody."""
    def __init__(self, access, context, services):
        from threading import Event, Thread, Lock
        self.authority, self.session_ref, self.profile_ref = access.authority, access.session_ref, access.profile_ref
        self.token, self.job_id = access.selection_token, context.job_id
        self.selection = dict(access._signed)
        self.identity = access.identity
        self.session = self.authority._sessions[self.session_ref]
        self.binding_digest, self.request_id = context.binding_digest, context.request_id
        self.expected_revision = context.transfer_revision
        self.deadline = context.authority_deadline()
        self.monotonic_end = self.authority.monotonic() + (self.deadline - self.authority.now()).total_seconds()
        self.next_due = self.authority.monotonic()
        self.stop_event, self.done = Event(), Event()
        self._cancel_lock = Lock()
        self.stop_children = services.stop_owned_children
        self.cleanup_failed = False
        self.reason = ''
        self.monitor = Thread(target=self._monitor, name='obtain-bound-' + str(self.job_id), daemon=True)

    def start(self):
        self.monitor.start()

    def stop(self, reason=''):
        self.stop_event.set()
        # The port is bounded, idempotent and independent of owner/recovery/SQL
        # locks. The monitor never changes authority, progress or proof caches.
        with self._cancel_lock:
            if reason and not self.reason:
                self.reason = reason
            try:
                self.stop_children()
            except Exception:
                self.cleanup_failed = True

    def _monitor(self):
        while not self.done.wait(0.25):
            current = getattr(self.authority, '_saved_selections', {}).get(self.session_ref)
            if (self.stop_event.is_set() or not self.authority.boundary.active
                    or self.authority._sessions.get(self.session_ref) is not self.session
                    or current is None or current[1] != self.selection['nonce']
                    or self.authority.monotonic() >= self.monotonic_end):
                self.stop('original_authority_window_ended')
                return

    def close(self, *, invalidate=True):
        if not self.done.is_set():
            self.stop()
            if self.monitor.ident is not None:
                self.monitor.join(timeout=2)
            self.done.set()
        if invalidate or self.cleanup_failed:
            getattr(self.authority, '_obtain_verified', {}).pop(self.job_id, None)
        if self.monitor.is_alive() or self.cleanup_failed:
            raise Blocked('Selected worker shutdown unconfirmed')
        self.token = self.session = None


class _SelectedSupervisor:
    """Cooperate with the existing synchronous native server and disk witness."""
    def __init__(self, authority):
        from threading import get_ident
        from . import grant_authority as ga, recovery
        if (type(authority) is not ga.PortableAuthority or not authority.boundary.active
                or recovery.installed_coordinator() is None):
            raise Blocked('Original native serving witness required')
        self.authority, self.boundary = authority, authority.boundary
        self.coordinator = recovery.installed_coordinator()
        self.owner_thread = get_ident()
        self.closed, self.failed, self._ticking = False, False, False
        self.cursor = 0
        authority._obtain_workers = {}

    def _owner(self):
        from threading import get_ident
        from . import recovery
        # Thread containment precedes even connection/scope inspection. It does
        # not replace the fresh recovery barriers used by each native pass.
        if (get_ident() != self.owner_thread or self.closed or self.failed
                or not self.boundary.active or self.authority.boundary is not self.boundary
                or recovery.installed_coordinator() is not self.coordinator
                or self.coordinator.closed or self.coordinator.blocked):
            raise Blocked('Original native supervisor authority required')

    def register(self, access, context, services):
        self._owner()
        previous = self.authority._obtain_workers.get(context.job_id)
        if previous is not None and not previous.done.is_set():
            if (previous.session is not self.authority._sessions.get(access.session_ref)
                    or previous.session_ref != access.session_ref or previous.selection != access._signed
                    or previous.binding_digest != context.binding_digest
                    or previous.expected_revision != context.transfer_revision):
                previous.stop('selected_job_changed')
                return False
            return not previous.stop_event.is_set()
        if previous is not None and previous.cleanup_failed:
            self.failed = True
            raise Blocked('Previous selected worker cleanup unconfirmed')
        worker = _SelectedWorker(access, context, services)
        self.authority._obtain_workers[context.job_id] = worker
        worker.start()
        return True

    def tick_once(self):
        from django.db import connection
        from . import grant_authority as ga, recovery
        from .portable_saved_authority import active_portable_access
        self._owner()
        if (self._ticking or active_portable_access() is not None
                or ga._CURRENT_WRITER.get() is not None or recovery._BARRIER_STACK.get()
                or connection.in_atomic_block or getattr(connection, '_disclosure_write_open', False)):
            raise Blocked('Native supervisor must run between request scopes')
        self._ticking = True
        try:
            workers = list(self.authority._obtain_workers.values())
            if not workers:
                return
            for offset in range(len(workers)):
                index = (self.cursor + offset) % len(workers)
                worker = workers[index]
                if worker.done.is_set():
                    continue
                if worker.stop_event.is_set():
                    self._finish(worker)
                    continue
                if self.authority.monotonic() >= worker.next_due:
                    self.cursor = (index + 1) % len(workers)
                    self._advance(worker)
                    return  # One bounded pass, never a queue-draining loop.
        finally:
            self._ticking = False

    def _finish(self, worker, *, invalidate=True):
        try:
            worker.close(invalidate=invalidate)
        except Exception:
            self.failed = True
            raise

    def _advance(self, worker):
        from .portable_saved_authority import portable_saved_scope
        from .obtain_native_context import native_job_context
        from .completion_dispatch import advance_selected
        started = self.authority.monotonic()
        if (started >= worker.monotonic_end or self.authority.now() >= worker.deadline
                or self.authority._sessions.get(worker.session_ref) is not worker.session
                or getattr(self.authority, '_saved_selections', {}).get(worker.session_ref) !=
                   (worker.identity, worker.selection['nonce'])):
            worker.stop('original_authority_window_ended')
            self._finish(worker)
            return
        try:
            with portable_saved_scope(self.authority, worker.session_ref, profile_ref=worker.profile_ref,
                    selection_token=worker.token, mutate=True) as access:
                context = native_job_context(access, job_id=worker.job_id, operation='observe')
                if (context.transfer_revision != worker.expected_revision
                        or context.binding_digest != worker.binding_digest or context.request_id != worker.request_id):
                    raise Blocked('Selected worker revision changed')
                context._deadline = worker.deadline
                services = selected_services(context)
                with worker._cancel_lock:
                    worker.stop_children = services.stop_owned_children
                context.revalidate()
                revision = advance_selected(context, expected_revision=worker.expected_revision, services=services)
                transfer = ObtainTransfer.objects.get(pk=worker.job_id)
                if type(revision) is not int or transfer.revision != revision:
                    raise Blocked('Selected worker cannot adopt a foreign revision')
                worker.expected_revision = revision
                terminal = (transfer.state != 'queued' or transfer.local_cancelled_at is not None
                    or transfer.native_progress.get('stage') in
                       {'verified', 'copy_unknown', 'copy_conflict', 'copy_pending', 'verification_mismatch'})
                verified = transfer.native_progress.get('stage') == 'verified'
            if terminal:
                self._finish(worker, invalidate=not verified)
            else:
                worker.next_due = started + 5
        except Exception:
            worker.stop('continuation_unavailable')
            self._finish(worker)
            if self.coordinator.closed or self.coordinator.blocked:
                raise  # A lost disk witness is not an ordinary pending result.
        # BaseException deliberately escapes through the launcher's ExitStack.

    def close(self):
        from threading import get_ident
        if get_ident() != self.owner_thread:
            raise Blocked('Supervisor cleanup requires its serving thread')
        if self.closed:
            if self.failed:
                raise Blocked('Selected supervisor cleanup unconfirmed')
            return
        self.closed = True
        workers = list(self.authority._obtain_workers.values())
        for worker in workers:
            worker.stop()
        for worker in workers:
            try:
                worker.close()
            except Exception:
                self.failed = True
        self.authority._obtain_workers.clear()
        self.coordinator = None
        if self.failed:
            raise Blocked('Selected supervisor cleanup unconfirmed')


def supervision_enabled():
    from . import grant_authority as ga
    return getattr(settings, 'PORTABLE_OBTAIN_SUPERVISE', not ga._fixture_database()) is True


@contextmanager
def selected_supervisor(authority):
    if getattr(authority, '_selected_supervisor', None) is not None:
        raise Blocked('Native supervisor cannot be replaced or revived')
    supervisor = _SelectedSupervisor(authority)
    authority._selected_supervisor = supervisor
    def revoke(ref):
        for worker in authority._obtain_workers.values():
            if worker.session_ref == ref:
                worker.stop('original_authority_window_ended')
    authority._revokers.append(revoke)
    authority.boundary._closing.append(supervisor.close)
    try:
        yield supervisor
    finally:
        supervisor.close()


def worker_running(authority, job_id):
    worker = getattr(authority, '_obtain_workers', {}).get(job_id)
    return worker is not None and not worker.done.is_set() and not worker.stop_event.is_set()


def start_selected_worker(access, *, job_id):
    from . import grant_authority as ga
    from .obtain_native_context import native_job_context
    if not supervision_enabled():
        return False
    supervisor = getattr(access.authority, '_selected_supervisor', None)
    if supervisor is None or supervisor.closed or supervisor.failed:
        return False  # Preserve the acknowledgement; never invent a new worker.
    with ga.recovery_guard(access.authority, access.session_ref), access.authority._lock:
        context = native_job_context(access, job_id=job_id, operation='observe')
        transfer = ObtainTransfer.objects.get(pk=job_id)
        if transfer.state != 'queued' or not transfer.provider_job or transfer.local_cancelled_at is not None:
            return False
        if transfer.job.publication_origin is None:
            return False
        binding = acquire.source_receipt(transfer)
        if ('delivery_binding' not in binding or not binding.get('complete_root')
                or 'completion_intent' not in binding):
            return False
        if transfer.native_progress.get('stage') in {
                'verified', 'copy_unknown', 'copy_conflict', 'copy_pending', 'verification_mismatch'}:
            return False
        services = selected_services(context)
        return supervisor.register(access, context, services)
