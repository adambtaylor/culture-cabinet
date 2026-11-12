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

### Saved references (EVENT-SCRAPER-02B)

The optional `event.saved_reference` contains exactly schema, source_id,
official_url, occurrence_ref, occurrence_basis and retention_policy_ref, bounded
to 3072 UTF-8 bytes. Only a current server-owned event on Save or Respond can
produce it. Existing whole-request receipts, human choices and save/Undo semantics
remain unchanged. No title, date, body, image, availability or new taste card is
stored through this field. Legacy rows are never backfilled from old card content.

Persistent bookmark permission is separate from the 900-second content cache.
Descriptors must supply a reviewed positive `reference_policy_ref`, a fixed
`restriction_namespace`, and an exact public `reference_url_validator`. Missing
policy means the existing opaque identity fallback, not permission by default.
Architecture's 02B1 disposition leaves all five production policies null:
no inspected source record establishes durable bookmark retention. This is a
missing basis, not a finding that private bookmarks are prohibited. The reserved
internal production identifier `event-private-bookmark-reference/v1` grants no
permission. Tests supply `fixture-event-private-bookmark-reference/v1` only in
fictional descriptors; production lookup rejects those references.

`SOURCE_NAMESPACES` is the immutable literal map for PBS, RRR, 303, Forum and
Moshtix to their respective `event-source:<source_id>` identities. No prefix
parser recognizes arbitrary source IDs. `events` is only the existing Moshtix
operation alias, not another restriction namespace. Restrictions remain valid
with no references or FeedbackTarget and when an active descriptor/policy is
missing. Recovery consumes the fixed map rather than inferring from labels or
requiring present references. The helper's optional `namespace_map` input defaults
to this immutable map; the original `descriptors` argument stays compatible.

After cache loss, a validated reference uses `ReferenceCandidate`, whose `url`
stays empty. `_display` supplies `reference_only` and the detached optional
`saved_reference` separately to Experience's stale-link conditional. The source
link does not assert navigation to an exact session, current content or stock.
Reference-only identities are excluded from fresh recommendation ranking, remain
available for the person's existing response/save actions and cannot refill a
reference on a later Save. Fresh source candidates take precedence by exact key.

`reference_only` means expired event history, including the real null-policy
opaque fallback. It does not mean permission to retain a link. A non-null
`saved_reference` independently identifies the checked permitted optional link;
null means render a stale opaque identity without a link. A current source card
without a URL still has `reference_only=False`. Consumers must use these explicit
fields rather than infer expiry or permission from a title or missing URL.

The pure helper exports `make_reference`, `validate_reference`,
`project_references`, `redact_history` and `referenced_namespaces`. Callers hold
the existing source-restriction barrier through reads/rendering or mutation;
current denied retention hides links without GET purges or source calls. Taste
owns the separate recovery transaction/replay/restore tail consuming the frozen
helper; Experience owns the template and actual rendered evidence. The helper's
detached redaction removes only this new field from every affected historical
action, including undone saves. Component handler tests do not establish that
separate recovery/restore or browser acceptance.

### Coherent producer, interface and recovery candidate

The E4 composition consumes PR83's producer/opaque-history corrections, PR84's
source controls, PR85's corrected native history forms and PR90's recovery tail.
The authored template and recovery files are consumed unchanged. Reference-only
response forms use a full native POST: the partial response endpoint retains its
current-deck membership guard and refuses an expired history row.

`test_event_reference_integration.py` exercises those actual rendered forms with
CSRF checks, a mocked PBS response and both fictional positive-reference and
ordinary production-null policies. It checks expiry, recommendation exclusion,
response/replay, save/Undo and a fresh authenticated session. Its separate isolated
recovery case creates the reference through normal handlers, applies an actual
source restriction without a FeedbackTarget and restores a pre-restriction backup.
Only the optional reference is purged; choices, saved identity, receipts and nonce
survive, and Undo cannot restore the removed reference. It uses the accepted
isolated migrated-memory recovery fixture pattern without changing production
authority, recovery or SQL guards.

These are mocked-source handler/recovery checks, not live acquisition, real browser
acceptance, a new retention permission or acceptance of all 35 sources. The first
combined run retained three old assertions for raw status labels; the checks now
assert both the actual outcome status and the interface's authored human wording.

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

### Affordable Art Fair Melbourne access hold (E6)

The exact robots readiness request returned HTTP403 (5495 bytes). It supplied
no usable policy decision; this does not establish a permanent publisher ban.
The fixed Melbourne listing remains closed before reservation or I/O, and its
closed descriptor reports access unconfirmed/permission-required with no guessed
fair dates, sessions, artists, stock or current empty state. No listing, alternate
route, redirect, login, archive or hidden endpoint was requested.

The normal guarded action records a source-local unavailable-access result;
GET and nonce replay make no request and write no taste history. Tests cover the
exact robots target, closed listing gate, zero-I/O collector and actual normal
authenticated action/render/replay. They establish safe handling of this blocker,
not fair acquisition, a verified run/session parser, browser or working acceptance.
Legitimate access evidence is still required before the listing can be read.

### Improv Conspiracy calendar readiness (E7)

The assigned homepage and robots paths use the existing fixed transport and one
ten-second origin reservation. Robots404 was permitted under the current reader.
The single bounded homepage attempt failed before usable HTTP with
`PublicTransportError(reason=child_exit)`. No current programme, advertised
calendar link or show/session format was obtained. The earlier research mention
of a Performance Calendar is not a verified endpoint and no calendar path is
guessed. No retry, alternative route, image, account or workshop acquisition occurs.

The unimplemented descriptor explicitly reports that current programme/calendar
evidence is missing. Its normal action renders unavailable; GET/replay make no
source request or taste write. Fixed-target, shared-origin and authenticated
normal-action checks cover that boundary without fabricating event rows. This is
a readiness component with a retained live failure, not working acquisition.

### Triple J / Double J shared-origin access boundary (E7)

Both fixed touring targets and their common robots URL reserve one `abc` origin
receipt through the existing event transport. Existing source receipt names and
history stay unchanged. A stricter publisher delay persists in the shared receipt
and applies across both source IDs, including failed sends and policy reads. No
second client, independent source allowance, fan-out or new posted URL is added.

One robots200 read (2311 bytes) was evaluated for both touring paths. Its wildcard
rules allow the configured CultureCabinet name, but it also explicitly restricts
ChatGPT-User, GPTBot and other named AI agents. That wildcard result does not
establish permission for this agent-controlled session. Both production listing
gates remain closed, and normal actions report permission-required with no source
I/O. No touring or Double J news page, alternate user agent, redirect or bypass
was attempted. A permitted source route/basis remains outstanding; no permanent
publisher-wide ban or general metadata licence is inferred.

Tests cover one shared stricter delay, fixed destinations, unchanged independent
source receipt names, zero-I/O gates and authenticated action/replay/render with
no taste writes. They prove readiness and truthful access handling, not current
Melbourne gig acquisition, article-as-gig semantics or working-source acceptance.

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

### West Space programme runs (E6)

The fixed official `/whats-on/` page contains rendered programme cards with
separate title, artist and explicit date-range text nodes. The thin adapter reads
only those fields and their same-origin `/program/` links. It does not execute
Gatsby scripts, fetch detail pages/images, adopt artwork offers or infer a
performance lineup from an artist label. The observed robots path returned 404;
the existing reader's 4xx rules and ten-second shared origin interval apply.

Programme ranges remain separate from openings, hours and performances:
`starts_at`, `ends_at` and `opening_date` stay unset. The existing pure gallery
overlap helper selects current runs and runs starting within eight weeks; no
gallery acquisition client is used. An abbreviated range takes its calendar-year
context from the publisher's trailing year, including a November-to-January wrap,
never from the retrieval date. A complete explicit range binds occurrence identity.
Changed markup is invalid; recognized out-of-window-only cards are unavailable,
not a verified current empty programme. The shared bounds remain 512 KiB,
48 scanned cards, nine leads and fifteen-minute content retention.

Fictional markup tests exercise normal guarded acquisition, replay/GET without
additional reads, run rendering and explicit response/save/Undo/fresh-session
identity persistence. They are component checks, not source-level real browser or
independent working acceptance. Persistent source-link permission is not supplied.

The actual guarded source action at `c1ab2a9` made one fixed HTTP200 read
(15774 bytes) and returned two current overlapping programme runs, with explicit
range occurrence identities and no invented opening/performance times. The May
2027 card was outside the eight-week start window. POST/replay returned302 and
render returned200; the single-request assertion covers replay and reload, and
history remained empty. Only bounded fact/diagnostic evidence was retained, no
page body or images. This is live normal-handler evidence, not a real rendered
browser, source retention grant or independent working-source acceptance.

### NGV closed routes and access evidence (E6 / 02C)

NGV has two fixed event-index routes: `programs` (the default) and `exhibitions`.
Both and the single robots target use the existing `ngv` origin reservation,
cross-process lock, clock identity and publisher-delay floor. Other source
receipt names stay unchanged. One action selects one page; there is no fan-out,
second network client or gallery/native acquisition dependency. Unknown route
values, non-string internal inputs and nonempty routes for other sources are
rejected before source acquisition.

The existing guarded action accepts optional `event_route` only with
`operation=event_source, source_id=ngv`. Its absence remains absent in cleaned
receipt input; the whole original cleaned dictionary is still hashed. An explicit
route cannot be substituted into a prior nonce receipt, even when it spells the
default. NGV outcomes/cache entries use `(ngv, programs, '')` and
`(ngv, exhibitions, '')` independently, and their labels identify the index.
Failures in one do not erase the other. Existing candidate-key deduplication
keeps a shared occurrence from becoming two reaction identities. Normal sources
keep their accepted keys, call signatures and outcomes.

`source_controls` exposes two plain `event_routes` dictionaries for NGV only;
each has `event_route` and `label`. Experience owns any route-specific controls.
Acquisition and parser ports receive the normalized route keyword only for NGV.
The live NGV descriptor remains **unimplemented/unavailable**: fixture injection
in route tests proves wiring, not a parser or current programme. No title/date
schema has been invented from the unobserved source page.

One exact robots read returned200/211 bytes and allowed both paths, with no
publisher delay beyond the ten-second floor. That same body evaluated both paths.
The first fixed programs-page format read timed out; no body or source date
format was obtained and no retry was made. Exhibitions has not been acquired and
stays separately closed. Robots access is not a model, redistribution or durable
bookmark grant. The inspected main-site terms concern ticketing/membership; store
terms concern a different purchase site. Neither supplies a persistent event-link
policy. No policy, credential, native Enhance, ticket or calendar action is added.

The isolated tests cover both routes sharing stricter pacing, exact destinations,
default/explicit-route nonce binding, zero-I/O GET/replay, per-route partial
failure, duplicate occurrence identity, current grant revalidation and truthful
unavailable production descriptors. Real NGV parsing/acquisition and affected
rendered acceptance remain outstanding; this package is a complete route caller
and transport prerequisite, not a working-source claim.

### Linden current public programmes (E6)

The thin Linden adapter reads only the fixed official public-programmes page's
`s8-datarepeater1_ajaxcontainer` region. The separately identified past-events
repeater is excluded. Each current boxed card supplies one heading, date/time
paragraph and one consistent official `/event-rsvp/` destination; no RSVP,
detail, archive pagination, script or image endpoint is requested.

The exact robots read returned HTTP 200/77 bytes and allowed the page at the
existing ten-second origin floor. Visible text boundaries preserve dates, clocks,
venue/audience notices and labels such as `Ages 2+` and `Ages 8-16`. Names and age
labels never create verified artist identities, public eligibility, prices or
ticket availability.

Explicit weekday/calendar dates are checked; missing years stay unknown. Local
clock ranges distinguish occurrences without inventing a timezone or absolute
instant. An overnight clock without a following date does not gain a guessed end
day. Session-scoped cancellations reconcile before the eight-week projection;
malformed/challenge pages are invalid and recognized out-of-window-only cards
are unavailable. Shared limits remain 512 KiB, 48 scanned cards, nine leads and
fifteen-minute content retention. No durable source-reference grant is supplied.

Prior evidence records 29 focused parser/transport/registry/engine/normal-handler
checks passing. One actual guarded action made one fixed HTTP 200 read and
returned four current events with source dates, local clock ranges and audience
notices. POST/replay and render evidence is separate from browser acceptance and
independent working-source acceptance. Retain the initial failed inspection;
no automatic retry or extra source read is implied.
