# Supplied event availability contract

`shelf.event_availability` is a pure bounded reducer. It accepts normalized event
records and supplied evidence; it does not fetch, poll, schedule, send notifications,
invent inventory or act on a purchase. UI and any future authorized persistence or
live adapter remain separate. A returned warning is data, not a delivered alert.

`SellerObservation` requires `AvailabilityScope`, typed `SellerStatus`
(`seller-low`, `almost-sold-out`, `available`, `sold-out`), an `EventEvidence`
reference, the exact observed event start, confidence in [0, 1], and a freshness
window. Evidence retains source ID, source event ID, official URL, snapshot hash,
position, provenance and checked-at (`retrieved_at`). The reference must match an
exact ID or URL in normalized event evidence; fixtures cannot become source evidence.
Attribution/confidence are trusted caller assertions, not authentication by this code.

Scope retains explicit session, tier and seller allocation IDs. These must be stable
identities, not ambiguous display labels. The most specific dimension labels the
scope; absence of all three means an explicitly event-wide seller assertion. The
reducer never promotes an allocation/tier/session statement into a whole-event fact.
Primary and resale scopes have independent history. There is no stock-count field,
count estimation, countdown or urgency instruction.

`EstimatedRisk` is separate, with an explicit basis, checked time, event start and
confidence. Its uncalibrated policy score is bounded to [0, .75], confidence to
[0, .5]. These conservative caps are policy, not calibrated probabilities. No score
is computed from sales, a capacity guess, repeated observations or presumed demand.
Scores >= .5 are the elevated band; lower scores are quiet. Within-band changes do
not repeat warnings. Fresh seller evidence takes precedence over a prior for the
same exact scope; both remain available in returned views.

`reduce_availability(events, signals, now=..., interests=..., previous=..., limit=9)`
reuses event reconciliation/date handling. Inputs must be materialized lists/tuples:
at most 100 events/evidence entries under the existing reconciler, 100 signals and
100 `EventInterest` values. `AvailabilityResult` returns typed `views`, `warnings`
and `memory`. Warnings include source/reference, checked-at, scope and confidence.
Date/cancellation notices use event evidence; their confidence is unknown (`None`)
because event records supply no confidence.

Freshness defaults to 900 seconds, bounded to 1..86400 seconds. Future checked times,
stale seller/event snapshots, past and unknown dates cannot produce current-stock
warnings. A fresh cancellation/postponement or changed date replaces stock advice
with one event notice, even if old stock evidence is stale. An old-date observation
cannot describe the new date; new evidence must be supplied. Date-only values remain
dates; equivalent timestamp offsets are not treated as a schedule change.

Latest observations win per exact scope/type; conflicting records at the same checked
time need caller resolution. Saved events precede high-fit events (explicit fit >=
.8), then other events. Save is priority, not enjoyment. At most nine notices are
returned. Unshown notices do not enter memory, so an output budget does not silently
discard them. Displayed material state suppresses repeats; time/confidence jitter
and stale-to-fresh refresh alone do not re-alert. Available stock is quiet initially
and becomes a neutral update after a scarcity warning.

Pass returned `memory` into the next explicit evaluation. Preserve exact source alias
continuity in supplied records across source reconciliation. The reducer does not
persist history or enforce actor scope: the authenticated caller stores it privately
per user/context and rehydrates typed values. The 200-entry memory bound fails
explicitly rather than evicting suppression history. No pruning policy or automatic
retry/notification loop is implemented.

Synthetic tests in `test_event_availability.py` use the existing official-snapshot
normalizer's fixtures. They prove local semantics, not current stock, probability
Private project coordination documents are omitted from this development export.
