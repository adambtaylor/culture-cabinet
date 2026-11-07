"""Offline-capable SAB completion evidence; see docs/sab-completion-hook.md."""

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import os
import re
import stat
import sys
import time
from uuid import uuid4


# The standalone repository script uses the same contract as the backend. An
# installed launcher must retain this module alongside the reviewed source.
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from shelf.completion_registry import (
    CHUNK, MAX_JSON, MAX_FILES, MAX_BYTES, READ_FLAGS, DIR_FLAGS, Conflict, MissingRegistration,
    directory, encode_json, nzo_id, path_parts, positive, private, read_json,
    registry_entry, require, signature,
)

ENV_FIELDS = ("SAB_NZO_ID", "SAB_STATUS", "SAB_PP_STATUS", "SAB_COMPLETE_DIR",
              "SAB_FILES", "SAB_BYTES")
REGISTRATION_WAIT_SECONDS = 5.0
REGISTRATION_POLL_SECONDS = 0.05


def registered_entry(registry_path, job):
    """Only a missing publication may wait; invalid/conflicting input fails now."""
    parts = path_parts(registry_path, absolute=True)
    deadline = time.monotonic() + REGISTRATION_WAIT_SECONDS
    while True:
        try:
            with directory("/" + "/".join(parts[:-1]), trusted=True) as parent:
                private(os.fstat(parent), 0o700)
                fcntl.flock(parent, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return registry_entry(read_json(parent, parts[-1], single_link=True), job)
        except (FileNotFoundError, MissingRegistration, BlockingIOError):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(REGISTRATION_POLL_SECONDS, remaining))


def decimal(value, ceiling):
    require(type(value) is str and re.fullmatch(r"[0-9]{1,20}", value))
    result = int(value)
    positive(result, ceiling)
    return result


from shelf.completion_files import verify_media, completion_receipt, postprocess_evidence


def publish(parent, evidence):
    name = evidence["request_id"] + ".json"
    receipt = completion_receipt(evidence, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    data = encode_json(receipt)
    require(len(data) <= MAX_JSON)
    temporary = ".pending-" + uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        try:
            # link is atomic and fails if ANY destination already exists.
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                    follow_symlinks=False)
        except FileExistsError:
            try:
                previous = read_json(parent, name)
                require(type(previous) is dict)
                stamp = previous["completion_observed_at"]
                require(type(stamp) is str and stamp.endswith("Z"))
                require(datetime.fromisoformat(stamp).utcoffset().total_seconds() == 0)
                if encode_json(previous) != encode_json(completion_receipt(evidence, stamp)):
                    raise Conflict()
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                raise Conflict() from None
        os.fsync(parent)
    finally:
        os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)


def run(registry_path, receipts_path, environ):
    """Return 0 verified, 1 unavailable, 2 conflicting receipt, 3 internal failure.

    Paths are trusted launcher arguments, never environment-derived. Do not log
    exceptions: SAB can expose script output, including sensitive job metadata.
    """
    try:
        env = {key: environ.get(key) for key in ENV_FIELDS}
        require(env["SAB_STATUS"] == "Running" and env["SAB_PP_STATUS"] == "0")
        nzo_id(env["SAB_NZO_ID"])
        entry = registered_entry(registry_path, env["SAB_NZO_ID"])
        require(env["SAB_COMPLETE_DIR"] == entry["root"])
        path_parts(receipts_path, absolute=True)
        require(receipts_path != entry["root"]
                and not receipts_path.startswith(entry["root"] + "/"))
        raw_files = env["SAB_FILES"]
        require(type(raw_files) is str and len(raw_files.encode("utf-8")) <= MAX_JSON)
        sab_files = json.loads(raw_files)
        require(type(sab_files) is list and 0 < len(sab_files) <= MAX_FILES)
        for name in sab_files:
            path_parts(name)
        require(len(set(sab_files)) == len(sab_files))
        sab_files = sorted(sab_files)
        expected_manifest = entry["files"]
        if entry["files"] is not None:
            require(sab_files == sorted(item["path"] for item in entry["files"]))
        else:
            entry = {**entry, "files": [{"path": name} for name in sab_files]}
        sab_bytes = (None if env["SAB_BYTES"] is None
                     else decimal(env["SAB_BYTES"], entry["max_bytes"]))
        with directory(receipts_path, trusted=True) as output:
            private(os.fstat(output), 0o700)
            files = verify_media(entry)
            publish(output, postprocess_evidence(
                {**entry, 'files': expected_manifest}, files, sab_bytes))
        return 0
    except Conflict:
        return 2
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return 1
    except Exception:
        # An unexpected programming failure is a distinct static, silent result.
        return 3


def main(argv=None, environ=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4 or args[0] != "--registry" or args[2] != "--receipts":
        return 1
    return run(args[1], args[3], os.environ if environ is None else environ)


if __name__ == "__main__":
    raise SystemExit(main())
