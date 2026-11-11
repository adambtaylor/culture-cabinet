# Saved API keys on Omarchy

Culture Cabinet uses the existing GNOME Secret Service, through a fixed system
Python helper. API credentials and clean-restart anchors are authenticated Fernet
records inside one explicitly provisioned application collection. The Fernet key
is a separate secret in that same collection and never leaves the helper. This
does not create a second filesystem vault or install another service.

The helper is `tools/cabinet_secret_service.py`. Its codec/client can be imported
by the app's Python environment without GI or cryptography dependencies. Its child
runs `/usr/bin/python3 -I -B` and uses the already installed GI/libsecret and pyca
cryptography packages. The native app remains responsible for current owner,
profile, connection, generation, expiry, consent and provider authority checks.
Saving a key performs no provider request and does not validate that key.

## One explicit collection, with observed protection

Host-only provisioning requests a fresh unique label: `CultureCabinet` followed
by a UUID's 32 lowercase hex characters. A label is not a collection identifier.
The helper calls standard D-Bus `CreateCollection` on its already verified
same-UID connection, with only that Label property and an empty alias. It saves
the actual returned collection reference, or the exact returned prompt reference
before invoking Prompt once. Completed is subscribed on the same connection and
main context, filtered to the unique service sender and exact prompt path. Only
a nondismissed object-path result qualifies. That path is durably retained even
if the method acknowledgement is subsequently lost, before any Collection proxy
or metadata/file check. An unavailable GI convenience binding is not required.

The actual returned collection component is decoded with GNOME's `_hh` escaping
and must canonically re-encode to the identical path. Its bounded ASCII filename
leaf must be safe and nonreserved. The exact object's Label must match the fresh
nonce; ownership comes from the retained creation receipt, not Label alone.
Only the decoded identifier plus `.keyring` in the registered directory becomes
a candidate. No collection scan, default/session alias, lowercasing, suffix
probe, adoption or guessed filename is permitted. A returned suffix is accepted
only as part of the actual returned canonical identifier.

GNOME alone displays the collection-password prompt. Cabinet never accepts that
password in a form, pipe, argument, environment variable or configuration. Normal
app operations cannot provision or unlock a collection. `provision()` is a trusted
terminal-only callable; it refuses non-terminal callers, uses a cancellable GNOME
prompt and a bounded private result pipe, and returns only status and a nonsecret
collection reference. It does not print passwords, keys or protocol records.
For an already registered ACTIVE selection, the same explicit terminal action
may request GNOME unlock of that exact collection. It never adopts an unrelated
collection, resumes uncertain provisioning or regenerates a missing master key.

The supported backend is GNOME Keyring 51.1. Before generating a Fernet master
key, and before every secret use or AVAILABLE result, the helper verifies current
service ownership/installed implementation and reads at most 20 public format
bytes from offset zero of **only the registered file**. It checks owner, mode,
link count, no-follow path and stable metadata around that read. GNOME's supported
encrypted binary prefix is required; empty-password textual, short, foreign or
changed files are unavailable. This follows the pinned GNOME 51.1 writer, not a
permanent `protected=true` setting. Before generating a master, setup observes
metadata of only the returned candidate, then writes one unique **public,
nonsecret** canary without replacement. Its exact returned item and unexpected
prompt references are retained before further inspection. The candidate must
appear/change metadata and have the supported 20-byte encrypted prefix. Setup
then deletes only that retained canary, confirms its absence through the new
owned collection's Items property, and requires a second metadata change and
the encrypted prefix again. An unrelated existing encrypted header alone cannot
satisfy this association proof. Only then is the filename verified and the stage
`FILE_BOUND`; master generation/write follows. Canary uncertainty, no observed
change, lost reply, unexpected prompt or plaintext leaves setup unavailable and
cannot trigger another canary write, master or collection creation.

Checks repeat after mutations and reads, before a private result is released.
Completed atomic file replacement is supported by reopening the same path; a
replacement during an observation fails. A post-mutation failure is unknown,
not a rollback claim. No keyring body is read, hashed, dumped or decrypted by the
format probe. This is format/provenance evidence, not password-strength or
whole-file authenticity evidence.

The nonsecret selection and setup provenance live in
`/home/tang/.local/state/culture-cabinet-key-store/selection.json`, owned0600 in
an owned0700 directory. Selection schema `cabinet-key-store-selection/v2` separates
requested label, operation UUID, unique service/caller provenance, actual prompt
and collection references, decoded identifier, verified filename and canary
reference. Unobserved values are null. Stages are `CREATE_PENDING`,
`PROMPT_PENDING`, `CREATED_UNVERIFIED`, `FILE_BOUND`, `ACTIVE`. File-transition
receipts and master phase are separate. A durable pending intent precedes each
OS side effect. Only verified completed setup is ACTIVE; ordinary helper
requests reject every pending stage and old v1 selections. Neither process death
nor another terminal action resumes an uncertain setup or regenerates a master.
The ordinary request/result protocol is unchanged.

### The one abandoned pre-master dummy

The failed operation `f6ed3437-1f93-4564-8a6d-e558e5dd6462` requested Label
`CultureCabinetb6a176add3874980b388e8e1dd41cdd0`; its old source lost the actual
returned path. The predicted path is unresolved. Its disposition is
`ABANDONED_UNRESOLVED_POSSIBLE_DUMMY_ORPHAN`, not successful OS cleanup.

After focused source acceptance and an explicit routed continuation, trusted
terminal-only `retire_known_dummy(failure_receipt_bytes)` may archive **only**
this pinned original v1 PROVISIONING file and exact failure receipt, matched by
hard-coded SHA256 and operation UUID. It rechecks the one predicted object's
negative observation and exact predicted file absence, writes/fsyncs a private
read-only, never-overwritten archive, and CAS-retires only the unchanged local
selection pointer. A changed pointer, post-master state, different receipt or
resolvable prediction is refused. No OS collection/file is deleted, located,
adopted or unlocked, and no real-key-bearing setup can use this disposition.
The archive retains exact original bytes/UUID and both negative observations.

A separate explicit `provision(continuation_archive=returned_uuid)` links one new
nonce/operation to that archive. Merely finding the archive never triggers a new
creation; missing/mismatched links are refused. No automatic retry exists. The
normal `provision(timeout=120)` call and ordinary private RPC keep their existing
behavior for installations without this failed-attempt archive. This source
change does not itself execute retirement or another GNOME operation.

### The exact ended v2 prompt attempt

The corrected dummy operation `ce102960-02f0-4697-8008-371439a02d04` reached
`PROMPT_PENDING` at `/org/freedesktop/secrets/prompt/p2`, then returned unavailable
without a captured collection reference. Its canary and master phases remained
`NOT_STARTED`. The first saved UI observation did not confirm both password
fields were filled; the terminal result does not establish the failure subtype.
The original caller `:1.20377` has ended. A prompt belongs to its original calling
connection and has no historical-result getter: a replacement must not replay
Prompt/Dismiss, try to recover an old result or infer that no OS collection exists.

After focused source acceptance, terminal-only
`retire_prompt_pending_dummy(failure_receipt_bytes, terminal_receipt_bytes)` may
dispose of only this second pinned local pointer. It requires the exact secure
v2 bytes and both retained receipt hashes, the complete null-result/zero-progress
predicate, and the exact immutable predecessor archive. It checks only bus-daemon
`NameHasOwner` metadata for the original caller, and refuses while that caller
is live. It never queries the old prompt or any keyring object/file.

The new read-only archive `abandoned-ce102960-02f0-4697-8008-371439a02d04.json`
retains original selection/receipts and their hashes, the returned prompt, ended
caller observation and predecessor link. Its disposition is
`ABANDONED_PREMASTER_PROMPT_UNRESOLVED`, with `possible_collection_exists=unknown`
and `os_orphan_deleted=false`. File fsync and directory fsync precede CAS pointer
removal. An interrupted identical archive can be revalidated and fsynced again;
foreign archives, changed pointers, captured Completed results or canary/master
progress refuse retirement. This is not a generic reset or cleanup mechanism.

Only a separately routed `provision(continuation_archive=second_archive_uuid)`
can create the next dummy. Both fixed ancestors must validate. Once the second
archive exists, the old link alone is insufficient. Any new pending selection
still prevents another creation; there is no retry loop or automatic retirement.

New v2 receipts also retain closed nonsecret prompt observations for actual
acknowledgement, Completed/dismissal/malformed result, timer expiry, observed
connection closure or call failure. Unobserved distinctions stay `UNOBSERVED`.
A Completed reference is saved before a later acknowledgement failure; a process
death with no observed terminal event does not become an invented timeout.
Previously accepted v2 receipts remain readable without the observation field.
The private ordinary RPC is unchanged.

The versioned dummy input helper must identify both labelled Password and
Confirm controls within the exact owned GCR dialog, verify intended focus, and
invoke its explicit Create action once. It cannot assume Tab navigation, read
password contents, log/echo the private generated dummy passphrase, use clipboard
fallback or target an unrelated window. Input readiness and all real GNOME
association/persistence/negative/native proofs remain separate from these tests.

## Terminal closure, lock and restart

Closing an enrollment terminal/helper does not erase a successfully persisted
key. Fresh helpers address the same explicit collection. If it is protected and
already unlocked, an authenticated native action can refill its current RAM lease
without key re-entry. A terminal keeper process is unnecessary.

Desktop logout, reboot or a collection lock can require ordinary GNOME unlock.
This host uses automatic login, which does not establish protected auto-unlock.
An SSH-started app connects only to the existing same-UID local bus. It never
launches a daemon, substitutes another bus or sends a remote unlock password.
Missing bus/selection/protection is unavailable; a protected locked collection is
locked. No blank-password or plaintext fallback exists.

Persistent keys do not themselves make the database safe to reopen. The native
same-installation clean-checkpoint path, stale-session/preview invalidation and
ordinary owner login belong to the native integration. Hard-kill/power-loss
recovery and unattended reboot unlock are not established by this helper.

## Private request and result contract

Use `invoke(request, timeout=10.0)`. The fixed child receives two inherited pipes;
stdout/stderr are discarded. One four-byte unsigned big-endian length precedes
strict UTF-8 JSON on each pipe. The limit is65536 bytes excluding that prefix;
EOF is required. Duplicate/unknown fields, trailing/truncated frames and nonfinite
values are refused. Secrets are UTF-8 strings, limited to8192 encoded bytes.
No secret travels in command arguments or environment variables. The client
kills/reaps a timed-out child; it never retries a mutation.
Before reading a request or loading GI/crypto, the private worker sets its core
limits to zero and Linux dumpability to zero, verifying both. This contains
native-library abort dumps locally, including pipe-based core collectors; no
global system/coredump setting is changed.
The worker pins libsecret's nonsecret service-name setting to the observed unique
bus owner and uses libsecret's asynchronous factory. Supplying a ready connection
to its raw constructor conflicts with the C constructor's own bus initialization.

All requests contain exactly `version`, `operation`, `record_kind`, `binding`,
`expected_revision`, `operation_id`, `record_if_put`. Version is
`cabinet-secret-service/v1`. Ordinary operations are `availability`, `put`,
`read`, `delete`, `reconcile`; provisioning is not an ordinary operation.
Record kinds are `credential` and `clean_restart_anchor`. No list/export/path,
collection or arbitrary-provider selector is accepted.

Credential binding includes installation UUID, positive integer owner ID/revision,
closed provider/purpose, and either native connection UUID or the original exact
legacy account/actor/profile binding. Initial supported storage families are
OpenAI, TMDB, Hardcover, LibraryThing, Hydra and SAB. Storage support does not add
native provider execution. Native scopes and legacy scopes remain distinct.
Record and binding field validators in the helper define the closed types;
generation, operation IDs and store revisions are canonical UUID strings.
Timestamps are canonical UTC ISO strings ending inZ. A null declared expiry
does not supply a usage grant or extend any native session/source authority.

Results have exactly `version`, `operation_id`, `status`, `revision`, `record`.
Only a successful private `read` includes a decrypted matching record. Never send
that record to HTTP, templates, sessions, logs, screenshots or evidence.

| Result | Meaning |
| --- | --- |
| availability AVAILABLE, revision null | Protected unlocked store and exact item metadata exist. No secret is loaded/decrypted. This is not validation or a mutation acknowledgement. |
| put AVAILABLE, revision nonnull | Exact operation's authenticated target and final intent receipt were read back. |
| read AVAILABLE, exact revision and record | Matching authenticated record on the private native pipe only. Expired credentials are refused. |
| delete REMOVED, revision nonnull | Exact target item deletion/absence and final receipt confirmed. Not forensic erasure/provider revocation. |
| reconcile AVAILABLE / REMOVED, revision nonnull | The original exact put/delete effect was observed. No target mutation was retried; a pending receipt may be finalized. |
| UNKNOWN, revision null | Effect not proved. Preserve pending state and original operation; do not fall back or blindly retry. |
| CONFLICT, revision null | Expected predecessor, binding or reused operation intent conflicts. |
| LOCKED / MISSING / UNAVAILABLE / CORRUPT | No private record is released. |

Reconciliation reuses the original mutation UUID and original expected previous
revision. That previous revision is null for initial create, including its later
reconciliation. Read/delete require an exact known store revision. An encrypted
pending receipt precedes replace/delete so failure cannot silently reuse the old
key. Deletion retains an encrypted operation receipt; explicit re-enrollment must
CAS against its tombstone revision, not pretend the record never existed.

The producer exports `DELETE_NO_EFFECT_GUARANTEE = direct-delete-locked/v1`.
A completed, validated **direct delete** result `LOCKED/null/null` means this
invocation did not attempt an intent or target write. After a write may start,
even a LOCKED-labelled error returns UNKNOWN. This does not establish that an
earlier invocation of the same UUID had no effect.

Only the native layer, with no prior uncertain invocation, may durably record the
matching nonsecret proof (schema, original operation UUID, canonical complete
delete-request SHA256, helper guarantee). A later explicit Remove command must
recheck current authority and atomically consume that proof **before IPC**, then
send the identical original binding/UUID/predecessor. Another verified direct
LOCKED may restore it. UNKNOWN, missing/malformed replies, receipt absence,
reconcile/status/read LOCKED or historical logs are never proof and never permit
a blind retry. Old pending rows are not backfilled. These native transitions are
separate from the helper's unchanged five-field response and RPC version.

Mutations and reads share the checked empty0600 `control.lock` inside
`/run/user/1000/culture-cabinet-key-store` (owned0700). This is only a writer lock,
not another credential file. Same-UID malicious processes and OS administrators
remain outside the trust boundary. Coordinated rollback of all local stores is
not defeated by these receipts; Python byte erasure is best effort.

## Checks and remaining OS proof

Run `/usr/bin/python3 -I -B tools/test_cabinet_secret_service.py -v`.
The explicit platform regression is
`/usr/bin/python3 -I -B tools/test_cabinet_secret_service.py --metadata-connect`.
It connects only to service metadata and checks the pinned owner/bus without
loading collections, opening a secret session or requesting any secret.
Tests use owned fictional records, actual Fernet, private child-process pipes,
cancellation and header fixtures. A test-only ciphertext backend is never a
production option. Its fresh-process result is component evidence, not proof
that a real GNOME collection was protected/unlocked or that native restart works.

Before real enrollment, the custodian must complete the reviewed GNOME prompt for
a disposable dummy collection with a nonempty test password. Required integration
evidence also includes empty-password refusal, protected-to-empty invalidation,
fresh helper/native retrieval after app/terminal exit, and exact cleanup of only
those owned test objects. No actual collection setup/unlock or real-key read is
claimed by the deterministic tests. The collection password belongs only in the
GNOME OS prompt; no replacement API key is needed for this proof.
