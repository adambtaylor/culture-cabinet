# Fictional TMDB adapter handoff

This increment implements three production modules: `shelf.tmdb_contract` owns
immutable facts, current-use policy and the trusted serial budget;
`shelf.tmdb_transport` owns NoFetch and pure request/address/envelope decisions;
`shelf.tmdb_parser` owns bounded decoding, atomic route mapping and context actions.
There is no selected HTTP client, runtime integration, registry, cache, ranker,
persistence, new feedback path or model transfer. Suggestions and source facts
never become user feedback or human approval.

`parse_tmdb(fetch, *, current_policy, clock, budget)` takes the exact request and
attempt retained by H's `FetchResult`. Keep the same `Clock` and `TmdbBudget` for
one serial job. `context_action(fact, *, request, current_policy)` makes only local
template decisions. The test-only supplier below feeds invented status, header
lines, byte chunks or reported failures through H's production guards; production
never imports or registers it. No TCP, DNS, TLS or pinning events are fabricated.

```python
from shelf.tmdb_contract import Clock, DiscoverQuery, TmdbBudget, VideosRequest
from shelf.tmdb_transport import fetch_tmdb
from shelf.tmdb_parser import parse_tmdb, context_action
from shelf.tests.fixtures_tmdb import (
    NOW, FixtureTransport, discovery, fixture_policy, video,
)

seconds = [0.0]
clock = Clock(monotonic=lambda: seconds[0], utcnow=lambda: NOW)
budget = TmdbBudget(clock=clock, fictional=True)
policy = fixture_policy()  # invented evidence, never provider permission
fetched = fetch_tmdb(DiscoverQuery('movie'), policy=policy, clock=clock,
    budget=budget, transport=FixtureTransport(discovery()))
page = parse_tmdb(fetched, current_policy=policy, clock=clock, budget=budget)
assert (page.outcome, page.reason) == ('success', 'page_items')
assert page.observations[0].source_url is None
seconds[0] += 1
parent = page.observations[0].source_identity
fetched = fetch_tmdb(VideosRequest(parent), policy=policy, clock=clock,
    budget=budget, transport=FixtureTransport({'id': parent.id, 'results': [video()]}))
videos = parse_tmdb(fetched, current_policy=policy, clock=clock, budget=budget)
action = context_action(videos.videos[0], request=fetched.fixed_request,
    current_policy=policy)
assert (action.reason, action.href) == ('fixture', None)
```

`Clock(monotonic=..., utcnow=...)`, `TmdbBudget(clock=..., fictional=True)`,
`DiscoverQuery('movie'|'tv', cursor=...)`, and `VideosRequest(SourceIdentity)` are
actual constructors. `PolicyEvidence('allow', reviewed_at, evidence_digest,
attribution)` and `TmdbPolicy(version, operations=..., field_permissions=...)`
record independent operation/field grants. Default unknown blocks. Use the normal
Django test entrypoint for this fixture example's tested equivalent; it needs no
key or live provider record. Fixture bytes are invented and never persisted.

## Outcomes and coverage

| Result | Meaning |
|---|---|
| `success/page_items` | At least one permitted movie, TV or video item; video-only success is valid. |
| `healthy_empty/no_results` | Valid zero-count discovery page or matching-parent empty videos. |
| `blocked` | Off, paused, unknown/denied rights, absent transport, auth/terms, invalid request or required fields unavailable. |
| `limited` | Quota/deadline/size/row/page limit, rate limit or partial required projection filtering. |
| `failed` | Malformed codec/scalar data, envelope failure or reported transport failure. |
| `quarantined` | Schema/count/request/identity conflict, unsafe redirect or orphan 304. |

`ready/response_ready` is an internal envelope state. `unchanged` is never emitted;
there are no conditional requests, snapshots or prior-job hash shortcuts. Envelope
limits precede status classification; bounded 304 precedes Location. Strict UTF8,
lexical nesting, JSON duplicate/nonfinite/number checks and all ignored-field
bounds precede schema and projection. Bad pages release no prefix. Unknown
optional fields are structurally checked then discarded. Movie title/release_date
and TV name/first_air_date remain distinct; dates never gain invented midnight.
Missing/empty/text overview and timestamp fractional precision remain distinct.
Source instructions remain ordinary strings, with no rendering or execution path.

Coverage reports admitted attempts, fully validated completed pages, retained
route items, totals, required-policy filtering and incomplete coverage. Page1 may
succeed with incomplete coverage. Obtain its budget-owned page2 handle through
`budget.issue_cursor(fetched.fixed_request)` only when totals advertise another
page; it cannot be cloned, reset, reused or moved to another job/media/query.
Page2 advertising more pages is `limited/pagination_limit`. This parser returns
only the current page. Earlier complete same-job pages may be included by a caller
only in limited aggregation after current `project_for_use` reprojection, with
explicit incomplete coverage; failed/blocked/quarantined results carry no items.
No aggregate engine is implemented.

The one budget enforces six admitted attempts/60s, >=1s spacing, two discovery
pages/media, <=80 released identities, two video requests, 1MiB/job, 256KiB/response,
request10s/job30s including parse. Equality expires deadlines. Header limits are
16KiB/64 fields; codec limits are depth12/nodes5000/global strings4096/title200/
overview2000, 20 discoveries/100 videos and numeric lexemes128 characters. Inclusive
size boundaries accept; the next unit rejects. Checks at lexical, row and
projection boundaries do not establish hard CPU or blocking IO cancellation.

`attempted_at` labels invocation. `checked_at` labels a completed bounded envelope,
including later codec/schema/deadline failure; it never proves a successful
observation or destination reachability. Early failures have null `checked_at`.
Each retained field has route/query/payload hash, JSON pointer, time and fictional
provenance. Hashes establish lineage only. `rights_snapshot` is historical evidence;
use requires current operation and field grants via `project_for_use`. Optional
unknown/denied fields strip; required identity/title/provenance or video fields
block. Reproject on reuse/revocation and discard denied fields. Transient permission
does not authorize retention, export, AI input or relabeling as human history.
Only `safe_diagnostics` is a public diagnostic projection; do not recursively
serialize FetchResult, raw bodies, requests or exceptions.

`source_off` disables the source; pause stops its current use; rights unknown or
deny disables the affected operation. Existing independent synthetic manual entry
and human feedback remain available under their existing grants. This adds no
real-data admission or new manual fallback. See [existing demo and recovery
limits](README.md): no-key synthetic operation, literal deployment-root refusal
from a candidate, one initial trusted session, no restart/lost-witness promotion,
logical rather than forensic erasure. Those limits still apply.

## Context and activation boundaries

Fixture `href`/`source_url` always remain null with reason `fixture`. Later authorized
real facts require current dated source assertions and operation/field policy,
matching parent/media/language, canonical fixed HTTPS TMDB context templates or
YouTube Trailer, exact 11-character key, official=true and language=en. No source
URL is trusted. No second fetch or per-link manual/account approval is required.
`metadata_checked_at` is the TMDB assertion time; `destination_checked_at=null` and
`ownership_verified=false`. A future label may say “listed by TMDB, marked official
by TMDB; metadata checked <date>”; never claim alive, playable or independently
official. Future anchors require `rel="noopener noreferrer"` and
`referrerpolicy="no-referrer"`; no embeds or private/taste/token payloads. Browser
redirects are outside these backend decision claims. No browser acceptance is
claimed here.

As of 2026-10-27, actual route fetch/transient processing, display/excerpts,
retention, and local selection rights are individually unknown and disabled.
Before live use obtain exact applicable official terms/version/effective or
observed date, including AI/ML/recommendation applicability, per-field use,
retention, revocation, activation freshness and attribution. FAQ research is not
full clearance; neither hobby use nor a proposed retention cap grants permission.
Provider export/redistribution/embeddings/external-model transfer remain denied
by scope. No cache or raw-body disk persistence exists. Before live display the
required notice is “This product uses the TMDB API but is not endorsed or certified
by TMDB.” Any mandated approved branding asset needs separate rights/provenance;
none is acquired here. No repository license or publication decision is made.

Actual client, DNS/connect pinning/rebinding/peer equality, TLS/SNI/certificate/CA,
wire limits, IO cancellation and credential conformance are **NOT IMPLEMENTED /
NOT VERIFIED**. Before incorporation, OS04 requires actual upstream release/hash/
modification evidence, all linked/transitive/TLS/CA licenses and notices,
compatibility duties, maintenance owner and replacement path; O owns any authorized
inventory/lock changes. Before activation prove every A/AAAA safe and numeric-pinned
connect/peer before credentials, correct Host/SNI/certificate trust, DNS/connect2s/
TLS3s/read5s/request10s/job30s cancellation, no redirect/retry/proxy/netrc/cookie/
environment CA/keylog, and bounded wire cleanup. Reject override names without
reading values. Backend credential sign-off must prove authorized send-only
resolution, CR/LF rejection and no argv/env/query/disk/log/error leaks.

S09 remains a separate backend D/V, actual-client conformance, OS04, dated
operation/field rights and backend credential gate followed by authorized NEW
discovery evidence with attribution/time/pause. Fixtures and title hydration do
not satisfy it. O commissions independent actual implementation V and separate Q
before integration. There is no live, phone, runtime, H1/H2 or Phase2 completion.

## Reproduce verification

Use the existing readonly venv and [dependency guidance](../DEPENDENCIES.md).
From a clean candidate source copy away from the literal deployment root, with no
`.demo-data`/runtime store, independently measure Linux/local writable btrfs,
Python-linked and SQL SQLite >=3.51.3, `sqlite_source_id()`, same-device file,
directory and parent fsync, conflicting exclusive nonblocking flock, and an
existing caller-owned writable TMPDIR. Measure before any WAL/full suite. Make a
fresh child on that measured storage and export it as TMPDIR before starting
Python. Retain its `wal-execution.json` F11 PASS report; no skip is acceptable.

```sh
${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=2
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

Require all 121 baseline tests, H and M tests, zero skips, check success and no
migration changes. No test labels, alternate ROOT/settings/loader, keepdb or runtime
bypass. F01–F15 and T01/T02/T07/T09/T11 plus T05's parse-deadline companion run
through normal discovery; H's guard tests remain intact. No hidden scripts or
worker artifacts are prerequisites. Rollback removes these additive pure modules,
fixtures and docs after dependent work is withdrawn; no schema or store rollback
is introduced. Engineering quality and independent review follow
Private project coordination documents are omitted from this development export.
