# Tribe Soundsystem RSS announcements

This separate next-source delta reads the public RSS feed at
<https://www.tribesoundsystem.com/events?format=rss>. It does not modify the main
P3.16 funnel candidate, register a new UI action, add a schema/dependency, join an
account, or deploy anything. Main owns integration and independent review after
the current funnel ships.

## Access checked on 30 October 2026

The official [robots file](https://www.tribesoundsystem.com/robots.txt) returned
200 and does not disallow `format=rss`. It explicitly disallows both
`/*?format=ical` and `/*&format=ical`, including the per-event calendar route.
Automated calendar requests are therefore outside this connector: no ICS fetch,
parser, alternate route or bypass is implemented. The previously reported 200
calendar response does not override this current publisher restriction. The
canonical event link is available for the user to read ordinary source details.

The inspected public homepage had no linked terms/privacy/legal page. A search
restricted to the official domain found no terms page. That is a limited access
check, not a claim that no terms exist or that republication is licensed. Recheck
publisher conditions before broader reuse; stop on access blocks or policy
changes. No private endpoint, JSON route, social account, private group, new
location research, signup or outreach was used.

One permitted feed read returned 200 `application/rss+xml; charset=UTF-8`, 23,949
bytes, SHA-256
`60944a56c13fefa7517421d755ed47faf59ff6d268aaf5274eb7d147d586a05d`.
The feed has 11 items and `lastBuildDate` of 7 October 2026 at 04:10:12 UTC. Its
items include publication timestamps from August/October 2026 and one from 2019.
Those timestamps are **publication metadata only**, never event start times.
The channel title says “Upcoming Events”, but this does not establish that each
announcement is still upcoming. Raw feed/robots and filtered response headers are
retained only in the author evidence directory, outside source. Cookies are not
retained or reused. Source descriptions are not packaged as test fixtures.

## Callable and projection

`tribe_events.fetch_listing(aware_now)` makes exactly one anonymous GET through
`discovery_transport.tribe()`. That wrapper has no destination argument and uses
only the fixed RSS URL. It reuses the existing transport's 128 KiB body cap,
five-second curl budget, six-second parent deadline, fixed CA trust, empty proxy,
no netrc/config, no redirects, no retries and minimum environment. Feed parsing
makes no network requests and never follows a GUID, description link or media URL.

`parse_listing(raw_bytes, retrieved_at=aware_iso_timestamp)` returns at most nine
`TribeEvent` objects extending the existing `EventCandidate`. It validates at most
48 source items, 2,000 XML nodes and 16 levels within the same 128 KiB body bound.
DTD/entity declarations, malformed/truncated XML, changed channel identity,
ambiguous primary fields, unsafe event URLs and conflicting IDs fail closed. The
whole bounded feed is validated before selecting the first nine source positions.
A valid empty feed has a distinct, explicitly partial empty outcome; a challenge,
error or unrecognizable source never establishes zero events.

The exact canonical host is `www.tribesoundsystem.com` and event paths are one
bounded lowercase slug beneath `/events/`. Credentials, ports, alternate hosts,
queries (including `format=ical`), fragments, traversal and nested paths are not
accepted as event identities. Stable opaque RSS GUIDs identify events; a missing
GUID falls back to the exact event URL. A permalink GUID must agree with the link.
Identical rows coalesce. Conflicting GUID/link/title/publication facts reject the
feed; equal titles never merge different URLs. No artist, venue or social identity
is joined by name.

Each lead preserves its short title, official link, source position, GUID when
present, retrieval time and raw-body SHA-256. `publication_label`/`published_at`
and `feed_build_label`/`feed_built_at` keep raw timestamp labels and normalized
publication/build times separate from event dates. Missing, malformed or
zone-less publication dates remain unknown. Future source timestamps are shown
as unverified. Retrieval expires under the existing 15-minute convention; the
freshness label additionally warns when the feed build is over seven days old.
A recent retrieval/build is never a claim that an event is upcoming.

No event times, end times, venue, address, location, lineup, cancellation status,
price, eligibility or ticket inventory are inferred from free text. These fields
stay unknown, including “location announced later”, “sold out” and cancellation
wording in descriptions. A short title containing cancellation wording remains
visible as source text, not a verified status. An original source link and an
explicit unknown/cancellation warning support manual review. The existing strict
`upcoming_events` helper does not admit these date/eligibility-unknown leads as
confirmed upcoming sessions. A future integration may display them as an
announcement lane and must retain those distinctions.

Source text is untrusted and must remain escaped in any future template. Full
article descriptions, encoded HTML, images, ticket links, private data and media
are not projected or retained in runtime candidates. No current-user choices,
saves, durable inventory, calendar or ticket records are written.

## Validation and handoff

Tests use invented RSS with mocked transport. They cover publication/event-date
separation, old/future/missing source dates, unknown locations/cancellation,
identity dedup/conflicts, strict destinations, XML/entity/size limits, opaque
errors, no follow-up requests and the existing transport controls. Saved actual
public feed bytes are separately normalized with the pinned existing runtime;
actual content is never part of the regression fixtures.

The source-only copy came from the 303-file funnel snapshot with aggregate digest
`26c77340f7eed9c7610809dbbb154299336dc5cf7cc796fdbc21963b2b268b1e`.
Before source transfer, SSH configuration was checked and the destination verified
as `tang@127.0.0.1:2222`, hostname `Helix`, with strict existing host-key checking.
All testing stays in the user's isolated
`${PRIVATE_LOCAL_PATH}` directory. No source
is disclosed to another host or service. Use the existing pinned venv and the
candidate's private 0700 scratch directory; no installation is needed:

```sh
cd backend
TMPDIR="$PWD/../${PRIVATE_RUNTIME_PATH} ${PRIVATE_LOCAL_PATH} -B -m unittest shelf.tests.test_tribe_events shelf.tests.test_discovery_transport shelf.tests.test_pbs_events shelf.tests.test_rrr_events shelf.tests.test_beat_events -v
```

The handoff records actual test results and exact changed-file/source hashes.
P3.17 integrates this callable into the existing explicit public-read action and
shared rating-funnel gathering. Combined HTTP tests verify that cache expiry,
current authority, replay and publication-date distinctions remain intact. Live
read and deployment evidence are kept separately from synthetic fixture tests.
