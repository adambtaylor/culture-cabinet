# Saved Obtain preview backend (P2.5)

The P3.2 [local handoff candidate](obtain-local-handoff.md) adds exact retrieval,
durable submission receipts, a separate explicit web submission review and local
handoff export. Its real production factory remains disabled unless an explicit server policy enables existing scoped records.
This page describes the preview/local-choice portion, which never enqueues.

The preview adds a search-only backend and local result ownership. Hearts remain
saved intent, separate from taste. Provider cancellation and an activation toggle
remain unavailable. A selected listing is not verified content, an available download, a
future download permission or evidence of full-work/edition correctness.

An authenticated actor with the currently selected authorized profile opens a
saved item's `obtain_preview_url`, then explicitly POSTs its fixed search URL
with CSRF. The server searches the saved title and scope, at most 20 results.
The runtime factory reads the existing approved Hydra private record, validates
its exact actor/profile, purpose, scope and existing expiry, closes it, and passes
that exact metadata to the reviewed Backend. Backend independently checks current
private authority for caps and search. There are no HTTP credentials, URLs or
provider overrides. Constructors and GET/save/confirm are inert. This candidate
was verified only with synthetic injected records/providers; no private store was
read and the existing private expiry was neither copied nor changed.

Hydra public source contract: 9.1.1 commit
`8ef3e6489aac8c8d776dca538127be38b2aff962`, supplied Java DTOs and transformer in
`.requirements/hydra-public-source/`. Parsing bounds JSON to 64 KiB and 20 rows,
rejects duplicate keys, conflicting result identities, malformed/error responses,
and inconsistent signed 64-bit GUID/id. Only whitelisted plain metadata survives.
URLs, raw responses, descriptions/HTML, private fields and credentials are never
stored or exposed. Title/scope checks screen listings; they do not auto-join content.
TV selection requires isolated S01E01 evidence and rejects packs/ranges/ambiguous
pilot claims. Audiobook category is 3030, never ebook. Unknown edition/full-work
claims stay explicit. Selection binds the exact provider GUID, normalized digest,
listing edition, saved snapshot, actor/profile and nonce/version in a signed token.

The compatibility parser returns `(rows, omitted_rows)`: a count of input rows
omitted solely for an unsafe, invalid or oversized title or display-only indexer
label. It validates the bounded envelope, GUID/id, size, category and other
structured fields first, including conflicts involving omitted rows. Exact
duplicates still collapse; conflicting identities reject the whole response.
Titles that survive validation retain their exact text; unsafe titles are never
truncated or rewritten into selectable listings. The 64 KiB response and 20-row
limits also cover omitted rows and indexer labels.

Both search callers record `partial` in the existing preview state when any rows
were omitted, including when none survive. The UI preserves this warning across
reloads and exact-listing review; it does not report an all-omitted response as
“No listings found”. The exact count is returned by the parser; only the presence
of omissions is persisted. No schema change is needed. Surviving rows still pass
the same ownership, expiry, digest, category, scope and exact-selection checks.
Missing album categories remain unverified and unselectable, even with FLAC in
the title. No acquisition scope or saved preference changes accompany this fix.

Previews expire after 15 minutes. Fresh choices require a surviving saved item and
current permission, including after provider work. Persisted request IDs and the
existing active identity constraints make same-choice confirmation repeatable
across service reconstruction; differing choices and stale tokens return 409.
Actor/profile isolation returns denial/404. Removing a heart retains old jobs and
selected-result history. Cancellation changes only local state. Separate submission
requires a fresh explicit user action and independent result/NZB verification.

The UI is integrated in `cabinet/saved_items.html` and its
`cabinet/obtain_preview.html` partial, using the routes in
`backend/shelf/obtain_views.py`. This candidate remains undeployed.
After deployment: save an item, open Obtain options, explicitly search, inspect
the exact listing and unknown claims, then confirm the local selection. The job
stays blocked pending independent operator setup and a fresh submission review;
repeated local-choice confirmation returns the same job, and cancellation changes
local state only. Rendered-form and backend tests
exercise this sequence with fictional data; no live or device acceptance is claimed.

Migration 0005 creates ObtainPreview, ObtainResult and ObtainSelection tables.
ObtainSelection has a unique job reference and a protected result reference; the
existing AcquisitionJob schema and all its constraints stay unchanged. Legacy
jobs have no binding row. Selection and job creation commit atomically. No data
migration or alteration of old migrations is included. This avoids making legacy
job writes depend on preview tables in partial schemas. All three new table names
must exist before preview actions proceed; old database startup stays inert.
The migration is applied only to the ephemeral test database. Rollback drops the
three new tables and loses preview/selection provenance while retaining legacy
jobs; back up before any separately authorized runtime schema change and roll
back code/schema together. No runtime migration, provisioning, service, browser,
deployment or credential access was performed.

Verification: focused parser unittest from backend/, normal whole suite through
`backend/manage.py test --verbosity=2` (no labels), then `check` and
`makemigrations --check --dry-run`. `${PRIVATE_RUNTIME_PATH}` records owned btrfs
scratch, SQLite versions/source ID, actual fsync and conflicting flock measured
before WAL execution. `${PRIVATE_RUNTIME_PATH}` records actual results and limits. The resumed verification
passed 6 focused parser tests and the full 582-test suite with zero skips (including
the unchanged two-connection saved-job race), plus Django check, migration dry-run
and the measured btrfs WAL gate. All original tests/fixtures and templates/CSS
match the supplied baseline manifest. The full suite ran once after the binding
repair; its 83.678-second log is `${PRIVATE_RUNTIME_PATH}`.
One worker only; no independent functional/maintainability review was commissioned
or claimed under the explicit no-spawning task constraint.
