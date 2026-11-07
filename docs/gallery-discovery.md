# Gallery discovery: source observations and looking experiments

Gallery source cards are temporary discovery observations. Exhibition runs, opening
sessions, source wording, availability, admission and artwork offers stay distinct.
A source observation is neither a calendar booking nor purchase authority. Source
adapters and the shared public discovery reducer own fetching and disclosure;
`gallery_curation.py` and `gallery_commerce.py` are pure functions with no transport,
model writes, subscriptions, accounts, shopping or device actions.

## Authorized editorial seed

The complete *Art taste and discovery brief for Cultural Cabinet*, prepared
31 October 2026, was read as the editorial seed. Library identity:
`libfile_4cca1f8741448191b3ae93ae64ddfcae`, version 0, SHA-256
`08c39a725e7612da7725adfaab6357d09b499aa185506b282da469fe4214364a`.
The local materialized Markdown hash was checked against that supplied identity.
The three exact principle quotations come from the authorized packet's
`owner_qualitative_principle.verbatim_quotes`; the code exposes them intact through
`principle_quotes`. They concern entering an image, revisiting it, and shared
interpretation. No numeric ratings or favourites are manufactured.

The seed belongs only to actor 1 and profile 1. `reasons`, `select` and
`principle_quotes` return empty tuples for every other identity, including boolean
IDs. The caller must also authorize the authenticated actor and owner profile;
these pure helpers are not an authentication system. Lisa, Family/kids and Together
must not receive these personal quotations or reasons by projection.

`reasons(card, *, actor_id, profile_id)` returns a tuple of short editorial reasons.
`card.gallery_data['artists']` contains source-attributed names; a known artist
can connect to an artist-level qualitative seed, without endorsing this work.
Exploratory artists remain explicit curatorial hypotheses. Venue-specific prompts
propose comparisons, rather than asserting unseen image qualities. Source facts
remain on the original card. Title text is never scanned for a depicted artist.
Quiet architecture, expressive or symbolic depth, landscape, awe and storms all
remain routes into immersion. Complexity and busyness are not relevance scores.
A discussion may change an encounter without establishing anyone else's taste.

`select(cards, *, actor_id, profile_id, limit=6)` rotates affinity, bridge and
unfamiliar buckets, preserving source order within each and returning original
cards. This is adjustable editorial ordering, not an inferred preferred ratio or a
rating. No revisit history is invented: a revisit needs an actual prior encounter.
There is no new taste store and the staged observations and seeds are unchanged.

## Strict all-in price contract

`evaluate_offer(offer, *, now)` returns a frozen `PriceGate` with `qualified`,
`total_aud` (a decimal string or `None`), `reasons` and `purchase_authorized=False`.
`dataclasses.asdict` can project it for display. A passing gate means only the
stated current cost and availability checks pass; exact purchase approval remains
required and practical fit, condition, rights and seller terms remain separate.

An offer requires nonempty `offer_id` and `offer_url`, `currency='AUD'`,
`availability='available'`, and an aware ISO `checked_at` no more than 15 minutes
before the supplied aware `now`. Missing, future, stale, unknown and sold-out
availability cannot qualify. This is a source assertion, not a reservation or a
promise that stock cannot change.

`components` must contain each of `artwork`, `shipping`, `framing`, `fees`, `tax`,
`installation`, and `fx`. Unrecognised components fail closed until reconciled
into these costs. Every component requires explicit nonempty `source_basis` and
one of:

- `known`: `amount` is a nonnegative finite AUD decimal string, integer or Decimal,
  expressing exact cents. Binary floats, booleans and sub-cent amounts are rejected.
- `included`: the source explicitly says this charge is already included in the
  artwork price; it contributes zero additional cost.
- `not_required`: an explicit basis says why this cost is unnecessary for the exact
  object and destination; it contributes zero additional cost.
- `unknown`: always blocks qualification, even if accompanied by a numeric zero.

Artwork itself must be `known`. For included or unnecessary charges, an optional
amount must be exactly zero. An optional component currency must be `AUD`. Absence
of a quote, omission from a listing, an AUD-looking symbol, or an unframed thumbnail
never establishes a zero framing, delivery, fee, tax, installation or conversion
cost. Included GST may be represented explicitly without counting it twice.

The sum uses integer cents, without rounding or decimal-context precision loss.
A complete total must be **strictly less than AUD 2500**. AUD 2499.99 passes the
numeric boundary; AUD 2500.00 and above fail. A sticker price below the ceiling
cannot compensate for any unknown cost. Historical brief observations remain
useful discovery leads but cannot qualify as current offers.

The older `discovery_sources.artwork_affordability` API retains its item-price
comparison for compatibility. `Candidate.artwork_label` now explicitly says item
price only, extras/availability unknown, and all-in cost not qualified. That legacy
helper cannot establish an all-in pass.

## Verification

From the project root with the supported Python/Django environment:

```sh
PYTHONPATH=backend python -m unittest shelf.tests.test_gallery_commerce shelf.tests.test_gallery_curation shelf.tests.test_discovery_sources
```

The focused pure tests exercise all required cost components, exact-boundary and
sub-cent failures, unknown costs, explicit inclusions, availability and freshness,
owner isolation, exploratory explanations, rotation, and preservation of source
cards. Existing UI/source tests cover the legacy price-label contract. No new
schema or persistent data migration is introduced by these helpers.
