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


def submission_factory(*, actor, profile_id, job):
    saved.authority(actor, profile_id)
    policy = operator_policy()  # Disabled means no credential or storage reads.
    context = SubmissionContext(actor.pk, profile_id, job.pk, policy)
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
    return SubmissionServices(context, Evidence(snapshot, selection), policy.max_bytes,
                              ReviewedRetriever(backend), backend, policy.complete_root,
                              policy.test_only, policy.intent(str(job.request_id)))
