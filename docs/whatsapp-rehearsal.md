# WhatsApp invitation and selected-reply rehearsal (P3.8)

This increment is an authenticated **fictional rehearsal**, separate from ticket
booking and calendar invitations. No real account is linked; no real messages or
contacts are read; no messages, social actions, purchases or calendar changes are
performed. The backend contains no browser transport, session cookies, phone
numbers, account credentials or personal-account API integration.

## Review the journey

Select the owner profile, then **WhatsApp · rehearsal**. Choose one observed
fictional direct chat and exact event session. Two Alex entries deliberately make
name-only selection insufficient. The preview includes the chat identity, purpose,
event/session, start/end with offsets, timezone, venue, official-source fixture,
all-in one-person price and fees, quote expiry, and complete outgoing text. This
chat invitation reserves no ticket and creates no calendar event.

Approve the exact preview to queue it, then separately simulate one send. The
fictional `unknown_then_sent` scenario first retains an unknown outcome; **Check
existing fictional evidence** reconciles the same receipt without another send.
`sent` means a simulated matching send acknowledgment, never delivered or read.
`failed` is terminal. `sending` after interruption means the outcome is uncertain,
not permission to resume or retry sending.

For replies, select a single fixture chat and review its one selected incoming
message. The app does not import an inbox. A neutral editable draft is created;
incoming instructions do not choose recipients, disclose content or grant send
authority. The Sam fixture intentionally contains hostile text and HTML to make
this boundary visible. All custom replies receive the stricter review: the normal
recipient/purpose/body approval, a separate sensitive-content acknowledgement and
the exact typed observed recipient identity. There is no claim to automatically
detect sensitive content. Saving an edit removes all prior approval.

Approval expires after 15 minutes, and invitations also require a current quote.
Refreshing the quote resets approval. Login-required, challenge and ambiguous
recipient fixture states pause approval/dispatch. These fixtures cannot be
switched to ready by an action; their blocking state is retained for inspection.
There is no live login/link action in this increment.

## Implementation and limits

* `message_views.py` uses bounded Django forms, owner/profile grants, persisted
  profile selection, CSRF middleware and escaped server templates.
* `messages.py` owns draft creation, approval, complete-state compare-and-swap,
  replay receipts and dispatch/evidence checkpoints. There is one private intent
  per actor/profile/exact chat/session or selected message. Repeated creation
  returns that intent; it cannot bypass a failed or unknown send.
* `message_provider.py` supplies three fictional observed direct chats, one
  selected message per chat and deterministic fixture evidence. It has no network,
  browser or account I/O. Non-sample provider requests are blocked.
* `MessageDraft` / additive migration `0012_message_draft` persists exact selected
  context, outgoing text, purpose, recipient, approval, nonce receipts, one dispatch
  key/attempt and matched evidence. The database constraint permits sample only.

Every accepted action consumes a version-bound nonce. Replaying an identical
form whose receipt is retained returns its receipt; using it for another action
conflicts. A pruned form is rejected as stale by its version and nonce. A complete-state
CAS also rejects changes made without advancing the version. Dispatch commits
`sending` and its one attempt before calling the fixture adapter. No database
transaction spans adapter I/O. Current actor/profile grants and persisted profile
selection are checked before reservation, immediately before I/O and before
accepting the result. Revocation or a late/conflicting result retains the uncertain
checkpoint. Exceptions contain no provider payload in stored evidence. Reconciliation
has no send call and requires matching dispatch key, exact content digest, chat
identity and fixture reference. Delivery/read status remains unknown.

Browser textarea CRLF and CR line endings are normalized to LF before validation,
storage and approval; dispatch/evidence digests bind that exact normalized text.
Other control characters remain rejected. Message bodies are bounded to 2,800
plain-text characters; forms to 16 KiB; command replay receipts and timeline entries
to 200 each. New edits, approvals and dispatches stop at these bounds. Dispatch
reserves capacity for its evidence result. Evidence-only reconciliation remains
available for an uncertain dispatch at either bound: it retains only the latest
200 command receipts and timeline entries. This is a bounded recent audit, not a
complete history. Pruning never removes the separately stored exact approved text,
approval, original dispatch key/content digest/one-attempt checkpoint, or latest
matched outcome evidence. Checking an existing receipt cannot send again.
There are no attachments, groups, bulk actions, contact resolution, background
polling, automatic retries, inbound webhook handling, redrafting after a terminal
send, or account-wide message history. This is a bounded synchronous workflow.
Selected context and draft text remain private local records; a production
retention/deletion design is still required before storing real conversations.

## Reproduce verification

Use the pinned environment on Omarchy from the isolated candidate directory,
with `${PRIVATE_RUNTIME_PATH}` owned by the author and mode 0700. First run the copied
read-only prerequisite probe, which verifies Linux/btrfs, SQLite, fsync and flock.
Do not start a candidate server or override the runtime path boundary.

```sh
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B ${PRIVATE_RUNTIME_PATH}
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B ${PRIVATE_RUNTIME_PATH} shelf.tests.test_messages
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=1
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B scripts/demo_messages.py ${PRIVATE_RUNTIME_PATH}
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

The demo exercises actual request handlers, forms, templates and persistence with
Django's test client and ephemeral in-memory database. It exports eight inert HTML
snapshots plus `journey.json`; buttons, inputs, forms and navigation are disabled
or removed. Ordinary scripted Django tests bypass CSRF; a separate enforced-CSRF
test proves rejection. The demo is not browser usability acceptance or live
WhatsApp evidence. No deployed database is read or migrated by these commands.

## Integration and rollback

The source baseline is the 219-file booking candidate digest
`f534e9a5c64958dd10fdb7f9b921c8e46624585e46f97d17e78f412d267502fe`.
This package owns only the messaging candidate and evidence. It changes the shared
model file by appending `MessageDraft`, adds message routes to `provider_urls.py`,
adds a navigation link to `provider_base.html`, and adds bounded message text styles
to `provider.css`. Integrators must merge those shared files, not overwrite another
worker's changes.

Migration 0012 depends directly on 0010. The social work package owns a parallel
0011 migration. When combining, add an explicit merge migration depending on both
leaves; do not rename or silently replace the social migration. Run the combined
migration/model checks and suite before promotion. No live migration or deployment
has been performed by this package. Reversing 0012 deletes drafts, approvals and
dispatch evidence; preserve the table and records during rollback. Old binaries
can ignore this new table, but must not reinterpret unknown receipts as permission
to resend.

## Remaining live activation work

Live personal WhatsApp integration is substantive and remains unavailable. the owner
will link the personal WhatsApp Web session on the Mac later. Any future work must
establish that ordinary supported Web UI use is permitted, operate inside that
authorized session, observe an exact unambiguous chat identity and selected context,
obtain explicit recipient/purpose/content approval, and pause on login/challenges.
No claim is made that a personal account has an arbitrary API or reliable unattended
browser transport. No reverse engineering, private API bridge, cookie/session
extraction, or backend browser-credential storage is authorized or implemented.

Live evidence must be independently grounded in the observed Web UI and tied to
the exact draft and recipient; opening a chat or clicking a button is not send
confirmation. A timeout or ambiguous result needs conservative human-assisted
reconciliation, not an automatic retry. A verified real source/session/price,
retention policy, reliable allowed identity/evidence observations, real browser
acceptance, separate independent functional/security and Astra maintainability
reviews, and separately authorized deployment/live actions remain open gates.

No dependency, framework, license or provenance change is introduced.
