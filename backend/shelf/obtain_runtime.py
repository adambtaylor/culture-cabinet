"""Server-only composition using existing scoped records; disabled by default."""
from dataclasses import dataclass
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
        return value


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
    """One current-job effect thread and a custody/deadline stop monitor."""
    def __init__(self, access, context, services):
        from threading import Event, Thread
        self.authority, self.session_ref, self.profile_ref = access.authority, access.session_ref, access.profile_ref
        self.token, self.job_id = access.selection_token, context.job_id
        self.selection = dict(access._signed)
        self.session = self.authority._sessions[self.session_ref]
        self.deadline = context.authority_deadline()
        self.monotonic_end = self.authority.monotonic() + (self.deadline - self.authority.now()).total_seconds()
        self.stop_event, self.done = Event(), Event()
        self.stop_children = services.stop_owned_children
        self.cleanup_failed = False
        self.reason = ''
        self.thread = Thread(target=self._run, name='obtain-selected-' + str(self.job_id), daemon=True)
        self.monitor = Thread(target=self._monitor, name='obtain-bound-' + str(self.job_id), daemon=True)

    def start(self):
        self.thread.start(); self.monitor.start()

    def stop(self):
        self.stop_event.set()
        try:
            self.stop_children()
        except Exception:
            self.cleanup_failed = True
            getattr(self.authority, '_obtain_verified', {}).pop(self.job_id, None)

    def _monitor(self):
        while not self.done.wait(0.25):
            current = getattr(self.authority, '_saved_selections', {}).get(self.session_ref)
            # These reads only stop custody. Every effect still uses the real
            # native/recovery fence; the monitor never authorizes continuation.
            if (self.stop_event.is_set() or not self.authority.boundary.active
                    or self.authority._sessions.get(self.session_ref) is not self.session
                    or current is None or current[1] != self.selection['nonce']
                    or self.authority.monotonic() >= self.monotonic_end):
                self.reason = 'original_authority_window_ended'
                self.stop()
                return

    def _run(self):
        from django.db import close_old_connections
        from .portable_saved_authority import portable_saved_scope
        from .obtain_native_context import native_job_context
        from .completion_dispatch import advance_selected
        import time
        try:
            while not self.stop_event.is_set() and self.authority.now() < self.deadline:
                started = time.monotonic()
                close_old_connections()
                with portable_saved_scope(self.authority, self.session_ref, profile_ref=self.profile_ref,
                        selection_token=self.token, mutate=True) as access:
                    context = native_job_context(access, job_id=self.job_id, operation='observe')
                    transfer = ObtainTransfer.objects.get(pk=self.job_id)
                    if transfer.state != 'queued' or transfer.local_cancelled_at is not None:
                        break
                    context._deadline = self.deadline
                    services = selected_services(context)
                    self.stop_children = services.stop_owned_children
                    advance_selected(context, expected_revision=context.transfer_revision, services=services)
                    transfer.refresh_from_db()
                    if (transfer.state != 'queued' or transfer.native_progress.get('stage') in
                            {'verified', 'copy_unknown', 'copy_conflict', 'copy_pending', 'verification_mismatch'}):
                        break
                # Exact-job service polling is at least five seconds apart;
                # no session touch, background keepalive or startup resumption.
                self.stop_event.wait(max(0, 5 - (time.monotonic() - started)))
        except Exception:
            self.reason = 'continuation_unavailable'
        finally:
            self.stop()
            close_old_connections()
            self.done.set()

    def close(self):
        self.stop()
        self.thread.join(timeout=10)
        self.monitor.join(timeout=2)
        if self.thread.is_alive() or self.monitor.is_alive() or self.cleanup_failed:
            raise Blocked('Selected worker shutdown unconfirmed')


def worker_running(authority, job_id):
    worker = getattr(authority, '_obtain_workers', {}).get(job_id)
    return worker is not None and not worker.done.is_set()


def start_selected_worker(access, *, job_id):
    from . import grant_authority as ga
    from .obtain_native_context import native_job_context
    if getattr(settings, 'PORTABLE_OBTAIN_SUPERVISE', not ga._fixture_database()) is not True:
        return
    authority = access.authority
    with ga.recovery_guard(authority, access.session_ref), authority._lock:
        if not hasattr(authority, '_obtain_workers'):
            authority._obtain_workers = {}
            def revoke(ref):
                for worker in authority._obtain_workers.values():
                    if worker.session_ref == ref:
                        worker.stop()  # Never join while logout holds the owner lock.
            authority._revokers.append(revoke)
            def close_all():
                failed = False
                for worker in authority._obtain_workers.values():
                    try:
                        worker.close()
                    except Exception:
                        failed = True
                if failed:
                    raise Blocked('Selected worker cleanup unconfirmed')
            authority.boundary._closing.append(close_all)
        if worker_running(authority, job_id):
            return
        previous = authority._obtain_workers.get(job_id)
        if previous is not None:
            if previous.cleanup_failed:
                raise Blocked('Previous selected worker cleanup unconfirmed')
            previous.close()
        context = native_job_context(access, job_id=job_id, operation='observe')
        transfer = ObtainTransfer.objects.get(pk=job_id)
        if transfer.state != 'queued' or not transfer.provider_job or transfer.local_cancelled_at is not None:
            return
        if transfer.native_progress.get('stage') in {
                'verified', 'copy_unknown', 'copy_conflict', 'copy_pending', 'verification_mismatch'}:
            return
        services = selected_services(context)
        worker = _SelectedWorker(access, context, services)
        authority._obtain_workers[job_id] = worker
        worker.start()
