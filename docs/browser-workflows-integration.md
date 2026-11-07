# Browser workflow integration (P3.9)

This source combines the reviewed P3.6 event-plan flow with the separately
reviewed social-discovery and WhatsApp rehearsal increments. It appends their
models/routes and merges navigation links; independent feature modules are
copied verbatim. Migration 0013 joins the additive 0011 and 0012 branches with no
operations. Existing user data, permissions and private keys must be preserved.

The integration also keeps ticket/calendar outcome reconciliation available
after the 200-action cap. New work remains capped. Only recent replay metadata
rotates, bounded to 200; original order, approval, dispatch and outcome evidence
remains intact. A pruned old form fails the current nonce/version and cannot
repeat a command. Meaningful HTTP regressions cover purchase and calendar
unknown-to-confirmed checks at the limit without a second submission.

The social and WhatsApp screens remain explicitly disconnected from real
accounts. Household social source selections can be prepared, but no browser
read happens until a separately implemented, permitted browser adapter is
activated. Signing in alone does not activate it. WhatsApp supports a complete
fictional invitation/reply rehearsal only, with exact selected chat/message
approval and conservative uncertainty. No messages or calendar invitations are
sent by this source. See [social discovery](social-discovery.md),
[WhatsApp rehearsal](whatsapp-rehearsal.md) and [event planning](event-planning.md)
for provider boundaries, evidence retention and live activation prerequisites.

Use the existing pinned Python environment and normal guarded test command:

```sh
${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=1
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

The ticket and message demonstrations run through real Django requests with
ephemeral databases and inert snapshots; use `scripts/demo_event_plan.py` and
`scripts/demo_messages.py` with output under the candidate's `.work` directory.
No candidate web server or browser-session workaround is introduced. Visual
acceptance and real-account end-to-end operation remain unverified.

An authorized promotion must back up and verify the private store, apply exactly
0011/0012/0013, and compare existing rows and keys before restarting. Keep the
current bounded demo expiry; this integration does not authorize extension or
automatic renewal. Code rollback must retain new receipts/tables and migration
history; never overwrite later user work with an earlier database backup.
