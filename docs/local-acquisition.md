# Local acquisition backend candidate

This slice adds an unconnected trusted Python service. Existing saved Obtain,
blocked/cancelled states, handlers, templates, models, migrations and demo behavior
remain unchanged. Nothing activates a transport on GET, heart, search or confirmation.
No live source decoding, provisioning, caps, search or enqueue was performed here.

`Backend` in `backend/shelf/acquisition_backend.py` consumes explicit trusted
`EntryMetadata` grants for Hydra and SAB. Every operation validates current grant
expiry, exact actor/profile, provider, purpose and scopes before opening a store;
the record must independently pass the same validation and exact owner binding.
These inputs are operator/backend authority, never user-submitted flags. Existing
application permission checks must issue/recheck grants at the eventual boundary.
The existing credential store's checked paths, locking and transactional records
are reused; no additional credential or checkpoint store exists.

| Provider | Fixed private directory under `${PRIVATE_LOCAL_PATH}` | Purpose | Local scope |
| --- | --- | --- | --- |
| hydra | `hydra-search` | `culture-cabinet-hydra-search` | `hydra-external-search` |
| sab | `sab-add-only` | `culture-cabinet-sab-add-only` | `sab-addfile-only` |

Local expiry is at most 90 days from entry and never beyond the declared expiry.
These scopes express local use, not newly granted upstream privileges. Existing
Hardcover, TMDB and LibraryThing policies are preserved. Private records are
repr-safe and wiped after each operation. Python/HTTP/JSON necessarily create
transient immutable copies; this is not a guarantee of forensic memory erasure.

`LoopbackTransport` implements the existing `Transport.send` interface and
`Backend` routes through `acquisition_contract.Client`. It uses Python's maintained
`http.client` and `socket`, fixed numeric IPv4 hosts and ports, no proxy lookup,
redirect following or retries. Connect timeout is 2 seconds; one 10 second deadline
covers connect/request/headers/body, including slow headers. Reads are at most
64 KiB plus one overflow byte. Compressed or oversized replies fail closed.
Failures have static messages and never expose URLs, bodies or upstream exceptions.

Caps and bounded text search are GETs to `http://127.0.0.1:5076/api`; search fixes
`limit=20`, `offset=0`, `o=json` and contract-approved categories. The exact effective
key is URL-encoded once as lowercase `apikey`. HTTP-200 XML errors, including code
100, are errors rather than successful caps. Search returns an opaque `PrivateSearch`
that refuses pickling, has no public dictionary and hides its bytes from repr.
There is deliberately no public/serializable result parser or upstream-link API.

Confirmed addfile is a multipart POST to `http://127.0.0.1:8080/api` with only
`mode=addfile`, `output=json`, lowercase `apikey` and file part `name` with fixed
filename `selection.nzb`. No addurl, admin, status, history, cancellation or bulk API
is implemented. Only an explicit Boolean success and one valid SAB queue identifier
in the documented `nzo_ids` array establish a queue receipt. Malformed, missing,
multiple or inconsistent IDs leave the outcome unknown. A strict explicit failure
with an error field establishes rejection without exposing its text.

`Backend.provider(actor_id=..., profile_id=..., snapshot=..., selection=..., nzb=...)`
binds exact selected bytes and evidence to the existing pipeline Provider port.
`Pipeline.dispatch(Evidence(...))` owns the checkpoint before sending. Recreated
fixture owners reuse queued/unknown checkpoints and never resend a lost reply.
Lookup/poll/cancel remain unavailable: add-only authority cannot reconcile them.
Direct service enqueue callers must supply that same durable ownership contract.
The [P3.2 candidate bridge](obtain-local-handoff.md) now supplies exact Hydra GET,
selection parsing, durable DB dispatch ownership and a separate CSRF submission
review. It accepts canonical UUID and legacy SAB job receipts. The production
factory composes existing scoped records only under an explicit, default-disabled server policy; completion, import,
indexing and player integration remain separate. No runtime activation is implied.

## Private one-time entry (not executed)

Use `/usr/bin/python3 -B tools/provision_local_acquisition.py`, separately from the
app venv. Only this operator tool imports PyYAML and cryptography. Its flags are
`--provider hydra|sab --source <approved-config-path> --account-id <explicit-owner>
--actor-id <explicit-id> --profile-id <explicit-id> --expires-at <UTC-time>`.
Use actual explicitly established bindings and a UTC expiry no more than 90 days
away. No key is accepted on argv or printed. There are no default owner IDs,
automatic directory creation, replacement, rotation, config writes or restart.
An operator must separately prepare the fixed owner-only store directories and
empty mode-0600 `control.lock` under the existing store's ownership/mount/ACL rules.
Never use `_fixture_store` for provisioning or runtime.

The tool reads at most 1 MiB from the explicitly selected source and opens it only
after binding/expiry validation. Hydra selection is solely YAML `main.apiKey`;
duplicate mapping keys and malformed/placeholder values are rejected. The supplied
`{OBF}` decoder is copied unchanged from `.requirements/decoder/`, including all
nine original tests. Provenance: NZBHydra2 9.1.1 commit
`8ef3e6489aac8c8d776dca538127be38b2aff962`, `SensitiveDataObfuscator.java`,
`MainConfig.java`, `ConfigReaderWriter.java`; fixed-key AES/CBC obfuscation is not
secret protection or authenticity. No custom cryptography was added.
SAB uses standard strict ConfigParser after its ConfigObj preamble, selects only
explicit `[misc] nzb_key`, and requires the existing 32 hexadecimal character
add-only key. `api_key` is never selected and is never a fallback. Source config
bytes are preserved. Store entry uses existing `commit_entry`, enter-only;
conflict/dirty/uncertain outcomes require existing private operator handling.

## Offline verification and next demonstration

Source tests: from `tools/`, `/usr/bin/python3 -B -m unittest -v
 test_hydra_key_source test_provision_local_acquisition`.
Backend focused tests: from `backend/`, run the prepared app Python with
`-B -m unittest -v shelf.tests.test_acquisition_transport
 shelf.tests.test_acquisition_backend`. All new HTTP tests inject a fake connection;
source tests use synthetic text and fake store adapters, never private sources.

Full verification uses the supported root `backend/manage.py test --verbosity=2`
with a fresh owned TMPDIR measured on local btrfs, followed by `check` and
`makemigrations --check --dry-run`. See `${PRIVATE_RUNTIME_PATH}` for actual commands,
counts, prerequisites, WAL evidence and limitations. No independent Q review was
commissioned because this worker was explicitly limited to one unspawned package.

Smallest next user demonstration: run the offline backend fixture that dispatches
one exact confirmed selection, recreates its pipeline owner, then repeats dispatch;
it produces one synthetic SAB receipt and one fake POST. The lost-reply case stays
unknown with no second POST. Real activation needs a separately reviewed durable
production checkpoint boundary and sanitized exact-result/NZB binding first.
This is not live search, SAB connectivity, device or full Phase 2 acceptance.
