# Availability-first restaurant rehearsal (P3.11)

This extends the existing `EventPlan`, ownership checks, version/nonce receipts
and ticket/calendar contracts. There is no second itinerary store, job system,
new dependency, credential or real booking adapter. An optional wish retains the
user's exact text, separately labelled from the fixture's suggested evenings.
This is the first occasion slice, not the complete authored-wish discovery hub.

## Try the slice

Open an event rehearsal plan and expand **What would make this evening work?**
Choose dining party size, vegetarian choices, a two-date window, preferred table
time, meal length, restaurant-to-event travel time and arrival buffer. The source
fixture explicitly reports Friday 18:30 booked out and Saturday 18:30 available.
At most three fresh, compatible alternatives appear, with the matching event
start/finish and exact restaurant branch. Travel minutes are user assumptions;
no route, location transmission or PTV availability is implied.

Choosing Saturday moves an unapproved ticket selection and its new quote to the
matching Saturday session in the same plan. Previously approved/submitted tickets
cannot move through this action. A different existing plan for that session
causes a conflict rather than a duplicate. Before any calendar submission, its
derived UID follows the new exact session; no dispatched calendar UID is changed.

Review the exact branch/address, date/time/timezone, party size, dietary request,
deposit/currency and cancellation/no-show terms. Approve those details, then
separately rehearse one reservation request. The unknown response scenario must
be reconciled using its existing request key. A failed result remains terminal.
No name, email, telephone, card or actual recipient is collected or transmitted.
Fictional menu evidence is not real dietary accommodation or an editorial review.

Search refresh clears old approval. Five-minute availability expires honestly;
missing/incompatible tables never become inferred inventory. Every selected
offer binds its full terms, party and branch. Every form binds the complete plan
preview. The durable `unknown` state and single send attempt are committed before
the reservation adapter call; crashes and stale responses cannot resend it.
Reconciliation remains possible after expiry and after the action-receipt cap.

Restaurant status is separate from tickets, calendar and friend invitations.
An event cancellation or incompatible reschedule warns that the restaurant needs
review, without claiming cancellation, update or refund. Restaurant cancellation,
rescheduling and calendar components are still unimplemented; confirmed receipts
are retained. Users can stop and leave the evening open; nothing chases them.

## Current booking routes, checked 29 October 2026

OpenTable's official [API documentation](https://docs.opentable.com/) requires
approved partner access. Its [Consumer API guidance](https://support.opentable.com/s/article/What-is-OpenTable-s-Consumer-API)
describes restaurant/group eligibility, program terms and fees, scoped credentials
and implementation approval. Cabinet has none of that access. The public seller
booking flow is a possible future human/browser handoff, not a connected API.

SevenRooms' [official API documentation entry](https://api-docs.sevenrooms.com/)
requires individually provisioned access and directs new integrations to its
partnership process. Its [integration overview](https://sevenrooms.com/platform/integrations-apis/)
describes reservation channels; it does not grant Cabinet credentials or a public
consumer API. No undocumented endpoint or authenticated session was inspected.

The adapter seam therefore fails closed for household mode. Live activation needs
a permitted official route for an exact restaurant, real availability provenance,
fresh all-in charges and cancellation terms, explicit scoped approval and verified
booking evidence. Unknown outcomes must be reconciled before any retry. Opening
a booking link is never confirmation. Development authorizes none of these live
actions or personal-data submissions.

## Verification and remaining work

Run the isolated HTTP demo with the project's pinned Omarchy Python:

```sh
${PRIVATE_LOCAL_PATH} -B scripts/demo_restaurant_plan.py ${PRIVATE_RUNTIME_PATH}
```

The script uses an ephemeral test database and produces four inert HTML snapshots
plus `journey.json`; it does not touch the deployed database. Regression tests
exercise alternative sessions, replay, stale approval, expiry, wrong evidence,
crashes, conflicting plans, access/CSRF and failure states. Existing ticket and
calendar regressions remain applicable. There is no schema migration; the optional
JSON component is ignored by the previous build. Rollback must retain its receipts.

Remaining date-night work includes real menu/review/source connectors, live table
availability and booking, supported updates/cancellation, optional drinks, separate
origins, verified PTV+bike rules/disruptions/last return/weather, whole-night cost,
calendar components and user acceptance. Beat Magazine remains a requested source,
not a fetched connector in this increment. No full-night readiness is claimed.

## Obtain failure discovered in live acceptance

The licensed Big Buck Bunny job was acknowledged by SAB but finished Failed:
`Aborted, cannot be completed`. No Mac import occurred. The observer previously
returned failure only to its worker while Obtain still showed queued. It now
persists exact-job failure with compare-and-swap, retains the binding, and displays
a useful failure explanation. Cancellation/concurrent changes cannot be overwritten;
missing/foreign/unreadable history stays pending/unknown. Terminal observation is
idempotent and cannot republish a completion registration or resubmit the download.
The full successful Obtain-to-Kodi journey remains blocked on an available lawful
release with valid selectable metadata; an old manual download is not that proof.
