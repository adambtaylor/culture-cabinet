# Shared bounded event acquisition

`shelf.event_scraping.collect(source_id, *, now)` returns the existing
`discovery_sources.Outcome`. Its first implemented source is `rrr`. The ID is
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
part of this slice. PBS is the next separately tested adapter; gallery subclasses,
Moshtix's distinct one/nine-row contracts and unavailable sources remain unchanged.

Affected checks cover engine/parser behavior and the real Django POST caller with
mocked transport, including replay, CSRF, revocation, cache expiry, escaping and
cross-source failure isolation. A separate bounded real-source observation records
its actual request count and result; mocked checks cannot establish live coverage
or browser/native Enhance acceptance.
