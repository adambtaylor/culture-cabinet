# Supplied cross-media feedback bridge

`backend/shelf/cross_media_feedback.py` is an additive, pure adapter and ranking
read model. It has no database queries, migrations, HTTP routes, provider calls,
disk writes or preference initialization. It consumes explicitly supplied human
history and separately supplied relationship evidence. It does not initialize
the owner's preferences. All new regression identities, feedback and credits are
synthetic; passing them establishes no live graph coverage or recommendation
quality.

## Input contracts and reconstruction

Use the existing `discovery_sources.Candidate` for music/events. For validated
book/movie/TV/work/edition identities, `catalogue_candidates(Cache(...))` projects
the existing cache nodes to that same dataclass. No title matching, alias guessing,
provider-root conversion or metadata-derived enjoyment occurs. Cache identity
resolution and neighbor traversal remain the responsibility of `cross_media.py`;
the bridge does not rewrite those contracts or automatically import its edges.

The caller must first authorize the current actor/profile, materialize complete
accepted histories and supply the current revisions. This module does not
authenticate provenance strings or accept untrusted request bodies as proof of
humanity. Materialized lists/tuples are required; a QuerySet or generator is
rejected rather than evaluated. The supplied actor/profile/mode must match across
snapshots. `rank` requires an exact `expected_revisions` map for the supplied
streams and raises `ContextMismatch` for stale, missing or conflicting snapshots.
The caller owns the freshness/authorization check around use of the result.

| Adapter | Supplied contract | Preserved semantics |
| --- | --- | --- |
| `from_intake` | Existing durable run dictionary and actor/profile/run/revision context, plus explicit `mode` | Calls `cross_media_ranking.replay`; latest correction replaces a choice, undo clears it, archived runs reject. Imported session provenance and unknown original response times remain unknown. |
| `from_discovery` | Dictionary with `id` (string), `actor_id`, `profile_id`, `mode`, `revision`, and actual `DiscoveryState.history` | Calls `discovery_reducer.replay`; undo restores the prior operation, including correction/save/restart, and restart clears the current projection. History's human origin, mode and timestamps are checked. |
| `from_feedback_events` | Complete materialized `FeedbackEvent` stream for one explicit target/signal/scope, plus its current revision and an independently mapped candidate `key` | Calls existing `feedback.reduce_events` and `validate_body`; reversal, explicit clear and erasure retain their existing meanings. Only an effective `origin='direct'` statement contributes. Imported, legacy and attributed values cannot establish enjoyment here. Requires normal Django initialization but performs no queries. |

`FeedbackSnapshot.revision` always retains the actual originating stream revision.
`Reaction.revision` retains the effective typed feedback event revision, even
when a later reversal restores it. Durable intake/discovery store positions rather
than event revisions, so these reactions retain `revision=None`, a stable original
history-position reference and the original response time. Index + 1 is never
presented as a revision. Reactions retain original scalar values (including 4
versus 5 and interest maybe versus yes); classification never replaces storage.

For typed numeric enjoyment, 4–5 enables reinforcement, 1–2 is negative and 3 is
neutral. This is an explicit bridge policy on the existing experienced 1–5 scale.
Anticipated yes/maybe remains `interested`, anticipated no remains `uninterested`.
Unknown, saved, skipped, opened, familiarity and completion remain distinct
read-model responses. They do not create enjoyment or attendance. Constraint text
is not interpreted or exposed as a derived feature. No dietary or other personal
constraint inference is supported.

Event enjoyment additionally requires explicit human attendance supplied through
`from_discovery(..., attended_keys=(event_key,))` or
`from_feedback_events(..., attended=True)`. The caller must retain the originating
attendance evidence; the bridge carries only that assertion, not a new attendance
history. Event dates, completion, source metadata and saves cannot supply it.
The existing discovery history has no standalone attendance operation, so its
enjoyment response alone deliberately does not activate event-to-music transfer.

## Edges, scores and lane fairness

`FactualEdge` and `FeatureEdge` are distinct frozen dataclasses. Both require an
explicit source, nonempty evidence reference/text and finite confidence in (0, 1].
These are supplied assertions; the module does not verify a source or fetch a URL.
Confidence is an input weight, not a calibrated probability or measured quality.

| Edge | Endpoint contract | Traversal |
| --- | --- | --- |
| Factual `lineup` | Event → artist | Bidirectional |
| Factual `recording-artist` | Recording → artist | Bidirectional |
| Factual `adaptation` / `shared-work` | Two narrative identities (book/movie/TV/work/edition) | Bidirectional; source relation direction remains in explanations |
| Inferred feature | Narrative identity → gallery/art-fair event | Forward only; terminal, never a new human seed |

Allowed thematic features are `visual-art`, `live-performance`,
`speculative-fiction`, `history` and `nature`. A common title, venue label,
provider suggestion or feature name without supplied evidence never creates an
edge. Artist credits and an event lineup are factual assertions; the ranking
transfer across them is still a recommendation inference, not a claim that the
person enjoyed every performer. A shared-work edge needs an explicit shared-work
reference; it is not generic similarity.

The graph permits at most two factual hops and one terminal feature hop, with
512 allocated path states per root, 512 candidates and 2,048 supplied edges.
This stays within the existing two-fact/one-neighbor traversal budget; this bridge
does not additionally traverse provider-neighbor edges. Duplicate exact edges are
discarded. The strongest path per root/candidate wins, with deterministic tie
ordering and original source/confidence/direction/hop evidence attached.

Scores are integer thousandths: start at 1,000, multiply by confidence and 0.5 per
fact or 0.1 for the terminal feature, rounding down. A root contributes at most
500; a feature path contributes at most 100. Across roots, inferred contributions
are capped at 150 and the combined score at 1,000. Scores are bounded ranking
weights, never new human feedback. Multiple lineups, adaptations, source copies
or paths from the same judged item cannot multiply that root's weight. Separate
current systems judging the same candidate retain their references in one lane.
A new explicit human enjoyment of a suggested item can create an independent root;
its provider lineage alone cannot do so.

Explicit negative/uninterested/hidden candidates are excluded and cannot be used
as intermediate vertices. A current negative from any supplied system wins over
a positive from another; no cross-system last-writer timestamp is invented.
Negatives do not infer dislike of other artists, genres or themes. Unknown or
skipped is not a negative. Already enjoyed items are not recommended again.
Event recommendations require a known aware start strictly after the supplied
`now`; this says nothing about availability, cancellations or ticket status.

Up to five enjoyed root identities are selected in stable key order. Each retains
its own lane, including empty inventory or a missing root card. A lane holds at
most 100 candidates ordered by its strongest path score, source rank and key.
Selection rotates over lanes, taking each lane's next unseen candidate. Shared
candidates retain every contributing membership but use one slot; duplicate or
exhausted pools cannot consume the next nonempty lane's turn. The maximum selection
is 50. The returned `path_budget_reached` flag reports truncation. Ranking does not
promise complete graph coverage. Empty input returns `needs-enjoyment` and no lanes;
existing broad exploration can remain a separate caller-owned result.

Every call reconstructs scores from current snapshots. There is no accumulating
preference vector to clear or migrate. Replaying the same restored history, source
cards, edges, revision context and evaluation time yields the same ranking.

## Integration call sites (reported only; unchanged)

No existing call site is wired to this module in this candidate.

* `discovery_views.index`, after `discovery_state.owned`, supplies the accepted
  `DiscoveryState` fields to `from_discovery`. Existing `candidates` supplies
  `Candidate` objects. A future integrator must separately supply verified lineup
  and attendance evidence; neither is present just because a provider card exists.
  Keep `discovery_reducer.select` as the existing broad exploration behavior.
* `provider_views._durable_action` / `durable_intake.hydrate` already operate on an
  owned run. A separate read path can supply that run's existing dictionary shape
  to `from_intake`, retaining actor/profile, run ID and revision. Do not append
  derived recommendations to durable history or change intake undo behavior.
* A read service alongside `feedback.feedback_context`, after its current
  authorization/recovery barrier, can supply complete already-reduced-policy
  `FeedbackEvent` streams to `from_feedback_events`. Keep signal-specific revisions
  and the existing `reduce_events` erasure/reversal rules. A current projection
  alone does not establish a direct-human origin. Target UUID → catalogue identity
  mappings must be explicitly verified by that caller, never guessed from titles.
* Existing `cross_media.paths_from` / `cross_media_ranking.rank` retain cache
  identity/neighbor behavior. A future caller may explicitly translate supported
  verified adaptation/shared-work facts into the new edge types, carrying their
  source evidence and an explicit confidence policy. This candidate supplies no
  automatic translation, registry fetch, UI merge or data migration.

Only current authorized snapshots from one mode/profile may be combined. Any
future caching must include the full revision map, graph snapshot and evaluation
time, and invalidate on undo/correction/restart/erasure. Sample-to-household
promotion is not provided. Removal consists of removing the new module, tests and
this document; there is no schema or stored preference rollback.

## Verification and limitations

Use the prepared `${PRIVATE_LOCAL_PATH}` runtime; do not install
dependencies. Follow [the scratch prerequisites](README.md#reproduce-verification)
before any WAL/full-suite execution. This candidate's measurements are in
`${PRIVATE_RUNTIME_PATH}`: Linux, owned private local btrfs scratch,
same-device/mount identity, Python-linked and SQL SQLite 3.54.0 (minimum 3.51.3),
the exact SQL source ID, successful actual file/directory/parent fsync and a
conflicting exclusive nonblocking flock. These are local measurements, not a
universal filesystem claim. There is no `.demo-data` in the candidate.

From `backend/`, the focused pure regressions are:

```sh
TMPDIR="$PWD/../${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B -m unittest shelf.tests.test_cross_media_feedback shelf.tests.test_cross_media shelf.tests.test_cross_media_ranking shelf.tests.test_discovery_reducer -v
```

Then, from the candidate root, use a fresh private child of the measured scratch
parent as `TMPDIR` before starting Python and run the full suite once after final
source/test/document edits:

```sh
TMPDIR="$(mktemp -d "$PWD/${PRIVATE_RUNTIME_PATH})"
export TMPDIR
${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=2
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

The normal entrypoint includes the additional typed feedback regressions in
`test_cross_media_feedback_events.py`. They use unsaved synthetic model instances
under `SimpleTestCase`, which forbids database queries. The existing whole-suite
entrypoint and all original tests are unchanged. Exact counts/results, F11 WAL
evidence, source-integrity verification and limitations are recorded in
`${PRIVATE_RUNTIME_PATH}`, with the author handoff in `${PRIVATE_RUNTIME_PATH}`.

No deployment, account, network, private data, real-user DB, credential or service
access was performed. No real preference was initialized, and fixtures do not
establish end-to-end/live acceptance. Independent functional and maintainability
Private project coordination documents are omitted from this development export.
does not commission child agents or claim an independent Q pass. No repository
license or publication readiness is asserted.
