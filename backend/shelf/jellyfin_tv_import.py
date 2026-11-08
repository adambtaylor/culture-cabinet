"""Selected destination byte verification; no SSH bootstrap, ORM or status writer.

The supervised Mac worker consumes a committed native intent through a legitimate
channel. A control frame is not permission; the parent owns current authority and
kills its child on authority loss. The worker never deletes source bytes.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import PurePosixPath
import platform
import re
import select
import socket
import stat
from uuid import UUID, uuid4

from shelf import completion_registry as registry
from shelf.episode_scope import episode_code
from shelf.jellyfin_native import TV_ROOTS


@dataclass(frozen=True)
class DestinationFileReceipt:
    request_id: str
    job_id: int
    nzo_id: str
    binding_digest: str
    content_identity_digest: str
    source_manifest_digest: str
    destination_binding_digest: str
    intent: str
    host_id: str
    root: str
    relative_path: str
    size: int
    sha256: str
    observed_at: str
    test_only: bool


@dataclass(frozen=True)
class ImportOutcome:
    status: str
    reason: str
    receipt: DestinationFileReceipt | None = None


@dataclass(frozen=True)
class CopyPlan:
    request_id: str
    job_id: int
    nzo_id: str
    binding_digest: str
    content_identity_digest: str
    source_manifest_digest: str
    destination_binding_digest: str
    intent: str
    host_id: str
    root: str
    relative_path: str
    size: int
    sha256: str
    scope: str
    expires_at: str
    test_only: bool
    create_parent: bool = False

    def validate(self, allowed_roots):
        for value in (self.request_id, self.intent):
            registry.require(type(value) is str and str(UUID(value)) == value)
        registry.positive(self.job_id, 2**63 - 1)
        registry.nzo_id(self.nzo_id)
        for value in (self.binding_digest, self.content_identity_digest,
                      self.source_manifest_digest, self.destination_binding_digest, self.sha256):
            registry.require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value))
        registry.require(self.root in allowed_roots and type(self.test_only) is bool
                         and type(self.create_parent) is bool)
        registry.path_parts(self.root, absolute=True)
        parts = registry.path_parts(self.relative_path)
        registry.positive(self.size, registry.MAX_BYTES)
        registry.require(type(self.host_id) is str and 0 < len(self.host_id) <= 200
                         and not any(ord(c) < 32 for c in self.host_id))
        code = episode_code(self.scope)
        registry.require(code is not None and PurePosixPath(parts[-1]).suffix.lower()
                         in ('.mkv', '.mp4', '.webm'))
        # Destination naming is confirmed before submission. A single explicit
        # episode token prevents packs/ranges silently becoming this episode.
        tokens = re.findall(r'(?i)s\d+e\d+(?:e\d+)*', parts[-1])
        registry.require(len(tokens) == 1 and tokens[0].upper() == code
                         and not re.search(r'(?i)' + code + r'\s*[-+&]\s*(?:e?\d|s\d)', parts[-1]))
        expiry = datetime.fromisoformat(self.expires_at)
        registry.require(expiry.utcoffset() is not None)
        if self.create_parent:
            registry.require(len(parts) == 2)
        return expiry

    def identity(self):
        # Reauthentication may provide a new actual deadline for inspection of
        # the same intent. It never changes its destination/content bindings.
        return {key: value for key, value in asdict(self).items() if key != 'expires_at'}


def _now():
    return datetime.now(timezone.utc)


def _check_time(expiry, clock):
    registry.require(clock() < expiry)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode


def _trusted_fd(descriptor):
    info = os.fstat(descriptor)
    registry.require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.geteuid())
                     and not info.st_mode & 0o022)


def _hash(parent, name, plan, expiry, clock):
    fd = os.open(name, registry.READ_FLAGS, dir_fd=parent)
    with os.fdopen(fd, 'rb', buffering=0) as source:
        before = os.fstat(fd)
        registry.require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                         and before.st_size == plan.size)
        digest, left = hashlib.sha256(), plan.size
        while left:
            _check_time(expiry, clock)
            chunk = source.read(min(registry.CHUNK, left))
            registry.require(bool(chunk))
            digest.update(chunk)
            left -= len(chunk)
        registry.require(source.read(1) == b'' and digest.hexdigest() == plan.sha256
                         and registry.signature(before) == registry.signature(os.fstat(fd))
                         and registry.signature(before) == registry.signature(os.stat(
                             name, dir_fd=parent, follow_symlinks=False)))
        return before, digest.hexdigest()


def _write_state(parent, value):
    temporary = 'state-' + uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(registry.encode_json(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, 'intent.json', src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _source_read(source, count, expiry, clock):
    _check_time(expiry, clock)
    try:
        descriptor = source.fileno()
    except (AttributeError, OSError, ValueError):
        descriptor = None  # In-memory synthetic input only; real stdin has an FD.
    if descriptor is not None:
        left = (expiry - clock()).total_seconds()
        registry.require(left > 0 and bool(select.select([descriptor], [], [], left)[0]))
    return source.read(count)


def _receipt(plan, digest, clock):
    material = {key: value for key, value in asdict(plan).items()
                if key in DestinationFileReceipt.__dataclass_fields__}
    material['sha256'] = digest  # The result of hashing destination bytes.
    return DestinationFileReceipt(**material, observed_at=clock().isoformat())


def receive_selected(plan, source, *, operation='inspect', allowed_roots=TV_ROOTS,
                     clock=_now):
    """Worker-side one-intent receive/inspect, operating on actual host bytes.

    Existing complete staging can finalize without receiving source bytes again.
    Partial/unknown staging is preserved and never reset to chase completion.
    The legitimate parent reconciles with inspect before sending a file stream.
    """
    try:
        registry.require(type(plan) is CopyPlan and operation in ('inspect', 'receive'))
        expiry = plan.validate(allowed_roots)
        _check_time(expiry, clock)
        registry.require(plan.host_id == socket.gethostname()
                         and (platform.system() == 'Darwin' or plan.test_only))
        parts = registry.path_parts(plan.relative_path)
        journal_name = '.cabinet-obtain-' + plan.request_id
        with ExitStack() as stack:
            root = stack.enter_context(registry.directory(plan.root, trusted=True))
            # directory(trusted=True) validates ancestors while traversing;
            # its returned descriptor and every relative ancestor still need
            # validation BEFORE journal creation or any source/file effect.
            _trusted_fd(root)
            root_identity = _identity(os.fstat(root))
            parent = root
            trusted_directories = [root]
            missing_parent = False
            for component in parts[:-1]:
                try:
                    child = os.open(component, registry.DIR_FLAGS, dir_fd=parent)
                except FileNotFoundError:
                    registry.require(plan.create_parent and len(parts) == 2 and parent == root)
                    missing_parent = True
                    break
                stack.callback(os.close, child)
                _trusted_fd(child)
                trusted_directories.append(child)
                parent = child

            def trusted():
                for descriptor in trusted_directories:
                    _trusted_fd(descriptor)

            trusted()
            try:
                journal = os.open(journal_name, registry.DIR_FLAGS, dir_fd=root)
            except FileNotFoundError:
                if operation == 'inspect':
                    return ImportOutcome('pending', 'intent_not_started')
                trusted()
                os.mkdir(journal_name, 0o700, dir_fd=root)
                os.fsync(root)
                journal = os.open(journal_name, registry.DIR_FLAGS, dir_fd=root)
            stack.callback(os.close, journal)
            registry.private(os.fstat(journal), 0o700)
            try:
                fcntl.flock(journal, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return ImportOutcome('pending', 'selected_copy_busy')
            try:
                state = registry.read_json(journal, 'intent.json', single_link=True)
                if type(state) is not dict or state.get('identity') != plan.identity():
                    raise registry.Conflict()
                new = False
            except FileNotFoundError:
                entries = list(os.scandir(journal))
                registry.require(not entries and operation == 'receive')
                state = {'identity': plan.identity(), 'inode': None}
                trusted()
                _write_state(journal, state)
                new = True
            if missing_parent:
                if operation != 'receive':
                    return ImportOutcome('pending', 'awaiting_copy')
                trusted()
                os.mkdir(parts[0], 0o755, dir_fd=root)
                os.fsync(root)
                parent = os.open(parts[0], registry.DIR_FLAGS, dir_fd=root)
                stack.callback(os.close, parent)
                _trusted_fd(parent)
                trusted_directories.append(parent)
            parent_identity = _identity(os.fstat(parent))
            trusted()
            try:
                existing = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                # Matching bytes alone do not adopt a foreign pre-existing file.
                # The durable journal binds the inode created by this intent.
                if state['inode'] != [existing.st_dev, existing.st_ino]:
                    raise registry.Conflict()
                try:
                    staged = os.stat('content', dir_fd=journal, follow_symlinks=False)
                except FileNotFoundError:
                    staged = None
                if staged is not None:
                    registry.require((staged.st_dev, staged.st_ino) ==
                                     (existing.st_dev, existing.st_ino))
                    if operation == 'inspect':
                        return ImportOutcome('pending', 'awaiting_finalize')
                    trusted()
                    os.unlink('content', dir_fd=journal)
                    os.fsync(journal)
            else:
                if new:
                    trusted()
                    fd = os.open('content', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=journal)
                    with os.fdopen(fd, 'wb', buffering=0) as output:
                        left = plan.size
                        while left:
                            trusted()
                            chunk = _source_read(source, min(registry.CHUNK, left), expiry, clock)
                            registry.require(type(chunk) is bytes and 0 < len(chunk) <= left)
                            remaining = memoryview(chunk)
                            while remaining:
                                _check_time(expiry, clock)
                                written = output.write(remaining)
                                registry.require(type(written) is int and written > 0)
                                remaining = remaining[written:]
                            left -= len(chunk)
                        registry.require(_source_read(source, 1, expiry, clock) == b'')
                        os.fsync(output.fileno())
                    staged, _ = _hash(journal, 'content', plan, expiry, clock)
                    state['inode'] = [staged.st_dev, staged.st_ino]
                    trusted()
                    _write_state(journal, state)
                else:
                    staged, _ = _hash(journal, 'content', plan, expiry, clock)
                    registry.require(state['inode'] == [staged.st_dev, staged.st_ino])
                if operation == 'inspect':
                    return ImportOutcome('pending', 'awaiting_finalize')
                _check_time(expiry, clock)
                trusted()
                os.link('content', parts[-1], src_dir_fd=journal, dst_dir_fd=parent,
                        follow_symlinks=False)
                os.fsync(parent)
                os.unlink('content', dir_fd=journal)
                os.fsync(journal)
            _, digest = _hash(parent, parts[-1], plan, expiry, clock)
            # Reopen named ancestors after publication; held descriptors alone
            # could otherwise attest a directory renamed out of the TV root.
            with registry.directory(plan.root, trusted=True) as current_root:
                registry.require(_identity(os.fstat(current_root)) == root_identity)
            target_parent = str(PurePosixPath(plan.root, *parts[:-1]))
            with registry.directory(target_parent, trusted=True) as current_parent:
                registry.require(_identity(os.fstat(current_parent)) == parent_identity)
            _check_time(expiry, clock)
            return ImportOutcome('verified', '', _receipt(plan, digest, clock))
    except (FileExistsError, registry.Conflict):
        return ImportOutcome('conflict', 'destination_exists')
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return ImportOutcome('unknown', 'selected_copy_unverified')


@dataclass(frozen=True)
class DestinationImportPorts:
    generation: object
    revalidate: object
    plan_for: object
    inspect: object
    receive: object
    clock: object


def destination_binding_digest(binding, relative_path, create_parent):
    from shelf.sab_selected import canonical_digest
    return canonical_digest({'destination': asdict(binding), 'relative_path': relative_path,
                             'create_parent': create_parent}, domain='obtain-tv-destination/v1')


@contextmanager
def _selected_source(completion):
    """Keep a stable no-follow source descriptor across the supervised copy."""
    from shelf.acquisition_contract import sab_name
    item = completion.files[0]
    root_path = completion.job.complete_root + '/' + sab_name(completion.job.request_id)
    parts = registry.path_parts(item.relative_path)
    with ExitStack() as stack:
        root = stack.enter_context(registry.directory(root_path, trusted=True))
        root_identity = _identity(os.fstat(root))
        parent = root
        for component in parts[:-1]:
            parent = os.open(component, registry.DIR_FLAGS, dir_fd=parent)
            stack.callback(os.close, parent)
        source = stack.enter_context(os.fdopen(os.open(parts[-1], registry.READ_FLAGS,
            dir_fd=parent), 'rb', buffering=0))
        before = os.fstat(source.fileno())
        registry.require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                         and before.st_size == item.size)
        yield source
        registry.require(registry.signature(before) == registry.signature(os.fstat(source.fileno()))
                         and registry.signature(before) == registry.signature(os.stat(
                             parts[-1], dir_fd=parent, follow_symlinks=False)))
        with registry.directory(root_path, trusted=True) as current:
            registry.require(_identity(os.fstat(current)) == root_identity)


def import_selected(context, completion, destination_binding, *, intent, ports=None):
    """Native import boundary; inspect/reconcile before any source transmission.

    Ports belong to the legitimate destination custodian and frozen code/channel
    binding. This module never bootstraps SSH or treats returned JSON as authority.
    """
    from shelf.obtain_tv_flow import DestinationBinding, SelectedCompletion
    from shelf.sab_selected import require_native
    registry.require(type(completion) is SelectedCompletion)
    job = completion.job
    require_native(context, job, 'import')
    try:
        registry.require(type(destination_binding) is DestinationBinding
                         and type(ports) is DestinationImportPorts
                         and ports.generation == destination_binding.service_generation
                         and ports.generation.service == 'destination_file'
                         and ports.generation in job.service_generations
                         and destination_binding.ref == job.destination_ref
                         and completion.state == 'completed' and len(completion.files) == 1
                         and completion.source_manifest_digest is not None
                         and completion.history_digest is not None and completion.hook_digest is not None)
        with context.effect_window():
            def current():
                require_native(context, job, 'import')
                registry.require(ports.revalidate() == ports.generation)
            current()
            plan = ports.plan_for(completion, destination_binding, intent)
            registry.require(type(plan) is CopyPlan)
            item = completion.files[0]
            registry.require((plan.request_id, plan.job_id, plan.nzo_id, plan.binding_digest,
                plan.content_identity_digest, plan.source_manifest_digest, plan.intent,
                plan.host_id, plan.root, plan.size, plan.sha256, plan.scope, plan.test_only) ==
                (job.request_id, job.job_id, job.nzo_id, job.binding_digest,
                 job.content_identity_digest, completion.source_manifest_digest, intent,
                 destination_binding.host, destination_binding.root, item.size, item.sha256,
                 job.episode_scope, job.test_only))
            registry.require(plan.destination_binding_digest == destination_binding_digest(
                destination_binding, plan.relative_path, plan.create_parent))
            plan.validate((destination_binding.root,))
            current()
            outcome = ports.inspect(plan)
            current()
            registry.require(type(outcome) is ImportOutcome)
            if outcome.status == 'pending' and outcome.reason == 'intent_not_started':
                # A committed native intent and current channel are required
                # before the first byte leaves the selected source descriptor.
                with _selected_source(completion) as source:
                    current()
                    outcome = ports.receive(plan, source)
                    current()
            elif outcome.status == 'pending' and outcome.reason == 'awaiting_finalize':
                outcome = ports.receive(plan, None)
                current()
            registry.require(type(outcome) is ImportOutcome
                             and outcome.status in ('verified', 'pending', 'conflict', 'unknown'))
            if outcome.status == 'verified':
                receipt = outcome.receipt
                registry.require(type(receipt) is DestinationFileReceipt)
                expected = {key: value for key, value in asdict(plan).items()
                            if key in DestinationFileReceipt.__dataclass_fields__}
                registry.require({key: value for key, value in asdict(receipt).items()
                                  if key != 'observed_at'} == expected)
                observed = datetime.fromisoformat(receipt.observed_at)
                registry.require(observed.utcoffset() is not None
                                 and completion.observed_at <= observed <= ports.clock()
                                 and observed < datetime.fromisoformat(plan.expires_at))
            else:
                registry.require(outcome.receipt is None)
            current()
            return outcome
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return ImportOutcome('unknown', 'selected_import_unverified')
