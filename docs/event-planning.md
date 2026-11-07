# Event planning candidate (P3.6)

This increment connects the existing ticket and calendar contracts to authenticated
Django screens and durable local storage. It is a **fictional rehearsal**. It makes
no purchases, provider calendar writes, invitations, contact lookups or Wallet
passes. Live source cards still link to source pages; they are not verified ticket
inventory. Nothing in this candidate grants the backend access to an agent's
connected accounts.

## Review the journey

In an approved provider runtime, select the owner profile, then **Event plans ·
rehearsal**. Choose one exact fictional session, a ticket quantity including the
owner, and explicitly selected fictional mailboxes. Two Alex entries deliberately
exercise mailbox disambiguation. A name alone is never recipient resolution.

Review price, currency, fees, refund restrictions and quote expiry. Approve those
details, separately rehearse human checkout, then check existing seller evidence.
The checkout reservation is **submitted / unknown**, even when a seller link was
opened. A confirmed response is explicitly simulated and retains its order and
ticket references. A failed response remains terminal; an unknown response is
reconciled without a second purchase attempt.

After booking evidence, approve the owner calendar action, then each selected
friend's exact invitation separately. Every preview identifies the event, dates,
timezone, venue, privacy and mailbox; attendees may see other recipients. Provider
acknowledgment means an invitation was submitted, not accepted. RSVP remains
unknown. Reschedule/cancellation fixtures retain the event UID and original order,
advance the calendar revision, and require fresh approval for each calendar or
invitation update. No cancellation is sent to a friend without a previously
confirmed invitation. Event cancellation is not an order cancellation or refund.

Use `unknown_then_confirmed` to exercise evidence reconciliation, or `failed` to
show a definite failure. Existing plans are reused for the same actor/session;
changing quantity or attendees on a second start conflicts rather than creating
another order. Editing attendees after creation, automatic source-update polling,
refund handling and retrying definite failures are intentionally not exposed yet.

## Run without a deployment

Use the repository's pinned Python environment (3.14 on Omarchy), from this
isolated candidate directory:

```sh
${PRIVATE_LOCAL_PATH} -B scripts/demo_event_plan.py ${PRIVATE_RUNTIME_PATH}
```

This uses Django's real request handlers, forms, CSRF-enabled middleware and an
ephemeral test database. The scripted test client bypasses CSRF tokens as Django
tests normally do; a separate enforced-CSRF regression checks rejection. The demo
exports six inert HTML snapshots and `journey.json`. It is not a running server,
live booking proof or browser usability acceptance. It neither reads nor migrates
the deployed database. Existing runtime path restrictions remain intact.

Full verification, after the project's filesystem/SQLite prerequisites:

```sh
${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=1
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

## Structure and persistence

* `event_plan_views.py`: strict bounded forms, authentication and ordinary server-rendered views.
* `event_plans.py`: one orchestration path around `TicketHandoff` and `CalendarHandoff`;
  complete-state compare-and-swap, version/nonce replay receipts, current profile
  grants and persisted-session selection checks before/after provider operations.
* `event_plan_provider.py`: explicit fictional sessions, quotes, contacts and evidence.
  The household quote provider is blocked; there is no backend calendar credential.
* `EventPlan` / additive migration `0010`: private actor/profile-owned contracts,
  quote metadata, resolved selections, command receipts and outcome evidence.
  No card details, barcodes or pass bytes are stored.

Before a calendar adapter call, the transaction saves `submitted_unknown` and the
one dispatch attempt. Repeated POSTs and new forms reuse that receipt. A crash,
timeout, revoked grant or stale result keeps the unknown state. Reconciliation
must read the existing provider result; it cannot invoke another send. Database
transactions never span adapter I/O. This remains a bounded synchronous workflow,
not a background job system. The plan caps action receipts at 200 and contacts at
10; ticket quantity must cover selected attendees and owner.

Ticket serialization is schema 2 to add verified failure evidence. Complete
schema-1 records remain readable; missing fields in schema 2 remain errors.
Earlier code cannot read schema-2 plans. Do not roll back binaries against these
new records without a reviewed compatibility plan. Migration 0010 adds only the
new table, but reversing it would delete event plans and receipts. Preserve the
table and data in any rollback. No migration has been applied to live data here.

## Live activation boundary

The remaining integration is substantive, not a toggle:

1. Resolve exact real event/session identity and official seller eligibility from
   permitted sources. Fetch an expiring quote with explicit all-in total, currency,
   fees, inventory, refund/cancellation restrictions and authoritative provenance.
   A source listing or ordinary link supplies none of those assurances by itself.
2. Obtain explicit approval of that quote, use the approved purchasing/human
   checkout path, and reconcile against actual seller booking and ticket evidence.
   Bind issuer evidence to this request and approved quote. Never interpret URL
   opening, a timeout, user-supplied verification flags or a screenshot alone as
   provider confirmation. No new payment account/API scope is granted here.
3. Resolve the owner calendar and contacts unambiguously with current read/write
   capabilities. Preview the exact selected mailboxes and notification/privacy
   consequences before a real action. Use the owner's event plus standard RSVP
   invitations; never directly write a friend's calendar.
4. Implement provider creation/update/cancellation and evidence reconciliation
   against stable provider IDs and this UID/revision. Persist returned IDs and
   revision evidence, handle conflicting changes and uncertain creates before
   retry. The fixture's deterministic IDs are not a real provider implementation.
   Agent Google Calendar tools expose read/create/update/delete capabilities, but
   their availability does not grant backend access. The read-only capability
   check returned no calendar identity/role; no target was selected. The inspected
   write schemas offered no explicit idempotency key, so blind retry is unsuitable.
5. Attach or link a Wallet artifact only from the confirmed ticket's verified
   issuer. Existing `WalletArtifact` validation remains reusable. This rehearsal
   has no Wallet pass, and never claims silent Wallet installation.
6. Obtain separately authorized deployment/migration and live actions. The prior
   live promotion/restart denial remains in effect; this candidate does not retry it.

No new dependencies, license choice, credential store or external communication
is introduced. Existing project notices and provenance remain unchanged. User
acceptance of the rendered flow and real-provider integration remain open work.
