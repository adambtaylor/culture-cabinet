"""Synthetic bytes and private scratch only; fixture durability is not runtime storage."""
from dataclasses import asdict, FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from uuid import UUID

from shelf.acquisition_contract import Blocked, ConfirmedSelection, ContractError
from shelf.acquisition_pipeline import (
    AdapterUnavailable, Checkpoint, Completion, EmbyReference, Evidence,
    ImportReceipt, LibraryItem, MediaSource, Pipeline, ProviderJob,
    QueueRejected, Snapshot, StagedFile, verify_staging,
)


REQUEST_ID = "7c035cd0-51eb-4e66-9096-68e1fe57a301"
SYNTHETIC_BYTES = b"Fictional full-work fixture; not playable media.\n"


def saved_job(**changes):
    values = dict(actor_id=11, profile_id=22, request_id=UUID(REQUEST_ID),
                  provider="tmdb", medium="movie", source_id=101,
                  edition="Synthetic edition A", scope="film", storage="local_library",
                  authorized_content=True, storage_check=True, status="blocked_configuration")
    values.update(changes)
    return SimpleNamespace(**values)


def evidence_for(snapshot):
    from shelf.acquisition_pipeline import scope_family
    media, episode = scope_family(snapshot.provider, snapshot.medium, snapshot.scope)
    return Evidence(snapshot, ConfirmedSelection(
        item_id=f"{snapshot.provider}:{snapshot.medium}:{snapshot.source_id}",
        media=media, edition=snapshot.edition, storage=snapshot.storage,
        authorized_content=True, user_confirmed=True, edition_verified=True,
        complete_work_verified=True, storage_checked=True, duplicate_checked=True,
        episode=episode))


def write_json(path, value):
    """Fixture-only atomic checkpoint replacement, including directory sync."""
    draft = path.with_suffix(".draft")
    with draft.open("w") as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(draft, path)
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_job(value):
    return ProviderJob(Snapshot(**value["snapshot"]), value["job_id"])


def read_staged(value):
    return StagedFile(**{**value, "job": read_job(value["job"])})


def read_receipt(value):
    return ImportReceipt(read_staged(value["staged"]), value["path"])


class FixtureCheckpoints:
    """Single-worker JSON test records, deliberately separate from fake adapters."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)

    def load(self, request_id):
        path = self.root / f"{request_id}.json"
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        value["snapshot"] = Snapshot(**value["snapshot"])
        if value["evidence"] is not None:
            evidence = value["evidence"]
            value["evidence"] = Evidence(Snapshot(**evidence["snapshot"]),
                                         ConfirmedSelection(**evidence["selection"]))
        for name, convert in (("job", read_job), ("staged", read_staged),
                              ("imported", read_receipt), ("emby", lambda v: EmbyReference(**v))):
            if value[name] is not None:
                value[name] = convert(value[name])
        return Checkpoint(**value)

    def save(self, checkpoint):
        prior = self.load(checkpoint.snapshot.request_id)
        if prior is not None and prior.snapshot != checkpoint.snapshot:
            raise ContractError("Fixture request identity conflict")
        write_json(self.root / f"{checkpoint.snapshot.request_id}.json", asdict(checkpoint))


class SimulatedCrash(RuntimeError):
    pass


class FixtureProvider:
    def __init__(self, root):
        self.path = Path(root) / "fake-provider.json"
        self.enqueue_calls = 0
        self.cancel_calls = 0
        self.requests = []
        self.reject = False
        self.lose_ack = False
        self.unavailable = False
        self.cancel_confirmed = False
        self.before_enqueue = None
        self.completion = None

    def enqueue(self, snapshot, request):
        self.enqueue_calls += 1
        self.requests.append(request)
        if self.before_enqueue:
            self.before_enqueue()
        if self.reject:
            raise QueueRejected
        if self.path.exists():
            raise AssertionError("Duplicate enqueue side effect")
        job = ProviderJob(snapshot, "fake-sab-job-1")
        write_json(self.path, asdict(job))
        if self.lose_ack:
            raise AdapterUnavailable
        return job

    def lookup(self, snapshot):
        if self.unavailable:
            raise AdapterUnavailable
        return read_job(json.loads(self.path.read_text())) if self.path.exists() else None

    def poll(self, job):
        if self.unavailable:
            raise AdapterUnavailable
        return self.completion

    def cancel(self, job):
        self.cancel_calls += 1
        if self.unavailable:
            raise AdapterUnavailable
        return self.cancel_confirmed


class FixtureImporter:
    """Publish a directory containing synthetic bytes and its receipt in one rename."""
    def __init__(self, root):
        self.root = Path(root) / "fake-library"
        self.root.mkdir(exist_ok=True)
        self.commit_calls = 0
        self.crash_after_commit = False
        self.before_publish = None

    def destination(self, staged):
        return self.root / staged.job.snapshot.request_id

    def lookup(self, staged):
        destination = self.destination(staged)
        if not destination.exists():
            return None
        try:
            receipt = read_receipt(json.loads((destination / "receipt.json").read_text()))
        except (OSError, ValueError, KeyError, TypeError):
            raise ContractError("Conflicting fixture destination") from None
        if receipt.staged != staged or receipt.path != str(destination / "content.fixture"):
            raise ContractError("Conflicting fixture identity or digest")
        verify_staging(replace(staged, path=receipt.path))
        return receipt

    def commit(self, staged):
        self.commit_calls += 1
        existing = self.lookup(staged)
        if existing is not None:
            return existing
        verify_staging(staged)
        destination = self.destination(staged)
        receipt = ImportReceipt(staged, str(destination / "content.fixture"))
        with TemporaryDirectory(prefix=".draft-", dir=self.root) as draft_name:
            draft = Path(draft_name)
            with (draft / "content.fixture").open("wb") as handle:
                handle.write(Path(staged.path).read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            verify_staging(replace(staged, path=str(draft / "content.fixture")))
            write_json(draft / "receipt.json", asdict(receipt))
            sync_directory(draft)
            if self.before_publish:
                self.before_publish(draft, destination)
            # Existing nonempty destinations cannot be overwritten by directory rename.
            os.rename(draft, destination)
            sync_directory(self.root)
        if self.crash_after_commit:
            raise SimulatedCrash("After fixture import commit")
        return receipt


class FixtureLibrary:
    def __init__(self):
        self.indexed = False
        self.record = None
        self.calls = 0

    def lookup(self, imported):
        self.calls += 1
        if self.record is not None:
            return self.record
        if not self.indexed:
            return None
        return LibraryItem(imported.staged.job.snapshot, "fixture-emby-item-7", (
            MediaSource(imported.staged.job, "fixture-media-source-9", imported.path,
                        imported.staged.sha256, imported.staged.size),))


class AcquisitionPipelineTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="acquisition-pipeline-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.saved = saved_job()
        self.checkpoints = FixtureCheckpoints(self.root / "checkpoints")
        self.provider = FixtureProvider(self.root)
        self.importer = FixtureImporter(self.root)
        self.library = FixtureLibrary()
        self.pipeline = self.new_pipeline()
        self.evidence = evidence_for(self.pipeline.snapshot)

    def new_pipeline(self):
        return Pipeline(self.saved, self.checkpoints, self.provider, self.importer, self.library)

    def dispatch(self):
        return self.pipeline.dispatch(self.evidence)

    def complete(self, **changes):
        if self.pipeline.checkpoint.state == "blocked":
            self.dispatch()
        job = self.pipeline.checkpoint.job
        path = self.root / "staging.fixture"
        path.write_bytes(SYNTHETIC_BYTES)
        staged = StagedFile(job, str(path), hashlib.sha256(SYNTHETIC_BYTES).hexdigest(),
                            len(SYNTHETIC_BYTES), True, False)
        self.provider.completion = replace(Completion(job, "completed", True, True, 0, staged),
                                           **changes)
        return staged

    def reach_import(self):
        self.complete()
        self.assertEqual(self.pipeline.advance().state, "verifying")
        self.assertEqual(self.pipeline.advance().state, "importing")
        return self.pipeline.advance()

    def reload_in_process(self, action):
        script = '''
import json, sys
from pathlib import Path
from shelf.acquisition_pipeline import Pipeline
from shelf.tests.test_acquisition_pipeline import (
    saved_job, FixtureCheckpoints, FixtureProvider, FixtureImporter, FixtureLibrary)
root = Path(sys.argv[1])
library = FixtureLibrary()
library.indexed = True
pipeline = Pipeline(saved_job(), FixtureCheckpoints(root / "checkpoints"),
                    FixtureProvider(root), FixtureImporter(root), library)
result = getattr(pipeline, sys.argv[2])()
print(json.dumps({"state": result.state, "emby": None if result.emby is None else
                 [result.emby.item_id, result.emby.media_source_id]}))
'''
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.root), action],
                                cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_exact_saved_media_mapping_and_immutable_snapshot(self):
        for provider, medium, scope, media, episode in (
                ("tmdb", "movie", "film", "film", None),
                ("tmdb", "tv", "series_s01e01", "episode", "S01E01"),
                ("tmdb", "tv", "episode_s02e02", "episode", "S02E02"),
                ("hardcover", "book", "audiobook", "audiobook", None)):
            with self.subTest(medium=medium):
                snapshot = Snapshot.from_saved_job(saved_job(provider=provider, medium=medium, scope=scope))
                evidence = evidence_for(snapshot)
                self.assertEqual((evidence.selection.media, evidence.selection.episode), (media, episode))
                self.assertEqual((snapshot.actor_id, snapshot.profile_id, snapshot.request_id),
                                 (11, 22, REQUEST_ID))
                self.assertEqual((snapshot.provider, snapshot.medium, snapshot.source_id,
                                  snapshot.edition, snapshot.scope, snapshot.storage),
                                 (provider, medium, 101, "Synthetic edition A", scope, "local_library"))
                with self.assertRaises(FrozenInstanceError):
                    snapshot.edition = "Different"

    def test_inconsistent_saved_combinations_are_rejected(self):
        for changes in ({"scope": "season"}, {"scope": "audiobook"},
                        {"provider": "hardcover"}, {"medium": "tv"},
                        {"source_id": 0}, {"source_id": True}, {"source_id": 2147483648},
                        {"request_id": "not-a-uuid"}, {"storage": "unknown"},
                        {"edition": ""}, {"authorized_content": False}, {"storage_check": False}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                Snapshot.from_saved_job(saved_job(**changes))

    def test_saved_acknowledgement_does_not_supply_verification(self):
        checkpoint = self.pipeline.checkpoint
        self.assertEqual(checkpoint.state, "blocked")
        self.assertIsNone(checkpoint.evidence)
        with self.assertRaises(ContractError):
            self.pipeline.dispatch(self.saved)
        for flag in ("storage_checked", "duplicate_checked"):
            with self.subTest(flag=flag), self.assertRaises(Blocked):
                replace(self.evidence.selection, **{flag: False})
        self.assertEqual(self.provider.enqueue_calls, 0)

    def test_repeat_click_and_reload_reuse_one_receipt_without_mutating_saved_intent(self):
        before = vars(self.saved).copy()
        first = self.dispatch()
        self.assertEqual(self.pipeline.dispatch(self.evidence), first)
        self.assertEqual(self.new_pipeline().dispatch(self.evidence), first)
        self.assertEqual(self.provider.enqueue_calls, 1)
        self.assertEqual(vars(self.saved), before)
        self.assertEqual(self.provider.requests[0].selection, self.evidence.selection)
        self.assertEqual(self.provider.requests[0].request_id, REQUEST_ID)

    def test_conflicting_saved_identity_cannot_replace_checkpoint(self):
        self.dispatch()
        for field, value in (("actor_id", 33), ("profile_id", 44), ("source_id", 102),
                             ("edition", "Different edition"), ("scope", "series_s01e01")):
            with self.subTest(field=field), self.assertRaises(ContractError):
                Pipeline(saved_job(**{field: value}), self.checkpoints,
                         self.provider, self.importer, self.library)
        self.assertEqual(self.provider.enqueue_calls, 1)

    def test_evidence_must_bind_actor_profile_request_source_and_edition(self):
        for field, value in (("actor_id", 33), ("profile_id", 44),
                             ("request_id", "7c035cd0-51eb-4e66-9096-68e1fe57a302"),
                             ("source_id", 102), ("edition", "Other")):
            with self.subTest(field=field), self.assertRaises(ContractError):
                self.pipeline.dispatch(evidence_for(replace(self.pipeline.snapshot, **{field: value})))
        for changes in ({"item_id": "tmdb:movie:102"}, {"edition": "Other"},
                        {"storage": "Other"}, {"media": "audiobook"}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                Evidence(self.pipeline.snapshot, replace(self.evidence.selection, **changes))
        self.assertEqual(self.provider.enqueue_calls, 0)

    def test_episode_evidence_rejects_packs_ranges_and_other_episodes(self):
        snapshot = Snapshot.from_saved_job(saved_job(medium="tv", scope="series_s01e01"))
        for episode in (None, "S01", "S01E02", "S01E01E02", "S01E01-E02"):
            with self.subTest(episode=episode), self.assertRaises(ContractError):
                Evidence(snapshot, replace(evidence_for(snapshot).selection, episode=episode))

    def test_dispatch_intent_is_durable_before_enqueue(self):
        def inspect():
            durable = FixtureCheckpoints(self.root / "checkpoints").load(REQUEST_ID)
            self.assertEqual(durable.state, "outcome_unknown")
            self.assertEqual(durable.evidence, self.evidence)
        self.provider.before_enqueue = inspect
        self.assertEqual(self.dispatch().state, "queued")

    def test_definite_rejection_allows_only_explicit_same_request_retry(self):
        self.provider.reject = True
        failed = self.dispatch()
        self.assertEqual((failed.state, failed.reason), ("failed", "queue_rejected"))
        self.pipeline.dispatch(self.evidence)
        self.assertEqual(self.provider.enqueue_calls, 1)
        self.pipeline.retry_rejected()
        self.provider.reject = False
        self.assertEqual(self.dispatch().state, "queued")
        self.assertEqual([r.request_id for r in self.provider.requests], [REQUEST_ID, REQUEST_ID])

    def test_ack_loss_reconciles_after_actual_process_reload_without_enqueue(self):
        self.provider.lose_ack = True
        self.assertEqual(self.dispatch().state, "outcome_unknown")
        self.assertEqual(self.reload_in_process("advance")["state"], "queued")
        self.assertEqual(self.new_pipeline().dispatch(self.evidence).job.job_id, "fake-sab-job-1")
        self.assertEqual(self.provider.enqueue_calls, 1)

    def test_crash_before_enqueue_does_not_authorize_automatic_retry(self):
        def crash():
            raise SimulatedCrash
        self.provider.before_enqueue = crash
        with self.assertRaises(SimulatedCrash):
            self.dispatch()
        self.assertFalse(self.provider.path.exists())
        self.assertEqual(self.reload_in_process("advance")["state"], "outcome_unknown")
        self.assertEqual(self.pipeline.advance().reason, "manual_reconciliation_required")
        with self.assertRaises(ContractError):
            self.pipeline.retry_rejected()
        self.pipeline.dispatch(self.evidence)
        self.assertEqual(self.provider.enqueue_calls, 1)

    def test_unavailable_lookup_never_reenqueues(self):
        self.provider.lose_ack = True
        self.dispatch()
        self.provider.unavailable = True
        for _ in range(3):
            self.assertEqual(self.new_pipeline().advance().state, "outcome_unknown")
        self.assertEqual(self.provider.enqueue_calls, 1)

    def test_reconciliation_rejects_wrong_request_identity(self):
        self.provider.lose_ack = True
        self.dispatch()
        wrong = ProviderJob(replace(self.pipeline.snapshot, actor_id=99), "wrong-job")
        write_json(self.provider.path, asdict(wrong))
        self.assertEqual(self.pipeline.advance().state, "failed")
        self.assertEqual(self.provider.enqueue_calls, 1)

    def test_queue_and_downloading_are_not_completion(self):
        self.dispatch()
        self.assertEqual(self.pipeline.advance().state, "downloading")
        self.assertEqual(self.new_pipeline().advance().state, "downloading")
        self.assertEqual(self.importer.commit_calls, 0)
        self.assertEqual(self.library.calls, 0)

    def test_file_presence_without_provider_success_does_not_authorize_verification(self):
        staged = self.complete()
        self.provider.completion = None
        self.assertTrue(Path(staged.path).is_file())
        self.assertEqual(self.pipeline.advance().state, "downloading")
        self.assertIsNone(self.pipeline.checkpoint.staged)
        self.assertEqual(self.importer.commit_calls, 0)

    def test_failed_and_contradictory_sab_completion_cannot_verify(self):
        staged = self.complete()
        for changes in ({"final_status": "failed"}, {"final_status": "running"},
                        {"repair_ok": False}, {"unpack_ok": False}, {"repair_ok": 1},
                        {"postprocess_status": 1}, {"postprocess_status": 2},
                        {"postprocess_status": 3}, {"postprocess_status": -1},
                        {"postprocess_status": False}, {"postprocess_status": None}):
            with self.subTest(changes=changes):
                self.checkpoints.save(replace(self.pipeline.checkpoint, state="queued"))
                self.provider.completion = replace(
                    Completion(staged.job, "completed", True, True, 0, staged), **changes)
                result = self.pipeline.advance()
                self.assertEqual((result.state, result.reason), ("failed", "provider_failed"))
        self.assertEqual(self.importer.commit_calls, 0)

    def test_success_without_bound_file_is_rejected(self):
        self.complete(staged=None)
        self.assertEqual(self.pipeline.advance().state, "failed")
        self.assertEqual(self.importer.commit_calls, 0)

    def test_wrong_provider_job_or_completion_snapshot_is_rejected(self):
        staged = self.complete()
        correct = self.pipeline.checkpoint
        for job in (replace(staged.job, job_id="other-job"),
                    replace(staged.job, snapshot=replace(staged.job.snapshot, source_id=102)),
                    replace(staged.job, snapshot=replace(staged.job.snapshot, edition="Other")),
                    replace(staged.job, snapshot=replace(staged.job.snapshot, medium="tv", scope="series_s01e01"))):
            with self.subTest(job=job):
                self.checkpoints.save(correct)
                self.provider.completion = Completion(job, "completed", True, True, 0, staged)
                self.assertEqual(self.pipeline.advance().state, "failed")
                self.checkpoints.save(correct)
                self.provider.completion = Completion(staged.job, "completed", True, True, 0,
                                                       replace(staged, job=job))
                self.assertEqual(self.pipeline.advance().state, "failed")
        self.assertEqual(self.importer.commit_calls, 0)

    def test_staging_requires_complete_scope_hash_and_size(self):
        staged = self.complete()
        for changes in ({"complete_work": False}, {"partial": True}, {"complete_work": 1},
                        {"size": staged.size + 1}, {"size": 0}, {"sha256": "0" * 64},
                        {"sha256": "unknown"}, {"path": "relative.fixture"}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                verify_staging(replace(staged, **changes))

    def test_nonregular_symlink_missing_and_partial_paths_are_rejected(self):
        staged = self.complete()
        link = self.root / "link.fixture"
        link.symlink_to(staged.path)
        fifo = self.root / "fifo.fixture"
        os.mkfifo(fifo)
        partial = self.root / "staging.part"
        partial.write_bytes(SYNTHETIC_BYTES)
        for path in (self.root, link, fifo, partial, self.root / "absent.fixture"):
            with self.subTest(path=path), self.assertRaises(ContractError):
                verify_staging(replace(staged, path=str(path)))

    def test_invalid_staging_fails_before_import_or_library(self):
        staged = self.complete()
        self.pipeline.advance()
        Path(staged.path).write_bytes(b"truncated")
        self.assertEqual(self.pipeline.advance().state, "failed")
        self.assertEqual(self.importer.commit_calls, 0)
        self.assertEqual(self.library.calls, 0)
        with self.assertRaises(ContractError):
            self.pipeline.retry_rejected()

    def test_restart_after_completion_retains_verification_evidence(self):
        staged = self.complete()
        self.pipeline.advance()
        self.assertEqual(self.reload_in_process("advance")["state"], "importing")
        self.assertEqual(self.pipeline.checkpoint.staged, staged)
        self.assertEqual(self.pipeline.checkpoint.evidence, self.evidence)
        self.assertEqual(self.importer.commit_calls, 0)

    def test_staging_is_rechecked_before_import_commit(self):
        staged = self.complete()
        self.pipeline.advance()
        self.pipeline.advance()
        Path(staged.path).write_bytes(b"changed after verification")
        self.assertEqual(self.pipeline.advance().state, "failed")
        self.assertEqual(self.importer.commit_calls, 0)

    def test_import_visibility_is_atomic_and_indexing_is_separate(self):
        observed = []
        def inspect(draft, destination):
            observed.append(destination.exists())
            self.assertEqual((draft / "content.fixture").read_bytes(), SYNTHETIC_BYTES)
            self.assertTrue((draft / "receipt.json").is_file())
        self.importer.before_publish = inspect
        imported = self.reach_import()
        self.assertEqual(observed, [False])
        self.assertEqual(imported.state, "importing")
        self.assertIsNone(imported.emby)
        self.assertEqual(Path(imported.imported.path).read_bytes(), SYNTHETIC_BYTES)
        self.assertEqual(self.library.calls, 0)
        self.assertEqual(self.pipeline.advance().reason, "awaiting_index")
        self.library.indexed = True
        available = self.pipeline.advance()
        self.assertEqual(available.state, "available")
        self.assertEqual(available.emby, EmbyReference("fixture-emby-item-7", "fixture-media-source-9"))
        self.assertEqual(self.pipeline.advance(), available)
        self.assertEqual(self.importer.commit_calls, 1)

    def test_duplicate_import_checks_identity_and_digest_without_overwrite(self):
        imported = self.reach_import().imported
        self.assertEqual(self.importer.commit(imported.staged), imported)
        for staged in (replace(imported.staged, sha256="0" * 64),
                       replace(imported.staged, job=replace(imported.staged.job,
                           snapshot=replace(imported.staged.job.snapshot, edition="Other")))):
            with self.subTest(staged=staged), self.assertRaises(ContractError):
                self.importer.commit(staged)
        self.assertEqual(Path(imported.path).read_bytes(), SYNTHETIC_BYTES)

    def test_conflicting_destination_is_not_overwritten(self):
        staged = self.complete()
        destination = self.importer.destination(staged)
        destination.mkdir()
        canary = destination / "unrelated.fixture"
        canary.write_bytes(b"preserve fixture")
        self.pipeline.advance()
        self.pipeline.advance()
        self.assertEqual(self.pipeline.advance().state, "failed")
        self.assertEqual(canary.read_bytes(), b"preserve fixture")
        self.assertFalse((destination / "content.fixture").exists())

    def test_import_commit_crash_recovers_in_new_process_without_staging_or_duplicate(self):
        staged = self.complete()
        self.pipeline.advance()
        self.pipeline.advance()
        self.importer.crash_after_commit = True
        with self.assertRaises(SimulatedCrash):
            self.pipeline.advance()
        self.assertIsNone(self.pipeline.checkpoint.imported)
        Path(staged.path).unlink()
        self.assertEqual(self.reload_in_process("advance")["state"], "importing")
        self.assertIsNotNone(self.pipeline.checkpoint.imported)
        self.assertEqual(self.reload_in_process("advance"),
                         {"state": "available", "emby": ["fixture-emby-item-7", "fixture-media-source-9"]})
        self.assertEqual(self.importer.commit_calls, 1)
        self.assertEqual(len(list(self.importer.root.glob("*/content.fixture"))), 1)

    def test_corrupted_committed_bytes_cannot_reconcile_as_success(self):
        imported = self.reach_import().imported
        Path(imported.path).write_bytes(b"corrupt")
        with self.assertRaises(ContractError):
            self.importer.lookup(imported.staged)

    def test_library_must_match_item_and_exact_media_source_identity(self):
        imported_checkpoint = self.reach_import()
        self.library.indexed = True
        record = self.library.lookup(imported_checkpoint.imported)
        source = record.media_sources[0]
        wrong_sources = [replace(source, **changes) for changes in (
            {"job": replace(source.job, job_id="wrong-job")},
            {"job": replace(source.job, snapshot=replace(source.job.snapshot, edition="Other"))},
            {"path": str(self.root / "other.fixture")}, {"sha256": "0" * 64},
            {"size": source.size + 1}, {"source_id": ""})]
        records = [replace(record, media_sources=(wrong,)) for wrong in wrong_sources]
        records += [replace(record, snapshot=replace(record.snapshot, source_id=102)),
                    replace(record, item_id=""), replace(record, media_sources=()),
                    replace(record, media_sources=(source, replace(source, source_id="ambiguous")))]
        for wrong in records:
            with self.subTest(record=wrong):
                self.checkpoints.save(imported_checkpoint)
                self.library.record = wrong
                result = self.pipeline.advance()
                self.assertEqual(result.state, "failed")
                self.assertIsNone(result.emby)

    def test_exact_source_can_be_selected_among_unrelated_sources(self):
        imported = self.reach_import().imported
        self.library.indexed = True
        record = self.library.lookup(imported)
        unrelated = replace(record.media_sources[0], source_id="other", path="/synthetic/other")
        self.library.record = replace(record, media_sources=(unrelated, *record.media_sources))
        self.assertEqual(self.pipeline.advance().emby.media_source_id, "fixture-media-source-9")

    def test_cancel_before_dispatch_is_local_and_does_not_call_provider(self):
        self.assertEqual(self.pipeline.cancel().state, "cancelled")
        self.assertEqual(self.pipeline.dispatch(self.evidence).state, "cancelled")
        self.assertEqual(self.provider.enqueue_calls, 0)
        self.assertEqual(self.provider.cancel_calls, 0)
        self.assertEqual(self.saved.status, "blocked_configuration")

    def test_cancelled_saved_job_maps_to_cancelled_checkpoint(self):
        self.saved = saved_job(request_id=UUID("7c035cd0-51eb-4e66-9096-68e1fe57a303"), status="cancelled")
        self.assertEqual(self.new_pipeline().checkpoint.state, "cancelled")
        with self.assertRaises(ContractError):
            Pipeline(saved_job(status="queued"), self.checkpoints,
                     self.provider, self.importer, self.library)

    def test_cancel_after_queue_requires_positive_provider_confirmation(self):
        self.dispatch()
        self.assertEqual(self.pipeline.cancel().state, "cancel_pending")
        self.provider.unavailable = True
        self.assertEqual(self.new_pipeline().advance().state, "cancel_pending")
        self.provider.unavailable = False
        self.provider.cancel_confirmed = True
        self.assertEqual(self.pipeline.advance().state, "cancelled")
        self.assertEqual(self.provider.enqueue_calls, 1)

    def test_cancel_verified_but_unimported_work_requires_provider_confirmation(self):
        self.complete()
        self.pipeline.advance()
        self.assertEqual(self.pipeline.cancel().state, "cancel_pending")
        self.provider.cancel_confirmed = True
        self.assertEqual(self.pipeline.advance().state, "cancelled")
        self.assertEqual(self.importer.commit_calls, 0)

    def test_cancel_unknown_outcome_reconciles_job_and_never_claims_local_cancel(self):
        self.provider.lose_ack = True
        self.dispatch()
        self.provider.unavailable = True
        self.assertEqual(self.pipeline.cancel().state, "cancel_pending")
        self.provider.unavailable = False
        self.provider.cancel_confirmed = True
        self.assertEqual(self.pipeline.advance().state, "cancelled")
        self.assertEqual(self.provider.enqueue_calls, 1)

    def test_cancel_unknown_with_no_matching_job_remains_pending(self):
        def crash():
            raise SimulatedCrash
        self.provider.before_enqueue = crash
        with self.assertRaises(SimulatedCrash):
            self.dispatch()
        self.assertEqual(self.pipeline.cancel().state, "cancel_pending")
        self.assertEqual(self.pipeline.advance().state, "cancel_pending")
        self.assertEqual(self.provider.cancel_calls, 0)

    def test_cancel_rejected_queue_is_local(self):
        self.provider.reject = True
        self.dispatch()
        self.assertEqual(self.pipeline.cancel().state, "cancelled")
        self.assertEqual(self.provider.cancel_calls, 0)

    def test_cancel_after_import_or_availability_preserves_bytes_and_library(self):
        imported = self.reach_import()
        self.assertEqual(self.pipeline.cancel(), imported)
        self.library.indexed = True
        available = self.pipeline.advance()
        self.assertEqual(self.pipeline.cancel(), available)
        self.assertEqual(Path(imported.imported.path).read_bytes(), SYNTHETIC_BYTES)
        self.assertEqual(self.provider.cancel_calls, 0)

    def test_cancel_after_uncheckpointed_import_reconciles_without_deletion(self):
        staged = self.complete()
        self.pipeline.advance()
        self.pipeline.advance()
        self.importer.crash_after_commit = True
        with self.assertRaises(SimulatedCrash):
            self.pipeline.advance()
        Path(staged.path).unlink()
        result = self.new_pipeline().cancel()
        self.assertEqual((result.state, result.reason), ("importing", "awaiting_index"))
        self.assertEqual(Path(result.imported.path).read_bytes(), SYNTHETIC_BYTES)
        self.assertEqual(self.provider.cancel_calls, 0)

    def test_cancel_cannot_accept_wrong_provider_identity(self):
        self.provider.lose_ack = True
        self.dispatch()
        wrong = ProviderJob(replace(self.pipeline.snapshot, profile_id=99), "wrong-job")
        write_json(self.provider.path, asdict(wrong))
        result = self.pipeline.cancel()
        self.assertEqual((result.state, result.reason), ("cancel_pending", "cancellation_identity_mismatch"))
        self.assertEqual(self.provider.cancel_calls, 0)
