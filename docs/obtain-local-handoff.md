# Candidate Obtain submission and local handoff

The additive automatic hook consumer, acquired-files view and explicit album
request mapping are described in [completion-library.md](completion-library.md).
They preserve the stronger legacy handoff documented below. In this candidate,
known hook evidence can appear automatically as a separate postprocessing state.

This candidate adds durable submission receipts, exact Hydra retrieval and a small
web bridge from search through local selection, fresh submission review, explicit
CSRF confirmation, persisted status and a local handoff export for Kodi. Runtime
submission has a real composition path, disabled by default by server policy. No deployed tree, runtime database,
private key, real media file or provider was accessed by this continuation.

The existing preview parser and confirmation retain their meaning. A saved heart,
listing match, or legacy storage acknowledgement never authorizes submission.
`review_submission` requires a selected Obtain result, current actor/profile
permission, fresh preview and trusted server composition. The factory validates
existing scoped records, exact release/scope/rights selection, bounded size,
current free storage and known Cabinet duplicates. These are selection checks;
undownloaded edition/content/full-work verification remains false. HTTP fields
cannot enable operator policy, change endpoints or construct credential grants.

The signed submission review lasts five minutes. It includes the entire saved
snapshot, exact result UUID and Hydra GUID, normalized listing digest, listing
size, approved maximum bytes, NZB SHA-256, selection attestations and preview
nonce/version. It also signs the request-UUID SAB name, exact job root, private
registry path and approved fixed per-job options. `submit` additionally requires a fresh explicit human confirmation
of rights, selection, scope, size and external-library/download duplicate review. The live factory retains the exact reviewed descriptor privately for five minutes
and requires its reviewed NZB hash at confirmation. It does not fetch a different
descriptor during confirmation; see `completion-kodi-loop.md` for the dynamic
article-ID regression and retention limits. The web bridge shows the exact listing,
edition, scope, reported encoded size, approved local size ceiling, destination,
rights choices and reviewed hash. Preparing a submission review is a CSRF POST
because it retrieves the NZB; the separate confirmation POST is the only enqueue
route. GET, hearts, search and local-choice confirmation never enqueue. Authority,
ownership, preview expiry and binding are checked again after retrieval and before
dispatch. These routes are shared by the candidate URL configurations; the saved
search flow is in the existing provider URL configuration.

## Retrieval evidence boundary

`docs/obtain-preview.md` identifies Hydra 9.1.1 commit
`8ef3e6489aac8c8d776dca538127be38b2aff962` and its JSON transformer/DTO contract:
the search `id` and `guid` are the same canonical signed 64-bit result identity.
The parent supplied the missing observed contract: GET on fixed loopback port
5076, `/api?t=get&id=<canonical signed 64-bit GUID>&apikey=<private key>` returned
HTTP 200, `application/x-nzb`, 8,624 bytes for `-3173234321348194361`.
The supplied official reference is https://newznab.readthedocs.io/en/latest/misc/api.html
(GET/get with id). This author did not fetch it or repeat the provider request.
The evidence and its attribution are recorded in
`.requirements/obtain-contract-evidence.md`; the previously referenced public
source directory remains absent, and no source contents are invented.

`Retriever(backend)` now uses Backend's existing private store/grant checks and
LoopbackTransport. The key is privately URL-encoded; no provider URL is accepted.
GET responses require HTTP 200 and `application/x-nzb`, no compression or redirect,
at most 1 MiB, a 2-second connect timeout and a 10-second absolute request deadline.
The existing 64 KiB caps/search bound is unchanged. Errors and exceptions are
sanitized, with no logging or retries. Other MIME/encoding variants are deliberately
unsupported until evidenced; no success semantics for them are guessed.

NZB validation accepts exactly `http://www.newzbin.com/DTD/2003/nzb` and
`https://www.newzbin.com/DTD/2003/nzb` when a namespace is supplied; unrelated
namespaces remain rejected. Existing unnamespaced input support is unchanged.
Validation uses Expat without external entity loading. A conventional external
DOCTYPE is allowed but never resolved. Internal subsets and entity declarations
are refused, as are malformed XML, unsupported structure, duplicate segment
numbers, empty segment sets, missing or invalid segment `number`/`bytes` fields,
oversized message IDs, more than 20,000 elements or
10,000 segments, and depth above eight. Message IDs remain opaque and private;
they are never exported or persisted. `RetrievedNZB` binds the original bounded
bytes to the complete exact result; the signed review binds their SHA-256.

The namespace compatibility fix alone does not check segment-number continuity,
retention or download completeness. The recovery also adds the album-only
known-gap check described in [completion-library.md](completion-library.md);
retention and an absent final tail remain unknown. The supplied live observations report that
recent Queen II FLAC listings lack categories and their NZBs have serious gaps
in segment numbers; no complete album has been downloaded. These observations
were not rechecked against live services. Missing categories remain unselectable,
and album protection/executable/pack exclusions are unchanged. Compatibility
regressions use fabricated NZB text and injected HTTP replies, with no media
retrieval. Exact author changes, tests and source hashes are recorded in
`${PRIVATE_RUNTIME_PATH}` and `${PRIVATE_EVIDENCE_PATH}`, with the source delta
in `${PRIVATE_EVIDENCE_PATH}`; independent
affected review and any live deployment are separate, pending work.

SAB 5.1.3 canonical UUID job identifiers and legacy `SABnzbd_nzo_*` identifiers
are accepted only in one consistent `status: true` / single `nzo_ids` receipt.
Malformed, multiple or inconsistent receipts remain unknown without retry. The
parent's successful licensed transfer had a lost raw receipt; its actual job ID
is unknown and is not reconstructed or replaced by the synthetic test UUID.

## Explicit operator policy; no activation performed

`obtain_runtime.submission_factory` constructs the real Backend and Retriever.
It loads only existing Hydra and SAB records through the private store, checking
provider purpose, exact scopes, actor/profile binding and original expiry. It
never provisions, renews or expands a grant. Backend independently rereads and
checks the current record before each operation; secret buffers are closed/wiped.
Keys never enter Django settings, forms, durable bindings or logs. Fixed endpoints
remain `http://127.0.0.1:5076/api` and `http://127.0.0.1:8080/api`.

Only a separately approved operator edit of server settings can define
`OBTAIN_SUBMISSION_POLICY`, an exact dictionary with these nonsecret fields:

| Field | Contract |
| --- | --- |
| `enabled` | Literal Boolean true; absent/false fails before storage/credential reads |
| `complete_root` | Canonical absolute controlled Complete directory, no symlink components |
| `max_bytes` | Positive integer maximum per selection/output, at most 64 GiB |
| `reserve_bytes` | Nonnegative integer free-space reserve, at most 64 GiB |
| `test_only` | Explicit Boolean propagated to the handoff |
| `registry_path` | Canonical absolute private registry file outside Complete; existing parent mode 0700 |
| `fixed_layout` | Literal true only after approval of the exact Complete/name layout with no sorting or rename |
| `script` | Exactly `culture-cabinet-completion`, the reviewed fixed launcher basename |
| `pp` | Exactly integer 2 (SAB repair/unpack option) |
| `category` | Exactly `*`; the instance's effective default category must obey the approved fixed layout |

The repository defines no active policy. This setting is ordinary server data,
requires no settings-time model imports and is never read from an environment
flag or request. Capacity must cover the entire maximum plus reserve; it is a
current check, not a disk reservation across concurrent jobs. Operational sizing
must allow SAB's intermediate/unpack needs. Policy is rechecked before dispatch;
changed root, maximum, test-only status, registry or job options invalidate an earlier signed review.
The authenticated owner chooses releases and acknowledges rights/duplicates in
the normal CSRF forms; no operator-per-file approval or fabricated preverification
is required. Legacy verification fields remain readable but cannot authorize
postdownload import; the existing genuine completion checks are retained.

The real factory selects the fixed script/options through the existing Backend,
Client, PublicRequest and multipart encoder. Each confirmed request sends
`nzbname=CC-<canonical Cabinet request UUID>`, `script=culture-cabinet-completion`,
`pp=2`, and `cat=*`. The upload still carries the exact reviewed NZB bytes. A release
title never supplies a directory name. Different requests have different names;
existing GUID/NZB and active-selection duplicate constraints still apply.

An approved deployment must guarantee that these options produce exactly
`<complete_root>/CC-<request UUID>`, with no category subdirectory, sorting,
job-name/folder renaming, or other relocation. This is an explicit prerequisite,
not something the add-only response proves. Before sending, the expected job path
must be absent (including directories, files and symlinks). The hook rejects any
actual directory differing from the recorded path. The earlier licensed test sent
`nzbname=CC-BigBuckBunny-licensed-test-20261028`, `pp=2`, `cat=*`, `script=None` and
observed that exact directory under Complete. It supports only that instance's
observed naming for that test; it does not establish arbitrary sorting settings
or prove this new hook workflow. No service setting is inferred or changed here.

The factory and durable transfer now supply the automatic registrar; no caller
supplies `job_root`, per-download registration or pre-known extracted hashes.
After one valid UUID or legacy acknowledgement is committed, submission builds
and atomically publishes the exact nzo/request/result/digest/NZB/scope/root entry.
A local failure leaves `queued` with `registration_pending`. Status rendering (or
an identical confirmed repeat) retries only publication from durable state, even
after restart. This recovery needs current application authority and the same
enabled operator policy, but no provider credentials, free-space reservation or
provider calls. Old transfers without this intent are not retroactively inferred.

Separately approved deployment must prepare the existing schema through 0008,
approve the fixed layout and policy, install the exact fixed launcher and shared
registry module, and prepare the private registry/receipt directories described in
[sab-completion-hook.md](sab-completion-hook.md). The launcher passes fixed registry
and receipt paths, never values derived from a request or SAB title. No settings,
services, script_dir, credentials or runtime database were changed here. Kodi
catalog ingestion and authoritative timestamp agreement remain blocked.

## Durable ownership and uncertainty

`ObtainTransfer` belongs one-to-one to the existing acquisition job. Its binding
contains the exact completion intent before sending, and no NZB bytes, credential or provider URL. Unique Hydra GUID and NZB hash
claims prevent another job/profile from submitting the same known result or
payload. Existing saved active-identity constraints remain intact. Content-level
duplicates across different results/NZBs still require the user’s external-library
check; neither titles nor hashes of different NZBs establish that.

Submission serializes the initial claim with `saved.cancel` by writing the job
inside a durable Django transaction. `DjangoCheckpoints` maps the existing
pipeline's dispatch checkpoint to ORM columns. A compare-and-swap revision rejects
stale owners. `Pipeline.dispatch` commits `outcome_unknown` before the only remote
mutation: the existing Backend/contract/transport's SAB `addfile`. The backend
retains its separate current credential/grant checks. No transaction spans the
send, and caller-owned outer transactions are refused before dispatch.

Lost replies, unexpected exceptions after sending, malformed acknowledgements and
failed acknowledgement checkpoint writes retain uncertainty. Repeated submissions
return the persisted receipt without retrieval or enqueue; a known queued
acknowledgement may retry its local registry publication. A concurrent SQLite
writer may receive a lock/conflict error and should reload status. No expiry,
lease takeover, automatic retry, queue lookup, history, admin or cancellation
operation is added. The SAB add-only NZB key cannot supply those operations.

A crash after claiming a receipt but before dispatch can leave `blocked`; it does
not auto-resume. A definite rejection becomes `failed`, also without a retry hook
in this candidate. Retained GUID/hash claims are deliberately conservative even
after local cancellation. Manual reconciliation/release needs separate design;
never delete a receipt merely to retry an uncertain remote operation.

Cancellation before dispatch or after definite rejection is local and final.
After a possible send it records local cancellation with remote cancellation
unconfirmed, keeps the active identity and refuses further handoff. Unknown job
IDs remain unknown. Cancellation neither contacts SAB nor deletes any file or
receipt. Already verified local receipts survive cancellation.

## Known-path handoff

`handoff` accepts only a trusted `LocalCompletion` for the exact known SAB job and
saved snapshot, result digest and reviewed NZB hash. This stronger legacy adapter contract must explicitly
attest final success, repair, unpack and postprocessing success, exact complete
work, expected absolute path, size and SHA-256. Hook invocation/exit, a filename,
a directory's existence or a successful addfile response is insufficient. No
hook environment, submitted path, directory search or remote progress is consumed
automatically. The SAB Running/PP0 hook instead emits the separate
`POSTPROCESS_FILE_VERIFIED` transport receipt, with unknown identity/completeness
and null acquired_at. It cannot satisfy this stronger legacy adapter by setting
verified flags true. Unknown submissions cannot be resolved by supplying a local file.

The existing pipeline verifier now opens every path component without following
symlinks, checks a regular file and rejects partial flags/names. It streams at
most the attested length plus one byte, bounded by the confirmed maximum and
128 GiB hard ceiling, and checks SHA-256, size, modification metadata and reopened
path identity. It also rejects directories, FIFOs, path aliases and parent
traversal. Only that known path is read; unrelated files are never enumerated.

`ObtainLocalFile` is an atomic in-place receipt with unique path and content hash.
The state changes with the receipt in one database transaction, so a failed write
rolls back and a retry re-verifies. Repeating the same handoff is idempotent;
conflicting or duplicate files are refused. The file is neither copied nor moved.
The trusted operator must supply a stable completed path. Verification is at a
point in time: this is not immutable media storage or a filesystem lock against
later modification, and future consumers must reverify before use.

`local_verified` means local byte verification. The owner-only `local-handoff/`
GET exports v2: exact path, SHA-256, size, verification time, stable content-hash
identity (`sha256:<digest>`), separate catalogue identity, known nzo_id and exact
request/result/NZB provenance. `relative_path` is under the reviewed Complete
root; old receipts without that root emit null rather than guess. The selected
media kind has basis `selected_scope`; observed media is unknown. `test_only`
comes from the signed server policy (legacy receipts default true). `acquired_at` is the trusted completion's supplied
timestamp only with an explicit source and basis `sab_history_completed_at` or
`authoritative_acquisition_record`, or null with basis `unavailable`; old receipts are not backfilled with invented
times. Kodi music-catalog admission stays false pending timestamp/consumer agreement. Optional bounded artist/track/album metadata requires a supplied provenance
label. Track/artist IDs are emitted only if supplied, and the catalogue identity
does not pretend to be a recording/track identity. A separately trusted
`playback_verified` attestation may mark the file `verified_playable`; byte checks
alone leave playback unverified. A repeated identical handoff re-verifies bytes
and updates verified-at while preserving acquired-at and identity.

The export does not read a file or start playback. Kodi owns controls and must
reverify current bytes before use. Indexing and actual playback remain separate,
unestablished stages. No Emby IDs, listening history, HDR/Atmos assertion or actual
media completion is fabricated. Unknown enqueue receipts still cannot acquire a
known-job handoff merely because a file exists, including the parent's Bunny file.

## Migration, verification and limits

0007 creates two new tables and constraints; 0008 adds optional acquisition time,
trusted metadata and a playback-verification flag to the local receipt. Neither alters old saved tables,
permissions, old migrations or deployment activation. `saved` reads optional
transfer state only when its table exists and preserves old-schema behavior.
These integration repairs add no model or migration changes. There is no data migration. Rolling code back must preserve transfer/local-file
records and active identities if any send might have occurred. Unapplying 0007
drops ownership/audit receipts and must not be used as a retry or cancellation
mechanism. No runtime database was migrated during this work.

Use the prepared `${PRIVATE_LOCAL_PATH} -B` runtime;
install nothing. Follow the measured Linux/btrfs/SQLite/fsync/flock prerequisites
in `docs/README.md` before WAL tests, using a private owned TMPDIR within this
candidate. The exact measurements, focused harness/logs and results are under
`${PRIVATE_RUNTIME_PATH}`. Focused Django tests use the normal boundary preparation and
synthetic test runner; full verification uses only the supported entrypoint:

```sh
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=2
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B backend/manage.py check
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

Tests use synthetic users, grants, fake keys/transports, text bytes and temporary
SQLite files only. Coverage includes two real SQLite connections competing for
one dispatch, durable intent visible before send, a fresh interpreter reading
receipts, tampering/ownership/revocation, lost replies/write failures, local
cancellation, failed postprocessing, partials/symlinks/replaced paths, duplicate
handoff, and isolated S01E01 rejection of packs/ranges/multiple episodes.
These tests establish fixture behavior only. No new actual retrieval, real licensed
media or service completion was attempted. The parent's prior live evidence is
attributed separately. Independent functional/maintainability review remains
pending; no child reviewer was started under the explicit sole-author rule.
Integration logs and exact final results are in `${PRIVATE_RUNTIME_PATH}`; original input handoffs are preserved.
Original implementation and hand-written fixtures add no dependency, borrowed
source, license selection or publication claim.

## F3 repair verification

The author repair evidence is in `${PRIVATE_RUNTIME_PATH}`; the independent
review and probe in `${PRIVATE_RUNTIME_PATH}` remain unchanged. Tests exercise the real
factory/encoder with fake existing records and provider replies, automatic
registration, UUID/legacy IDs, distinct names, early-hook coordination, atomic
publication faults/restart/status recovery, collisions, and unknown replies with
no resend. Existing F1/F2 and background regressions are retained. A fresh whole
suite and clean-source verification use the existing runtime and newly measured
Linux/btrfs/SQLite/fsync/flock scratch prerequisites. No independent PASS or live
automation completion is claimed.
