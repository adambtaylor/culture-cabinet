# Culture Cabinet fictional feedback candidate

P1.1-02 adds typed feedback, correction, eligible undo and explicit export to the
finite fictional catalogue. The local paper/ink UI preserves login, profile choice
and CSRF POST logout. Use fictional inputs only. Inactive slots remain separate.
No provider, discovery, chooser, ranking or external requests are included.
Deep-dive links remain future P1.2/P1.3 work in `backlog-deep-dive-links.md`.

## Feedback and access

Enjoyment is numeric 1–5 after experience. Anticipated interest, familiarity,
saved/skipped/opened actions, completion and constraints remain separate. Legacy
ratings retain their unknown basis and original `LOW → HIGH` scale labels.
Validation/retry pages retain the submitted command; stale forms link to a fresh
page. Every service rechecks current authority; hidden controls provide no access
boundary. Unauthorized or revoked profile contexts return generic denial.

Exports require an explicit CSRF POST and current export permission. No export
runs on page load, and no server-side export file or upload is created. Markdown
is for reading; JSON/CSV are structured copies for tools. The HTTP surface has no
import, authority, erasure, backup or restore endpoint.

## Contributor map

See [DEPENDENCIES.md](../DEPENDENCIES.md) for the prepared Python/venv runtime
and pinned dependencies. Start with these paths:

| Concern | Source |
| --- | --- |
| Supported entrypoint and command/path/environment boundary | `backend/manage.py`, `backend/cabinet/boundary.py` |
| Request routes, handlers and input validation | `backend/cabinet/urls.py`, `backend/shelf/views.py`, `backend/shelf/forms.py` |
| Feedback events and current projections | `backend/shelf/feedback.py`, `backend/shelf/models.py` |
| Portability codecs and import contracts | `backend/shelf/portability.py` |
Private project coordination documents are omitted from this development export.
| Actual HTML and rendering regressions | `templates/`, `backend/shelf/tests/test_ui.py` |
| Fictional actors, grants and storage fixtures | `backend/shelf/tests/synthetic_feedback.py`, `backend/shelf/tests/fixtures.py` |

## Supported first synthetic session

The repaired `demo_runserver --synthetic-demo` command owns the coordinator for
Django's checks, migration check, handler and dispatch lifetime. Startup is lazy:
construction, argument parsing and handle validation do not open the production
store or install a coordinator. `inner_run` validates the fixed bind before
acquiring the validated store and entering `synthetic_demo_lifecycle`.
No request-loop `using_recovery` glue is needed for this command.

The command binds only guest IPv4 `127.0.0.1:8000`, with reload and threading off.
There is no ingress, host forwarding, alternate bind or always-on service supplied
by this increment. The existing literal deployment-root guard requires
`${PRIVATE_LOCAL_PATH}`; it refuses this nested candidate directory.
The deployment-only sequence, after separate integration clearance, is:

```sh
# At the literal deployment root only, with a prepared empty fictional store:
${PRIVATE_LOCAL_PATH} -B backend/manage.py demo_migrate --synthetic-demo
${PRIVATE_LOCAL_PATH} -B backend/manage.py demo_bootstrap --synthetic-demo
${PRIVATE_LOCAL_PATH} -B backend/manage.py demo_runserver --synthetic-demo
```

These commands are documentation, not deployment authorization. No setup or server
was run for this repair. Empty-store bootstrap explicitly creates fictional
mutate/export grants; existing stores are neither reset nor silently elevated.
Existing grants default false. Bootstrap prompts for a disposable operator
password; there is no default password. Do not bypass the entry point, substitute
paths/settings or load private settings.

The database is `.demo-data/synthetic.sqlite3`; the fixed sibling `.demo-recovery`
journal is outside `.demo-data` and database backups. Lifecycle holds an independent
SQLite control connection, retained file identities and a lifetime lock while
Django request connections may close/reopen. It exclusively creates and syncs the
journal before committing the durable SQLite `application_id=0x43434631` first-use
marker. The marker is a provisioning guard, not evidence of current restrictions;
backup preserves it without adding events or changing semantic row counts.

Only one trusted initial fictional session is supported. Existing journals, used
or marked stores, restored used copies, restart and lost witness remain blocked.
Even an empty marked database cannot reprovision after journal deletion. There
is no restart, reset, promotion or lost-witness recovery shortcut. Rolling back
to a pristine pre-start database and removing all external evidence lies outside
the trusted provisioning boundary. Current recovery requires the same live
witness on the same host; an isolated reconciled restore cannot be promoted or
served. This supplies no personal-data recovery or always-on guarantee.

## Clear, source redaction and erasure

Ordinary exports use `cc.feedback.v1`. Exports containing non-erased source masks
use `cc.feedback.v2`, with Boolean `source_redacted` on every record and an exact
`source_redactions` count. JSON, CSV and the Markdown appendix agree on version;
attachment names are `culture-cabinet-fiction-v1.<format>` or
`culture-cabinet-fiction-v2.<format>`.

An explicit clear is a direct human null statement, not an erased or redacted
record. A source ban removes non-direct content and provenance, retaining complete
structural history: UUIDs, revisions and correction/reversal references. Human
corrections survive the ban; reversing a correction can reveal only the masked
predecessor's null value. Personal feedback erasure dominates redaction, closes
the affected scope and retires profile receipts without their payload hashes.
Fully erased-only exports remain v1. Erasure is logical; it does not establish
forensic deletion or recall downloaded copies.

Detail pages say `Not recorded.` at revision zero and `No current value.` for
revised nulls. Their context has no reason-for-null field. Markdown can distinguish
`Explicitly cleared` from `Source content redacted` using package flags; erased
statements are omitted from its human summary.

Both import paths check current local source policy before receipts, no-op
acknowledgements, preparation or writes, and repeat the check in the write phase.
A v2 mask requires an independently committed local ban; a package cannot install
one. Banned v1 non-direct content, including null/provenance rows, rejects as a
whole batch. Non-erased direct records on a banned namespace additionally need an
independently trusted human binding: the synthetic caller separately witnesses
approved fictional statements or authorized local direct mutations before export
or input parsing. Its manifest binds canonical records, including values,
attribution, time and references, and travels separately across hops. Each
destination revalidates its own ban and bindings; new human continuations require
new witnessed approvals. Incoming origin labels or digests cannot approve
packages. There is no hidden persisted provenance, HTTP approval issuer or
self-service arbitrary-package approval.

## Reproduce verification

Use the prepared Python/venv described in [DEPENDENCIES.md](../DEPENDENCIES.md);
no installation is needed. Run verification only from a clean source
checkout/copy away from the literal deployment root
`${PRIVATE_LOCAL_PATH}`, with no `.demo-data` present. Never run the
full suite in the deployed runtime tree: the existing verification-entrypoint
tests assume a source copy and exercise refusal of runtime CLI commands there.
Do not override ROOT, settings, paths or loaders to satisfy this contract.

Before running the suite, independently measure the scratch prerequisites for
F11: Linux, writable local btrfs storage, Python-linked and SQL-reported SQLite
versions at least 3.51.3, the SQL `sqlite_source_id()`, successful actual file,
directory and parent-directory fsync, and conflicting exclusive nonblocking
flock. Record these measurements before any WAL execution. This is the measured
Linux/btrfs/SQLite test context, not universal platform support. Missing
prerequisites or an F11 skip do not establish a pass or authorize skipping it.

Set `TMPDIR` to an existing caller-owned writable directory on that measured
local btrfs storage before starting Python. From the clean source copy, create
a fresh child for this invocation and run the whole supported suite:

```sh
test -d "${TMPDIR:?Set a caller-owned measured scratch parent}" && test -w "$TMPDIR" || exit 1
TMPDIR="$(mktemp -d "$TMPDIR/culture-cabinet-check.XXXXXXXX")" || exit 1
export TMPDIR
${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=2
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

The boundary accepts only the whole suite; test labels are unsupported. Normal
discovery includes the 100 existing backend tests and 21 tests in
`backend/shelf/tests/test_ui.py`. Require 121 passing tests, zero skips, successful
check/migration dry-run, and a `wal-execution.json` F11 PASS report in this
invocation's TMPDIR. Verification uses ephemeral in-memory fictional
actors/profiles/grants and caller-owned scratch storage, never a runtime store.
The UI tests render actual templates and submit their hidden commands; the
feedback cases use transaction tests without an outer atomic block. They retain
the existing 20 UI methods and their assertions and add a catalogue/detail
regression for undo-to-null and erasure. No separate UI runner is required.

Historical controlled evidence only: `${PRIVATE_RUNTIME_PATH}` records the
earlier 20/20 worker UI run; `${PRIVATE_RUNTIME_PATH}` records
H's earlier backend/lifecycle results. These artifacts and their worker scripts
are not checkout requirements or verification commands. The prior independent
functional PASS for the unchanged 49-file controls remains evidence for those
controls; it does not clear Q or the affected null-label functional recheck.
Private project coordination documents are omitted from this development export.
No schema/migration change is part of this repair; restoring a snapshot does not
reset the first-use marker or replace the live witness.

These are request/markup and backend checks, not browser, visual, screen-reader,
390px-device, large-text or phone acceptance. CSS provides responsive stacking,
wrapping, visible keyboard focus and comfortable controls for independent review.
No runtime store, credentials, household/personal data, network, server, model or
worker was used by M. No H1/H2, host-loss, real-data or full-P1 acceptance is
claimed. Independent affected functional and Q rechecks must use the exact
deliverable without worker files before integration; Q remains FAIL pending that
review. Controller canary postchecks and integration clearance remain pending.

Private project coordination documents are omitted from this development export.
are recorded in `../THIRD_PARTY_NOTICES.txt`. This repair selects no repository
license and grants no publication rights; unestablished source provenance remains
unknown.
