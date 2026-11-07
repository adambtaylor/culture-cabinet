# Triple R public event reader

This increment adds an explicit **Read Triple R events** action to Music & events.
An authenticated household profile can request one anonymous GET of
<https://www.rrr.org.au/events>. The first nine cards in source order enter the
existing 15-minute session cache and the Across your Cabinet overview. Opening
either page performs no external request. No new store, migration or credential
is required. Only explicit human responses persist minimal identity/provenance.

The subsequent isolated P3.5 increment adds a separate per-card detail action;
see [Triple R detail enrichment](rrr-details.md). The bounds and unknown fields
below describe the listing response, before an explicit detail request.

The request uses the existing 128 KiB parent-memory/response limit, five-second
curl timeout, six-second parent deadline, no retries, redirects, cookies, netrc,
proxy credentials, detail-page crawling or pagination. A truthful application
User-Agent identifies the reader. The source cannot supply a request destination.
Transport errors, 403, 429, malformed pages, changed markup and missing event
cards produce an explicit unavailable/invalid result, not a claim of zero events.
An unsuccessful explicit refresh replaces the previous result for this source.

## Source evidence and limits

On 29 October 2026, the public listing returned HTTP 200 within the existing
budget. Its event cards contain numeric source IDs, names, detail links, displayed
date labels, calendar categories and optional venue names. Only these small factual
fields are retained; descriptions, images, scripts, ads and detail-page content
are excluded. Source positions and a SHA-256 of the retrieved page accompany the
normalized evidence. The original page is never rendered or executed.

The page and a sampled detail page omit the year. Therefore the original date
label is displayed with **year and start time unverified**. No year, time zone,
start/end instant, upcoming status, cancellation state, admission eligibility,
ticket price or availability is inferred. Multi-day date labels remain intact.
The retrieval timestamp describes this fetch, not the source's update time.
Gig Guide maps to music and Community Events to community; other calendars remain
unclassified. Venue and performer names do not establish canonical identity,
lineup membership or recommendation lineage. Matching titles are not deduplicated;
conflicting numeric IDs or detail URLs fail closed. This is partial source coverage.

The public [robots policy](https://www.rrr.org.au/robots.txt) permits general access
outside `/admin/`; its [site disclaimer](https://www.rrr.org.au/about-us/disclaimer)
does not establish accuracy or completeness. Recheck access conditions before
expanding scope. A block or changed policy is a stop condition, not a reason to
change agents, endpoints or networks. University of Melbourne was considered but
its [website terms](https://www.unimelb.edu.au/legal/website-terms) prohibit automated
retrieval without permission; that source remains unconnected.

## Development and validation

`shelf.rrr_events` is a bounded standard-library HTML normalizer. It accepts bytes
and an offset-bearing retrieval time and performs no I/O. The only runtime caller
is the existing explicit public-read flow; nonce reservation, CSRF, profile/grant
checks before and after I/O, cache expiry and human history rules are unchanged.
The supplied-snapshot event registry retains its original meaning and boundary.

Tests `shelf.tests.test_rrr_events` and `shelf.tests.test_rrr_ui` use synthetic HTML
and mocked transports, including source drift, ambiguous identity, URL boundaries,
escaping, revocation, replay, expiry and lack of automatic preferences. Use the
Private project coordination documents are omitted from this development export.
full suite through `backend/manage.py test --verbosity=1` with an approved private
TMPDIR. Never run tests against the deployed data store.

The candidate is source-only and undeployed. The reviewed 997-test base is kept
separately. No provider/download experiments or Mac importer changes are part of
this increment. Direct Dark Matter acquisition and one-off Kodi import remain
separate evidence: automatic TV import is still absent from the albums-only Mac
consumer, and the previous dispatcher has expired.
