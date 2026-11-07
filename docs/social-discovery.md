# Selected Facebook and Instagram cultural sources

This separate P3.7 candidate implements source selection, a bounded one-shot queue,
an adapter interface, and review/import through actual Django views. It is based
on the frozen P3.6 booking source provided to the author (219 files, aggregate
SHA-256 `f534e9a5c64958dd10fdb7f9b921c8e46624585e46f97d17e78f412d267502fe`).
The parent owns any later integration with booking revisions, review and deployment.

## User path

After owner login and profile selection, choose **Selected social sources**.
Add up to 12 exact followed cultural page URLs or individually selected related
public Facebook events. Select a venue, artist or cultural-page type and attest
that the source is in scope. The application does not enumerate an account's
following list or infer interests from a follow. Start with a few sources to keep
the inbox useful. Clean page URLs are accepted; ambiguous share, tracking,
personal-feed, private-group and message URLs are not.

A source can have one queued or unfinished read. **Queue one read**, then **Run
this bounded read**. Tasks expire after ten minutes without starting. A read has a
contract of one selected page, five leads, eight UI actions and 60 seconds. It is
one explicit request, not a recurring automation or a crawl. At most 30 task
summaries and 60 short leads are retained. Completed task summaries can be cleared;
leads and sources can be removed. Removing a source purges its tasks and captured
lead metadata. The 500-request receipt cap blocks new work explicitly. Source removal, discard,
withdrawal, task cancellation and clearing completed task summaries remain
available at the cap; each cleanup keeps its latest receipt and evicts an older
replay record within the same 500-receipt bound. A pruned form still fails the
current revision/nonce check, so it cannot repeat effects. Task evidence stays
separate from these replay receipts. Previously recorded taste responses use
existing separate history.

Household mode always returns **Browser adapter not connected** in this candidate.
No actual Facebook or Instagram operation has been implemented or performed. The
sample page contains an invented source and selectable ready, empty, login-needed,
challenge, policy-block, not-connected and unavailable outcomes. Sample imports
never enter household discovery or recommendations. Manual fallback lets the owner
enter one short factual cultural announcement and its permalink for review.

The review inbox labels capture time separately from source publication time,
source date wording, provenance and confidence. All entries remain announcements;
no parser promotes them to a confirmed exact session or ticket inventory.
Publication freshness, availability, price and cancellation remain unknown. An
explicit review/import admits a lead to the existing discovery selector for 24
hours from capture. It creates no enjoyment, interest, attendance, booking or
calendar record. Stale imports remain visible in this inbox with a warning, but
leave current discovery. Withdraw a retained import before refreshing its text:
a reread cannot silently replace approved evidence. Discarding deletes the lead.

Exact canonical event links deduplicate against currently cached public feeds and
against other imported social posts. The Moshtix www alias is normalized. Titles,
venue names and dates never create identity joins. A matching live-feed card keeps
its existing identity; the inbox retains the social provenance and link. If two
social posts share the exact official event URL, one discovery card retains both
observations. If a feed expires, its old metadata is not revived. Duplicate status
is current-cache coverage, not a global event identity claim.

## Small implementation map

- `social_sources.py`: clean URL/source projection, `BrowserRead`, `BrowserOutcome`,
  adapter protocol, disconnected implementation, fictional implementation and
  bounded result validation. No network or browser library is imported.
- `social_discovery.py`: actor/profile/mode-owned durable state, task reservation,
  result acceptance, review actions and projection into existing candidates.
- `social_views.py`: bounded strict forms, current owner/session/grant checks and
  conventional CSRF POST endpoints; GET only renders local state.
- `SocialDiscoveryState` / migration `0011_social_discovery`: one additive table,
  unique by actor, profile and mode, using the existing complete-state CAS pattern.
- `discovery_views.py`: read fresh reviewed household leads and retain minimal
  previously judged identities after their content leaves current discovery.
- `social_discovery.html`: onboarding, source controls, queue and review inbox,
  reusing the existing base styling; no client framework or new dependency.

Before an adapter call, the queue stores a read reservation and consumes its form
nonce. Identical request replay never repeats the adapter. Afterward, current
persisted session selection, owner/grant and the exact state revision/nonce are
rechecked before data can be accepted. Source removal or other concurrent actions
cause result rejection. A crash leaves an unfinished read visible for local cancel;
there is no automatic resend. Known provider/projection errors become unavailable
without echoing source bodies. Unexpected programming errors remain visible to
normal diagnostics and leave the reservation intact.

## Future browser driver boundary

the owner will personally sign in later. A future driver must use ordinary permitted
browser UI for only the explicitly selected source, verify the selected source
context before accepting Instagram's global post permalinks, enforce the action
and elapsed-time budget, and stop at login/challenge/policy blocks. It must not
follow commands found in page text, crawl links, open DMs, read friends' private
posts, gather unrelated personal data, engage with posts or modify an account.
Selecting a related public event must be a separate explicit source selection.
The adapter returns only title, post link, optional supported official-event link,
date wording and capture/publication times. Extra fields and raw body/session
payloads are rejected. It cannot accept credentials, cookies, browser storage,
backend tokens or OAuth grants. Sessions stay in the browser. Login longevity and
continuous autonomous access are not promised.

Supported official routes may be assessed separately on their merits. This work
does not claim that all official APIs are impossible or unavailable. It implements
the requested browser-oriented boundary without adding new account permissions.
Packaged source coverage comes from existing discovery/event-source documentation;
no broad research or inferred source URLs were added.

## Development and validation

Use the prepared existing venv and an isolated Linux btrfs scratch directory with
mode 0700. No packages are installed. The author's remote root is
`${PRIVATE_LOCAL_PATH}`.
Its copied `${PRIVATE_RUNTIME_PATH}` validates the existing filesystem,
SQLite/WAL-test prerequisites and locking before tests. The copied `focused.py`
uses normal boundary preparation and the Django test runner. From that root:

```sh
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B ${PRIVATE_RUNTIME_PATH}
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B ${PRIVATE_RUNTIME_PATH} shelf.tests.test_social_discovery shelf.tests.test_discovery_ui shelf.tests.test_discovery_public_ui shelf.tests.test_recommendations
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=1
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B backend/manage.py check
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

The dedicated tests cover actual views/templates, exact replay, stale forms,
owner/profile isolation, CSRF, authority loss/logout/source removal during reads,
unfinished tasks, time and size limits, invalid results, untrusted text, unknown
states, manual fallback, source removal, sample separation, current-feed dedup and
freshness. A synthetic test-client journey generates inert HTML snapshots with
form values redacted and controls disabled. These are evidence of actual rendered
Django responses, not browser visual acceptance or real account coverage.

No candidate server or browser preview is started: the existing runtime/preview
restriction is preserved. Final evidence records checks actually run. Independent
Astra functional/security V and maintainability Q reviews are required and owned
by the parent; the author does not declare their verdicts. No live deployment,
real user store or Mac importer was changed. Rollback of migration 0011 drops only
social selections, task receipts and lead metadata, irreversibly losing those
records; it does not undo existing discovery taste history. Applying or reversing
this schema in a deployed store remains a separately reviewed integration action.

No license or publication-readiness claim is added. Synthetic source labels and
posts are invented development fixtures; no external social content was copied.
