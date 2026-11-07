# User-amended Phase 3: music and events

This bounded increment follows the explicit music/live-events amendment, replacing
the earlier Kermit-delivery scope. It adds an authenticated provider-navigation
link to `/discovery/`, which uses the existing owner, selected-profile and current
view/select grant checks. Inactive Lisa/Family remain inactive. Existing intake,
SavedItem/Obtain, credentials, LibraryThing checker and LTAPIguard are unchanged.
No messaging, account writes, ticketing, acquisition or playback is added. Two anonymous public read operations are available only through explicit authenticated CSRF POSTs.

After separate integration and migration approval, sign in with an existing owner,
select that profile and choose **Music & events**. **Reported seeds** asks for
confirmation without inventing past ratings. **Play the isolated sample** supplies
invented music, venues and events. Respond to both sample artists to see independent
lanes and shared membership; respond to the sample venue to boost its events.
Choose a negative/unknown response or save for later to see their different effects.
The response section retains hidden negative candidates for correction. Undo reverses
the latest effective action; restart clears the current projection, retains history,
and can itself be undone. Logout/reload reconstructs state from the database.

Manual research references are development examples. Public studio and historic-credit facts are distinct; unresolved-person markers are synthetic. These examples establish no real preference, personal relationship, attendance or future event availability.

Manual research references are development examples. Public studio and historic-credit facts are distinct; unresolved-person markers are synthetic. These examples establish no real preference, personal relationship, attendance or future event availability.

`discovery_sources.py` contains typed identities, small public JSON projections,
fixed ListenBrainz requests and an isolated synthetic transport seam.
`discovery_transport.py` implements only artist radio and the verified anonymous
Moshtix GraphQL operation using the installed curl and CA bundle.
MusicBrainz keeps CC0 core IDs/names/titles only. ListenBrainz is artist-seeded radio,
not a people-who-liked graph; its recording link conveys no audio license. Moshtix
parses public event IDs, dates and venue labels without title-only entity joins.
Unknown timezone becomes unknown date; dates retain supplied offsets. No coordinates
or home location are inferred. Price, cancellation and availability remain unverified.
Fixture dates are fixed and deliberately stale, not refreshed on page load.

Packaged `backend/shelf/data/discovery_registry.json` copies the non-secret source
handoffs. Explicit, inferred-adjacent and candidate-for-unconfirmed-deribin records
remain distinguishable. Official URLs are deep dives only, not scrape permissions.
Forum's Moshtix coverage remains unverified; Thornbury's researched Moshtix venue ID
2362 joins only exact venue IDs to the Thornbury seed. The anonymous Moshtix
request uses the exact verified query: eventStartDateFrom/eventStartDateTo are Date
scalars, region is [VIC], pageIndex is 0 and pageSize is 1. A rolling eight-week
window and includePastEvents:false/publicOnly:true/searchableOnly:true bound the
request. No pagination, invented status/price/coordinates or title-based Forum join
is available. The normalized 30 verified references drive the runtime registry/UI;
production has no dependency on .requirements. Fringe 2026 is finished, Common
Rooms is year-round, Affordable Art Fair next runs August 2027 (not current), and
Halftix is a discount overlay. Unconfirmed deribin/Darebin remains unconfirmed.
Artwork affordability requires
AUD <2500 per individual artwork; admission-only, unknown and unconverted prices
remain unknown. The actual card template evaluates supplied artwork price fields
against that strict bound; source events with no item price say “Artwork price
unknown; check official source”, with entry fees separate. This is not a claim of
affordable live artwork inventory. No purchase or booking operation exists.

`discovery_reducer.py` replays only human commands and interleaves up to five positive
seed lanes, preserving multi-membership and exploration. Negative hides that item;
unknown, saves and source suggestions do not create positive evidence. A new explicit
human response to a suggestion may create a seed. Venue preference boosts candidates
with an explicit venue identity; venue names never create a join.

`discovery_state.py` uses one new `DiscoveryState` table, unique by actor/profile/mode.
Migration 0006 creates only that table. Household and fixture identities cannot cross
the mode boundary; there is no sample promotion/import route. Minimal human commands,
IDs, source rank/retrieval/provenance, timestamps and nonce digests survive restart.
No raw source responses, descriptions or artwork are retained. Nonces plus revisions
and a compare-and-swap write reject stale forms; identical accepted requests replay
without duplication. The 500-operation history bound fails explicitly. There is no
automatic pruning or erasure UI. Reversing migration 0006 loses this discovery history;
it does not reverse or modify any older schema/data. A missing table displays read-only
preview; no automatic migration occurs.

All HTTP mutations are bounded CSRF POSTs. GET, saves, responses, unknown/corrections
and sample mode never request providers. Artist radio accepts only packaged canonical
artist seeds and easy/medium/hard mode, bounded to five similar artists and three
recordings each. Returned recording IDs, source artist IDs, positions and seed lineage
are retained; missing recording titles and audio destinations stay unavailable.
Source suggestions never create likes, listens or choices, and no source output goes
to AI. Multiple human seed lanes and exploration retain the existing selector.

The transport has fixed endpoints, no credentials/auth headers, curl configuration,
proxy, netrc, redirects or retries; only LANG/LC_ALL enter its environment. Curl has a
five-second limit and the parent enforces a six-second deadline and 128 KiB body cap
in memory, including unknown-length streams. JSON rejects duplicate keys, non-finite
numbers and invalid UTF-8. Error/timeout/rate-limit/empty labels never echo source
bodies or exception text. Test transports are mocks; no actual provider request was
made for author validation and no provider quality/availability acceptance is claimed.

Public POSTs consume the existing revision/nonce with a durable digest before I/O;
identical duplicates perform no further I/O, stale/repurposed forms conflict. A fresh
user action with a new nonce may retry. No public operation enters human history or
changes choices. Actor, profile, grants, persisted session selection and state revision
are rechecked after I/O before accepting a result. Public results/statuses live only in
a bounded process-local, session/actor/profile-scoped memory cache for 15 minutes;
worker restart/eviction loses that cache. Other workers may not display that response.
GET does not refresh it. A previously judged ID and minimal provenance remain available
for correction/unsave after content expiry, without retaining provider titles/bodies.
Public receipts are capped at 1,000; human history remains capped at 500 operations.

MusicBrainz remains blocked without a truthful operator-approved maintainer contact
and shared aggregate ≤1 request/second limiter; no contact is invented. Public verified
canonical IDs and links can display. Ticketmaster remains blocked without its key.
Gallery/community/regional lanes remain visible with partial/unknown live coverage;
radius and drive-time claims are unavailable. No bulk website ingest is implemented.

Verification uses the prepared venv and an owned local btrfs TMPDIR. No installation,
network, service, browser, deployment or real store is involved. Pure focused checks:

```sh
cd backend
TMPDIR="$PWD/../${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B -m unittest shelf.tests.test_discovery_sources shelf.tests.test_discovery_reducer shelf.tests.test_discovery_transport -v
```

From the candidate root, using the ordinary guarded entrypoint (including unchanged WAL/boundary checks):

```sh
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=1
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B backend/manage.py check
TMPDIR="$PWD/${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

The normal suite includes the new actual-template/auth/CSRF/history tests and all
unchanged baseline tests. Actual results and changed-file hashes are recorded in
`${PRIVATE_RUNTIME_PATH}`. This follow-up uses local source/test validation only;
no additional workers, reviews, runtime or deployment are part of this change.
The renewed Mac/guest demo is untouched and Phase 2 real hands-on acceptance remains
pending. Research attribution does not confer a repository license or publication
approval; no new dependency or licensed supplementary MusicBrainz data is introduced.
