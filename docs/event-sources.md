# Bounded event sources

`backend/shelf/event_sources.py` adds transient event normalization and reconciliation
on the existing `discovery_sources.Candidate` contract. It uses the existing strict
JSON decoder, identity helpers, Moshtix projection, artwork affordability rule and
bounded transport. No model, migration, view, URL, template or cross-media module
changes are required. Nothing connects this new callable to the running UI.

## Exact artist to upcoming gig helper

`artist_upcoming_gigs(artist_id, candidates, now=..., localities=...)` accepts a
canonical MusicBrainz artist UUID using the existing MBID/artist identity validator.
It reuses `upcoming_events`, `event_timing` and exact source-ID/URL reconciliation.
Localities are explicit (up to 25); no distance, travel time or venue-name identity
join is inferred. Only music-category events in those localities are eligible.
Existing public/access/date rules still apply. A future result does not prove stock,
artist attendance or eligibility for a particular person.

Each `EventCandidate` may carry up to 25 `LineupReference` values. The authenticated
caller supplies these after verifying the artist identity and lineup reference on
the exact normalized event record, using `dataclasses.replace` to attach the tuple.
A reference retains canonical artist MBID, complete `EventEvidence`, bounded
reference text, confidence and explicit `verified=True`. The parser never promotes
performer names or embedded flags to verified references. Syntax validation does not
prove a UUID is a real MusicBrainz artist; that check remains a caller attestation.

Only references bound to the winning record and current retrieval time are used.
A newer omitted lineup cannot inherit an older verified one; cancellations remove
older matches before traversal. Exact source links can merge venue/guide/seller
records; equal titles/dates cannot. Each `ArtistGig` retains the normalized event,
all source evidence and deduplicated existing `cross_media_feedback.FactualEdge`
values (`event -> artist`, relation `lineup`). A new synthetic integration test
passes these edges through the unchanged reinforcement implementation.

RRR, PBS and known venues remain guide sources in `SOURCES`. Bandsintown is listed
only in `UNCONNECTED_SERVICES`: consumer access is unverified and artist-team scoped.
There is no fabricated URL, key, client or falsely connected registration. No live
lookup/monitoring is added. See [availability](event-availability.md) for the separate
seller/risk contract.

## Source registry

The registry records source navigation and category separately from connection
capability. These URLs were supplied in the task; no website or API was contacted
for this implementation. Website entries accept explicitly supplied snapshots and
do not advertise an API connection.

| Source ID | Category | Official listing URL | Capability |
| --- | --- | --- | --- |
| `science-gallery` | university arts | https://melbourne.sciencegallery.com/whats-on | Supplied snapshot |
| `rmit` | university | https://www.rmit.edu.au/events | Supplied snapshot |
| `unimelb` | university | https://events.unimelb.edu.au/ | Supplied snapshot |
| `forum` | venue | https://forummelbourne.com.au/shows | Supplied snapshot; Moshtix coverage unknown |
| `rrr` | radio editorial | https://www.rrr.org.au/events | Supplied snapshot |
| `pbs` | radio editorial | https://www.pbsfm.org.au/news/pbs-events | Supplied snapshot |
| `darebin` | council arts | https://arts.darebin.vic.gov.au/Whats-on/Event-calendar | Supplied snapshot |
| `thornbury` | venue | https://www.moshtix.com.au/v2/venues/thornbury-theatre/2362 | Exact Moshtix venue ID 2362; website is not an API |
| `moshtix` | public API | https://api.moshtix.com/v1/graphql | Explicit bounded callable; live behavior unvalidated here |

The raw user label `deribin theater` remains an unconfirmed candidate mapping to
Darebin. It is not an accepted source ID or automatic preference/venue join.

`restaurant_events` is a separate supported source category, with `dining_event`
as its candidate category. No specific restaurant URL was supplied or verified,
so no restaurant is invented in the default registry. A trusted caller may pass
an explicitly configured `sources` mapping containing an `EventSource` with the
approved restaurant's official listing URL and `supplied-snapshot-only`
capability. Snapshot content cannot modify this registry. Tests use only
`https://dining.example.invalid/events` for this seam. A restaurant/place record
does not become an event; a dining event must have an explicit event record and
a usable future date to enter public upcoming results. Reviews are ignored.

## Callable contracts

`fetch_moshtix(now)` makes one explicit anonymous request through the existing
transport and returns the existing `Outcome` contract. It requests the first
page of at most nine events in VIC, sorted by start date, within eight weeks.
The verified query retains `Date` variables, `includePastEvents:false`,
`publicOnly:true` and `searchableOnly:true`. Only `id`, `name`, `startDate` and
venue ID/name/address locality/region are selected. There is no pagination,
retry, account, credential, institution login, background task or booking.
The transport still bounds responses to 128 KiB, curl to five seconds and the
parent process to six seconds. Redirects, proxies and curl/netrc configuration
remain disabled. Its original callers still default to one result.

`parse_moshtix(raw, now=..., retrieved_at=..., provenance=...)` parses bytes from
that specific filtered operation. Public/searchable flags describe the request
contract; they are not independently present in the returned verified fields.
Malformed envelopes, IDs, venue metadata, regions and excess rows reject the
whole response with `SourceError`. Past, exactly-now, beyond-eight-week,
date-only or timezone-missing starts are suppressed. Numeric and decimal-string
event IDs normalize together. Exact integer venue ID 2362 alone joins the
existing Thornbury venue identity. Venue names and event titles never join IDs.
Status, end date, lineup, admission and artwork prices remain unknown because
the verified selection does not supply them. A future date does not establish
that a show is still scheduled, has tickets, or will be enjoyable.

`parse_official_snapshot(raw, source_id=..., retrieved_at=..., provenance=...,
sources=SOURCES)` accepts a bounded JSON byte snapshot. `provenance` must be
`supplied-snapshot` or `fixture`. Retrieval time must contain an offset. The
caller supplies and attributes the snapshot; URL validation cannot authenticate
its origin or establish freshness. No URL, remote JSON-LD context, HTML page or
linked entity is fetched. There are two small accepted shapes:

* `{"format":"event-metadata-v1","events":[...]}` containing explicit
  `kind:"event"` records.
* A JSON-LD event object, array, or top-level `@graph`. Supported types are
  `Event`, `MusicEvent`, `ExhibitionEvent`, `TheaterEvent`, `FoodEvent` and
  `EducationEvent`, either compact or schema.org HTTP/HTTPS terms. Type arrays
  are supported. Other nodes count toward the row limit but do not become events.

At most nine top-level rows/nodes are accepted, including duplicates. Both shapes
use `name`, `url`, `startDate`, optional `endDate`, and optional explicit
`timezone:"Australia/Melbourne"`. Metadata uses `id`; JSON-LD uses `identifier`
or `@id`. A safe `@id` URL can supply a missing JSON-LD `url`. With no source ID,
the official event URL supplies identity. The registry's listing page itself
cannot identify an individual event.

Metadata `status` is one of `scheduled`, `rescheduled`, `cancelled`, `postponed`,
`ended`, `unknown`. JSON-LD `eventStatus` recognizes `EventScheduled`,
`EventRescheduled`, `EventCancelled`, `EventPostponed` with compact or schema.org
HTTP/HTTPS spelling. Absent/unrecognized JSON-LD status remains unknown.
Explicit `eligibility` is `public`, `student`, `invite` or `unknown`; absence is
unknown. This is a documented snapshot extension, not a claim that those sites
provide this field or that generic JSON-LD audience descriptions prove access.
Optional `searchable` is a Boolean. No access level is inferred from university
affiliation, attendee data, student ticket offers, reviews or the user's profile.

Optional `location` contains a name and `address.addressLocality`. Metadata
`lineup` accepts up to 25 explicit names. JSON-LD `performer` accepts names or
objects with `name`, singly as an object or in a list. Absent/empty lineup is
unknown. Other source fields, including reviews, attendee data and descriptions,
are not projected.

Admission is `admission:{price,priceCurrency}` in metadata or a single JSON-LD
`offers` object (or one-element list). Multiple offers remain unknown rather than
inventing one ticket price. Offer lists are limited to nine. Missing price or
currency remains unknown; no free admission is inferred. Explicit
`artwork:{price,priceCurrency,per}` is separate. Only a known AUD amount strictly
below 2500 with `per:"individual artwork"` is within the artwork budget. Ticket
or admission prices, foreign currencies, ranges and missing values never prove
artwork affordability. No currency conversion is performed.

Official links require the exact registered HTTPS host, without credentials,
ports, queries, control characters, backslashes, traversal or login/redirect path
segments. Fragments and trailing slashes are removed for URL identity. Query-based
event URLs, additional hosts, HTML extraction, recursive JSON-LD graphs, arbitrary
vocabularies and login-only listings are outside this slice. Malformed supported
event rows reject the snapshot rather than returning partially validated cards.

## Dates, deduplication and presentation

`EventCandidate` extends `Candidate` with event/source IDs, raw and normalized
start/end dates, date precision, timezone, status, eligibility, lineup and evidence.
Each observation keeps source ID, event ID, official URL, retrieval timestamp,
provenance, original position and SHA-256 of the supplied bytes. Raw responses are
not retained. Source-only keys stay within the existing official/Moshtix namespaces.
Fixture candidates use fixture keys, `provenance="fixture"` and no display `url`;
the official URL remains explicitly labelled evidence for reconciliation only.

`reconcile_events` accepts at most 100 candidates and 100 total evidence records.
It joins same-source event IDs or exact normalized official URLs, including
transitive ID/URL aliases, and preserves unique observations. Equal titles never
merge events. Fixture and source evidence never merge. The newest observation
supplies current fields; missing new prices/lineup do not inherit stale values.
At equal retrieval times, cancellation/postponement/end and restrictive audience,
searchability or unknown-date evidence win. A newer unknown status does not
reinstate a withdrawn event; a newer explicit scheduled/rescheduled observation
can. The aggregate retains the evidence supporting this reconciliation.

Always use `upcoming_events(candidates, aware_now)` for public presentation. It
reconciles **before** suppressing cancellation, postponement, ended/past-start
listings, unusable dates, non-public/unknown eligibility and explicitly nonsearchable
events. Retain official snapshot candidates until reconciliation so a cancellation
can suppress an older future listing. This function is not an incremental cache;
Moshtix batches replace a previous bounded response rather than implying that
absent events were cancelled.

Timed dates require an explicit offset or an unambiguous Melbourne local time
with the named timezone. Melbourne DST uses the runtime's IANA zone data. A local
clock in the skipped hour or repeated hour is unknown unless an offset resolves
it. A supplied offset contradicting an explicit Melbourne zone is rejected.
Missing timezone never uses the server timezone. Date-only values stay dates;
they are not converted to midnight. Date-only filtering requires the explicit
Melbourne zone and includes today with `today-date` precision. A date-only end is
inclusive. Timed ends are exclusive. Events already started are conservatively
excluded even when a supplied end is still future; ongoing exhibition discovery
is outside this slice. An opening event is never manufactured from an exhibition
run, venue record, or restaurant record.

## Verification and integration limits

All new examples in `test_event_sources.py` are explicitly synthetic. Focused
tests cover contracts and adversarial inputs; they establish no live source
coverage, availability, authentication, audience eligibility or recommendation
quality. No dependency was installed and no service/account was accessed.

Follow the measured prerequisites in [README.md](README.md#reproduce-verification):
use this isolated source candidate with no `.demo-data`, prepared venv, owned 0700
local btrfs scratch, same-device/mount evidence, linked and SQL SQLite >=3.51.3,
SQL source ID, actual file/directory/parent fsync and conflicting exclusive
nonblocking flock. Record measurements before any WAL test, then create a fresh
owned child and export its absolute path as `TMPDIR` before Python starts.
This run records measurements in `${PRIVATE_RUNTIME_PATH}` and the child
path in `${PRIVATE_RUNTIME_PATH}`.

From `backend`, with that `TMPDIR`:

```sh
${PRIVATE_LOCAL_PATH} -B -m unittest shelf.tests.test_event_sources shelf.tests.test_discovery_sources shelf.tests.test_discovery_transport -v
```

After final source/test/doc edits, from the candidate root, run the full suite
once through the unchanged guarded entrypoint, then its normal checks:

```sh
${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=2
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

Exact commands, counts, outcomes, hashes and limitations belong in
`${PRIVATE_RUNTIME_PATH}`; `${PRIVATE_RUNTIME_PATH}` records the handoff. Require
zero test skips and this invocation's `wal-execution.json` PASS before claiming
the local suite passed. No independent quality PASS is claimed: the required
separate maintainability review remains for a later reviewer, consistent with
this task's sole-author/no-child-agent constraint.

The new callable is not wired to an HTTP route, scheduled job, persistence layer
or deployment. Live acceptance and future UI integration remain separate work.
Rollback removes the new module/tests/docs and restores the transport's optional
page-size addition; there is no data migration. No repository licence or
publication authority is added by these synthetic fixtures or supplied URLs.
