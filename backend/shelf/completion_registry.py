"""Shared private registry contract for Cabinet and the offline SAB hook."""
from contextlib import ExitStack, contextmanager
import fcntl
import json
import os
import re
import stat
import time
from uuid import UUID, uuid4

from shelf.episode_scope import ScopeError, parse_scope


MAX_JSON = 262144
MAX_FILES = 128
MAX_BYTES = 64 * 1024**3
CHUNK = 1024 * 1024
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
DIR_FLAGS = READ_FLAGS | os.O_DIRECTORY


class Unavailable(ValueError):
    pass


class Conflict(ValueError):
    pass


class MissingRegistration(Unavailable):
    pass


def require(condition):
    if not condition:
        raise Unavailable()


def path_parts(value, *, absolute=False):
    require(type(value) is str and 0 < len(value) <= 4096)
    require(value.startswith("/") == absolute and "\\" not in value)
    parts = value[1:].split("/") if absolute else value.split("/")
    require(len(parts) <= 64)
    for part in parts:
        require(part not in ("", ".", "..") and not any(ord(c) < 32 for c in part))
        lower = part.lower()
        require(not lower.endswith((".part", ".partial", ".tmp", ".download", ".crdownload")))
        require(not set(lower.split('.')[1:]) & {"part", "partial", "tmp", "download", "crdownload"})
        require(not lower.startswith(("_unpack_", "_failed_", "_incomplete_")))
    return parts


@contextmanager
def directory(path, *, trusted=False):
    """Open each absolute component separately: never follow a parent symlink."""
    parts = path_parts(path, absolute=True)
    with ExitStack() as stack:
        fd = os.open("/", DIR_FLAGS)
        stack.callback(os.close, fd)
        for part in parts:
            if trusted:
                info = os.fstat(fd)
                require(info.st_uid in (0, os.geteuid()))
                require(not info.st_mode & 0o022
                        or (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))
            fd = os.open(part, DIR_FLAGS, dir_fd=fd)
            stack.callback(os.close, fd)
        yield fd


def private(info, mode):
    require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == mode)


def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def encode_json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n").encode("utf-8")


def read_json(parent, name, *, single_link=False):
    fd = os.open(name, READ_FLAGS, dir_fd=parent)
    with os.fdopen(fd, "rb", buffering=0) as source:
        before = os.fstat(source.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_JSON)
        require(not single_link or before.st_nlink == 1)
        private(before, 0o600)
        data = source.read(MAX_JSON + 1)
        require(len(data) <= MAX_JSON and signature(before) == signature(os.fstat(source.fileno())))
    return json.loads(data, object_pairs_hook=unique_object)


def nzo_id(value):
    require(type(value) is str and len(value) <= 112)
    if re.fullmatch(r"SABnzbd_nzo_[A-Za-z0-9_-]{1,100}", value):
        return
    require(str(UUID(value)) == value)


def positive(value, ceiling):
    require(type(value) is int and 0 < value <= ceiling)


def registry_entry(registry, job):
    require(type(registry) is dict and set(registry) == {"version", "requests"})
    require(type(registry["version"]) is int and registry["version"] == 2)
    entries = registry["requests"]
    require(type(entries) is list and len(entries) <= 256)
    seen_jobs, seen_requests, seen_roots = set(), set(), set()
    selected = None
    for entry in entries:
        require(type(entry) is dict and set(entry) == {
            "nzo_id", "request_id", "result_id", "result_digest", "nzb_sha256",
            "complete_root", "root", "files", "scope", "max_bytes", "test_only"})
        nzo_id(entry["nzo_id"])
        request = entry["request_id"]
        require(type(request) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request))
        require(entry["nzo_id"] not in seen_jobs and request not in seen_requests)
        seen_jobs.add(entry["nzo_id"])
        seen_requests.add(request)
        require(type(entry["scope"]) is str
                and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", entry["scope"]))
        try:
            parse_scope(entry['scope'])
        except ScopeError:
            require(False)
        require(type(entry["result_id"]) is str and str(UUID(entry["result_id"])) == entry["result_id"])
        for key in ("result_digest", "nzb_sha256"):
            require(type(entry[key]) is str and re.fullmatch(r"[0-9a-f]{64}", entry[key]))
        require(type(entry["test_only"]) is bool)
        path_parts(entry["complete_root"], absolute=True)
        path_parts(entry["root"], absolute=True)
        require(entry["root"].startswith(entry["complete_root"] + "/"))
        require(entry["root"] not in seen_roots)
        seen_roots.add(entry["root"])
        positive(entry["max_bytes"], MAX_BYTES)
        files = entry["files"]
        # None permits ordinary downloads with no pre-known extracted manifest.
        # When an independent manifest exists, all its paths must match SAB.
        require(files is None or (type(files) is list and 0 < len(files) <= MAX_FILES))
        paths, total = set(), 0
        for item in files or []:
            require(type(item) is dict and "path" in item and not set(item) - {"path", "bytes", "sha256"})
            path_parts(item["path"])
            require(item["path"] not in paths)
            paths.add(item["path"])
            if "bytes" in item:
                positive(item["bytes"], entry["max_bytes"])
                total += item["bytes"]
            if "sha256" in item:
                require(type(item["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]))
        require(total <= entry["max_bytes"])
        if entry["nzo_id"] == job:
            selected = entry
    if selected is None:
        raise MissingRegistration()
    return selected


def check_destination(registry_path, complete_root, name, *, before_send=False):
    """Validate control storage and the fixed layout without reserving a SAB name."""
    parts = path_parts(registry_path, absolute=True)
    require(len(parts) > 1)
    path_parts(complete_root, absolute=True)
    path_parts(name)
    require('/' not in name)
    require(not registry_path.startswith(complete_root + '/'))
    with directory('/' + '/'.join(parts[:-1]), trusted=True) as parent:
        private(os.fstat(parent), 0o700)
    with directory(complete_root, trusted=True) as root:
        info = os.fstat(root)
        require(info.st_uid in (0, os.geteuid()) and not info.st_mode & 0o022)
        if before_send:
            try:
                os.stat(name, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise Conflict()


def publish_registration(registry_path, entry):
    """Merge under a directory lock, then fsync and atomically publish one snapshot.

    A retry accepts only the identical binding. No IDs or roots are recycled;
    capacity exhaustion requires a separately reviewed retention policy.
    """
    registry_entry({'version': 2, 'requests': [entry]}, entry['nzo_id'])
    parts = path_parts(registry_path, absolute=True)
    require(len(parts) > 1)
    with directory('/' + '/'.join(parts[:-1]), trusted=True) as parent:
        private(os.fstat(parent), 0o700)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        name = parts[-1]
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is None:
            registry = {'version': 2, 'requests': []}
        else:
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
            registry = read_json(parent, name, single_link=True)
            try:
                registry_entry(registry, entry['nzo_id'])
            except MissingRegistration:
                pass
        for previous in registry['requests']:
            if any(previous[key] == entry[key] for key in ('nzo_id', 'request_id', 'root')):
                if previous != entry:
                    raise Conflict()
                os.fsync(parent)
                return
        registry['requests'].append(entry)
        registry_entry(registry, entry['nzo_id'])
        data = encode_json(registry)
        require(len(data) <= MAX_JSON)
        temporary = '.registration-' + uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            if info is not None:
                require(signature(info) == signature(os.stat(name, dir_fd=parent,
                                                            follow_symlinks=False)))
            else:
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise Conflict()
            # All writers of this private registry must hold the directory lock.
            # Rename leaves no second hard link after a crash, unlike link+unlink.
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.fsync(parent)


