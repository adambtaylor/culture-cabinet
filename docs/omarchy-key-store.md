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

Host-only provisioning creates a new name `CultureCabinet` followed by a UUID's
32 lowercase hex characters. It does not adopt a pre-existing collection, use
`login`/`session`/default aliases, scan collections or guess a suffixed filename.
The selected object path must exactly match the new identifier and the exact
formerly absent file in `/home/tang/.local/share/keyrings`.

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
permanent `protected=true` setting. If an empty collection is not materialized,
provisioning may first store a fixed public canary; no generated master/API key is
stored until the protection check passes. The canary is deleted afterward.

Checks repeat after mutations and reads, before a private result is released.
Completed atomic file replacement is supported by reopening the same path; a
replacement during an observation fails. A post-mutation failure is unknown,
not a rollback claim. No keyring body is read, hashed, dumped or decrypted by the
format probe. This is format/provenance evidence, not password-strength or
whole-file authenticity evidence.

The nonsecret selection and setup provenance live in
`/home/tang/.local/state/culture-cabinet-key-store/selection.json`, owned0600 in
an owned0700 directory. An explicit `PROVISIONING` intent precedes the OS prompt;
only completed setup publishes `ACTIVE`. Cancellation or uncertainty leaves the
intent unavailable. Do not hand-edit it, stamp it protected, remove it to force a
new collection, or retry uncertain provisioning automatically. The custodian must
reconcile that exact named setup, with no unrelated collection discovery.

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

Mutations and reads share the checked empty0600 `control.lock` inside
`/run/user/1000/culture-cabinet-key-store` (owned0700). This is only a writer lock,
not another credential file. Same-UID malicious processes and OS administrators
remain outside the trust boundary. Coordinated rollback of all local stores is
not defeated by these receipts; Python byte erasure is best effort.

## Checks and remaining OS proof

Run `/usr/bin/python3 -I -B tools/test_cabinet_secret_service.py -v`.
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
