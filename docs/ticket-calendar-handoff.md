# Ticket and calendar handoffs

This source-only candidate provides callable, immutable Python contracts in
`shelf.ticket_handoff` and `shelf.calendar_handoff`. It imports the existing
`ContractError`/`Blocked` errors and bounded-text validator. There are no routes,
models, migrations, templates, dependencies, network calls, purchases, calendar
writes or invitations. The existing deployment and source frameworks are unchanged.

All verification is synthetic. A fixture receipt proves a local transition, not an
actual order, issued ticket, installed pass, calendar entry or delivered invitation.
The real calendar targets, provider eligibility, grants and current approvals remain
for the authenticated root integration to resolve. The parent already resolved
Lisa's personal and work mailboxes privately; no event was selected and no invitation
authorized. Both must be supplied explicitly without embedding private addresses
in product source or tests. No tokens,
accounts or real recipient addresses are fabricated by these modules.

## Integration contract

`Event` holds exact event/session identities, title, offset-aware start/end, IANA
timezone, location, official source URL, source label, status and privacy. Unknown
dates/timezone/status and unverified sources can be previewed but block approval.
Initial cancelled events also block. Source verification and issuer verification
are **trusted caller attestations**, not facts established by URL syntax or a digest.
Discovery's partial event data must be resolved on the official source before use;
do not invent missing end times, venue identities, prices or eligibility.

`preview()` returns ordinary dictionaries ready for escaped Django templates.
They include source/privacy labels, exact details, blockers and a digest. Digests
bind intent; they are not authorization tokens, signatures or grants. Do not pass
request-body dictionaries into `from_dict()` as authority. Authenticate the actor,
recheck current grants and bind the displayed digest/revision to a CSRF-protected
explicit user command. No GET, discovery, save, purchase approval or export implies
an Add, Share or Invite.

The root must store the complete `to_dict()` representation privately and reload
with `from_dict()`. Schema 1 rejects missing/extra top-level fields and inconsistent
states. Records are JSON-compatible and contain no runtime-generated secret. Ticket
storage includes private issuer references and possibly a private Wallet delivery
URL; do not log it or expose it as a preview. Calendar storage includes explicitly
resolved recipient details. Use existing authenticated storage, with access and
retention rules appropriate to these private records.

These modules deliberately do not add a database or another persistence framework.
The root must serialize read/modify/write using a transaction with a row lock or
compare-and-swap on the **entire stored version**, including approval/receipt changes
that do not increment the event/offer revision. Save a reservation durably before
showing a human final-click handoff or starting any later external calendar work.
If saving fails, do not expose instructions or perform I/O. If the outcome or save
acknowledgment is uncertain, reload/reconcile; never reset the receipt. Pure objects
alone cannot prevent concurrent callers from acting on two stale snapshots.

Retain and reuse request IDs and receipts. Enforce uniqueness on ticket `active_key`
(actor + event + session), including unresolved or already confirmed orders; a new
request ID, price or quantity must not bypass an existing order. Additional intended
purchases require a separate root reconciliation/approval policy, absent here.
For calendars, retain one evolving intent per actor/event/session and use its UID
and resolved calendar target to upsert. Creating another object is not a new approval
for the existing external event. No persistence pruning/reset/retry escape hatch is
provided; calendar receipts are bounded at 200 and then fail explicitly.

## Tickets and Wallet

`TicketOffer` requires quantity, a three-letter currency, exact decimal-text total
including fees, official seller link and seller label. There is no rounding, exchange
rate or fee calculation. The root must verify all fees, current availability, dates,
source freshness and the exact checkout page. Both seller verification and known
`shop_eligibility='eligible'` are required. `unknown` and `unsupported` are blockers
for every adapter, including manual handoff; manual mode is not a Shop bypass.

| Callable | Local result |
| --- | --- |
| `TicketHandoff(actor_id, request_id, offer)` | Requested intent, no purchase |
| `approve(actor_id=..., expected_revision=..., preview_digest=..., user_confirmed=True)` | Exact approved intent |
| `revise(offer, actor_id=..., expected_revision=...)` | New revision; clears approval before submission |
| `reserve_submission(...)` | Receipt plus human-only instructions, or existing receipt plus `None` |
| `confirm_purchase(...)` | Issuer-confirmed purchase, still no issued ticket |
| `record_ticket_issued(...)` | Separate issuer issuance reference bound to the order |
| `record_wallet_available(...)` | Trusted issuer pass availability, still no user addition |
| `confirm_wallet_add(...)` | Explicit user report for the exact issuer artifact |

`reserve_submission` requires the latest exact `current_offer`, revision, digest,
separate `human_final_click_authorized=True`, and explicit
`adapter=ManualOfficialTicketAdapter()`. The default `TicketAdapter` blocks.
The manual adapter returns instructions for the official-ticket remote desktop;
only the human may click the seller's final purchase control. It performs no browser
or payment action. The app never requests, accepts or stores card/payment credentials.

Reservation deliberately records `purchase_state='submitted'` and
`submission_outcome='unknown'` **before** displaying final-click instructions. The
preview says this is a reservation with unknown outcome, not payment evidence. If the
human never clicks, a crash occurs, or the seller response is lost, repeated calls
and JSON/process restarts return the same receipt without another handoff. Submitted
or confirmed purchases cannot be revised into fresh purchase intents. There is no
automatic retry or “failed, buy again” transition; root reconciliation is required.
Issuer confirmation must match the approved digest; ticket issuance must match the
confirmed order. Repeated identical confirmations are idempotent; conflicting ones
are rejected.

`WalletArtifact` accepts only `issuer_pkpass` or `apple_wallet_url`, a safe HTTPS URL,
an independently trusted issuer origin and explicit verified issuer provenance. The
URL must match that origin. These labels do not verify signatures or perform a
fetch; root verification remains necessary. No `.pkpass`, barcode, substitute ticket,
copy of a protected/rotating barcode, or Wallet installation is generated. Private
issuer URLs are excluded from previews/reprs. An artifact change clears the user's
prior addition confirmation. The only addition flag is
`user_wallet_add_confirmed`; there is no asserted “Wallet installed” state.

## Calendar commands and ICS

`CalendarHandoff(actor_id, request_id, event, modified_at)` provides exact previews for
`export`, `add`, `share` and `invite`. Add needs a `CalendarTarget`; Share and Invite
also need a separately resolved `Recipient` (stable identity, label and mailbox).
The command, actor, target, recipient, revision and exact event are all included in
the approved digest. Recipient fields are forbidden on Add and Export. Nothing
resolves Lisa or chooses a calendar automatically.

`preview('share' or 'invite', target=..., recipients=[...])` additionally accepts an
explicit list/tuple of 1..10 resolved `Recipient` values. It preserves every mailbox
and label, including two mailboxes with the same person ID. It rejects duplicate
mailboxes, mixed singular/list arguments and recipient lists on Add/Export. The
returned `recipients` and per-mailbox `previews` visibly retain both supplied
addresses. This is a read-only grouped contract, not an integrated UI or bulk send.
The group digest cannot approve any command; each member preview retains the
existing separate exact approval. Adapters remain blocked by default. Recipient
resolution does not establish calendar access or permission to invite.

The existing 42 ticket/calendar tests already cover exact approval, unknown outcome
and restart deduplication, issuance, issuer Wallet links/user confirmation, distinct
calendar commands and ICS privacy. They remain unchanged. Five new recipient-list
tests cover the missing grouped-preview behavior using only `example.invalid`
mailboxes. Native Shop eligibility for Ticketmaster AU/Moshtix remains unverified:
the existing eligibility blocker also blocks manual handoff, with no bypass.

`approve(...)` returns `(new_state, receipt)`. Repeating the same exact command,
including with a different click key, returns the existing receipt. Reusing a key
for a different command is rejected. `reserve(key, actor_id=..., adapter=...)` returns
`(new_state, receipt, fresh)`: only `fresh=True` permits a later root operation after
the new state is committed. The default `CalendarAdapter` blocks all external work;
no enabled service adapter is supplied. Unknown submissions survive restarts and
block another command to that target/recipient until an independently verified
`confirm(...)`. Renaming a target does not bypass that blocker. A synthetic adapter
in tests only admits the local reservation and performs no service operation.

`export_ics(key, actor_id=...)` requires a current, separately approved Export receipt
and returns UTF-8 bytes. Export does not mark a calendar entry as added or confirmed.
The RFC5545 representation uses CRLF, escapes TEXT values, and folds at 75 octets
including continuation whitespace without splitting UTF-8 characters. Text control
characters and unsafe/credential-bearing URLs are rejected before rendering; public
source/seller links omit query strings and fragments. Exported source labels remain
visible, with `CLASS:PRIVATE` by default. The export contains no order references,
actor identity, calendar identifier or recipient address.

ICS always uses `METHOD:PUBLISH` with no ATTENDEE or ORGANIZER, even after a separate
Invite approval. It is not a scheduling invitation. The stable UID hashes the actor
and event/session identity, so it survives revisions, restarts and new request IDs.
`SEQUENCE` follows the event revision and `DTSTAMP`/`LAST-MODIFIED` use the explicit
revision timestamp, giving byte-identical repeats. `revise(...)` preserves event/
session identity and invalidates old approvals. Export updates use the same UID and
a higher sequence. A cancelled revision after a prior export approval or confirmed
Add exports `STATUS:CANCELLED` with the original exact times. It does not send
`METHOD:CANCEL` or remove anything from a real calendar. Import clients differ in
whether repeated imports update/remove existing events; no client behavior is claimed.

Times are explicit instants with offsets, converted into the IANA zone for previews
and UTC `DTSTART`/`DTEND` in ICS. UTC avoids floating time and the need to invent
VTIMEZONE rules; the original zone is also retained in an extension property.
`local_time(...)` rejects nonexistent DST times and date-only input; overlapping local
times require an explicit `fold=0` or `fold=1`. Never assume a timezone or midnight for
an unknown date. Naive event timestamps and subsecond precision are rejected.

## Minimal synthetic export example

Run from `backend` with the prepared runtime. This example makes local bytes only;
all identities, venue and source assertions below are fictional test inputs.

```python
from shelf.calendar_handoff import CalendarHandoff, Event

fixture = Event(
    event_id='fixture-event', session_id='fixture-session', title='Synthetic concert',
    start='2027-01-15T20:00:00+11:00', end='2027-01-15T22:00:00+11:00',
    timezone='Australia/Melbourne', location='Synthetic venue',
    official_source='https://events.example.invalid/concert',
    source_label='Synthetic source fixture', status='scheduled', source_verified=True,
)
state = CalendarHandoff('fixture-actor', 'fixture-request', fixture,
                        modified_at='2026-10-28T22:00:00+00:00')
preview = state.preview()  # render this exact preview before the explicit command
state, receipt = state.approve(
    actor_id=state.actor_id, expected_revision=preview['revision'],
    preview_digest=preview['digest'], key='fixture-export', user_confirmed=True,
)
# Integration must atomically persist state.to_dict() before acknowledging approval.
ics_bytes = state.export_ics(receipt.key, actor_id=state.actor_id)
```

## Verification and limitations

The original frozen package added five module/test/documentation files. Background
integration adds the minimal recipient-list preview and its separate tests; shared
models, views, URLs, templates and the original 42 tests remain unchanged.
The prepared Python 3.14.8 runtime is
used without dependency installation. `${PRIVATE_RUNTIME_PATH}` records the
actual Linux/local writable btrfs mount and same-device identity, Python-linked and
SQL SQLite 3.54.0 (minimum 3.51.3), SQL source ID, actual file/directory/parent fsync,
and conflicting exclusive nonblocking flock. These were measured before any WAL
execution. The scratch child is owned, writable and mode 0700.

From the candidate root, set `TMPDIR` to the exact measured fresh child recorded in
that file before starting Python. The author commands are:

```sh
# Focused pure tests, from backend/ (TMPDIR remains absolute):
${PRIVATE_LOCAL_PATH} -B -m unittest shelf.tests.test_ticket_handoff shelf.tests.test_calendar_handoff -v
# Full suite, from the candidate root, once after final source/docs edits:
${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=2
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

Exact commands, counts, exits, baseline hashes and results are recorded in
`${PRIVATE_RUNTIME_PATH}`; logs and the actual WAL evidence remain under
`${PRIVATE_RUNTIME_PATH}`. No runtime store, credentials, live connectivity, payment, Wallet,
calendar, invitation, deployment or real-user acceptance was tested. Cross-process
fixtures exercise private JSON receipt restoration and duplicate refusal only.
There is no migration/rollback action: this bounded package has no storage or route
integration to activate. Future persistence, UI, provider reconciliation, permissions
and resolved personal targets remain explicit root work. No license is selected and
no publication readiness is claimed.

Private project coordination documents are omitted from this development export.
this task expressly prohibits child agents and has a sole author. Author inspection
and synthetic tests are not an independent quality PASS.
