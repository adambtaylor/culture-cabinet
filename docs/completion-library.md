# Automatic postprocessing files and explicit albums

## Current integration (P3.19)

The bounded dispatcher now uses the exact-job history observer and version-two
movie, selected S01E01 and FLAC album consumers. See
[Automatic completion dispatch](automatic-kodi-dispatcher.md) for current operation,
per-medium receipts, scan reconciliation and live-evidence limits. P3.19 changes
source only: migration 0009 and the existing scoped integration are already
installed; no migration, credential grant or download is part of this increment.
The ordinary HTTP v1 handoff remains available for compatibility.

## Final completion timestamp contract (original staged handoff)

The following describes the original v2 export boundary. Statements about pending
activation refer to that earlier stage; current activation is described above.

The ordinary `completion_consumer.export` and its HTTP endpoint retain version 1
and null acquisition times, so the current Mac dispatcher remains compatible.
An independently authorized trusted history reader can call
`completion_history.export_completed(actor=..., profile_id=..., job_id=...,
context=..., history=...)` with one exact-job SAB history response. This function
does not obtain credentials, call SAB, install a poller, persist an observation or
grant history access. The existing add-only NZB key cannot supply this evidence.
At that stage, live history collection awaited separate authorization and integration;
the current dispatcher uses the subsequently authorized exact-job observer.

Version 2 has state `COMPLETED_FILES_VERIFIED`, `sab_history_completed: true`, and
each file receives the provider's integer-second `completed` timestamp with
basis `sab_history_completed_at`. It requires a freshly reverified owned hook
receipt, one exact nzo ID, exact registered output directory, the fixed completion
script, `Completed` status, no failure message, and a timestamp no earlier than
the hook (at SAB's second precision) and no later than the current file check.
The hook timestamp, export time, Mac import time and file mtime are never used as
acquisition time. Other provider fields, including credentials and private text,
are not exported. SAB's reported completion does not independently establish
repair, complete album identity or playable audio. Catalog eligibility remains
false until the Mac consumer verifies its own requirements.

`completion_evidence_sha256` binds the completion time, nzo/request/result/NZB
provenance and exact file manifest; it remains stable across export checks. A v2
consumer must track it alongside the immutable hook receipt hash, so an already
imported v1 receipt can gain timestamp evidence without recopying or rescanning.
It must reject conflicting completion evidence for the same binding. The original Mac consumer rejected v2. It has since been supplemented by the
reviewed bounded v2 consumers and dispatcher; the original restriction is no
longer the current integration status. Every v2 call requires separately obtained history and repeats
ownership and file verification. Synthetic tests are not live acquisition proof.

The SAB 5.1 history schema is documented at
https://sabnzbd.org/wiki/configuration/5.1/api#history-functions and was checked
against the installed SABnzbd 5.1.3 `sabnzbd/database.py` implementation.

Album NZB preflight now rejects positive unique segment sets that do not cover
1..N for each file, including missing first or interior segments. XML order need
not be sorted. This is a known-gap check only: a missing final tail and expired
articles cannot be inferred from NZB numbering. A passing descriptor still needs
the actual download, hook, copy and Kodi readback.

This isolated candidate consumes the existing v2 hook receipt on the owner's
next request-status, saved-list or acquired-files visit. No per-file attestation
is required. `/profiles/<profile_id>/acquired-files/` lists owned submitted jobs;
its per-job `handoff/` endpoint rechecks bytes before exporting JSON. There is no
background watcher or idle-page polling. A receipt arriving while a page is open
appears on refresh. Reading these pages never calls a provider or resubmits NZBs.

`PostprocessReceipt` and its `PostprocessFile` rows are separate from the stronger
legacy `ObtainLocalFile`/`LocalCompletion`. Transfer state remains `queued` while
the separate local state is `postprocess_verified`. The exported stage remains
`POSTPROCESS_FILE_VERIFIED`. Final SAB history, repair/CRC execution, full-work
identity and playback are unknown. Remote progress is null/unavailable under
add-only permissions. The legacy final-completion path is preserved.

The consumer uses only the configured private registry and exactly
`<receipts>/<owned request UUID>.json`. It rechecks application ownership, current
policy, acknowledged nzo ID, exact selected result/digest, reviewed NZB hash,
request directory and the full registry entry. Shared receipt construction checks
every receipt/handoff field; shared no-follow file verification checks declared
paths, SHA-256, sizes and the exact job tree. It never searches unrelated media.
The original hook observation must be after the durable transfer intent and not
in the future. The private hook/registrar/filesystem remain trusted, not signed.

Persistence is transactional, serialized by a transfer write lock with current
binding and cancellation checks. Duplicate/restart imports preserve file identity and original verified-at;
`checked_at` records the latest successful local check. A changed/missing receipt,
changed bytes, cancellation, mismatched binding or invalid file set marks previous
local evidence unavailable and suppresses its export. Historical rows remain.
A retry can recover after transient storage failure or restoration of identical
bytes. Unknown acknowledgements never register/import or resend. No automatic
resolution of uncertain submissions or failed hooks is added.

Each exported file has `sha256:<digest>` identity, path relative to Complete,
measured size, real nzo/request/result/NZB binding, verified-at and null acquired-at
with basis `unavailable`. Album authorization is a separate object from observed
files. No listening history, music likes, playback, Kodi controls or TV behavior
changes. Kodi may validate tags/codec/identity later; its strict music catalog
contract is explicitly **not satisfied** (`kodi_music_catalog_eligible: false`).

## Album entrypoint

`album_acquisition.prepare_album` is an explicit trusted operator/test entrypoint,
not a discovery/save/like action. It accepts an independently selected request:

```python
job = prepare_album(
    actor=actor, profile_id=profile_id,
    identity=selected_request['identity'],
    authorization=selected_request['authorization'],
    selected_guid=selected_request['hydra_guid'],
    selected_title=selected_request['listing_title'],
    backend=backend,  # fake in tests; existing scoped Backend after activation
)
```

`identity` has exactly `namespace`, `artist_id`, `release_id`, `artist`, `album`,
`track_count`, `source`. Namespace is `musicbrainz` (supplied canonical artist and
**release** UUIDs) or `licensed_catalogue` (supplied publisher/catalogue IDs and
source label). No ID is generated or guessed. `track_count` is the total for the
selected release, 1–128, from its supplied source; multiple discs may use relative
subdirectories. `authorization` has exactly a nonempty `rights_basis` and explicit
true `one_album`, `storage_checked`, `duplicates_checked`. It is stored separately
with the user's authorization time; that time is never an acquisition timestamp.

Existing discovery uses string MusicBrainz artist/recording identities; SavedItem
and acquisition snapshots require bounded integers. `AlbumRequest` supplies a
small dedicated local integer mapping to the canonical artist/release identity.
It does not insert music into SavedItem, convert UUIDs into numbers, or change old
saved identities. Its provider is explicitly `album_request`, medium `music`,
scope `album_flac`. Release uniqueness is enforced per actor/profile. A previous
request, including cancellation/uncertain dispatch, requires operator review;
this minimum entrypoint deliberately offers no recycling or retry-new-request path.

The entrypoint fetches actual caps and searches `t=search&cat=3040` only when text
`q` search and the lossless subcategory are advertised. Parent Audio/3000 alone
and audiobook/3030 do not qualify. `music-search` caps are understood, but no
unsupported artist/album advanced query is assumed. Exact Hydra GUID and listing
title must match the caller's selection, including the artist-plus-album prefix.
Pack/discography/protection/executable indicators fail closed. The listing,
supplied album identity and declared count are covered by the result digest;
authorization is separately signed into the ordinary submission review.

The returned job uses the existing `submission_factory`, `review_submission` and
`submit(confirmed=True)` flow (or its CSRF web review). Preparation alone never
enqueues. Existing size/capacity/duplicate checks, scoped grants, exact NZB hash,
durable dispatch, fixed SAB name/options and automatic registration apply.
No new credential scope or status/history/cancel endpoint exists.

Album output requires distinct `.flac` tracks, exactly the selected track count,
each beginning with the FLAC marker. P3.19 also permits bounded JPEG/PNG artwork
and opaque CUE evidence in the complete hashed manifest; unknown formats, logs,
archives and duplicate track bytes remain refused. CUE is hash-only and never
copied or interpreted. The Mac album consumer separately checks audio streams,
tags, numbering and durations, preserves embedded/conventional local artwork,
and verifies exact Kodi songs and album IDs. Catalogue identity remains selected
metadata rather than independent content recognition. Partial counts, missing
paths and identity changes fail closed.

## Historical activation handoff (already integrated before P3.19)

These original setup requirements are retained as historical contract context.
They are not instructions to rerun migrations, provision grants or fetch an album
for the P3.19 source-only upgrade.

1. Integrate the reviewed candidate with migration
   `0009_album_postprocess_files`. It adds three tables and widens only the
   acquisition-job scope constraint with the album case. It leaves all old rows,
   saved constraints and prior migrations intact. At the authorized deployment
   root, the existing stopped-runtime/verified-backup procedure is
   `backend/manage.py catalogue_prepare --provider-demo --plan`, followed by
   `catalogue_prepare --provider-demo` and `--check`, using the established venv.
   No runtime database was accessed here. Do not unapply after album submissions:
   the old constraint cannot represent them, and dropping receipts loses audit
   and duplicate protection. Code rollback must preserve these records.
2. Supply the existing server-only `OBTAIN_SUBMISSION_POLICY` in trusted settings,
   with **all** `SubmissionPolicy` fields explicitly set: `enabled`,
   `complete_root`, `max_bytes`, `reserve_bytes`, `test_only`, `registry_path`,
   `fixed_layout`, `script`, `pp`, `category`. Use the actually approved paths and
   size budget, `script='culture-cabinet-completion'`, `pp=2`, `category='*'`, and
   approve the exact `Complete/CC-<request UUID>` layout without sorting/renaming.
   Set `OBTAIN_COMPLETION_RECEIPTS` to the fixed approved absolute receipts
   directory. Neither setting can be supplied by HTTP or environment variables.
3. The user has approved per-job hook installation. The deployment owner must
   install that fixed launcher and the reviewed `tools/sab_completion.py`,
   `backend/shelf/completion_registry.py` **and**
   `backend/shelf/completion_files.py`, preserving their repository layout.
   The launcher invokes Python with fixed `--registry` and `--receipts` arguments
   matching Cabinet's settings. It is selected per job; no global/category default
   script change is needed. Both processes need trusted same-UID access, 0700
   control directories, 0600 control files, safe ancestors and local fsync/flock/
   hard-link support. Prepare these paths explicitly before enabling submission.
   No launcher, script_dir, service or live policy was installed here.
4. Use existing valid Hydra search/get and SAB add-only grants already bound to
   the authorized owner/profile. Retain 0007/0008/0009 state and registry across
   restart. Registry capacity remains 256 entries; no automatic pruning/reuse.
   Runtime settings remain disabled unless the deployment owner composes them.
5. Separately perform the requested **one licensed Queen FLAC album** acceptance
   after selecting its exact licensed release, supplied artist/release IDs,
   metadata source/count, exact listing and rights basis. The author did not
   search for or download Queen, access credentials/media, or perform a live test.
   An unrelated or previously observed download is not proof of this path.

## Verification and limits

The author used only the established venv and private synthetic scratch, with
measured Linux/btrfs, SQLite 3.54.0, fsync and conflicting flock prerequisites.
Exact commands/results and the final source digest are in `${PRIVATE_RUNTIME_PATH}`.
The final whole regression runs from a clean source copy, with no runtime store.
Tests cover real factory/encoder → registry → hook → consumer, duplicates,
transaction failure/retry, database close/reopen, cancellations during verification,
foreign ownership, unknown acknowledgements, changed files, two-track album,
partial/count/identity/marker mismatch, actual caps and retained film/pilot/book
constraints. All file contents and provider replies are synthetic; FLAC markers
are not playable music and establish no live acceptance.

Reverification is synchronous and bounded by the existing 64 GiB/128-file job
limits (20 jobs per library page); large files can delay a page. Files must be
quiescent. This is point-in-time verification, not an immutable snapshot or a
promise about later bytes. SAB can still fail after its hook; add-only access
cannot observe that final result. A failed/missing hook has no valid receipt;
local registration repair does not rerun it. No independent maintainability PASS
is claimed; the requested separate review was not commissioned under the explicit
no-child-workers constraint. No dependencies, licence selection or deployment.
