# Offline acquisition pipeline

P3.2 adds a narrow Django dispatch checkpoint adapter and explicit local-file
receipt in [obtain-local-handoff.md](obtain-local-handoff.md). The standalone
full pipeline described below still has no production importer/Emby adapter.
Its verifier is shared with that candidate and now bounds reads and rejects
symlinks in every path component.

`backend/shelf/acquisition_pipeline.py` is an unconnected Python contract. It maps
the existing `AcquisitionJob` snapshot into separate checkpoints. The Django
model still permits only `blocked_configuration`/`cancelled`; no pipeline state
is written there. Saved items and taste feedback retain their existing meaning.
Actor/profile authorization remains at the existing service boundary. These
classes and adapter attestations are trusted backend inputs, not an API for
caller-supplied authority flags.

Identity includes actor, profile, request UUID, provider, medium, source ID,
edition, exact scope and storage acknowledgements. Mapping is limited to
movie/film → film, tv/series_s01e01 → episode/S01E01 and book/audiobook → audiobook.
`Evidence` separately binds a `ConfirmedSelection` to that entire snapshot.
Saved confirmation alone does not establish current capacity or duplicate checks.
Dispatch requires exact release/scope/rights authorization and those practical
checks. Edition/full-work flags are legacy receipt fields, not predownload
requirements or proof. Actual content/completeness remains a postdownload check.
The separate Running/PP0 hook emits transport evidence only and cannot satisfy
the stronger final-completion/import contract below.

## Recovery and adapter contracts

`dispatch(evidence)` persists `outcome_unknown` before enqueue. Repeated clicks
reuse that checkpoint. A definite `QueueRejected` permits explicit
`retry_rejected()` with the same identity. Lost acknowledgements, unavailable
lookup and crashes before/after the send require stable-request reconciliation
or manual resolution. An absent lookup result never permits re-enqueue.

Each `advance()` performs one checkpointed step:

| State | Required evidence / next step |
| --- | --- |
| blocked | Separate matching verification before dispatch |
| queued / downloading | Poll the exact provider job |
| verifying | Explicit completed status, successful repair/unpack and postprocessing; exact complete-work staging identity, regular nonpartial file, SHA-256 and size |
| importing, no receipt | Reconcile existing commit first; otherwise recheck staging and commit atomically |
| importing, receipt retained | Wait for an exact library item and media source |
| available | Immutable Emby item ID **and** media-source ID |
| failed | Provider/evidence failure; only definite queue rejection has a retry operation |
| cancel_pending | Reconcile uncertain dispatch/import; require confirmed provider cancellation |
| cancelled | Terminal; no enqueue |

A SAB hook invocation or exit alone proves nothing. The future sanitized adapter
must provide successful final status and repair/unpack/postprocessing evidence;
hooks also run on failures. The contract never reads a hook environment.
Full film/audiobook and exact S01E01 scope are adapter attestations, not guesses
from a title, filename, suffix or category. Byte checks do not validate codecs,
rights, actual media content, HDR or Atmos.

Checkpoint storage must atomically preserve complete records and serialize one
owner per request across processes. No production store is implemented. The
importer must publish atomically without overwriting a conflicting destination,
retain identity/digest receipts and reconcile a commit whose checkpoint was
lost, even after staging disappears. It must own stable staging/destination
paths; local byte checks alone do not solve concurrent filesystem mutation.
`AdapterUnavailable` leaves work pending. Unexpected exceptions propagate with
the prior durable intent intact; adapters must not expose secrets in exceptions.
No distributed exactly-once guarantee is claimed.

Import completion is distinct from library indexing. A scan/update acknowledgement
is insufficient. The returned item and exactly one selected media source must
match the snapshot, provider job, imported path, SHA-256 and size. The resulting
`EmbyReference` identifies that record; it proves neither playability nor display
capabilities. `PlaybackEndpoint` is interface-only for future Kodi-on-Mac or
Frame/Emby implementations; authenticated session/access checks and sends are
unimplemented.

Cancellation is local only before dispatch or after definite queue rejection.
Otherwise it remains pending until confirmed by the provider. A committed import,
including one discovered after a lost checkpoint, is retained and indexing may
continue. No cancellation deletes files or library records. The existing
`saved.cancel` is limited to its current blocked local jobs and is not a future
provider-cancellation implementation.

## Compact fixture walkthrough

Use the existing Python 3.14.8 environment from this candidate source copy. Set
`TMPDIR` to an absolute, caller-owned scratch directory with mode 0700 before
starting Python. From `backend/`, this example uses only synthetic bytes and
fixture checkpoints/fake adapters from the new test module:

```sh
${PRIVATE_LOCAL_PATH} -B - <<'PY'
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from shelf.acquisition_pipeline import Completion, Pipeline, StagedFile
from shelf.tests.test_acquisition_pipeline import (
    FixtureCheckpoints, FixtureProvider, FixtureImporter, FixtureLibrary,
    SYNTHETIC_BYTES, evidence_for, saved_job,
)

with TemporaryDirectory(prefix="offline-acquisition-") as folder:
    root = Path(folder)
    provider, importer = FixtureProvider(root), FixtureImporter(root)
    library = FixtureLibrary()
    pipeline = Pipeline(saved_job(), FixtureCheckpoints(root / "checkpoints"),
                        provider, importer, library)
    def show(checkpoint):
        print(checkpoint.state, checkpoint.reason, checkpoint.emby)
    show(pipeline.checkpoint)
    show(pipeline.dispatch(evidence_for(pipeline.snapshot)))  # synthetic attestations
    show(pipeline.advance())
    job = pipeline.checkpoint.job
    path = root / "staging.fixture"
    path.write_bytes(SYNTHETIC_BYTES)
    staged = StagedFile(job, str(path), sha256(SYNTHETIC_BYTES).hexdigest(),
                        len(SYNTHETIC_BYTES), True, False)
    provider.completion = Completion(job, "completed", True, True, 0, staged)
    for _ in range(4):
        show(pipeline.advance())
    library.indexed = True
    show(pipeline.advance())
PY
```

Expected progression: `blocked → queued → downloading → verifying → importing
→ importing(awaiting_index) → importing(awaiting_index) → available`.
The final reference is exactly
`EmbyReference(item_id='fixture-emby-item-7', media_source_id='fixture-media-source-9')`.
Those are fictional identifiers for a text fixture, not a real Emby record or
playable media. Temporary files disappear when the example exits.

## Contributor checks and limits

From `backend/`, focused standard-library tests:

```sh
${PRIVATE_LOCAL_PATH} -B -m unittest shelf.tests.test_acquisition_pipeline -v
```

Then from the source root, using the same private `TMPDIR`:

```sh
${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=2
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

Use the normal entrypoint and its unchanged guards; test labels are unsupported
there. Follow the existing engineering/scratch prerequisites in `docs/README.md`.
Tests cover durable fixture reloads in fresh Python processes, ambiguous enqueue,
completion failures, wrong identities, atomic synthetic import, commit crash,
delayed indexing, exact media-source matching and cancellation.

JSON checkpoint storage and the directory-publishing importer live only in the
test module. They demonstrate recovery under a single fixture owner; they are
not a second production persistence system. No dependencies, migrations, routes,
provider calls, credentials or product activation were added. Rollback removes
the three added source/test/documentation files; there is no runtime data
migration to reverse. Production persistence, real adapter validation, activation
and independent implementation/maintainability review remain later work. This
slice adds original implementation and hand-written synthetic fixtures; it makes
no license selection or publication-readiness claim.
