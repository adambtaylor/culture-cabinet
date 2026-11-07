# PBS Events: bounded public discovery

The PBS Events reader makes one explicit anonymous GET of
<https://www.pbsfm.org.au/news/pbs-events>. It normalizes only the source's
upcoming-card attachment, never Past News, descriptions, images or audio. At most
48 cards are parsed and nine distinct leads returned. Explicit past dates and
dates beyond eight weeks are omitted. Missing visible years remain unknown;
machine metadata cannot supply one. Date ranges remain ranges, not sessions.

`pbs_events.PBSEvent` keeps title, exact official URL, raw displayed date, explicit
calendar dates where consistent, source-labelled notices, retrieval time and page
hash. A displayed Free Event or Sold Out notice is a publisher assertion, not a
seller/RSVP availability check. Venue, price, admission, status, artist identity,
start/end clocks and timezone remain unknown where the listing has no distinct
field. In particular, the source's midnight machine timestamps do not mean a
midnight performance. No missing year, venue from article prose, or current stock
is inferred. Exact duplicate URLs coalesce; conflicting identity/facts reject the
page. A changed/empty/challenge page is unavailable, not zero inventory. A valid
page whose recognizable cards are all outside the date window yields a labelled
bounded empty result.

The existing anonymous curl transport limits the response to 128 KiB, curl to
five seconds and the parent to six seconds. No cookies, credentials, redirects,
retries, linked-page reads or caller-supplied destination exist. GET rendering
must not fetch; the integration uses the existing authenticated CSRF POST,
nonce/reservation/recheck, 15-minute session cache and explicit-feedback path.
The reader does not add a background schedule or persistent event inventory.

## Source access and actual evidence

On 30 October 2026, the official robots file returned 200 and specifies
`Crawl-delay: 10`; it does not disallow this listing. The bounded reader reserves
one shared ten-second PBS host slot before I/O, including failed reads. A
0600, owner-checked, single-link, no-follow file in `/tmp` coordinates threads and
processes for the deployment owner. It stores only boot ID and monotonic time;
wall-clock changes cannot open an early slot, and a new boot resets the clock.
The state survives app restarts but contains no user/source content. Lock
contention produces a local interval notice. Unsafe/corrupt state fails closed.
Any future PBS endpoint must use the same reservation function; a browser or
another program outside this app is not coordinated by it.

This is a small private factual-link reader, not article republication or a
public API claim. Recheck publisher conditions before broader reuse. Stop on
blocks or policy changes. Public sources:

- <https://www.pbsfm.org.au/robots.txt>
- <https://www.pbsfm.org.au/news/pbs-events>
- <https://www.pbsfm.org.au/news/pbs-presents> (researched, not implemented here)

The official HTML advertises Events and Presents RSS feeds, but both returned
200 with zero items during the audit. They are not a working inventory feed.
The actual Events HTML fetched at 04:23:57Z was 84,172 bytes, SHA-256
`aa337f95d3036a10bc1b2db431557140027e27c9f4bedb3cad55a354def08164`.
Its upcoming attachment contained Sunday Sounds: JAZZPARTY, visibly dated
15 November 2026; Past News cards were separate. Actual page bytes are audit
evidence outside the source tree. Tests use synthetic markup only.

## Integrated discovery flow

The `pbs` operation is wired to the existing authenticated public action,
empty-key validation, nonce reservation and post-I/O grant/revision check.
`discovery_sources.fetch_public` accepts it only with empty seed/mode and delegates
to `pbs_events.fetch_listing(now)` rather than the Moshtix JSON parser. Source
status and registry distinguish this reader from unconnected PBS Presents.

The explicit Read PBS Events form displays its outcome and escaped source
wording. Bounded overflow results are available for all currently read event
sources, not just Beat, while preserving category filters and explicit rejection.
Expired or sample records cannot enter that overflow. No PBS detail action,
date-night badge, bookable session or restaurant suitability is implemented here.

`test_pbs_ui` exercises real Django requests with mocked transport: replay,
authority/CSRF, post-fetch revocation and supersession, cache expiry, minimal
feedback persistence, escaping, failed refresh and mixed-source overflow.
The affected Beat date-night overflow assertions use the generic context name;
their original behavioral checks are retained. Main owns final combined full-suite
validation, independent review and any authorized deployment.

Pure focused tests (from a source-only copy's `backend`, using the prepared pinned
Omarchy runtime and private TMPDIR):

```sh
${PRIVATE_LOCAL_PATH} -B -m unittest shelf.tests.test_pbs_events shelf.tests.test_rrr_events shelf.tests.test_beat_events shelf.tests.test_discovery_transport -v
```

No schema change, account grant, installation, booking or deployment is part of
this module. Verification results and actual parsed evidence are reported in the
worker handoff separately from these source instructions.
