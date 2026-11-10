# Shared bounded event acquisition

`shelf.event_scraping.collect(source_id, *, now)` returns the existing
`discovery_sources.Outcome`. Implemented sources are `rrr` and `pbs`. The ID is
fixed application configuration; URLs, selectors, credentials and callbacks are
not accepted. An unsupported ID or a missing/naive observation clock fails before
I/O. `now` is this action's aware observation time, not a publisher update time.

The normal caller is the existing authenticated provider-discovery
`POST /profiles/<id>/discovery/action/`, `operation=rrr`, under
`cabinet.provider_urls`. `discovery_sources.fetch_public` delegates to the shared
driver, which uses the unchanged `discovery_transport.rrr` and
`rrr_events.parse_listing`. This is not portable native Enhance activation.

The existing fixed anonymous GET retains its 128 KiB, five-second curl/six-second
parent bounds, no redirects/retries/cookies/credentials/pagination, and at most
nine parsed cards. GET/reload never fetches; the form's CSRF, nonce, current
profile/grant/revision checks and post-I/O recheck remain unchanged. Replayed
POSTs cannot fetch twice. Source refresh failures replace only that source's
cached result. The existing session cache expires after fifteen minutes; only
explicit human feedback persists minimal identity/provenance.

Recognized RRR leads return `ready`; their missing years, clocks, eligibility and
ticket availability remain unknown. They are not passed through the confirmed
upcoming-event filter. The parser retains exact source IDs/URLs, source order,
observation time and response digest; titles never establish identity. Raw HTML
and article prose are not retained. An empty shell, challenge, changed markup or
malformed response returns `invalid`, never a claim of zero inventory. HTTP
refusal (`error`), other HTTP errors (`error`), rate limiting (`rate-limited`) and
timeout (`timeout`) use safe fixed messages; no response body/exception text is
displayed and no retry is attempted.

This reuses RRR's existing private factual-link reader scope. Public readability
and robots access do not establish broader redistribution or model-input rights.
No new store, scheduler, listener, schema, browser, inference or account access is
part of this slice. The PBS adapter shares this driver; gallery subclasses,
Moshtix's distinct one/nine-row contracts and unavailable sources remain unchanged.

Affected checks cover engine/parser behavior and the real Django POST caller with
mocked transport, including replay, CSRF, revocation, cache expiry, escaping and
cross-source failure isolation. A separate bounded real-source observation records
its actual request count and result; mocked checks cannot establish live coverage
or browser/native Enhance acceptance.

PBS's normal explicit POST and its `pbs_events.fetch_listing(now)` compatibility
caller both delegate to the driver. Acquisition still calls the existing
`discovery_transport.pbs()`, including the shared ten-second host reservation
before network I/O. Local interval refusal returns `rate-limited` without a read.
The unchanged pure PBS parser alone applies the visible-year/eight-week rules,
keeps date ranges rather than fabricating sessions, and retains source notices
without turning them into ticket availability. Recognized out-of-window cards
yield a bounded `empty`; missing/challenge markup remains `invalid`. Unknown-date
leads remain useful. No PBS real-source call is implied by deterministic checks.

## 303 occurrence port (E3a)

The next producer operation is `venue303`, an empty-key/radio-mode POST through
that same authenticated provider-discovery route. Its fixed listing is
`https://303.net.au/gigs-events`. Experience owns its visible read control; this
backend slice alone does not establish a rendered entry button or native Enhance.

`EventCandidate` and `EventEvidence` append optional `occurrence_ref` and
`occurrence_basis`. Old constructors remain valid. New listing candidates use
`ListingEvent`, adding small `listed_date` and `source_notice` labels. Its
`date_status(now)` includes unknown timezone/session identity and attributed
notices for existing escaped card rendering. Source clocks without a zone do not
become instants. A date range stays one range, not expanded sessions.

`occurrence_event` binds the normalized observation to a versioned source key.
A proved session ID takes priority; otherwise the exact event URL and explicit
date/time/precision distinguish the occurrence. Missing identity uses only the
HTML digest and source position and is visibly unresolved. Reconciliation uses
only source/mode/occurrence aliases for these records; legacy URL/show-ID aliases
cannot bridge sessions. Every attached reference must match the occurrence and
source mode. Seller binding also requires occurrence identity and an existing
locator on the same reference, while permitting fresh receipt times/digests.

The 303 parser inspects one bounded listing: at most 48 cards/100 observations,
nine returned leads, eight-week explicit date window. It calls existing field/date
normalization directly and hashes the actual HTML, never fabricated snapshot JSON.
`parse_official_snapshot` retains its supplied-only guards. Fixture candidates
retain fixture provenance and no outgoing display URL.

The separate fixed `event_listing_transport` reuses existing curl/TLS/parent
containment: 512 KiB, five-second request/six-second parent, no redirects/retries,
cookies, proxy or credentials. A private locked origin receipt reserves every
attempt and shares at least ten-second pacing across processes, including robots.
Explicit initial robots evidence must be recorded before a live listing; stricter
publisher pacing is saved in the shared receipt. Robots are not crawled by GET,
replay or ordinary listing actions. No ICS/detail/image/ticket link is followed.
