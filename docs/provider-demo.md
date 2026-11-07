# Real catalogue R&D demo

This separate local entrypoint explores real film, series and book records. The
original fictional catalogue and its feedback/export/recovery routes stay outside
the provider surface. No JavaScript, external images or fonts are required.

From the authorized, prepared runtime root, the operator starts:

```sh
${PRIVATE_LOCAL_PATH} -B backend/manage.py catalogue_runserver --provider-demo
```

The command fixes the bind to **guest 127.0.0.1:8000**, without reload or threading.
Do not add a bind argument. It refuses the nested candidate source tree; do not
change the literal-root guard. This guide does not deploy or start the service.
The existing local account, profile grants and source access must already be
prepared by the operator. There is no default password or credential setup here.

1. Open the guest loopback page. Log in with the local `demo-curator` account and
   the operator-supplied password. Choose the available owner profile.
2. Choose **Start discovery** to fetch a broad round of up to nine films, series
   and books. A shorter pool or source outage may produce fewer results. Each
   card shows its medium, source, fetched time, exploration lane and source link.
3. Keep **Experienced it — enjoyed it**, **Interested — not experienced**,
   **Not for me** and **Don’t know it** distinct. Only explicit human responses
   affect taste. Search/add, provider suggestions and saved hearts do not imply likes.
4. When durable storage is prepared, **Continue current run**, response corrections,
   **Clear this response** and **Clear latest response (undo)** retain history.
   Undo clears the response rather than restoring a previous correction.
   Human responses and minimal judged-source provenance survive logout/restart.
   Provider descriptions, metadata and the unjudged pool expire after one hour.
   **Refresh source metadata — keep history** explicitly refetches a broad deck.
   Opening pages never fetches provider records. Counts, selector version, current
   run status and card selection reasons explain the changing round.
5. Search a favourite, add it, then respond. Expand an original human positive
   through **TMDB** or **Hardcover** neighbors once per temporary deck, including
   failed attempts. Provenance is root → provider neighbor source → candidate,
   not verified population co-likes. Media lanes remain independent; no semantic
   cross-media features or adaptation facts are verified. Source links retain
   attribution; missing verified trailers are explicit.
6. **Archive current history and start new run** retains the archived history and
   opens an empty run. Resume available nonarchived runs separately; histories do
   not merge. **Export human history (JSON)** downloads separate histories using
   a CSRF-protected POST. Archived runs remain in that export.
7. Without prepared durable storage, the catalogue honestly shows temporary
   prototype behavior: choices expire within one hour and reset has no undo.
   Preparation and migrations are operator work; this guide performs neither.

All source operations require explicit CSRF-protected POSTs with server-issued
revision/nonce. Reload stale forms before retrying. Provider library writes and
real downloads remain off. Use the actual logout POST when finished, then log in
and select the same profile to continue an owned durable run.

When the operator has prepared saved storage, a heart on a round card or search
result saves to **To watch**, **To read** or **Favourites** for the selected profile.
Saving is durable local intent, separate from enjoyment and human discovery responses. Repeating a heart is idempotent; it does not advance the round. Follow
**Saved items** to view or remove hearts. Controls stay hidden while storage is
unprepared; an attempted save then returns 503. After a new login, select the owner
profile again to reach existing lists and requests, even after intake expires.

From a saved title, open **Review obtain options**. Review the exact server-owned
title and fixed scope: **full film**, **full audiobook** (never ebook), or **series
S01E01 only** (never a season or full series). Enter an explicit edition label,
confirm your legal right to public-domain or licensed content, and acknowledge
that the local destination and capacity are unknown. The edition is your stated
selection, not verified provider metadata. Submit **Confirm local obtain request**
to record a `blocked_configuration` request and open its status page.

Hydra and SAB are verified to exist on the guest. Culture Cabinet acquisition is
not yet connected or authorized: availability, size and storage capacity remain
unknown. Confirmation never queues or starts a download. The status page and
**Your local obtain requests** show the immutable selection, status and reason.
Use **Cancel local request** to mark only that local request `cancelled`; there is
no remote transfer. Requests remain accessible after removing the heart.
Duplicate confirmations share one request across lists. To change an edition,
cancel, then open a fresh review; an old review still resolves to the old request.
Invalid or stale confirmations return a plain error: return to the list and open
a fresh review. Storage failures require operator preparation or a later retry.
All save, removal, confirmation and cancellation actions use CSRF-protected POSTs;
the signed review token is server supplied. No provider writes or transport occur.

TMDB attribution: This product uses the TMDB API but is not endorsed or certified
by TMDB. Books are sourced from Hardcover. Local source binding does not establish
verified provider account identity.

Verification uses mock source records, actual Django templates and an ephemeral
test database in this candidate, without real provider requests or a production
database. Browser layout, keyboard use,
large text and screen-reader acceptance remain separate checks.

Offline visual fixture helper: `${PRIVATE_RUNTIME_PATH} --check-fixtures`
checks the isolated official Django test database without binding. Add
`--persistent-fixture` to check the fixed private test file. Only the operator
launches `--persistent-fixture --minutes 20` on 127.0.0.1:8001, with a separate
Mac loopback forward on 8767. Stop and restart the same command, log in again,
and compare restored choices, hearts and blocked/cancelled jobs. Test secrets
are ephemeral, so old login sessions need not survive the process restart.
Fixture credentials are public test data in helper source only. Never open its
syntactic source URLs. The operator removes only the helper-created
`${PRIVATE_RUNTIME_PATH}` after the restart check. No production
settings, database, account or credentials participate.
