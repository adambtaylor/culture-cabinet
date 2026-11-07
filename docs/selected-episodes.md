# Explicit owner-selected TV episodes

An unqualified series Obtain still defaults to the legacy S01E01 pilot. For any
other single episode, the owner chooses both season and episode before search.
Season is bounded 0–999 (including explicitly selected specials), episode 1–999.
A blank pair retains the pilot default; a partial or invalid pair refuses before
provider work. There is no autonomous season or series acquisition.

The canonical persisted scope is `episode_s02e02`, with at least two digits and
three digits only where needed. It is stored in preview/job snapshots, signed
reviews, authorization and immutable transfer/completion evidence. The legacy
`series_s01e01` value keeps its original meaning and receipt hashes. Scope never
comes from a downloaded filename. Changing show, selected episode or listing
invalidates the old approval. Same-episode duplicates are refused, including
legacy versus explicit S01E01; distinct episodes of one show can coexist.

The selected episode drives exact listing token/season/episode checks, Kodi
canonical filenames and episode NFO/Cabinet IDs/readback, and Apple TV season,
episode and title metadata. The series TMDB ID remains a show ID, never an
invented episode ID. A neutral episode code is used when an actual title is
unknown. Selected catalogue metadata does not independently identify content.

Migration 0017 widens the existing scope columns to 16 and the allowed TV scope
family. It adds no episode tables or custom SQLite function dependency. The
central pure parser enforces canonical bounds at application/consumer boundaries.
The migration and deployment need exact-source tests and independent review;
existing rows, grants, credentials, journals and the approved expiry are preserved.
Do not restore an older database over new work or narrow constraints after new
explicit episode jobs exist.

## External completed jobs

A Home Hi-Fi or manually submitted SAB job is not an old Cabinet Obtain request.
It cannot be relabeled with an invented Cabinet request, result or NZB hash.
A separately explicit owner-selected completed-job intake must retain actual
external origin, actual SAB ID/final history, selected show/episode authorization,
source paths/hashes and descriptor provenance if actually retained. Missing
descriptor evidence stays unavailable; a fresh Hydra fetch does not prove bytes
originally accepted by SAB. There is no arbitrary filesystem or catch-all intake.

The one proposed S02E02 acceptance is coordinated through
`episode-evidence/selected-job-handoff.json`. Home Hi-Fi retains processing
ownership until the parent explicitly coordinates one owner. No engineering
remux/import may race its one-off importer. Final history plus verified bytes
does not invent independent repair/CRC checks. Originals and all uncertain
remux/handoff journals remain preserved by this software.
