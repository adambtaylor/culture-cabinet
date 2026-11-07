# P3.2 background contract integration

The three frozen inputs are disjoint source packages: events (44 new tests),
reinforcement (37), and ticket/calendar (42), each on the same 665-test base. Their
separate 709/702/707 suite counts therefore yield a union of 788 tests, not the sum
of those three full suites. This integration adds 41 tests: availability (23), exact
artist gigs including the typed-edge integration (13), and recipient lists (5).
The expected combined full count is 829; actual execution records are authoritative.

The completed reinforcement module and both of its test files are untouched. The
ticket module and original 42 ticket/calendar tests are unchanged; the calendar
edit is limited to a read-only grouped recipient preview. Event normalization,
source reconciliation and existing date/identity validators are reused. No URLs,
views, models, migrations, deployment, credentials, user data, network, installs or
child agents are part of this integration. Obtain and playback changes remain with
their separate owners. The running P3.1 tree was not accessed or modified.

Private project coordination documents are omitted from this development export.
[availability](event-availability.md), [reinforcement](cross-media-feedback.md) and
[ticket/calendar](ticket-calendar-handoff.md) for callable contracts and boundaries.
These helpers still require authenticated UI/services, private persistence,
source/issuer verification and authorized live adapters before product acceptance.
Exact artist-lineup attestations and availability observations must be supplied by
that integration. Bandsintown is unconnected. Ticketmaster AU/Moshtix native Shop
eligibility is unverified/blocked, including the manual-handoff blocker. Wallet
means an issuer pass link and separate explicit user addition confirmation.

## Local verification and export

The existing runtime is `${PRIVATE_LOCAL_PATH}`
(Python 3.14.8, linked and SQL SQLite 3.54.0). Before WAL tests, fresh owned scratch
was measured on local writable btrfs, same device as its parent, mode 0700. Actual
file/directory/parent fsync and conflicting exclusive nonblocking flock passed.
SQLite exceeds the existing 3.51.3 gate. No installation or runtime substitution
was performed. An initial mount probe selected an overmounted entry on a tie; the
corrected probe uses the latest most-specific mount entry, as the existing F11
test does. `${PRIVATE_RUNTIME_PATH}` records the successful measurements.

Set `TMPDIR` to the exact scratch child in that record before starting Python.
Focused tests use `-B -m unittest` from `backend/` for the three new modules,
`test_event_sources`, `test_discovery_sources`, `test_discovery_transport`,
`test_ticket_handoff` and `test_calendar_handoff`. The single combined full run uses
the existing guarded entrypoint from the candidate root:

```sh
${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=2
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

Author orchestration in `${PRIVATE_RUNTIME_PATH}` records commands, exits, test counts,
timings and logs and checks source hashes are unchanged during the full suite. The
existing recovery suite supplies actual scratch WAL evidence in `wal-execution.json`.
Results and changed-file hashes are consolidated in `${PRIVATE_RUNTIME_PATH}`.

The clean export copies `backend`, `docs`, `templates`, `static`, `tools` and the root
dependency/notices files into an owned scratch source tree, excluding bytecode,
private `.requirements`, `.work`, runtime stores and repository metadata. Both
`backend/shelf/fixtures/discovery_sample.json` and `cross_media_sample.json` must be
present with matching SHA-256 hashes. It runs the focused suite plus the existing
pure cross-media/feedback tests and Django system checks from that exported tree,
without relying on input handoffs or private research files. This is a clean-source
contract/fixture check; the full suite is run only once on the integrated candidate.

There is no new storage to migrate or rollback, no service to activate, and no live
workflow acceptance. Independent functional/maintainability review remains pending:
the user required one author and no child agents. No independent review PASS or
publication readiness is claimed.
