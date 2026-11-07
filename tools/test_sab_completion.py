"""Synthetic fixtures only; no SAB service, credentials, real media or network."""

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sab_completion as hook


JOB = "468ad695-4260-4ee0-b4bf-2e9b5a631803"
LEGACY = "SABnzbd_nzo_fixture"
REQUEST = "7c035cd0-51eb-4e66-9096-68e1fe57a303"
ALLOWED_ENV = ("SAB_NZO_ID", "SAB_STATUS", "SAB_PP_STATUS", "SAB_COMPLETE_DIR",
               "SAB_FILES", "SAB_BYTES")


class GuardedEnvironment(Mapping):
    """Fail on enumeration, copying, or even a probe of a forbidden key."""

    def __init__(self, values):
        self.values_by_key = values
        self.reads = []

    def __getitem__(self, key):
        if key not in ALLOWED_ENV:
            raise AssertionError("forbidden environment access")
        self.reads.append(key)
        return self.values_by_key[key]

    def __iter__(self):
        raise AssertionError("environment enumeration")

    def __len__(self):
        raise AssertionError("environment size probe")


class CompletionTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / ".work" / "Author" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="completion-fixture-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.control = self.base / "control"
        self.receipts = self.base / "receipts"
        self.media = self.base / "finished"
        for path in (self.control, self.receipts, self.media):
            path.mkdir(mode=0o700)
        (self.media / "disc").mkdir()
        self.payload = b"synthetic public-domain fixture\n"
        self.file = self.media / "disc" / "track.flac"
        self.file.write_bytes(self.payload)
        self.entry = {
            "nzo_id": JOB, "request_id": REQUEST, "scope": "fixture_track",
            "root": str(self.media), "complete_root": str(self.base), "max_bytes": 1024,
            "result_id": "e25445b9-23a8-46f0-8307-07eb685229ad", "result_digest": "a" * 64,
            "nzb_sha256": "b" * 64, "test_only": True,
            "files": [{"path": "disc/track.flac", "bytes": len(self.payload),
                       "sha256": hashlib.sha256(self.payload).hexdigest()}],
        }
        self.registry = self.control / "pending.json"
        self.write_registry()
        self.values = {
            "SAB_NZO_ID": JOB, "SAB_STATUS": "Running", "SAB_PP_STATUS": "0",
            "SAB_COMPLETE_DIR": str(self.media), "SAB_FILES": json.dumps(["disc/track.flac"]),
            "SAB_BYTES": "100", "SAB_API_KEY": "synthetic-forbidden-key",
            "SAB_PASSWORD": "synthetic-forbidden-password",
            "SAB_FINAL_NAME": "synthetic-forbidden-name",
            "HOME": "/synthetic/forbidden", "TMPDIR": "/synthetic/forbidden",
        }

    def write_registry(self, entries=None):
        self.registry.write_text(json.dumps({"version": 2, "requests":
                                            [self.entry] if entries is None else entries}))
        self.registry.chmod(0o600)

    def test_missing_publication_wait_is_bounded_and_never_opens_media(self):
        self.registry.unlink()
        with patch.object(hook, 'REGISTRATION_WAIT_SECONDS', 0.05), patch.object(
                hook.time, 'monotonic', side_effect=[10.0, 10.01, 10.06]), patch.object(
                hook.time, 'sleep') as sleep, patch.object(hook, 'verify_media') as verify:
            self.assertEqual(self.invoke(), 1)
        sleep.assert_called_once()
        verify.assert_not_called()
        self.assertEqual(list(self.receipts.iterdir()), [])

    def test_hook_waits_for_atomic_publisher_directory_lock(self):
        fd = os.open(self.control, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(hook.time, 'sleep', side_effect=lambda _: fcntl.flock(
                    fd, fcntl.LOCK_UN)) as sleep:
                self.assertEqual(self.invoke(), 0)
            sleep.assert_called_once()
        finally:
            os.close(fd)

    def test_two_requests_cannot_bind_the_same_job_directory(self):
        other = self.entry | {'nzo_id': LEGACY, 'request_id': 'another-request'}
        self.write_registry([self.entry, other])
        with patch.object(hook, 'verify_media') as verify:
            self.assertEqual(self.invoke(), 1)
        verify.assert_not_called()

    def invoke(self, values=None, registry=None, receipts=None):
        env = GuardedEnvironment(self.values if values is None else values)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = hook.main(["--registry", str(registry or self.registry),
                                "--receipts", str(receipts or self.receipts)], env)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(env.reads, list(ALLOWED_ENV))
        return result

    @property
    def receipt(self):
        return self.receipts / (REQUEST + ".json")

    def assert_unavailable(self, **kwargs):
        self.assertEqual(self.invoke(**kwargs), 1)
        self.assertFalse(self.receipt.exists())

    def test_running_pp0_receipt_and_truthful_timestamp_contract(self):
        before = datetime.now(timezone.utc)
        self.assertEqual(self.invoke(), 0)
        receipt = json.loads(self.receipt.read_text())
        stamp = datetime.fromisoformat(receipt.pop("completion_observed_at"))
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, datetime.now(timezone.utc))
        self.assertEqual(stamp.utcoffset().total_seconds(), 0)
        self.assertEqual(receipt["state"], "POSTPROCESS_FILE_VERIFIED")
        self.assertEqual(receipt["sab_status"], "Running")
        self.assertEqual(receipt["sab_pp_status"], 0)
        self.assertFalse(receipt["sab_history_completed"])
        self.assertIsNone(receipt["acquired_at"])
        self.assertEqual(receipt["acquired_at_basis"], "unavailable")
        self.assertIsNone(receipt["repair_verified"])
        self.assertIsNone(receipt["crc_verified"])
        self.assertFalse(receipt["full_work_verified"])
        self.assertEqual(receipt["content_identity"], "unverified")
        self.assertEqual(receipt["files"], self.entry["files"])
        exported = receipt["handoff"][0]
        self.assertEqual(exported["media_identity"], "sha256:" + self.entry["files"][0]["sha256"])
        self.assertEqual(exported["relative_path"], "finished/disc/track.flac")
        self.assertEqual(exported["nzo_id"], JOB)
        self.assertEqual(exported["verified_at"], stamp.isoformat().replace("+00:00", "Z"))
        self.assertEqual(exported["selected_scope"], "fixture_track")
        self.assertEqual(exported["observed_media"], "audio")
        self.assertEqual(exported["observed_media_basis"], "filename_extension_only")
        self.assertTrue(exported["test_only"])
        self.assertFalse(exported["kodi_music_catalog_eligible"])
        self.assertFalse(exported["playback_started"])
        self.assertIsNone(exported["acquired_at"])
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.receipt.stat().st_uid, os.geteuid())
        self.assertEqual(self.file.read_bytes(), self.payload)
        self.assertEqual(list(self.receipts.iterdir()), [self.receipt])

    def test_legacy_id_and_optional_bytes(self):
        self.entry["nzo_id"] = LEGACY
        self.values["SAB_NZO_ID"] = LEGACY
        del self.values["SAB_BYTES"]
        self.write_registry()
        self.assertEqual(self.invoke(), 0)
        self.assertIsNone(json.loads(self.receipt.read_text())["sab_bytes"])

    def test_failed_unknown_and_status_case_never_publish(self):
        for status in ("Failed", "failed", "FAILED", "Downloading", "Extracting",
                       "Completed", "running", "completed", "COMPLETED", " Completed", "Completed\n", "", None):
            with self.subTest(status=status), patch.object(hook, "verify_media") as verify:
                self.assert_unavailable(values={**self.values, "SAB_STATUS": status})
                verify.assert_not_called()
        for pp in ("1", "2", "3", "-1", "00", "0 ", "", None):
            with self.subTest(pp=pp):
                self.assert_unavailable(values={**self.values, "SAB_PP_STATUS": pp})

    def test_missing_required_environment_fields(self):
        for key in ALLOWED_ENV[:-1]:
            values = dict(self.values)
            del values[key]
            with self.subTest(key=key):
                self.assert_unavailable(values=values)

    def test_guard_rejects_secret_reads_and_enumeration(self):
        env = GuardedEnvironment(self.values)
        for operation in (lambda: env.get("SAB_API_KEY"), lambda: env.get("SAB_PASSWORD"),
                          lambda: dict(env), lambda: list(env), lambda: len(env)):
            with self.assertRaises(AssertionError):
                operation()
        self.assertEqual(self.invoke(), 0)
        text = self.receipt.read_text()
        self.assertNotIn("synthetic-forbidden", text)
        self.assertNotIn("SAB_API_KEY", text)
        with patch.object(hook, "verify_media", side_effect=RuntimeError("synthetic-forbidden")):
            self.assertEqual(self.invoke(), 3)

    def test_unregistered_job_does_not_open_media_or_infer_name(self):
        with patch.object(hook, "verify_media") as verify:
            for job in ("SABnzbd_nzo_other", "468ad695-4260-4ee0-b4bf-2e9b5a631804", "../job"):
                self.assert_unavailable(values={**self.values, "SAB_NZO_ID": job})
            verify.assert_not_called()
        self.write_registry([])
        self.assert_unavailable()

    def test_complete_directory_must_exactly_match_registry(self):
        for root in (str(self.base), str(self.media) + "/", str(self.media) + "/../finished",
                     str(self.media) + "/.", "/tmp/arbitrary-output"):
            with self.subTest(root=root):
                self.assert_unavailable(values={**self.values, "SAB_COMPLETE_DIR": root})

    def test_receipts_cannot_be_written_into_media_tree(self):
        (self.media / "disc").chmod(0o700)
        for directory in (self.media, self.media / "disc"):
            self.assert_unavailable(receipts=directory)
            self.assertFalse((directory / (REQUEST + ".json")).exists())
        self.assertEqual(self.file.read_bytes(), self.payload)

    def test_missing_partial_extra_and_corrupt_files(self):
        self.file.unlink()
        self.assert_unavailable()
        partial = self.file.with_suffix(".partial")
        partial.write_bytes(self.payload)
        self.assert_unavailable()
        partial.unlink()
        self.file.write_bytes(self.payload[:-1])
        self.assert_unavailable()
        self.file.write_bytes(b"x" * len(self.payload))
        self.assert_unavailable()
        self.file.write_bytes(self.payload)
        extra = self.media / "unrelated.flac"
        extra.write_bytes(b"unrelated fixture")
        with patch.object(hook.hashlib, "sha256") as digest:
            self.assert_unavailable()
            digest.assert_not_called()
        extra.unlink()
        (self.media / "extra-directory").mkdir()
        self.assert_unavailable()

    def test_symlink_leaf_parent_and_root(self):
        other = self.base / "outside.flac"
        other.write_bytes(self.payload)
        self.file.unlink()
        self.file.symlink_to(other)
        self.assert_unavailable()
        self.file.unlink()
        (self.media / "disc").rmdir()
        (self.media / "disc").symlink_to(self.base, target_is_directory=True)
        self.assert_unavailable()
        alias = self.base / "alias"
        alias.symlink_to(self.media, target_is_directory=True)
        self.entry["root"] = str(alias)
        self.values["SAB_COMPLETE_DIR"] = str(alias)
        self.write_registry()
        self.assert_unavailable()

    def test_symlink_ancestor_registry_and_receipt_directory(self):
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        self.assert_unavailable(registry=alias / "control" / "pending.json")
        self.assert_unavailable(receipts=alias / "receipts")
        other = self.control / "actual.json"
        self.registry.rename(other)
        self.registry.symlink_to(other)
        self.assert_unavailable()

    def test_hardlink_fifo_and_directory_are_not_media(self):
        self.file.unlink()
        other = self.base / "outside.flac"
        other.write_bytes(self.payload)
        os.link(other, self.file)
        self.assert_unavailable()
        self.file.unlink()
        os.mkfifo(self.file)
        self.assert_unavailable()
        self.file.unlink()
        self.file.mkdir()
        self.assert_unavailable()

    def test_registry_paths_reject_traversal_and_partial_components(self):
        for path in ("../outside.flac", "/absolute.flac", "disc/../track.flac",
                     "disc//track.flac", "disc/./track.flac", "disc\\track.flac",
                     "disc/track.part", "disc/track.PARTIAL", "_UNPACK_fixture/track.flac",
                     "disc/track.tmp", "disc/track\x00.flac"):
            with self.subTest(path=path):
                self.entry["files"][0]["path"] = path
                self.write_registry()
                self.assert_unavailable()

    def test_registry_schema_duplicates_and_owner_modes(self):
        for entries in ([self.entry, self.entry], [{**self.entry, "request_id": "../escape"}],
                        [{**self.entry, "nzo_id": "not-a-job"}],
                        [{**self.entry, "SAB_API_KEY": "synthetic-forbidden"}],
                        [{**self.entry, "files": self.entry["files"] * 2}]):
            self.write_registry(entries)
            self.assert_unavailable()
        self.registry.write_text('{"version":1,"version":1,"requests":[]}')
        self.assert_unavailable()
        self.write_registry()
        self.registry.chmod(0o644)
        self.assert_unavailable()
        self.registry.chmod(0o600)
        self.receipts.chmod(0o755)
        self.assert_unavailable()
        self.receipts.chmod(0o700)
        self.control.chmod(0o755)
        self.assert_unavailable()

    def test_size_and_number_bounds(self):
        for limit in (0, True, len(self.payload) - 1, hook.MAX_BYTES + 1):
            self.entry["max_bytes"] = limit
            self.write_registry()
            self.assert_unavailable()
        self.entry["max_bytes"] = 1024
        self.write_registry()
        for key in ("SAB_BYTES", "SAB_FILES"):
            for value in ("", "-1", "0", "1.5", " 4", "9" * 21, "media.flac"):
                with self.subTest(key=key, value=value):
                    self.assert_unavailable(values={**self.values, key: value})
        self.assert_unavailable(values={**self.values, "SAB_BYTES": "1025"})
        self.file.write_bytes(b"x" * 1025)
        with patch.object(hook.hashlib, "sha256") as digest:
            self.assert_unavailable()
            digest.assert_not_called()

    def test_untrusted_control_ancestor_is_rejected(self):
        self.base.chmod(0o777)
        try:
            self.assert_unavailable()
        finally:
            self.base.chmod(0o700)

    def test_unrelated_registered_job_is_not_opened(self):
        other = {**self.entry, "nzo_id": LEGACY, "request_id": "other-fixture",
                 "root": str(self.base / "nonexistent-unrelated-media")}
        self.write_registry([other, self.entry])
        self.assertEqual(self.invoke(), 0)

    def test_oversized_registry_is_rejected_before_decoding(self):
        self.registry.write_bytes(b" " * (hook.MAX_JSON + 1))
        with patch.object(hook.json, "loads") as decode:
            self.assert_unavailable()
            decode.assert_not_called()

    def test_fsync_failure_before_publication_leaves_no_receipt(self):
        with patch.object(hook.os, "fsync", side_effect=OSError("fixture failure")):
            self.assert_unavailable()
        self.assertEqual(list(self.receipts.iterdir()), [])
        self.assertEqual(self.invoke(), 0)

    def test_multiple_files_aggregate_ceiling_and_exact_directory_membership(self):
        second = self.media / "disc" / "second.flac"
        second.write_bytes(self.payload)
        self.entry["files"].append({**self.entry["files"][0], "path": "disc/second.flac"})
        self.values["SAB_FILES"] = json.dumps([item["path"] for item in self.entry["files"]])
        self.entry["max_bytes"] = len(self.payload) * 2 - 1
        self.write_registry()
        self.assert_unavailable()
        self.entry["max_bytes"] = 1024
        self.write_registry()
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(json.loads(self.receipt.read_text())["total_bytes"], len(self.payload) * 2)

    def test_duplicate_and_restart_preserve_first_receipt_bytes(self):
        self.assertEqual(self.invoke(), 0)
        before = self.receipt.read_bytes()
        inode = self.receipt.stat().st_ino
        for _ in range(3):
            self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertEqual(self.receipt.stat().st_ino, inode)
        self.assertEqual(list(self.receipts.iterdir()), [self.receipt])

    def test_conflicting_replay_never_overwrites(self):
        self.assertEqual(self.invoke(), 0)
        before = self.receipt.read_bytes()
        self.assertEqual(self.invoke(values={**self.values, "SAB_BYTES": "101"}), 2)
        self.entry["scope"] = "different_scope"
        self.write_registry()
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertEqual(self.invoke(values={**self.values, "SAB_STATUS": "Failed"}), 1)
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_existing_malformed_or_symlink_receipt_is_a_conflict(self):
        self.receipt.write_bytes(b"{")
        self.receipt.chmod(0o600)
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.receipt.read_bytes(), b"{")
        self.receipt.unlink()
        target = self.base / "untouched"
        target.write_bytes(b"synthetic untouched")
        self.receipt.symlink_to(target)
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(target.read_bytes(), b"synthetic untouched")

    def test_existing_receipt_requires_exact_json_types(self):
        self.assertEqual(self.invoke(), 0)
        receipt = json.loads(self.receipt.read_text())
        receipt["version"] = True
        self.receipt.write_text(json.dumps(receipt))
        before = self.receipt.read_bytes()
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_same_request_with_changed_job_or_content_conflicts(self):
        self.assertEqual(self.invoke(), 0)
        before = self.receipt.read_bytes()
        self.entry["nzo_id"] = LEGACY
        self.values["SAB_NZO_ID"] = LEGACY
        self.write_registry()
        self.assertEqual(self.invoke(), 2)
        self.entry["nzo_id"] = JOB
        self.values["SAB_NZO_ID"] = JOB
        changed = b"x" * len(self.payload)
        self.file.write_bytes(changed)
        self.entry["files"][0]["sha256"] = hashlib.sha256(changed).hexdigest()
        self.write_registry()
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_concurrent_same_evidence_has_one_receipt(self):
        def invoke(_):
            return hook.run(str(self.registry), str(self.receipts), GuardedEnvironment(self.values))
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(invoke, range(8))), [0] * 8)
        self.assertEqual(list(self.receipts.iterdir()), [self.receipt])

    def test_failure_before_atomic_publish_and_restart(self):
        with patch.object(hook.os, "link", side_effect=OSError("synthetic error")):
            self.assert_unavailable()
        self.assertEqual(list(self.receipts.iterdir()), [])
        orphan = self.receipts / ".pending-orphan"
        orphan.write_bytes(b"incomplete synthetic receipt")
        orphan.chmod(0o600)
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(orphan.read_bytes(), b"incomplete synthetic receipt")

    def test_crash_after_link_leaves_complete_idempotent_receipt(self):
        real_unlink = os.unlink
        with patch.object(hook.os, "unlink", side_effect=OSError("simulated crash cleanup")):
            self.assertEqual(self.invoke(), 1)
        self.assertTrue(self.receipt.exists())
        before = self.receipt.read_bytes()
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.receipt.read_bytes(), before)
        for orphan in self.receipts.glob(".pending-*"):
            real_unlink(orphan)

    def test_change_during_hash_is_rejected(self):
        real_hash = hashlib.sha256
        file = self.file

        class MutatingHash:
            def __init__(self):
                self.digest = real_hash()

            def update(self, chunk):
                self.digest.update(chunk)
                file.write_bytes(b"x" * len(chunk))

            def hexdigest(self):
                return self.digest.hexdigest()

        with patch.object(hook.hashlib, "sha256", MutatingHash):
            self.assert_unavailable()

    def test_ordinary_new_download_computes_hash_and_size_without_expected_manifest(self):
        self.entry["files"] = None
        self.entry["scope"] = "film"
        self.write_registry()
        self.assertEqual(self.invoke(), 0)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["files"], [{"path": "disc/track.flac", "bytes": len(self.payload),
                                          "sha256": hashlib.sha256(self.payload).hexdigest()}])
        self.assertIsNone(receipt["expected_manifest"])
        exported = receipt["handoff"][0]
        self.assertEqual(exported["selected_scope"], "film")
        self.assertEqual(exported["observed_media"], "audio")
        self.assertEqual(exported["content_identity"], "unverified")
        self.assertFalse(exported["full_work_verified"])
        before = self.receipt.read_bytes()
        self.file.write_bytes(b"x" * len(self.payload))
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_optional_expected_hash_and_size_independently_enforced(self):
        for expected in ({"path": "disc/track.flac"},
                         {"path": "disc/track.flac", "bytes": len(self.payload)},
                         {"path": "disc/track.flac", "sha256": hashlib.sha256(self.payload).hexdigest()}):
            self.entry["files"] = [expected]
            self.write_registry()
            self.assertEqual(self.invoke(), 0)
            self.receipt.unlink()
        for expected in ({"path": "disc/track.flac", "bytes": len(self.payload) + 1},
                         {"path": "disc/track.flac", "sha256": "0" * 64}):
            self.entry["files"] = [expected]
            self.write_registry()
            self.assert_unavailable()

    def test_sab_file_list_unknown_shapes_and_undeclared_paths_refused(self):
        self.entry["files"] = None
        self.write_registry()
        values = ["4", "null", "{}", "[]", "[null]", '["../outside"]',
                  '["/absolute/file"]', '["disc/track.flac", "disc/track.flac"]',
                  '["disc/track.part.flac"]', '["disc/unexpected.flac"]',
                  json.dumps(["disc/track.flac"] * (hook.MAX_FILES + 1)),
                  " " * (hook.MAX_JSON + 1)]
        for value in values:
            with self.subTest(value=value[:80]):
                self.assert_unavailable(values={**self.values, "SAB_FILES": value})
        extra = self.media / "not-declared.txt"
        extra.write_bytes(b"never read this unrelated fixture")
        with patch.object(hook.hashlib, "sha256") as digest:
            self.assert_unavailable()
            digest.assert_not_called()

    def test_dynamic_files_total_ceiling_and_empty_file_refused(self):
        self.entry["files"] = None
        self.write_registry()
        self.file.write_bytes(b"")
        self.assert_unavailable()
        self.file.write_bytes(b"x" * 1025)
        self.assert_unavailable()
        self.file.write_bytes(b"x" * 600)
        (self.media / "disc" / "second.flac").write_bytes(b"y" * 600)
        self.values["SAB_FILES"] = json.dumps(["disc/track.flac", "disc/second.flac"])
        self.assert_unavailable()

    def test_result_binding_complete_root_and_export_replay_conflicts(self):
        self.assertEqual(self.invoke(), 0)
        before = self.receipt.read_bytes()
        self.entry["result_digest"] = "c" * 64
        self.write_registry()
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.receipt.read_bytes(), before)
        self.entry["complete_root"] = str(self.base / "unrelated")
        self.write_registry()
        self.assertEqual(self.invoke(), 1)
        self.entry["complete_root"] = str(self.base)
        self.entry["result_digest"] = "a" * 64
        self.write_registry()
        receipt = json.loads(before)
        receipt["handoff"][0]["acquired_at"] = receipt["verified_at"]
        self.receipt.write_text(json.dumps(receipt))
        self.assertEqual(self.invoke(), 2)

    def test_cli_usage_is_silent_and_does_not_touch_environment(self):
        env = GuardedEnvironment(self.values)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            for args in ([], ["--help"], ["--registry", "synthetic-forbidden"],
                         ["--registry", str(self.registry), "--receipts", str(self.receipts), "extra"]):
                self.assertEqual(hook.main(args, env), 1)
        self.assertEqual((out.getvalue(), err.getvalue(), env.reads), ("", "", []))


if __name__ == "__main__":
    unittest.main()
