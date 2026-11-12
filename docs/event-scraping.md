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
coverage and retention limits. The initial E4 slice connected five existing producers:
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

### 24 Moons (E5 first slice)

`moons24` adds only the fixed official `/events` programme and its exact robots
target to the shared bounded listing transport. The first robots response allowed
the path with the existing ten-second minimum; listing and robots reads share
that origin's existing reservation mechanism. No seller, archive, social page,
detail link, API or script endpoint is fetched. Other source gates stay intact.

The small parser recognizes BOTH `this-week` and `further-upcoming` sections.
An empty later-events section does not erase the current-week cards. Card names
come from the event anchor's heading, not the Friday/Saturday wrapper heading.
Explicit dates and AEDT/AEST offsets are checked against Melbourne on both days
of an overnight range. Missing years/offsets remain unknown and are never filled
from the week or retrieval date. Stale-only, malformed or unrecognized programmes
fail visibly; a bounded empty result requires explicit empty markers and a
current recognized week. Existing 48-card/9-lead/eight-week limits remain.

Official links open the programme, whose card anchors advertise external sellers;
they do not claim to navigate to an exact session or establish available tickets.
Occurrence identities use explicit ranges. Conflicting simultaneous cards on
that shared programme URL retain distinct unresolved snapshot observations;
titles do not merge them. Source tags remain separate attributed notices.

The actual guarded source action at `0e56fc7` acquired one HTTP200 programme
response (26167 bytes) and produced two current dated leads with correct card
names, replay/GET extra acquisitions zero and taste-history rows zero. The earlier
`4c0337f` handler observation had correct dates but incorrectly used weekday titles;
that semantic failure and its correction are retained separately. These receipts
are live acquisition/handler evidence, not a browser or working-source acceptance.
No body or image archive, durable source-reference grant or native activation is
introduced. The 24 Moons release remains independent of PR83's recovery/UI tails.

### APW event-only homepage delegation (E6)

The event source calls the existing immutable `gallery_transport.read` for the
fixed APW homepage. It shares that reader's host lock, ten-second pacing and
one-hour refusal backoff; no second limiter/client is installed. Its nonwaiting
public-read callback refuses a busy origin instead of sleeping or retrying;
the enclosing normal discovery handler revalidates profile authority. Robots
readiness is explicit and separate. The observed robots200 permitted the homepage
with Crawl-delay10, matching the existing owner. No gallery transport, native
authority, offer parser or aggregate gallery fetch is changed or invoked.

The homepage has several promotional hero panels. The source leaf selects only
one explicitly dated event panel and passes its exact escaped heading/opening/run
labels to the existing pure APW field validator. Original response provenance and
hash remain authoritative. Promotional panels do not become events. Opening and
exhibition run produce distinct occurrence identities; title changes do not change
those identities. The run has calendar dates, not invented daily sessions. A past
opening with missing run dates is unavailable, not a current exhibition claim.

Initial fixture failures came from missing whitespace in synthetic title markup
and inconsistent test clocks; those runs remain recorded. The19-method component
run retained one stale-fixture clock error (18PASS), whose focused correction then
passed. After the actual multi-hero selector correction, all seven affected APW
methods pass. These are qualified checks, not a claimed single green aggregate.

The first actual normal action acquired200/26971 bytes but returned invalid under
the old single-hero assumption. One necessary field inspection identified the
multiple panels and the sole dated opening (6 November2026), without run dates.
The corrected normal action at `a20e516` encountered SourceError before usable HTTP;
its precise transport subtype is unverified. POST/replay302 and render200 still
made only one source attempt and no taste history. Both failures are retained,
with no automatic retry. No current APW acquisition, real browser, persistent
source-reference permission or independently working-source acceptance is claimed.
