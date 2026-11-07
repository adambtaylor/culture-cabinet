# SAB postprocessing file evidence

The additive Cabinet consumer and explicit FLAC-album activation are documented
in [completion-library.md](completion-library.md). Hook evidence remains the
weaker postprocessing stage described here; it now has separate Cabinet file rows
and a view instead of being adapted into legacy final-completion evidence.

`tools/sab_completion.py` is an offline, standard-library Linux/POSIX hook. It
requires no credentials and performs no network, subprocess, installation,
configuration, media move or media-content write. The supplied public SAB 5.1.3
source excerpt in `.requirements/sab-source-evidence.txt` establishes that
`postproc.py` sets `nzo.status = Status.RUNNING` immediately before
`external_processing`. `newsunpack.py` exports `pp_status = job_result` and
`files` as JSON containing relative `newfiles` paths. Hooks also run on failures.
The implementation follows that source contract; no live SAB was inspected.

Exactly `Running` plus PP `0`, matching registration and successful local byte
checks produce `POSTPROCESS_FILE_VERIFIED`. This is not final SAB history
`Completed`. `Completed`, failed/unknown states, nonzero PP (including repair or
unpack failures), malformed/missing evidence and unknown file-list shapes are
refused. A zero job result reports no error from that SAB stage; it does not prove
that CRC or repair ran, validate codecs, or establish the requested work/edition.
`repair_verified` and `crc_verified` therefore remain null.

## Registration and activation boundary

The enabled trusted factory binds a confirmed request to `CC-<request UUID>` and
exactly `<complete_root>/CC-<request UUID>`. Its policy must explicitly approve
SAB's fixed Complete layout with no category subdirectories, sorting or renaming.
The encoder selects only the fixed `culture-cabinet-completion` launcher, `pp=2`
and `cat=*` per job. The observed licensed Bunny name proves only the earlier
test's layout, not all SAB configurations. No service configuration is changed.

`obtain_acquisition.completion_registration` builds the exact accepted nzo,
request/result/digest/NZB hash, scope, root and size-policy entry from durable
transfer state; it takes no caller-supplied job directory. After committing a
single valid UUID/legacy acknowledgement, submission automatically publishes the
private registry using `shelf.completion_registry`. The intent was committed
before sending. Unknown/lost/ambiguous acknowledgements never register and never
resend. No pre-known content manifest or per-download operator ceremony is needed.

Publication serializes cooperating writers with a bounded exclusive directory
flock, validates the whole existing registry, rejects conflicting nzo/request/root
bindings and unsafe destinations, writes/fsyncs an exclusive 0600 temporary file,
and atomically replaces the registry snapshot under that lock. It fsyncs the
parent directory. Identical retries preserve bytes; orphan unpublished temporary
files are ignored. A failure before or after replacement can be repaired from
the acknowledged transfer by later status/restart rendering, without provider
calls. The private directory and all its writers are trusted and must use this
lock protocol; no uncoordinated editor may rewrite it. The registry remains
bounded to 256 entries/256 KiB and is never silently pruned or recycled.

A fast hook polls for a missing registration or busy publisher lock for at most
five seconds, at 50 ms intervals. It reads a complete snapshot under a shared
lock. Invalid registries and mismatched roots fail immediately. If publication
cannot finish in the window, the hook returns unavailable, with no receipt and no
resend. Later local publication repair alone does not rerun a failed SAB script;
a later authorized hook invocation must revalidate current files. There is no
status/history/admin/cancel API or background replay service. Persistent storage
failure can therefore leave queued work without completion evidence.

The runtime remains disabled by default. A separately approved deployment must
install the reviewed fixed launcher, `tools/sab_completion.py`, and its shared
`backend/shelf/completion_registry.py` and `backend/shelf/completion_files.py`
modules, preserving their repository layout
(or installing the `shelf` package). The launcher invokes the hook with fixed
registry/receipt paths. It must use the same approved registry path as Cabinet,
and both processes must have the required trusted UID/filesystem access. No
launcher, global/category default script, script_dir or active policy was
installed or changed during this repair. No root, script or registration can be
supplied through HTTP.

The exact v2 registry shape is illustrated with wholly fictional identifiers:

```json
{
  "version": 2,
  "requests": [{
    "nzo_id": "468ad695-4260-4ee0-b4bf-2e9b5a631803",
    "request_id": "7c035cd0-51eb-4e66-9096-68e1fe57a303",
    "result_id": "e25445b9-23a8-46f0-8307-07eb685229ad",
    "result_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "nzb_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "complete_root": "/srv/cabinet-fixtures/Complete",
    "root": "/srv/cabinet-fixtures/Complete/CC-7c035cd0-51eb-4e66-9096-68e1fe57a303",
    "scope": "film",
    "max_bytes": 1048576,
    "files": null,
    "test_only": true
  }]
}
```

`files: null` is the ordinary new-download contract. The hook obtains paths only
from SAB's declared `newfiles`, computes size and SHA-256 from the bounded files,
and records them as transport evidence. It does not manufacture a trusted content
manifest from these observations. If an independent expected manifest exists,
`files` can instead contain `{ "path": "film.mkv" }` entries, optionally with
positive `bytes` and/or lowercase SHA-256 `sha256`. Its path set must exactly match
SAB's declaration and every supplied expectation is enforced. Expectations are
retained in the receipt, so changing them conflicts with an earlier receipt.

Registries and receipts are bounded to 256 KiB. At most 256 registered requests,
128 declared files per job and 64 GiB aggregate observed bytes are accepted.
Unknown versions/fields, duplicate JSON keys or paths, invalid bindings and
reused request/job IDs or job directories within a registry fail closed. Bindings must never be
recycled across registries; there is no historical cross-registry identity index.
V1 registries are deliberately refused because they lacked exact result binding
and described the wrong SAB stage. Original input handoffs remain historical.

## Filesystem and environment

Registry and receipt directories must exist with owner-only mode 0700; registry
and receipt files use 0600. Control ancestors must be root/hook-UID owned and not
group/world writable, except root-owned sticky parents. Paths come from fixed,
trusted launcher arguments. Media and control ancestors are opened component by
component with `O_NOFOLLOW`; symlinks, FIFOs, directories as files, hardlinked
media, traversal, aliases, partial components/extensions and empty files fail.
The receipt directory cannot be inside the selected job tree.

Only these environment keys are read, individually; no enumeration or copying:

| Key | Accepted value |
| --- | --- |
| `SAB_NZO_ID` | Exact registered canonical UUID or legacy `SABnzbd_nzo_*` ID |
| `SAB_STATUS` | Exactly `Running` |
| `SAB_PP_STATUS` | Exactly `0` |
| `SAB_COMPLETE_DIR` | Exactly the registered absolute job root |
| `SAB_FILES` | Bounded JSON array of unique relative nonpartial paths |
| `SAB_BYTES` | Optional positive decimal reported bytes within the ceiling |

`SAB_API_KEY`, `SAB_PASSWORD`, names, categories, HOME and all other environment
fields are never read. SAB_BYTES is reported metadata, not final extracted size.
Only the registered job tree is visited. Every directory must contain exactly
the declared children; undeclared entries cause refusal before their content is
opened. A SAB configuration leaving extra artifacts needs an explicit later
contract decision, not a recursive discovery fallback. File lengths and their
aggregate are bounded before hashing; reads use 1 MiB chunks and one overflow
byte. Descriptor metadata and the root identity are checked for changes.

## Receipts and Kodi handoff

The Python entrypoint is `run(registry_path, receipts_path, environment_mapping)`;
the CLI accepts exactly `--registry <absolute-path> --receipts <absolute-path>`.
A later fixed launcher can call it directly with `os.environ`; only the six keys
above are accessed. Both interfaces remain silent, including errors:

| Exit | Meaning |
| --- | --- |
| 0 | Newly published or identical, freshly reverified evidence |
| 1 | Unsupported/missing/failed evidence, unsafe file/path or I/O failure |
| 2 | Existing malformed, unsafe or conflicting receipt, preserved unchanged |
| 3 | Unexpected internal failure; no exception details emitted |

`<request_id>.json` contains the v2 state, exact registration provenance, reported
SAB stage, sorted observed path/hash/size records and optional expectations.
`content_identity: unverified` and `full_work_verified: false` are intentional.
Selected film/audiobook/S01E01 scope is authorization, not observed media identity.
Each `handoff` entry supplies:

- Stable `media_identity: sha256:<digest>`, SHA-256 and measured `size`.
- `relative_path` under the registered Complete root and the known `nzo_id`.
- Request/result/NZB provenance and `selected_scope`.
- `observed_media` audio/video/unknown, explicitly based only on filename extension.
- `verified_at`, nullable actual `acquired_at`, `acquired_at_basis`, and `test_only`.

`completion_observed_at` and `verified_at` are the first hook observation time,
not SAB's final history timestamp or an acquisition date. `acquired_at` stays null
with basis `unavailable`. Kodi music-catalog admission remains false until an
authoritative timestamp source/basis is agreed and the consumer contract reviewed.
The separate consumer imports file entries without playback, listening history,
codec verification or invented track/artist identity. The hook receipt cannot be
passed as the stronger legacy `LocalCompletion`; Cabinet's transfer remains
queued while its separate postprocessing file state can be verified.

Publication writes/fsyncs an exclusive 0600 temporary file, links it atomically
to the final name without overwrite, then fsyncs the directory. Identical retries
preserve the entire first receipt and timestamps. Comparison includes generated
handoff fields; a tampered acquisition date is a conflict. Concurrent duplicates
converge on one receipt. Crashes before publication may leave ignored `.pending-*`
files; crashes after linking leave a complete receipt that a retry can validate.
The hook removes only its own temporary file. It never sweeps or overwrites.

The hook UID, SAB environment, registrar, launcher and filesystem are trusted;
this is not a signed history record. Media must be quiescent. Stat checks do not
provide an immutable snapshot or guarantee against changes after verification.
Consumers must revalidate current bindings/files and respect later unavailable
or conflicting outcomes; an old receipt alone is not current readiness. Local
hard-link and file/directory fsync support are required. Fault injection is not
physical power-loss testing.

## Offline verification

Run `python3 -B -m unittest discover -s tools -p test_sab_completion.py -v`.
Tests cover actual Running/PP0, failed/unpack-bad states, unknown shapes, dynamic
and expected manifests, unexpected files, no secret reads, bounds, path attacks,
mutation, duplicate/crash recovery and conflicting/tampered handoffs. The Django
runtime tests also pass a real-factory submission through registration and this
hook using synthetic stores, transport and files. The F3 repair full suite, hook tests and clean-source checks are recorded in
`${PRIVATE_RUNTIME_PATH}`; historical author and independent review evidence is
preserved unchanged.

The prior publicly licensed Bunny physical test is evidence only: it is neither
fixture data, a preference nor activation authority. No real download is touched.
Independent re-verification of the repaired source remains pending; this author
claims no independent review PASS or live automation completion.
