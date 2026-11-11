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

## Forum adapter (E3b)

`forum` is the second new-origin operation over the same occurrence/caller port,
with fixed `https://forummelbourne.com.au/shows`. The parser accepts recognized
show cards and explicit session containers only. A yearless label stays unknown;
a range remains one range; a card-scoped notice affects only that card's explicit
sessions, and an unrelated page banner has no cancellation authority. Sold-out
wording is attributed source text, not a live ticket inventory assertion.

The initial adapter markup contract is covered by fictional fragments. Actual
current source shape/coverage requires its own successful bounded source receipt;
a blocked robots/listing read does not validate selectors. Rendering the existing
card template with a synthetic response does not prove the visible read button,
real-browser accessibility or portable/native Enhance flow. Experience owns those
separate controls and affected presentation checks through Orchestration.

The initial Forum robots request returned HTTP 200 with unrecognized content;
permission was not established and no listing request followed. Its real listing
transport therefore returns a policy-unavailable exception before reservation or
I/O; the driver renders `permission-required`. A later exact source-access
resolution is needed to open that path. Parser/caller fixtures do not change this
access fact. 303's robots request allowed its fixed listing, whose one integrated
read then failed in transport; no live parser-shape success is claimed for either.

## Closed shared source action (E4)

`event_source_registry.SOURCES` is the finite 35-source catalogue. A descriptor
contains fixed acquisition/parser functions, official targets, origin groups,
coverage and retention limits. Only five existing producers are implemented:
RRR, PBS, 303, Forum and Moshtix. Registration is not acquisition readiness;
unimplemented sources return an honest unavailable result without network I/O.
Forum's real access guard remains unchanged. Metadata supplied to templates never
contains executable callables or source-supplied configuration.

The new ordinary action is `operation=event_source` with a closed `source_id`.
It requires the existing household mode, revision, nonce and CSRF fields, with
empty key/radio/choice fields. Other actions reject a nonempty source ID. Missing
legacy fields remain absent in receipt input. The source ID participates in the
existing whole-request digest, so a nonce cannot switch to another source.

Generic and legacy controls address the same source cache slot. Failed refresh
through either control replaces stale success for that source while preserving
other sources. Current actor/profile/grant checks still run before and after I/O;
GET, reload and identical POST replay send nothing. PBS retains its fixed public
URL, shared ten-second reservation, parser/date/notice rules and 15-minute cache.
Moshtix keeps its exact existing one-row API query and shares the previous normal
response validation with its generic control; the distinct nine-row helper is
not substituted and no new provider query is introduced.

Experience owns the registry controls in the discovery template. Its context is
`event_sources`, a tuple of plain dictionaries with `source_id`, `label`,
`implemented`, `access_state`, `official_urls`, `origin_groups`, `coverage`,
`retention_seconds` and `unavailable_message`. Existing result rows still contain
`source`, `at` and `outcome`; source output and notices stay escaped. Metadata
origin grouping is not a second limiter: acquisition delegates to the existing
origin-specific reservation/backoff functions and their current receipt paths.

The normal handler tests cover source-ID substitution, legacy/generic refresh
isolation, replay/GET/no-I/O, authority/CSRF/revocation, responses/save/undo and
fresh-session identity persistence. Exact saved-reference URL restoration is a
separate narrow persistence decision: an opaque saved ID is not advertised as a
complete source reference. No new source store, native Enhance activation,
scheduler, source expansion, model call or private browser access is added.

### Bar Open / Make It Up Club (E5)

The exact official homepage was reachable but exposed a Gigs shell without dated
event text or event metadata. Its advertised Make It Up Club page was also
reachable and identified the weekly Tuesday recurrence and social links, without
a readable dated programme. Both exact paths were checked against the publisher's
robots rules; the same source-origin reservation persists across them. Only the
fixed Make It Up Club path is installed as the normal source acquisition.

The thin parser positively recognizes that page's heading, programme section and
recurrence statement. It produces `ProgrammeUnavailable`, which the shared driver
maps to the existing `unavailable` outcome using the closed descriptor's static
wording. Malformed/challenge HTML remains `invalid`; this is never a successful
empty programme, a date generated from Tuesday or a current-inventory claim.
The exception's text is not reflected to the person. No arbitrary/detail/social
endpoint or image is followed to fill the gap.

An actual guarded `event_source/bar_open` action at `8e2819c` made one fixed
GET200 (48914 bytes), returned that unavailable result and rendered normally.
Replay/reload made no extra acquisitions and wrote no taste history. The 21 pure
and 6 normal-handler checks are component evidence; Bar Open is still blocked
for the full working-source goal by the missing permitted dated programme.
It needs a usable current programme or a separately scoped legitimate visual
observation path; existing social links do not grant private access. This source
failure does not hold My Aeon or other assigned adapters.

### My Aeon (E5)

The adapter recognizes the exact official Webflow event header/list/card fields.
It preserves explicit calendar years, date-only precision and unknown show time,
timezone, admission, lineup and availability. Known dates are filtered to the
local eight-week window; unknown-year labels remain unresolved observations.
An unsupported explicit year/date format fails visibly rather than hiding an
old year as an unknown current lead. Exact URL/date duplicates may reconcile;
similar titles, different URLs or separate dates never bridge identities.
The shared 48-card/nine-lead limits and existing transient cache remain.

The actual official page still supplies January–April 2026 cards in November.
One normal guarded action at `46a5b51` fetched HTTP200 (27663 bytes) and returned
`unavailable`, not successful empty or a rolled-forward current programme.
Replay/GET extra acquisitions and acquired taste-history rows were zero. Current
card fixtures exercise the supported parser, but no current live event is claimed.

The first robots response was HTTP200 with exactly zero bytes. The prior reader
rejected it locally. The narrow correction recognizes a valid empty file with no
groups/rules under [RFC 9309 sections 2.2/2.2.1](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.2.1).
The exact original empty body was reinterpreted from its retained byte count/hash
without another source request. Nonempty unknown text, challenge HTML, denials,
transport failures and Forum's gate remain unchanged; existing stricter pacing
survives an empty file. This is protocol parsing, not a content/retention licence.

Saved focused evidence records 76 pure and 7 normal-handler checks passing after
the correction, while retaining the original recognition/test failures. No body,
poster or private/account endpoint is archived or followed. My Aeon's full
working-source acceptance still needs a permitted current programme, source
reference basis, rendered/browser evidence and independent acceptance.
