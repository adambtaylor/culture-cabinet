# Triple R detail enrichment

The separate P3.5 increment adds **Read this event’s Triple R details** to each
current Triple R discovery card. It requests one page only after an authenticated
POST. The event must be in that actor/profile/session's unexpired listing cache;
the browser supplies an event key, never a destination URL. Source identity,
canonical URL and title must match the selected listing before any enrichment is
accepted. The same profile/grant, nonce/replay and post-fetch authority checks apply.

The existing fixed-origin transport retains its 128 KiB limit, five-second curl
timeout, six-second parent deadline, no redirects or retries, anonymous request
and truthful User-Agent. It does not follow any links found in the response.
The source's public robots policy and disclaimer were checked again on 29 October
2026; no detail-page prohibition was present. Blocks are reported, not bypassed.

## What the source supports

The sampled [Environmental Film Festival detail](https://www.rrr.org.au/events/efa-presents-environmental-film-festival-2026)
explicitly supplies start/end dates with years, clock times and a venue address.
The sampled [Wesley Anne gig](https://www.rrr.org.au/events/jon-s-williams-and-david-craven--1)
supplies clock times and address but no year. Its missing year remains unknown.
The [Kindred fundraiser](https://www.rrr.org.au/events/the-night-before-halloween-party-fundraiser)
links to Oztix under **More info**; that source label is preserved. Cabinet neither
calls it a verified official seller nor establishes price, availability or admission.

Only exact observed When/Where shapes are normalized. Explicit full dates become
calendar date strings and clock labels become 24-hour text; these are separate
fields, never offset-bearing instants. No timezone is inferred from a venue address,
and no missing year is borrowed from the current date or the other end of a range.
Unknown/ambiguous formats retain raw source text with unknown normalized values.
Invalid dates, weekday disagreements, reversed explicit ranges, mismatched event
identity and conflicting venues reject the detail response. The original listing
date always remains visible alongside detail wording; a detail is not a silent
rewrite of the listing evidence. Missing venue/address stays unknown.

One source-labelled More info link can be displayed when it satisfies the existing
HTTPS link rules plus a public-style hostname check. Queries, fragments, credentials,
unsafe encodings, nonstandard ports, local/numeric hosts and ambiguous multiple
links are omitted rather than rewritten. Links are escaped and opened only by the
human, with no-referrer/noopener. Destination ownership, DNS and ticket status are
unverified. This reader has no ticket checkout or purchase capability.

## Cache and evidence

Details retain their own retrieval timestamp and page SHA-256. The listing's
original retrieval evidence and expiry stay unchanged. Detail results are nested
in the existing transient listing outcome; they cannot survive the listing's
15-minute expiry or its next refresh. If the listing expires during a request,
the result is discarded. An unsuccessful detail refresh removes old enrichment
and displays a safe error while preserving the listing. No source text appears
in error messages. No enrichment enters durable human-choice history.

Both discovery and Across your Cabinet display the same result. GET requests
perform no reads. The overview links back to discovery for the explicit action.
There is no background fetch, crawl, new credential, database migration or store.

## Validation and status

Synthetic tests in `test_rrr_details.py` and `test_rrr_detail_ui.py` cover explicit
and missing years, date/time ambiguity, identity/canonical mismatches, safe links,
cache expiry, failed refresh, session isolation, revocation, superseding actions,
CSRF/replay and escaping. Actual public retrieval and captured-page replay are
recorded separately from synthetic tests. Use the repository's isolated boundary
and full `backend/manage.py test --verbosity=1` workflow with private TMPDIR.

The reviewed 1,016-test P3.4 snapshot remains separate and unchanged. This candidate
is undeployed; the prior live promotion/restart denial is still in force. Browser
visual acceptance is not claimed. No media-provider troubleshooting or Mac importer
changes belong to this increment.
