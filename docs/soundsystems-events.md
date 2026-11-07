# Sound Systems of Melbourne: public shows reader

This separate candidate adds one bounded anonymous GET of
<https://soundsystemsofmelbourne.com.au/shows/>. It does not change the frozen
funnel candidate, UI, registry, accounts, background schedules or deployment.

`soundsystems_events.fetch_listing(now)` returns the existing `Outcome` contract;
`SoundSystemEvent` extends `EventCandidate`. The parser reads only server-rendered
event-card HTML. It never reads or executes Next.js data/scripts, private backend
endpoints, linked social pages, maps, images, calendar buttons or the calendar feed.
The source's public calendar feed previously returned 429 in parent research;
this increment makes zero requests to it and claims no healthy feed.

## Exact projection and limits

- Retain title verbatim, raw visible calendar date, explicit normalized date when
  valid, displayed start/end clock labels, source location wording, retrieval time,
  position and SHA-256. Do not correct source typos or borrow a year/timezone.
- An end clock earlier than the start is **not** a verified next-day end instant.
  Ending date, timezone, ticket stock, prices, public eligibility, artist identities
  and cancellation state remain unknown. Clock labels are not approved sessions.
- TBA/TBC/secret/embargoed locations keep their original wording and an undisclosed
  state, with empty normalized venue. Other locations are attributed source text;
  addresses are not geocoded and no precise private location is inferred.
- The observed affirmative community clause can be retained as a source-attributed
  claim and short supporting phrase. Negative/conditional clauses are excluded.
  It does not prove public admission or independent suitability. Outdoor and
  day-party status stay unknown: a waterfront venue or afternoon start does not
  establish either. No unobserved descriptor format is invented for this slice.
- The source has no local stable event ID/permalink. Each key is explicitly a
  local observation identity over referenced post, source date and title. Missing
  references fall back to visible date/title. Exact repeats coalesce and conflicting
  facts fail closed. This is not canonical cross-source identity, and a changed
  date cannot silently be treated as an authorized reschedule.
- `url` is the public listing for human inspection. `official_url`, provider event
  ID and those `EventEvidence` identity aliases remain empty. **Do not populate
  them with the shared listing URL:** that would merge different shows. A test
  confirms distinct observations remain separate through `reconcile_events`.
- One rendered public Instagram post reference may be retained without visiting
  it. Only exact HTTPS `instagram.com`/`www.instagram.com` p/reel paths are accepted;
  the observed `igsh`/`stkn` tracking parameters are removed. Other/unsafe links
  are omitted. This supplies no claim that the original post/account was read or
  authenticated and no browser/social activation.

At most 48 cards are parsed; at most nine distinct leads are returned. Explicit
dates outside today through eight weeks are omitted. Unknown dates stay labelled.
An empty/challenge/drifted page is unavailable, not proof of no events. A recognizable
page containing only out-of-window cards returns a labelled bounded empty outcome.
The existing fixed curl transport caps the response at 128 KiB, curl at five seconds
and its parent at six seconds. No caller destination, credentials, cookies, redirect,
retry, feed fallback or pagination exists. 429 and other failures are explicit.

## Access and observed evidence

Checked 30 October 2026. The public [robots file](https://soundsystemsofmelbourne.com.au/robots.txt)
returned 200 with `User-agent: *` and `Allow: /`; no crawl-delay was supplied.
Its SHA-256 was `ff509bc30bdebf2a5e0130448b2f3d0a809403c9267221b05ccd74e0cda37f2f`.
The shows page exposed no terms/privacy link, and no separate website-use terms
were located in the bounded primary-source search. This is a private fact/link
projection, not article/image republication or a claim of a general reuse licence.
Recheck conditions before broader use and stop on future access restrictions.

At 04:47:36Z the shows page returned HTTP 200, 42,198 bytes, SHA-256
`4af93acbb1aca22f1e6741925fcf2117d9ac6658f0b07c5dd228d55b6ab8ac7b`.
Replay through the pinned Omarchy parser produced four distinct observations:
31 October (TBA location, 22:00–06:00 clock labels), 1 November (Collingwood
Town Hall), 7 November (Abbotsford Convent), and 12 December (Seaworks). The
7 November description contains the retained community claim. Outdoor/day-party
status stayed unknown for all four. Exact normalized evidence is outside source
at `soundsystems-evidence.json`; actual HTML is not a committed test fixture.

A subsequent single live read through the new adapter on the isolated Omarchy
runtime at 04:56:50Z returned `ready` with all four expected dates and the same
page hash. This verifies the bounded transport and parser together, not UI wiring,
ticket stock, original social posts or calendar-feed availability.

The existing SSH alias was reverified as `tang@127.0.0.1:2222`, host `Helix`.
The isolated source copy is
`${PRIVATE_LOCAL_PATH}`.
Using its private 0700 `.work` as TMPDIR and the prepared pinned runtime,
**61 tests passed, zero skips**: 15 new tests plus the unchanged PBS, Triple R,
Beat and public-transport regressions. Log: `${PRIVATE_RUNTIME_PATH}` there.
No database test, account access, installation, reservation or live-data write
was needed. Full integration and independent review remain separate.

## Shared discovery integration

P3.17 adds the explicit soundsystems operation to the existing public dispatch,
action and state allowlists. It uses the same nonce, current-authority recheck and
15-minute cache as PBS. The source also participates in the shared rating-funnel
gather. Generic source overflow exposes every returned observation without
special ranking. Source text is escaped; TBA and attributed source notices stay
visible. No date-night suitability, ticket availability, calendar session or
cross-source identity is manufactured.

Combined HTTP tests verify four distinct source observations despite the common
listing URL, individual ratings, cache expiry, revocation and undisclosed-location
privacy. Live normalization evidence is separate from synthetic regression data.
