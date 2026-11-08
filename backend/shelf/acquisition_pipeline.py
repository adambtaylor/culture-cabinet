"""Inert acquisition contract. Adapters and serialized checkpoint ownership are external.

Saved jobs supply identity, never verification or actor/profile authority. Nothing
here imports the ORM, installs a transport, or writes production checkpoints.
"""
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Protocol
from uuid import UUID

from shelf.acquisition_contract import ConfirmedSelection, ContractError, PublicRequest
from shelf.episode_scope import ScopeError, episode_code


SCOPES = {
    ("tmdb", "movie", "film"): ("film", None),
    ("tmdb", "tv", "series_s01e01"): ("episode", "S01E01"),
    ("hardcover", "book", "audiobook"): ("audiobook", None),
    ("album_request", "music", "album_flac"): ("music", None),
}


def scope_family(provider, medium, scope):
    """Resolve the exact persisted identity without enumerating selected episodes."""
    try:
        episode = episode_code(scope)
    except ScopeError as error:
        raise ContractError(str(error)) from None
    if (provider, medium) == ('tmdb', 'tv') and episode is not None:
        return 'episode', episode
    try:
        return SCOPES[provider, medium, scope]
    except (KeyError, TypeError):
        raise ContractError('Inconsistent saved scope') from None


def _identifier(value):
    if (type(value) is not str or not value.strip() or len(value) > 200
            or any(ord(c) < 32 for c in value)):
        raise ContractError("Invalid identity")
    return value


@dataclass(frozen=True)
class Snapshot:
    actor_id: int
    profile_id: int
    request_id: str
    provider: str
    medium: str
    source_id: int
    edition: str
    scope: str
    storage: str
    authorized_content: bool
    storage_check: bool

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in
               (self.actor_id, self.profile_id, self.source_id)):
            raise ContractError("Invalid saved identity")
        try:
            valid_uuid = str(UUID(self.request_id)) == self.request_id
        except (ValueError, TypeError, AttributeError):
            valid_uuid = False
        if (not valid_uuid or self.source_id > 2147483647
                or not scope_family(self.provider, self.medium, self.scope)
                or self.storage != "local_library"
                or self.authorized_content is not True or self.storage_check is not True):
            raise ContractError("Inconsistent saved snapshot")
        _identifier(self.edition)

    @classmethod
    def from_saved_job(cls, job):
        return cls(**{name: (str(job.request_id) if name == "request_id"
                             else getattr(job, name)) for name in cls.__dataclass_fields__})

    def check_selection(self, selection):
        media, episode = scope_family(self.provider, self.medium, self.scope)
        if (type(selection) is not ConfirmedSelection or
                (selection.item_id, selection.media, selection.edition,
                 selection.storage, selection.episode) !=
                (f"{self.provider}:{self.medium}:{self.source_id}", media,
                 self.edition, self.storage, episode)):
            raise ContractError("Verification does not match saved selection")


@dataclass(frozen=True)
class Evidence:
    """Separate adapter attestations bound to the entire immutable saved identity."""
    snapshot: Snapshot
    selection: ConfirmedSelection

    def __post_init__(self):
        self.snapshot.check_selection(self.selection)


@dataclass(frozen=True)
class ProviderJob:
    snapshot: Snapshot
    job_id: str

    def __post_init__(self):
        _identifier(self.job_id)


@dataclass(frozen=True)
class StagedFile:
    job: ProviderJob
    path: str
    sha256: str
    size: int
    complete_work: bool
    partial: bool


@dataclass(frozen=True)
class Completion:
    job: ProviderJob
    final_status: str
    repair_ok: bool
    unpack_ok: bool
    postprocess_status: int
    staged: StagedFile | None


@dataclass(frozen=True)
class ImportReceipt:
    staged: StagedFile
    path: str


@dataclass(frozen=True)
class MediaSource:
    job: ProviderJob
    source_id: str
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class LibraryItem:
    snapshot: Snapshot
    item_id: str
    media_sources: tuple[MediaSource, ...]


@dataclass(frozen=True)
class EmbyReference:
    item_id: str
    media_source_id: str


@dataclass(frozen=True)
class Checkpoint:
    snapshot: Snapshot
    state: str = "blocked"
    evidence: Evidence | None = None
    job: ProviderJob | None = None
    staged: StagedFile | None = None
    imported: ImportReceipt | None = None
    emby: EmbyReference | None = None
    reason: str = "verification_required"


class QueueRejected(Exception):
    """Adapter guarantees that enqueue had no side effect; explicit retry is safe."""


class AdapterUnavailable(Exception):
    """Operation unavailable or outcome ambiguous; never implies rejection."""


class Checkpoints(Protocol):
    """Trusted atomic records, with one serialized owner per request across processes.

    A future production mapping must enforce this lease and durable writes before
    side effects. This module supplies no persistence implementation.
    """
    def load(self, request_id: str) -> Checkpoint | None: ...
    def save(self, checkpoint: Checkpoint) -> None: ...


class Provider(Protocol):
    def enqueue(self, snapshot: Snapshot, request: PublicRequest) -> ProviderJob: ...
    def lookup(self, snapshot: Snapshot) -> ProviderJob | None:
        """Stable request identity; None means unresolved, never permission to enqueue."""
        ...
    def poll(self, job: ProviderJob) -> Completion | None: ...
    def cancel(self, job: ProviderJob) -> bool:
        """True only after confirmed cancellation; unavailable grants raise."""
        ...


class Importer(Protocol):
    def lookup(self, staged: StagedFile) -> ImportReceipt | None: ...
    def commit(self, staged: StagedFile) -> ImportReceipt:
        """Atomic visibility, no overwrite; reconcile same identity AND digest on retry.

        Destination conflicts raise ContractError. An uncertain commit must remain
        discoverable through lookup, including when staging has since disappeared.
        """
        ...


class Library(Protocol):
    def lookup(self, imported: ImportReceipt) -> LibraryItem | None: ...


class PlaybackEndpoint(Protocol):
    def play(self, reference: EmbyReference) -> None:
        """Future Kodi-on-Mac or Frame/Emby endpoint owns authenticated session checks."""
        ...


def _open_known_file(path):
    """No symlinks in any component, including when reopening after hashing."""
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in Path(path).parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=directory)
    finally:
        os.close(directory)


def verify_staging(staged, *, max_bytes=128 * 1024**3):
    """Bounded byte verification at this instant, not a lock on future file contents.

    The trusted completion adapter owns provenance, exact work/edition and a stable
    completed path. Consumers must reverify before later use of a local receipt.
    """
    if (type(staged) is not StagedFile or staged.complete_work is not True or staged.partial is not False
            or type(max_bytes) is not int or not 0 < max_bytes <= 128 * 1024**3
            or type(staged.size) is not int or not 0 < staged.size <= max_bytes
            or type(staged.sha256) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", staged.sha256)
            or type(staged.path) is not str or not Path(staged.path).is_absolute()
            or len(staged.path) > 4096 or str(Path(staged.path)) != staged.path
            or ".." in Path(staged.path).parts or "\0" in staged.path
            or any(s.lower() in {".part", ".partial", ".tmp", ".download", ".crdownload"}
                   for s in Path(staged.path).suffixes)):
        raise ContractError("Incomplete staging evidence")
    try:
        with os.fdopen(_open_known_file(staged.path), "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != staged.size:
                raise ContractError("Staging is not the complete regular file")
            hasher = hashlib.sha256()
            remaining = staged.size
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ContractError("Staging content truncated")
                hasher.update(chunk)
                remaining -= len(chunk)
            if handle.read(1):
                raise ContractError("Staging content grew")
            digest = hasher.hexdigest()
            after = os.fstat(handle.fileno())
            with os.fdopen(_open_known_file(staged.path), "rb") as named:
                info = os.fstat(named.fileno())
                if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
                    raise ContractError("Staging path changed")
    except OSError:
        raise ContractError("Staging file unavailable") from None
    if (digest != staged.sha256 or
            (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ContractError("Staging content mismatch")


class Pipeline:
    def __init__(self, saved_job, checkpoints, provider, importer, library, *, trusted_snapshot=None):
        if getattr(saved_job, 'publication_origin', None) is not None:
            # The native caller has just resolved current source authority.
            # Its detached snapshot remains RAM-only, never a restored identity.
            if (type(trusted_snapshot) is not Snapshot
                    or (trusted_snapshot.provider, trusted_snapshot.medium) != ('tmdb', 'tv')
                    or any(getattr(trusted_snapshot, key) != (str(saved_job.request_id) if key == 'request_id'
                           else getattr(saved_job, key)) for key in ('actor_id', 'profile_id', 'request_id',
                            'edition', 'scope', 'storage', 'authorized_content', 'storage_check'))):
                raise ContractError('Fresh trusted source snapshot required')
            self.snapshot = trusted_snapshot
        else:
            if trusted_snapshot is not None:
                raise ContractError('Legacy pipeline cannot borrow source identity')
            self.snapshot = Snapshot.from_saved_job(saved_job)
        self.checkpoints = checkpoints
        self.provider, self.importer, self.library = provider, importer, library
        if saved_job.status not in {"blocked_configuration", "cancelled"}:
            raise ContractError("Unsupported saved state")
        existing = checkpoints.load(self.snapshot.request_id)
        if existing is None:
            cancelled = saved_job.status == "cancelled"
            checkpoints.save(Checkpoint(self.snapshot,
                state="cancelled" if cancelled else "blocked",
                reason="cancelled" if cancelled else "verification_required"))
        elif existing.snapshot != self.snapshot:
            raise ContractError("Request identity conflict")

    @property
    def checkpoint(self):
        checkpoint = self.checkpoints.load(self.snapshot.request_id)
        if checkpoint is None or checkpoint.snapshot != self.snapshot:
            raise ContractError("Checkpoint identity conflict")
        return checkpoint

    def _save(self, checkpoint, **changes):
        result = replace(checkpoint, **changes)
        self.checkpoints.save(result)
        return result

    def _job_matches(self, job, expected=None):
        if (not isinstance(job, ProviderJob) or job.snapshot != self.snapshot
                or (expected is not None and job != expected)):
            raise ContractError("Provider identity mismatch")

    def dispatch(self, evidence):
        if type(evidence) is not Evidence or evidence.snapshot != self.snapshot:
            raise ContractError("Separate matching verification required")
        self.snapshot.check_selection(evidence.selection)
        checkpoint = self.checkpoint
        if checkpoint.evidence is not None and checkpoint.evidence != evidence:
            raise ContractError("Verification identity conflict")
        if checkpoint.state != "blocked":
            return checkpoint
        request = PublicRequest("addfile", selection=evidence.selection,
                                request_id=self.snapshot.request_id)
        # A lost response or a crash immediately after this write forbids re-enqueue.
        checkpoint = self._save(checkpoint, state="outcome_unknown", evidence=evidence,
                                reason="dispatch_intent")
        try:
            job = self.provider.enqueue(self.snapshot, request)
        except QueueRejected:
            return self._save(checkpoint, state="failed", reason="queue_rejected")
        except AdapterUnavailable:
            return self._save(checkpoint, reason="manual_reconciliation_required")
        self._job_matches(job)
        return self._save(checkpoint, state="queued", job=job, reason="")

    def retry_rejected(self):
        checkpoint = self.checkpoint
        if checkpoint.state != "failed" or checkpoint.reason != "queue_rejected":
            raise ContractError("Only definite queue rejection permits retry")
        return self._save(checkpoint, state="blocked", reason="retry_same_request")

    def _find_job(self):
        job = self.provider.lookup(self.snapshot)
        if job is not None:
            self._job_matches(job)
        return job

    def _import_receipt(self, checkpoint, receipt):
        if (not isinstance(receipt, ImportReceipt) or receipt.staged != checkpoint.staged
                or not Path(receipt.path).is_absolute()):
            raise ContractError("Import identity mismatch")
        return receipt

    def advance(self):
        checkpoint = self.checkpoint
        if checkpoint.state == "cancel_pending":
            return self.cancel()
        try:
            if checkpoint.state == "outcome_unknown":
                job = self._find_job()
                if job is None:
                    return self._save(checkpoint, reason="manual_reconciliation_required")
                return self._save(checkpoint, state="queued", job=job, reason="")
            if checkpoint.state in {"queued", "downloading"}:
                completion = self.provider.poll(checkpoint.job)
                if completion is None:
                    return self._save(checkpoint, state="downloading", reason="")
                self._job_matches(completion.job, checkpoint.job)
                if (completion.final_status != "completed"
                        or completion.repair_ok is not True or completion.unpack_ok is not True
                        or type(completion.postprocess_status) is not int
                        or completion.postprocess_status != 0):
                    return self._save(checkpoint, state="failed", reason="provider_failed")
                if completion.staged is None or completion.staged.job != checkpoint.job:
                    raise ContractError("Completion staging identity mismatch")
                return self._save(checkpoint, state="verifying", staged=completion.staged, reason="")
            if checkpoint.state == "verifying":
                verify_staging(checkpoint.staged)
                return self._save(checkpoint, state="importing", reason="import_intent")
            if checkpoint.state == "importing":
                if checkpoint.imported is None:
                    receipt = self.importer.lookup(checkpoint.staged)
                    if receipt is None:
                        verify_staging(checkpoint.staged)
                        receipt = self.importer.commit(checkpoint.staged)
                    receipt = self._import_receipt(checkpoint, receipt)
                    return self._save(checkpoint, imported=receipt, reason="awaiting_index")
                item = self.library.lookup(checkpoint.imported)
                if item is None:
                    return self._save(checkpoint, reason="awaiting_index")
                if item.snapshot != self.snapshot:
                    raise ContractError("Library item identity mismatch")
                receipt = checkpoint.imported
                sources = [source for source in item.media_sources if
                           (source.job, source.path, source.sha256, source.size) ==
                           (checkpoint.job, receipt.path, receipt.staged.sha256, receipt.staged.size)]
                if len(sources) != 1:
                    raise ContractError("Library media source mismatch or ambiguity")
                reference = EmbyReference(_identifier(item.item_id), _identifier(sources[0].source_id))
                return self._save(checkpoint, state="available", emby=reference, reason="")
        except AdapterUnavailable:
            return self._save(checkpoint, reason="adapter_unavailable")
        except ContractError:
            return self._save(checkpoint, state="failed", reason="evidence_mismatch")
        return checkpoint

    def cancel(self):
        checkpoint = self.checkpoint
        if checkpoint.imported is not None or checkpoint.state in {"available", "cancelled"}:
            return checkpoint
        if checkpoint.state == "blocked" or (
                checkpoint.state == "failed" and checkpoint.reason == "queue_rejected"):
            return self._save(checkpoint, state="cancelled", reason="cancelled_before_dispatch")
        checkpoint = self._save(checkpoint, state="cancel_pending", reason="cancellation_unconfirmed")
        try:
            # Import may have committed just before a checkpoint write was lost.
            if checkpoint.staged is not None:
                receipt = self.importer.lookup(checkpoint.staged)
                if receipt is not None:
                    receipt = self._import_receipt(checkpoint, receipt)
                    return self._save(checkpoint, state="importing", imported=receipt,
                                      reason="awaiting_index")
            job = checkpoint.job or self._find_job()
            if job is not None:
                self._job_matches(job)
                checkpoint = self._save(checkpoint, job=job)
                if self.provider.cancel(job) is True:
                    return self._save(checkpoint, state="cancelled", reason="provider_cancelled")
        except AdapterUnavailable:
            return checkpoint
        except ContractError:
            return self._save(checkpoint, reason="cancellation_identity_mismatch")
        return checkpoint
