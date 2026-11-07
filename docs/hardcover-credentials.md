# Hardcover credentials: contributor and operator guide

This slice implements private entry and local storage only. Credentials remain
**UNVERIFIED, source off**. Entry does not verify remote account, scopes or expiry,
activate a source, or establish a working live API.

The frozen [V gate](../.requirements/C-repair-V-gate.json) accepts the repaired
entry/storage slice. Its [independent report](../.requirements/C-repair-V.json)
records 235 passing suite tests, zero skips, and 103 independent API, bootstrap,
PTY and process-death cases. Actual site controls and human entry remain pending.
Under O's current ordering, this documentation follows stable V and precedes
separate Astra Q, so Q can inspect documentation and immutable code together.
That sequencing changes no acceptance or deployment gate.

## Responsibilities and boundaries

[credential_entry.py](../backend/shelf/credential_entry.py) owns the standalone
bootstrap, foreground terminal checks, hidden prompts and fixed receipts.
[credential_store.py](../backend/shelf/credential_store.py) owns validation,
exclusive locking, record transactions and explicit reconciliation. Both are
original standard-library glue; neither depends on Django startup. Internal
fixture constructors are test interfaces, never production path options.

Future broker, worker isolation, release tickets, activation, revision/invalidation
bridge, runtime launch/stop guards, provider transport and provider acceptance
require separate implementation and independent gates. Every future secret
reader/sender must retain the approved runtime guard through exit/reaping.
Current entry/storage acceptance supplies none of those facilities. Live,
public-release, new-runtime and Phase 2 gates remain separate.

The approved backend-only key grant is exactly `read:catalog:search`,
`read:catalog:data`, `read:library`, `write:library`, for at most 90 days.
The broader risk of `write:library` is already approved; intended application
writes remain adding/updating genuine human status or ratings, with new entries
Private and existing privacy preserved. Deletion, journal and import are outside
that permission.

## Before any human key creation

O must freeze the reviewed code/runtime hashes and read-only worker/reviewer
boundaries. Actual owner, ancestors, modes, ACLs, local filesystem, mount
transitions and provisioning need inspection. Exclude the credential directory
from source, exports, ordinary backups and all worker/reviewer mounts; demonstrate
actual denial independently of permissions. Prove stopped deployment, no
overlapping launch, release disabled and the installed writer lock. V/Q must
inspect actual site controls and the private human route, including audit,
tracing, debugging, dump and session-recording exclusions.

Only after O site clearance does the human privately verify the actual account,
four scopes and expiry, create the actual key and type it in their own unrecorded
private TTY. Never route it through chat, tools, browser automation, arguments,
environment, source, logs or a clipboard workflow. In that shell session set
`ulimit -c 0` before entry; this is a session core limit, not a global setting or
proof that other recording channels are excluded.

## Fixed deployment interface

These are post-clearance commands, not instructions to execute during review:

```sh
${PRIVATE_LOCAL_PATH} -I -S -B ${PRIVATE_LOCAL_PATH} enter
${PRIVATE_LOCAL_PATH} -I -S -B ${PRIVATE_LOCAL_PATH} replace
${PRIVATE_LOCAL_PATH} -I -S -B ${PRIVATE_LOCAL_PATH} remove
${PRIVATE_LOCAL_PATH} -I -S -B ${PRIVATE_LOCAL_PATH} reconcile
```

Choose one verb. No additional arguments, environment input or store override
exists. UID and effective UID must be 1000; GID and effective GID must be 1000.
All three standard descriptors must name the same foreground controlling TTY
in the current session. Pipes, redirected output and background entry refuse.

The fixed store is
`${PRIVATE_LOCAL_PATH}`.
Its leaf and private parent require 0700, owner 1000/GID 1000. Files require
0600, single-link regular files without ACLs; ancestors and same-device/mount
identities are checked without following symlinks. The persistent, empty
`control.lock` takes an exclusive nonblocking lock and is never replaced,
unlinked or truncated. The CLI creates no directories and repairs no permissions.
Recognized artifacts are `credential.json` (at most 8192 bytes),
`transaction.pending` (at most 128 bytes) and at most one
`.entry-<32 lowercase hex>.tmp` (at most 8192 bytes).

The minimal standalone bootstrap requires the exact absolute interpreter/script
and `-I -S -B`. It checks both sibling code files as UID1000/GID1000, exact 0444,
single-link regular files without ACLs, checks identities around bounded reads,
revalidates retained ancestry and compiles the checked storage bytes directly.
O supplies immutable deployment boundaries; these checks do not defeat a
malicious same-UID owner. No mandatory root provisioner, new dependency or general
vault is introduced.

All entered values are hidden, including account ID, actor/profile IDs, both UTC
expiry dates, token, intent and confirmation. The four fixed scopes are disclosed,
not editable prompts. Dates use `YYYY-MM-DDTHH:MM:SSZ`; local expiry must be future,
within 90 days of local commit and no later than declared provider expiry.
The token accepts 1–4096 ASCII bytes in the supported token alphabet. One absolute
120-second deadline covers metadata and confirmation as well as token input;
filesystem syscall latency is not bounded. Overflow is drained hidden to a line
delimiter or deadline and refused, never accepted as a truncated token.
Catchable exits restore saved terminal flags; SIGKILL cannot restore them.

## Operations and recovery

- Clean `enter` requires no record; hidden `STORE` confirms storage.
- Clean `replace` requires the existing record and matching binding; re-enter
  metadata and token, then `STORE`. Expired records can be replaced.
- Clean `remove` requires matching account/actor/profile and `REMOVE`; it needs
  no old token and permits expired records.
- `reconcile` with hidden `present` requires re-entry of the matching binding,
  expiry pair and token, then `RECONCILE`. It validates the existing state rather
  than adopting a temporary record. Hidden `absent` plus `RECONCILE` only confirms
  an already absent record. Both retain strict artifact/contradiction checks.
- Hidden `discard` displays its fixed local-retirement purpose and revocation/
  erasure limits, then requires exact `DISCARD`. It safely retires only the fixed
  record and recognized local transaction artifacts without reading the old token
  or record/temp contents. Recognized bounded incomplete markers/temps are
  supported; unknown or unsafe artifacts refuse. The blocker is synced before
  deletion, removal before marker cleanup. Explicit repeated discard can recover
  interrupted discard; after confirmed clean absence, fresh entry is possible.

Discard is still operation `reconcile`. Removal/discard neither revoke the key
at Hardcover nor erase copies. A lost key needs separate human provider revocation.

Receipts contain only fixed operation/outcome/reason words:
`credential-entry <operation> <outcome> <reason>`. Successful `stored`, `removed`
or `reconciled` with `complete` exits 0; `refused` exits 2. `commit_unknown` means
the data-directory durability barrier was not confirmed; `durable_cleanup_unknown`
means that barrier completed but cleanup is uncertain. Both exit 3; lost receipt
output also exits 3. A missing marker can follow cleanup uncertainty.
Receipt reasons are fixed: `complete`, `invalid_input`, `unsafe_store`,
`dirty_store`, `conflict`, `busy`, `expired`, `io_failure`, `interrupted`.
Preflight refusals use `credential-entry refused invalid_command` or
`credential-entry refused deployment`, exit 2.

After refusal or uncertainty, stop and diagnose with the owner; never blindly
retry, extract the token or use manual `rm`. Use reviewed explicit reconciliation
only after diagnosis. Unsupported storage, unsafe metadata or unknown artifacts
need owner diagnosis, not automatic repair.

Storage is plaintext. Snapshots, backups, Python/kernel copies, root and malicious
same-UID actors remain outside protection claims. Best-effort buffer clearing is
not zeroization; unlink is not erasure. Tested SIGKILL/restart outcomes do not
establish power-loss persistence or absolute durability.

## Contributor verification and rollback

Use the prepared Python 3.14.8 runtime in [dependencies](../DEPENDENCIES.md).
Follow [README verification setup](README.md#reproduce-verification): a clean
source copy away from the literal deployment root, no `.demo-data`, and a fresh
owned private TMPDIR. Before any WAL/F11 execution, record actual Linux/local
writable btrfs, same-device/mount identity, linked and SQL SQLite >=3.51.3,
`sqlite_source_id()`, file/directory/parent fsync and conflicting exclusive
nonblocking flock measurements. Then run the supported whole suite:

```sh
${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=2
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

Require current 235 tests, zero skips, successful checks and this invocation's
`wal-execution.json` F11 PASS. README's 121 count is historical. Credentials use
owned NONcredential files and disposable PTYs. Bootstrap tests hard-fail the
production opener; real UID1000/GID1000/0444 code is exercised with redirected
fixture locations and modeled root/home ancestry. That does not attest actual
deployment ancestry. Independent V reports F11 PASS and 103 additional cases;
worker scripts are evidence, not required project entrypoints. This docs-only
handoff ran no application tests.

Rollback restores the prior 58-file source baseline, keeps source off and release
disabled, and leaves the current store untouched. Human revocation is separate.
There is no schema or data migration and no automatic credential downgrade.

Private project coordination documents are omitted from this development export.
The project licence remains undecided; no publication authority is granted.
Retain [platform notices](../THIRD_PARTY_NOTICES.txt) and dependency notices;
standard-library glue does not establish an OSS licence for the application.
