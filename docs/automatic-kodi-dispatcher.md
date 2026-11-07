# Automatic completion dispatch to the Mac

The bounded Mac dispatcher discovers actual acknowledged Cabinet film, explicitly selected TV episode and album jobs over
existing owner SSH. It does not submit downloads, access indexers, play media or
change the Kodi source configuration. Its expiry and renewal must match the
current approved backend run; neither service renews itself.

Each listed job must retain its owner/profile, request, selected result, exact NZB
hash, SAB identity and selected catalogue identity. Before importing, the dispatcher
calls the existing final-history observer, which checks the exact SAB completion
and current file bytes. A historical file or a manually imported episode cannot
be assigned a fabricated Cabinet request to make this test pass.

The Mac consumers reuse the existing owner transfer and Kodi helper modules
without modifying them. Films support one verified video file. TV supports the authoritative selected single episode (default S01E01), with a local show NFO carrying the selected
TMDB show ID and an episode NFO carrying a Cabinet episode identifier. Albums
require the selected artist/release identity and complete FLAC track count,
hash-bound audio/tag/duration checks, and exact Kodi song/album readback.
The dispatcher chooses a fixed adapter and separate cache/state by actual medium.
It stores the actual receipt
before sending it back through the existing bounded operator feedback command.

A single private process lock prevents concurrent dispatchers. The durable job
journal records a consumer launch before starting it. The consumer itself reserves
its single Kodi scan before the RPC. After interruption, it reads back that same
library entry rather than issuing another scan. An uncertain feedback SSH result
is retried with the exact saved receipt before more import work. Acknowledged
imported results are terminal; blocked or unknown results reconcile forward.
Identical callbacks are no-ops. Unknown outcomes are not labelled failed or ready.

Cabinet displays these independent observations:

1. SAB reports a completed download with its authoritative completion timestamp.
2. Downloaded file bytes matched the registered manifest at a recorded time.
3. A scan may have been reserved; that is not proof that it finished.
4. The Mac verified the bytes and video stream and observed the matching library
   path and stable movie identity, when an actual receipt contains that evidence.
5. Playback, complete-work identity and continued availability remain unverified.

This increment's live existing-media check verifies the already downloaded Dark
Matter episode on Omarchy and Mac against its prior Kodi entry. It does not prove
a new provider fetch or automatic Cabinet job import. The only actual Cabinet
movie request in the current database failed at the provider; no successful
Cabinet-bound completion exists to dispatch. Tests use isolated synthetic jobs
for the complete state-machine path. No test promotes synthetic handoffs into
live consumer adapters or writes invented success receipts to the user's database.

Stop the owned dispatcher for rollback and preserve its journals, consumer
receipts, cache files and Kodi history. Do not erase an uncertain receipt to retry,
restore an older database, remove library items, or restart a failed download.
The original dispatcher increment changed source only. P3.21 selected episodes
require migration 0017; see [selected episodes](selected-episodes.md).

## Running and testing

Run on the approved Mac from the reviewed source tree. An explicit single
dispatch cycle uses the same import and receipt actions as the worker for any
eligible job:

```sh
python3 -B tools/mac_completion_dispatcher.py \
  --expires 2026-11-06T04:17:29+00:00 \
  --renewal 20261030-approved-seven-days --once
```

Omit `--once` for the approved bounded worker. It checks one page of up to sixteen
jobs per minute, advances a durable cursor and wraps after the final page. The
fixed private runtime is `dispatcher-evidence/runtime` in the Mac engineering
workspace. Its `status.json` distinguishes healthy listing with zero eligible
jobs from unavailable observation and actual acknowledged imports. Job journals
and consumer receipts stay private. One unresolved receipt blocks only its own
job, so cancelled or unavailable work cannot starve other completions.

Mac-only injected adapter tests:

```sh
python3 -B -m unittest discover -s tools -p 'test_mac_*.py' -v
```

The backend full Django suite includes producer discovery, authority, duplicate
bindings, receipt validation and actual HTTP status rendering. Neither test suite
uses live provider downloads, Kodi scans or playback.

## Album files and metadata

The final album export binds `album_identity` both at top level and inside the
hashed final completion evidence. It must equal the current selected AlbumRequest
and its authorization. The internal request ID is not a MusicBrainz release ID.
A complete bounded manifest can include FLAC tracks, JPEG/PNG artwork and opaque
CUE evidence. Only conventional local cover/folder artwork is copied alongside
the tracks, at its original relative path; CUE files are checked by hash and
never copied, parsed, executed or scanned. Embedded front-cover art is preserved
inside the unchanged FLAC bytes. Missing art/description remains unknown; no
online scraper or Kodi source/settings change is enabled.

TV and album consumers reserve one durable scan before requesting it. A timeout
or interrupted scan is reconciled by exact library readback without another scan.
Imported receipts are terminal. Per-medium state lives under `consumer/tv` and
`consumer/album`; existing movie state remains at `consumer`. The dispatcher lock
and job journals are shared across upgrades and must be preserved.

The existing diagnostic album is synthetic and its original test tones are not
a real Usenet album. Its read-only library evidence cannot establish an actual
Cabinet-bound download. There is currently no completed Cabinet-bound TV or album
job available for live automatic-import acceptance.

## Apple TV destination

Apple TV uses an independent completion contract, dispatcher and receipts. Kodi
continues to use the fixed adapters described here. See
[Apple TV completion](apple-tv-completion.md) for stream-copy compatibility,
watchfolder uncertainty, explicit episode selection and live-acceptance limits.

P3.21 explicit selection, compatibility and migration boundaries are documented in
[selected episodes](selected-episodes.md).
