# Bounded gallery primary-source checker

`backend/shelf/gallery_sources.py` exports `fetch(now, *, transport=None)`,
`GalleryOutcome(Outcome)` and `GalleryEvent(Candidate)`. The outcome adds separate
`offers` and `source_checks` tuples. The injected test transport accepts one fixed
URL and returns `(http_status, bytes)`; it is never bound to a caller-supplied URL.
The runtime transport is `gallery_transport.read`.

The fixed scope is Gertrude's `/exhibition/et-cetera/`, APW's homepage and APW's
`/megan-cope/goompi`, plus the two hosts' robots files. Each explicit refresh has
at most five reads, a shared 512 KiB response budget and a thirty-second runtime
budget. Each HTTP request is bounded to five seconds (six seconds parent-side),
no redirects or retries. A later read may be deferred by the overall deadline;
accepted earlier facts remain available with individual failure statuses.
Robots receipts may be reused in-process for one hour; there are only two cache
keys. Missing robots (404) permits the fixed reads; unknown/unavailable robots,
disallowed listing paths and crawl delays above ten seconds stop that source.
An offer-only robots denial is reported at the offer stage while retaining the
permitted exhibition. Only an explicit robots 404 bypasses a per-path policy check. Each host
uses a private, locked owner-specific `/tmp` receipt, ten seconds between requests
and an hour's source-wide backoff after HTTP 403 or 429. Invalid interval state
fails closed. No alternate endpoints, public caches or identities are attempted.
The existing application owns the fifteen-minute result cache.

Gertrude's opening's omitted year is bound to the explicit year of that same
exhibition run. A conflicting weekday, missing AM/PM or changed expected markup
is rejected visibly as `parser-changed`. No date is rolled into a later year.
Only events within eight weeks whose opening or explicit exhibition ending has
not passed are retained. APW's closing time and exhibition run remain unknown.
`gallery_data` separates `opening_starts_at`/`opening_ends_at` from
`exhibition_starts_on`/`exhibition_ends_on`, and carries original date wording,
Melbourne timezone, source URL and check time. These are source facts, never
owner preference claims or attendance/ticket guarantees. Event identity includes
the source URL, title and explicit opening/run-start dates so a later exhibition
on a reused homepage cannot inherit an earlier exhibition's identity.

Goompi is a separate offer dictionary, never an event or a price-qualified
recommendation. Its current explicit GST-inclusive artwork price is preserved;
shipping, framing, fees, installation, currency conversion and stock remain
unknown because a listing is not a delivered checkout quote. The exact domestic
A$33-per-tube tariff is retained as conditional source wording, without turning
it into a known delivered shipping charge. Components conform
to the commerce gate's `status`, `amount` (known only), and `source_basis` schema.
The checker never authorizes a purchase. No images, scripts, JSON-LD contexts,
remote APIs, accounts, forms or cart requests are fetched or executed.

Primary pages were inspected on 31 October 2026. APW's terms permit retrieval
for information and forbid commercial exploitation:
https://www.australianprintworkshop.com/terms-use . This implementation is for
private informational metadata retrieval. Gertrude robots returned 404; APW
robots returned 200 with Crawl-delay 10 and no denial of these paths. The evidence
owner's `gallery-evidence/current-source-*` records preserve inspection lineage.
Packaged `gallery_*.html` test fixtures contain only exact minimal factual markup
excerpts from those pages; they are neither full copyrighted page copies nor
runtime seeds. Fixtures never reseed live results after failure.

Follow the prepared runtime, clean source-copy location and measured scratch
prerequisites in [README.md](README.md#reproduce-verification) before running
verification. From that supported environment run the whole suite:

```sh
.venv/bin/python -B backend/manage.py test
```

The entrypoint accepts the whole-suite command only; positional test labels are
rejected. Do not override the project boundary, settings, paths or loaders to
run verification.
No migration or model changes are required. Removing the source dispatch and
these modules rolls back this checker; previously durable records remain owned
by existing application workflows. Live access or future programme/stock
coverage cannot be established by local fixture tests.
