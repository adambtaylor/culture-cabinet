# Supervised taste synthesis

Synergize records one explicit request for the current household taste snapshot.
The app has no model API key, backend inference grant or unattended inference
worker. A bounded internal weekly trigger can queue a pending-review snapshot;
it cannot reserve or execute inference. See weekly-synthesis.md. A queued receipt means **awaiting supervised Astra handoff**, never a
completed or mocked analysis. The supported operator route is a Codex
collaboration task with model `gpt-6-astra` and reasoning `high`.

## Integration and ownership

`shelf.taste_funnel.synthesis_snapshot(actor, profile_id)` resolves current owner
authority and returns exactly `actor_id`, `profile_id`, `mode`, `revision_map`,
`signals`, and `input_digest`. The digest is `shelf.calendar_handoff.digest` of
the other five fields (sorted compact JSON, ASCII escaped, no non-finite values).
It covers the entire revision map, including edits that leave visible signals
unchanged. Snapshot validation permits at most 500 minimal signals and 256 KiB
of JSON. Each signal has only `key`, `title`, `medium`, `source`, `basis`, `value`.
Typed bases include `interest`, `enjoyment`, `legacy_choice`, `favourite`, `social_intent`, `discovery_seed` and `authored_observation`. Authored observations carry a bounded qualitative summary, never a numeric rating; complete message provenance remains in the owned authored state and its revision fingerprint. Their medium remains explicit, including `graphic_novel` and `animation`. Assistant-origin suggestions are excluded from owner taste signals.
Interest/enjoyment require exact integers 1–5, favourite requires boolean `true`,
and legacy choice requires a nonempty string of at most 100 characters. Boolean
and string representations of a number are not numeric ratings.
Different bases may reference the same item; they must agree about its medium,
while captured titles/source labels may differ. Historical conflicts remain explicitly described strings.
No address, description, private message or whole browsing history is exported.

The authenticated POST calls `synthesis.request(request, profile_id,
expected_digest)`. It repeats current persisted session/profile/grant checks
inside a database transaction and captures the fresh trusted snapshot. A unique
actor/profile/input-digest constraint reuses queued, uncertain, failed and
completed receipts. Changing the snapshot requires another explicit Synergize
action; it does not schedule work. A failed request cannot be implicitly retried
by clicking the same input again.

`synthesis.status(run, current_digest)` is a read-only presentation helper for a
run already resolved by the caller under current authority. It returns
`awaiting_operator`, `running`, `completed`, `failed`, `unknown`, `stale`, or
`not_requested`. Only a current completed run exposes `output`; a completed
receipt for old input remains historical and returns `stale` with no current
output. Render all returned labels, titles and explanations with Django's normal
autoescaping. A receipt is never an authorization to select another profile.

## Operator handoff

The deployed CLI is fixed to
`${PRIVATE_LOCAL_PATH}`, the existing
SSH owner actor 1 (`demo-curator`), and profile 1. It uses the same server renewal
and expiry file/checks as `record_google_handoff.py`. It caps the operation at
30 seconds and rechecks the session and current grant at persistence boundaries.
Candidate paths cannot run this CLI against live state. The service functions
require an explicit session guard; there is no browser result-upload endpoint.

After the user's explicit Synergize request and within the current deployment
session, the trusted operator runs these from the literal deployed root:

```sh
.venv/bin/python -B tools/record_taste_synthesis.py reserve \
  --run RUN_UUID --renewal CURRENT_RENEWAL --expires BOUNDED_ISO_EXPIRY
```

Only the first reservation returns the bounded prompt and signals. Before that
return, the transaction commits `running`, `attempts=1`, the reservation revision
and time. The packet binds `run_id`, `request_identity`, `input_digest`, the full
`revision_map`, `reservation_revision`, `model`, and `reasoning`. The operator
passes the prompt/signals to one Codex collaboration task using the specified
model and reasoning. Input titles and source names are untrusted data, never
instructions; the task should not browse, use tools, or infer private traits.
No API client or credential lookup occurs in the app or CLI.

Repeated reservation returns `reconciliation_only: true` and identity metadata,
without the prompt/signals. It **does not authorize another inference**. If the
first export or task launch has an uncertain outcome, inspect the existing task
and retain or record `unknown`. Do not reset attempts or generate a replacement
task. A lost export may remain unresolved; availability is intentionally
secondary to avoiding duplicate billed work.

## Actual result receipt

The trusted operator obtains the real response from that exact reserved task.
The response output schema is deliberately small:

```json
{
  "basis": "inferred_taste_association",
  "clusters": [{
    "label": "Tentative theme",
    "members": ["exact:input:key"],
    "explanation": "A hypothesis about the submitted taste signals."
  }],
  "links": []
}
```

There must be 1–20 clusters (1–100 distinct input keys each), and at most 40
links. Links contain `from`, `to`, `explanation`; endpoints must be exact input
keys from different media. Unknown identities, duplicate links, additional
fields, absent clusters, excessive text, and factual-relationship basis labels
are rejected. A link is not required when the input cannot justify one. These
are **inferred taste associations, not factual relationships or sourced facts**.
Structural validation does not prove the semantic quality of an explanation.

The operator wraps this output in a JSON object containing the seven binding
fields from the packet, plus `evidence_id` (the real task/evidence identity),
`outcome: "completed"`, `reason: ""`, and `output`. Record it through stdin:

```sh
.venv/bin/python -B tools/record_taste_synthesis.py record \
  --run RUN_UUID --renewal CURRENT_RENEWAL --expires BOUNDED_ISO_EXPIRY \
  < actual-astra-receipt.json
```

The CLI accepts at most 128 KiB and rejects duplicate keys or non-finite JSON.
No arbitrary uploaded JSON can authenticate its model origin: authenticity
comes from the trusted operator observing the actual Codex task. Never label a
synthetic test response or another model's output as an actual Astra completion.

Failures/unknown outcomes carry `output: null` and one of `inference_failed`,
`operator_interrupted`, `response_unavailable`, `invalid_response` as `reason`.
Exact receipt replay does not change revision or timestamps. Conflicting final
receipts are rejected. An unknown outcome may be reconciled to a final result
for the same reservation, retaining the prior uncertainty evidence. Failed and
completed receipts are terminal; no automatic retry exists. Completion after a
later taste edit is retained only as a historical result.

## Validation, migration, and rollback

Migration `0016_taste_synthesis` depends on `0015_obtain_completion_status` and
adds only `SynthesisRun`. The service never changes an input snapshot after
creation. Durable evidence contains the original reservation, the latest
outcome, and at most one reconciled unknown record; this is a bounded receipt,
not an exhaustive operator activity log.

The focused `shelf.tests.test_synthesis` suite uses an ephemeral database,
actual Django HTTP/CSRF/session/grant checks, and synthetic operator results.
It forbids network calls and tests repeated requests, stale revisions/profile
changes, one reservation, forged input identities, wrong model/reasoning,
unknown/failed states, exact replay, and the fixed CLI boundary. Run it through
the normal isolated Django test runner; do not activate a candidate server.
The parent integration owns final application HTTP acceptance and any separately
identified real synthetic-input Astra inference proof.

Rollback disables Synergize/operator entry points while preserving the table
and all receipts. Do not delete reservations, reverse the migration after use,
reset `attempts`, or restore an older database to force a second inference.
Existing requested work requires explicit operator reconciliation, not a new
automatic run.
