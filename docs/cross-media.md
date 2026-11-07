# Offline cross-media projection

This isolated backend implements the approved [design](../.requirements/cross-media-design.md).
It adds no route, model, migration, production store or dependency. The existing
provider intake selector and authoritative preferences are unchanged. Its richer
lanes deliberately require a separate caller; they are not substituted into the
live funnel. No inference, embeddings or provider requests run here.

`cross_media.py` validates namespaced source snapshots, projects supplied provider
rows, resolves supplied factual documents, and returns fixed sample contexts.
`cross_media_ranking.py` replays supplied durable event dictionaries and builds
seed lanes. Both use only standard Python. All APIs return copied plain data;
only `sample_context` performs I/O, reading its fixed bundled JSON fixture.

## Source and snapshot APIs

Import from `shelf.cross_media`:

- `identity(provider, medium, source_id)` and `canonical_url(key, slug=None)`:
  strict namespaces for Hardcover books, TMDB movies/TV, Wikidata entities and
  Open Library works/editions/authors. URLs are constructed from validated IDs;
  Hardcover needs a verified source slug, otherwise its URL is null.
- `hardcover_projection(book, rows, *, provenance='supplied')`: native
  `cached_similar_book_ids` list, nullable `canonical_id` and
  `cached_similar_books_updated_at`; shuffled/duplicate details preserve source
  ID order and original rank gaps when details are missing. Freshness comes only
  from the supplied timestamp. It does not invoke the existing helper that
  stamps parsing time as fetch time.
- `tmdb_projection(medium, payload, *, relation='tmdb-recommendation',
  checked_at=None, provenance='supplied')`: native `results`, `id`, and
  `title`/`name`. Recommendations and genre/keyword similarity retain different
  relation kinds. Neither asserts a proven population of people who co-like items.
- `resolve_documents(entities, *, redirects=None, external_ids=None,
  provenance='supplied')`: entity envelopes use `id`, optional `lastrevid`,
  `labels.en.value`, and `claims.Pxxx[].mainsnak.datavalue.value`. Redirects are
  supplied `{from,to}` pairs. TMDB documents are `{medium,id,external_ids:
  {wikidata_id}}`; this wrapper explicitly names the source medium.
  Deprecated statements are discarded before parsing values. Missing/unknown
  claims add no false facts. Invalid documents raise `InvalidData`.
- `resolve_alias(key, aliases)`: caller-supplied verified alias table, mapping
  namespaced keys to lists of targets. Singleton chains resolve; conflicting
  targets are ambiguous; cycles/depth exhaustion fail. Aliases cannot cross
  namespaces. Missing aliases leave an identity unchanged.
- `isbn_join(work_id, works, editions, hardcover_editions, books)`: supplied OL
  `/works/OL...W` documents, edition `/books/OL...M` documents whose native
  `works:[{key:...}]` references match, checksum-valid `isbn_10`/`isbn_13`
  arrays, Hardcover editions' nullable `isbn_10`/`isbn_13` strings and native
  `book_id`, followed by Hardcover books' `id`/nullable `canonical_id`.
  Normalized ISBN10/13 equivalents join; conflicting canonical books remain
  ambiguous. No title joins or invented Hardcover Open Library fields.

`Cache(data=None)` validates and copies `{schema:'cross-media-cache-v1', nodes,
edges, records}`. `snapshot()` returns a copy; `to_json()` and
`Cache.from_json(text)` round-trip the full projection across an offline restart.
The caller owns persistence. No file/database store is provided. Nodes contain
identity, title, canonical URL, provenance and supplied/unknown freshness. Edges
contain canonical source/target, kind, original provider rank and combined
evidence. Records are `{key,outcome}`. `outcome(status, items=None, *,
checked_at=None, retry_at=None, expires_at=None, reason=None)` distinguishes
`items`, `empty`, `ambiguous`, `failed`, `timeout` and `rate-limited` (e.g. a
supplied HTTP 429 result). Outcome examples are simulated, not captured failures.
Known timestamps must include an offset; null remains unknown. There is no clock
read, automatic expiry extension, retry, fetch or freshness invention. Consumers
may compare the retained expiry against their explicitly supplied clock.

P144 is **based on**, not a claim of faithful adaptation. P4969 supplies the
inverse direction and merges evidence at identical canonical endpoints. Traversal
steps retain the original direction and add `traversal`, `from_key`, `to_key`.
P4947/P4983 preserve movie/TV identity; P648 distinguishes W/M/A suffixes. Author
references remain metadata and cannot bridge books or become book suggestions.
Available statement IDs, revisions, claim rank and canonical Wikidata source
links are retained. Reconstructed data cannot claim known revision, statement ID
or freshness. Synthetic adversarial cases explicitly label synthetic identifiers.

## History and ranking

`replay(run, *, actor_id, profile_id, run_id, expected_revision)` and
`rank(run, cache, *, actor_id, profile_id, run_id, expected_revision, size=15)`
are pure. A run is `{actor_id,profile_id,id,revision,history}` with optional null
`archived_at`; `id` is the supplied string form of the durable run primary key.
Actor/profile/run/revision mismatches and archived runs raise `ContextMismatch`.
These checks do not establish runtime authentication or access authority; an
integrating caller must supply already-authorized data. No ORM/session is read.

Events retain the actual durable shape:

```python
{
    'card': {'key': 'tmdb:movie:81001', 'provider': 'tmdb', 'medium': 'movie',
             'source_id': 81001, 'title': 'SYNTHETIC example',
             'url': 'https://www.themoviedb.org/movie/81001',
             'fetched_at': '2026-01-01T00:00:00+00:00',
             'lane': 'synthetic-test', 'origin': 'neighbor', 'root': ''},
    'choice': 'enjoyed', 'operation': 'choose',
    'responded_at': '2026-01-01T00:00:00+00:00',
    'recorded_at': '2026-01-01T00:00:00+00:00',
    'provenance': 'authenticated-human-post',
}
```

The example is wholly fictional. `choose`/`correct` replace the latest choice for
that key. `undo` with null choice clears it, without restoring an older value.
Imports retain `operation='import'`, `responded_at=None` and
`provenance='server-session-v1; original response time unknown'`. Imported
validated human choices can seed lanes, while their original response time
remains unknown. Replay retains their provenance without relabeling it.

`enjoyed` seeds an experienced lane; `interested` seeds an anticipated lane.
A genuine human reaction to a neighbor establishes a new root. Suggestions,
exposures, saves/opens, unknown and inferred facts create no roots. Unsupported
nonresponse history operations fail validation. Empty/no-positive history returns
`needs-feedback` with broad-exploration text and no invented taste. Negative,
unknown and cleared positives recompute lanes on replay. Already judged cards
are excluded from suggestions. A source cache contains no private run context.

Default seed order is experienced first, then anticipated, in effective stored
response order (corrections move a key to the latest position). Up to five roots
have independent lanes. Within each lane, provider-neighbor rank precedes factual
expansion, then fewer facts/shorter path/key break ties. Multiple paths to a card
choose the first deterministic path in that lane; multiple lane memberships and
their exact paths remain in the deduplicated round-robin selection. No taste
vector is averaged. Source exhaustion leaves empty lanes without claiming an
error or successful live coverage.

## Fixed implementation limits

These are heuristics and resource defaults, not user facts:

| Budget | Limit |
| --- | ---: |
| JSON snapshot bytes | 2 MiB |
| Nodes / edges / outcome records | 512 / 2048 / 128 |
| Native ID/alias/document lists | 1000 |
| Provider detail rows / claims per property | 100 / 100 |
| Combined evidence records per edge | 16 |
| Alias hops / total simple path steps | 8 / 8 |
| Traversal states and returned paths per seed | 512 |
| Current human lanes / members per lane | 5 / 100 |
| Provider-neighbor / factual hops per root | 1 / 2 |
| Durable history events | 2000 |
| Selection default / maximum | 15 / 50 |

Identity mappings cost no factual hop but still consume total path steps and
visited-node budgets. Neighbor links traverse only forward. Factual and identity
links can traverse in either direction with truthful explanation direction.
Cycles never repeat a path node. A deterministic traversal cap can omit later
paths; lane `path_budget_reached` flags that limit conservatively. Counts reaching
a cap do not imply complete coverage. Structural overflow raises `InvalidData`.

## Sample UI handoff

`sample_context(trail_id)` accepts exactly `dune`, `dune-messiah`, and
`synthetic-lanes`. It returns fresh plain dictionaries/lists. Invalid types or
selectors raise `ValueError` before fixture I/O; M should convert this to 404.
Default selection, if the query parameter is absent, should be `dune`. The fixed
fixture path is `backend/shelf/fixtures/cross_media_sample.json`. The precise
context fields and representative contexts are recorded in
`../${PRIVATE_RUNTIME_PATH}`.

The research trails reconstruct only the supplied Dune/Messiah public facts and
redirect/deprecation trap; they are not captured API payloads. Source statement
IDs/revisions/freshness stay unknown. Known-work Hardcover/ISBN mappings remain
unresolved. Separate wholly fictional data demonstrates checksum joins. The five
lane example uses simulated rankings and fictional human responses; none are
the owner's likes. No license or live-source coverage is asserted. Research basis and
source semantics come from the approved design; no external research was run.

M owns the authenticated read-only `samples/cross-media/` route, navigation,
template and UI tests. Render plain values with normal autoescaping. Use
`temporary_notice` and sample labels to qualify inherited catalogue claims.
Use descriptive canonical links, with `noopener noreferrer` for new tabs. No
remote assets, feedback controls, provider `_state`/`_context`, durable hydration,
profile/session mutation or live requests belong in this sample view.

## Verification and removal

Follow [scratch prerequisites](README.md#reproduce-verification), with the
existing Python 3.14.8 and a private 0700 `TMPDIR`. Run the two pure backend test
modules from `backend/`:

```sh
${PRIVATE_LOCAL_PATH} -B -m unittest shelf.tests.test_cross_media shelf.tests.test_cross_media_ranking -v
```

Run the full suite, including Django UI tests, and checks through the standard
runner from the source root:

```sh
${PRIVATE_LOCAL_PATH} -B backend/manage.py test --verbosity=2
${PRIVATE_LOCAL_PATH} -B backend/manage.py check
${PRIVATE_LOCAL_PATH} -B backend/manage.py makemigrations --check --dry-run
```

The normal entrypoint remains unchanged, including its whole-suite guard. Tests
use synthetic memory/scratch data. No real-user DB or runtime service is used.
Removing these six additions removes this isolated feature; no schema rollback
or authoritative preference conversion is involved. Independent source review
remains separate from local functional test evidence.
