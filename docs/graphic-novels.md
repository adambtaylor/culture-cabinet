# Graphic novels in the private catalogue

Graphic novels use Hardcover's documented integer `book_category_id=4`.
They remain `medium=book`, with `hardcover:book:<id>` identities, existing
To read / Favourites hearts, and existing human response history. There is no
migration, new medium, title-based merge, or durable identity rewrite. The author
route supplied for this task is `gpt-6-astra/xhigh/default`; this is implementation
and source-only verification, not an independent review verdict.

The authenticated catalogue and intake page offers **Fetch graphic novels** and
an optional literal **Title contains (this page only)** filter. Each explicit, revision/nonce and
CSRF protected POST reads one Hardcover category-filtered page of at most nine
books, ordered by source popularity then ID. The optional case-insensitive title
substring is applied locally to that page, after validating every returned book.
It does not search the whole category; zero local matches do not suppress the next
source page. Explicit next-page fetches stop at
ten pages. Ordinary Book search still searches every book category. A short,
empty, already-judged or cache-limited page never claims to exhaust books.
Offset pages can shift with popularity; there is no complete-inventory claim.
GET never fetches. Existing five-call initial deck, two-call book search/neighbor
budgets, request size/time bounds, 120-card cache and one-hour expiry remain.
The new browse action uses one call, with at most twelve series memberships and
twelve editions per returned book; these nested lists are partial metadata.

Cards display source classification and escaped synopsis, source series identity,
nullable/fractional position, details, compilation status and subordinate edition
summaries. Result series groups use numeric series IDs, preserving result rank
and separate volume/omnibus book IDs, even when series names coincide. Repeated
edition IDs are deduplicated beneath their owning work; mismatched parents and
conflicting metadata are flagged per field: agreement retains the fact, while
disagreement clears that value and remains ambiguous through further duplicates,
regardless of row order. Format/information conflicts do not invalidate an agreed
canonical reference. Editions never create candidates, hearts or
responses. `canonical_id` remains a source reference, including unresolved,
invalid, ambiguous or cyclic references. Cycles are detected within the supplied
page/edition list; no external target resolution is attempted. No work aliases
are automatically merged, so existing durable identities remain unchanged.

Optional metadata is session-only. Missing fields/old cache cards are unknown;
explicit null/empty values are absent; malformed values are invalid. Unsupported
category integers remain unclassified. Titles, series names and adaptations never
establish graphic-novel classification. A category browse response containing a
non-4 category fails explicitly rather than silently relabelling or filtering it.
An explicit fetch can enrich old cached book metadata without changing its lane,
root, key or cache expiry. Durable snapshots continue to exclude this metadata;
existing saved items, receipts, histories and migrations retain their contracts.

Covers are **external links**, not inline images. Only validated HTTPS DNS URLs
without credentials, unsafe ports, controls, backslashes or fragments are linked;
known local/reserved hosts and numeric addresses are rejected. Links open a new
tab with `noopener noreferrer` and `referrerpolicy=no-referrer`. These checks do
not authenticate the image host or assert official publisher status. No server
image fetch, proxy, host allowlist or CSP expansion was added. The canonical
Hardcover book page remains the deeper-dive fallback. Publisher/official-preview
links are explicitly unavailable because the supplied evidence does not establish
the public `links` JSON shape. That field is neither queried nor dumped.

The existing book/film/TV graph and provider-neighbor lanes retain their identity
and feedback contracts. Tests use clearly synthetic memoir, reportage/history,
literary/slice-of-life, fantasy/SF, manga and superhero books, typed synthetic
P144 based-on statements and adaptation-credit evidence. They exercise graph
provenance, separate prose/graphic works, human-root fairness, negative feedback,
correction/restart, and labelled capped inferred theme transfer. There is no new
live cross-media adapter; Hardcover cached similar IDs are provider neighbors,
not verified population co-like data. Fetch/add/save/unknown/click are not likes.
These fixtures express neither the owner's tastes nor actual provider coverage.

The supplied official Hardcover references are [Books](https://raw.githubusercontent.com/hardcoverapp/hardcover-docs/main/src/content/docs/api/GraphQL/Schemas/Books.mdx),
[BookSeries](https://raw.githubusercontent.com/hardcoverapp/hardcover-docs/main/src/content/docs/api/GraphQL/Schemas/BookSeries.mdx),
[Images](https://raw.githubusercontent.com/hardcoverapp/hardcover-docs/main/src/content/docs/api/GraphQL/Schemas/Images.mdx)
and the [GraphQL schema](https://raw.githubusercontent.com/hardcoverapp/hardcover-docs/main/schema.graphql).
These document integer category 4 as Graphic Novel (1 Book, 10 Light Novel),
nullable/fractional series positions and compilation membership. The original
slice used offline tests only; the P3.18 live correction is recorded below.
Offline Django client tests render real
forms/cards and exercise own-profile authority, stale/replayed actions, saved
intent, old caches and expiry. Author test results and the stable source manifest
in `${PRIVATE_RUNTIME_PATH}` are external evidence, excluded from the shipped source.

Deployment remains separate: integrate the stable candidate after the required
independent review, then restart the existing private catalogue runtime through
its unchanged guarded deployment process. No migration is needed for this slice;
any existing runtime preparation requirements remain. This isolated source tree
was not deployed and no service was started.

## P3.18 live query correction

On 30 October 2026, bounded read-only requests through the existing private
credential route returned HTTP 200 for the ordinary six-book deck but HTTP 403
for category browsing. The original category document always included `_ilike`,
even with an empty title. Hardcover's official
[API limitations](https://raw.githubusercontent.com/hardcoverapp/hardcover-docs/main/src/content/docs/api/Getting-Started.mdx)
explicitly disable this operator for API tokens. A 403 is therefore not sufficient
evidence that this credential has expired or needs broader scopes.

The corrected document requests category 4 with its existing numeric offset,
nine-book page and stable ordering, without any disabled text-search operators.
No grant, key, endpoint, credential transport or response bound changes. The
local title filter is deliberately page-scoped: the supported
[search endpoint](https://raw.githubusercontent.com/hardcoverapp/hardcover-docs/main/src/content/docs/api/guides/Searching.mdx)
does not provide a category filter, so a filtered mixed search window would not
be an equivalent full-category title search.

The corrected `browse_books` path returned HTTP 200 and nine normalized cards
in one live request at 05:27 UTC, including Watchmen and Heartstopper: Volume One.
Forty focused provider/metadata/graphic-novel tests passed, followed by the new
HTTP regression for an empty local title-filter page retaining navigation.

Evidence is kept outside shipped source in `dispatcher-evidence/hardcover*`.
The live checks record HTTP status, bounded counts and public book references,
without raw provider response bodies, credentials or account metadata. This
correction still requires the combined candidate's independent review and
deployment; an isolated live request does not establish a deployed UI result.
