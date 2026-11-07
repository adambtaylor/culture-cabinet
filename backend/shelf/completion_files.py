"""Shared byte checks and truthful SAB postprocessing receipt construction."""
from contextlib import ExitStack
import hashlib
import os
import stat

from shelf.episode_scope import ScopeError, parse_scope

from shelf.completion_registry import (
    CHUNK, READ_FLAGS, DIR_FLAGS, directory, path_parts, positive, require, signature,
)


def postprocess_evidence(entry, files, sab_bytes):
    return {'version': 2, 'state': 'POSTPROCESS_FILE_VERIFIED',
        **{key: entry[key] for key in ('nzo_id', 'request_id', 'result_id',
            'result_digest', 'nzb_sha256', 'scope', 'root', 'complete_root',
            'max_bytes', 'test_only')},
        'files': files, 'total_bytes': sum(item['bytes'] for item in files),
        'expected_manifest': entry['files'], 'sab_status': 'Running', 'sab_pp_status': 0,
        'sab_files': sorted(item['path'] for item in files), 'sab_bytes': sab_bytes,
        'transport_verification': 'local_sha256_and_size',
        'postprocess_evidence': 'sab_job_result_zero', 'repair_verified': None,
        'crc_verified': None, 'content_identity': 'unverified', 'full_work_verified': False,
        'sab_history_completed': False, 'acquired_at': None, 'acquired_at_basis': 'unavailable'}


def verify_media(entry):
    """Visit only the registered tree; reject extra entries before opening them."""
    children = {"": set()}
    for item in entry["files"]:
        parts = path_parts(item["path"])
        for index, name in enumerate(parts):
            parent = "/".join(parts[:index])
            children.setdefault(parent, set()).add(name)
            if index < len(parts) - 1:
                children.setdefault("/".join(parts[:index + 1]), set())

    with ExitStack() as stack:
        root = stack.enter_context(directory(entry["root"]))
        dirs = {"": root}
        snapshots = []
        for relative in sorted(children, key=lambda p: (p.count("/"), p)):
            if relative:
                parent, _, name = relative.rpartition("/")
                fd = os.open(name, DIR_FLAGS, dir_fd=dirs[parent])
                stack.callback(os.close, fd)
                dirs[relative] = fd
            fd = dirs[relative]
            before = os.fstat(fd)
            found = set()
            with os.scandir(fd) as listing:
                for item in listing:
                    require(item.name in children[relative])
                    found.add(item.name)
            require(found == children[relative])
            snapshots.append((fd, signature(before)))

        observed, total = [], 0
        for item in sorted(entry["files"], key=lambda f: f["path"]):
            parent, _, name = item["path"].rpartition("/")
            fd = os.open(name, READ_FLAGS, dir_fd=dirs[parent])
            source = stack.enter_context(os.fdopen(fd, "rb", buffering=0))
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1)
            positive(before.st_size, entry["max_bytes"])
            total += before.st_size
            require(total <= entry["max_bytes"])
            if "bytes" in item:
                require(before.st_size == item["bytes"])
            kind = None
            if entry['scope'] == 'album_flac':
                from shelf.album_files import file_kind
                kind = file_kind(item['path'], before.st_size)
            digest, remaining = hashlib.sha256(), before.st_size
            while remaining:
                chunk = source.read(min(CHUNK, remaining))
                require(bool(chunk))
                if remaining == before.st_size:
                    # Markers are format evidence only, never album identity.
                    if kind == 'track':
                        require(chunk[:4] == b'fLaC')
                    elif kind == 'art':
                        png = item['path'].lower().endswith('.png')
                        require(chunk.startswith(b'\x89PNG\r\n\x1a\n') if png
                                else chunk.startswith(b'\xff\xd8\xff'))
                    # CUE is bounded opaque source evidence. Never parse or execute it.
                digest.update(chunk)
                remaining -= len(chunk)
            require(source.read(1) == b"")
            if "sha256" in item:
                require(digest.hexdigest() == item["sha256"])
            snapshots.append((fd, signature(before)))
            observed.append({"path": item["path"], "bytes": before.st_size, "sha256": digest.hexdigest()})
        for fd, before in snapshots:
            require(before == signature(os.fstat(fd)))
        # Detect a renamed/replaced root or ancestor before publishing evidence.
        with directory(entry["root"]) as current:
            require(signature(os.fstat(current)) == signature(os.fstat(root)))
    return observed


def completion_receipt(evidence, stamp):
    """Kodi handoff describes observed bytes, never inferred listening or identity."""
    try:
        parse_scope(evidence['scope'])
    except ScopeError:
        require(False)
    provenance = {key: evidence[key] for key in
                  ("request_id", "result_id", "result_digest", "nzb_sha256")}
    relative_job = evidence["root"][len(evidence["complete_root"]) + 1:]
    exports = []
    for item in evidence["files"]:
        suffix = item["path"].rsplit(".", 1)[-1].lower()
        observed = ("audio" if suffix in {"flac", "mp3", "m4a", "m4b", "wav", "ogg", "opus"}
                    else "video" if suffix in {"mkv", "mp4", "avi", "webm"} else "unknown")
        exports.append({"media_identity": "sha256:" + item["sha256"],
            "relative_path": relative_job + "/" + item["path"],
            "sha256": item["sha256"], "size": item["bytes"], "nzo_id": evidence["nzo_id"],
            "provenance": provenance, "selected_scope": evidence["scope"],
            "observed_media": observed, "observed_media_basis": "filename_extension_only",
            "content_identity": "unverified", "full_work_verified": False,
            "verified_at": stamp, "acquired_at": None, "acquired_at_basis": "unavailable",
            "test_only": evidence["test_only"], "kodi_music_catalog_eligible": False,
            "indexed": False, "playback_started": False, "reverify_before_playback": True})
    return {**evidence, "completion_observed_at": stamp, "verified_at": stamp, "handoff": exports}

