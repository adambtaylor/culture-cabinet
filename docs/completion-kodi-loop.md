# Exact reviewed NZB → final completion → Mac Kodi

P3.10 builds on the reviewed Obtain forms. The live integration attempt found an
NZBPlanet release with one article ID changing on each Hydra retrieval. Cabinet
correctly rejected the differing SHA-256 before creating a transfer. Do not
normalize article IDs or weaken that check.

The live factory now retains the reviewed descriptor in `ObtainReviewedNZB` in
the existing private database. Confirmation uses those exact bounded bytes,
rechecks ownership/grants, current selection and policy, the signed five-minute
review and SHA-256. A new review with different bytes invalidates the older token.
No descriptor is accepted from HTTP or rendered in HTML; the hash remains visible.
Descriptors expire logically after five minutes and expired rows are removed by
subsequent reviews. An idle database can retain expired bytes until a later review;
they cannot be submitted. This is temporary private storage, not an audit export.
Existing durable transfer/unknown-outcome semantics still prevent repeat enqueue.

`completion_observer.observe` is an operator-only exact-job adapter. It uses the
existing authorized local read of `${PRIVATE_LOCAL_PATH}`,
a bounded no-follow in-memory SQLite snapshot, no new credential or SAB administrative
API. This adapter supports the installed rollback-journal format only. WAL or an
active journal is unavailable and retried later; it is never silently ignored. It first verifies
current actor/profile, registered request/result/descriptor binding and known job.
Only that nzo's minimal final-history fields are read. Missing history is pending;
failed is failed; conflicting/unavailable data is unknown. Existing files alone
cannot establish completed status. The freshly verified hook receipt and exact
SAB completion timestamp produce the existing version-two handoff.

The operator export now binds the catalogue identity and exact selected edition
into the completion-evidence hash. This says what was selected, not that bytes
independently prove the film/episode identity, full work, playback or enjoyment.
The ordinary HTTP v1 handoff remains compatible with the existing album consumer.
The bounded Mac video worker accepts the explicit v2 contract; it never sends a
movie into the album importer and never starts playback.

Validation: focused real HTTP composition with dynamic-descriptor regression,
exact SQLite reads, final timestamp/binding checks, failure/unknown handling,
consumer injected transport/Kodi tests, then one separately labelled licensed
live provider fetch. A previous manual download or a synthetic fixture alone
cannot establish the entire live path. Exact download/import receipts belong in
the increment evidence, not fabricated test results.

Deployment uses an additive migration and preserves current user data, keys and
the existing demo expiry. Code rollback retains the additive table/migration and
all durable transfer/import receipts; it does not restore an older database over
new user work. Stop only the owned worker on rollback; preserve verified media and
Kodi IDs/history. There is no automatic cleanup, re-enqueue, playback or renewal.
