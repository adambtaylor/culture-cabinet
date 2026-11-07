# Halftix: official source link, permission required for ingestion

Halftix belongs in Cabinet's shared event-source registry as a discount-ticket
retailer. Its official [All Events page](https://halftixmelbourne.com/all-events/)
is a useful manual route to cheaper options. A retailer link is not an event,
stock check, booking quote or authority to purchase.

`halftix_events.fetch_listing(now)` returns the existing `Outcome` contract with
`permission-required` and no candidates. The registry marks the connection
`manual-link-only; permission-required`. The existing generic snapshot parser
refuses this connection, so registering the source cannot enable a back-door
import. There is no transport, background fetch, account, cart or booking action.
An enabled reader would eventually return the existing `EventCandidate` contract;
this package does not fabricate candidates to stand in for missing permission.

## Publisher restriction observed on 30 October 2026

The official [Terms](https://halftixmelbourne.com/website-terms-and-conditions/)
allow personal, non-commercial local extracts, but restrict redistribution and
electronic retrieval-system storage without written permission. The operative
clause says: “Nor may you transmit it or store it in any other website or other
form of electronic retrieval system.” The official
[Disclaimer](https://halftixmelbourne.com/website-disclaimer/) repeats that restriction.
Cabinet therefore does not ingest, cache or redisplay the site's listings.
No attempt was made to contact the publisher or obtain permission.

The robots URL could not be read through the research tool. That is an unresolved
access check, not permission and not evidence of an origin denial. No alternate
endpoint or access workaround was attempted after finding the terms restriction.
Future activation requires permission covering Cabinet's use and a fresh robots
and access check before implementing a bounded reader.

## What the actual public pages established

The live page observation showed variable advertised discounts, not uniform
half-price inventory: one offer displayed a 35–51% range, while other offers
showed 36%, 37% and 41%. Session-specific amounts, stock labels and off-sale
cutoffs were displayed. These were observations, not reserved stock or current
checkout verification. Do not derive face value from rounded percentages, or
assume an offered amount includes every fee when the exact breakdown is absent.
No listing descriptions or inventory snapshots are shipped in source.

The [FAQ](https://halftixmelbourne.com/faqs/) says venues allocate seats in the
stated category, with no seat selection at booking, and it does not offer phone
bookings. Entry handling varies by event: some source listings described voucher
and photo-ID exchange, while another described promoter-emailed tickets and
sharing booking contact details with that promoter. Follow the specific offer's
instructions. Exchange conditions are conditional, not a blanket refund promise.

This is a source-only delta from the frozen funnel candidate. Integrate the
registry entry and the honest source-status message; do not label it connected.
Tests exercise the no-fetch boundary and rejection by the generic snapshot path.
No deployment, browser/cart interaction, copied inventory, or real booking was
performed. Permission remains the blocker to automatic normalization.
