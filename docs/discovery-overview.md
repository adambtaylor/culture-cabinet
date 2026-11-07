# Across your Cabinet

The authenticated `/recommendations/` page brings the existing film, series,
book/graphic-novel and music/event rounds together. Its profile route requires
the same current selected owner and view/select grants as intake. Navigation
from every provider page makes it reachable after login and profile selection.

The view reads the current owned intake run and household DiscoveryState. It
reuses durable hydration, the existing intake selector and discovery reducer;
there is no new ranking model, preference store, migration or provider operation.
Choices from another tab are read again before rendering. Catalogue history and
music/event history retain separate revisions. At most five positive catalogue
seeds and five music/event seeds are listed; this does not combine them into a
single inferred taste. Existing catalogue expansion restrictions still apply.
The overview requests only unjudged discovery candidates before slot allocation.
The discovery page keeps its existing response-inclusive default. Expired saved
identities cannot occupy slots or inflate lane counts; a positive historical
identity can still anchor an explicitly linked current candidate without itself
being recommended.

Current source content is session scoped: the catalogue's one-hour expiry and
public discovery's 15-minute expiry are unchanged. An expired deck can retain a
durable judged identity as a seed, but cannot claim current source coverage or
revive old recommendations. Past event dates are excluded from recommendation
slots. Unknown dates, ticket availability, price and cancellation remain unknown.
Fixture histories are never read into this page. Missing optional tables leave a
read-only empty/history-free view, with no automatic migration or session upgrade.

Cards retain their canonical source links, source attribution, retrieval time and
graphic-novel classification when supplied. Retrieval is not source freshness.
Provider text is escaped; links open with no opener or referrer. No source page,
image or video is fetched automatically. Existing destination pages own explicit
fetch and response forms. The overview has no write form besides inherited logout.

This is a combined current-data overview, not a live factual cross-media graph.
Verified adaptations, shared themes, event lineup evidence and new event-source
connectors are still separate integration work. The existing offline research
samples remain labelled and separate. Recent public outcomes distinguish ready,
empty and failed/rate-limited requests; connector prerequisites do not imply a
successful current fetch. Missing results never mean an exhausted catalogue.

`test_recommendations` uses synthetic provider-shaped rows and isolated Django
users/history to verify the rendered view, authority, stale-tab corrections,
expiry, sample separation and GET purity. No live provider coverage, real taste,
deployment or end-to-end acquisition is established by these tests. Existing
recovery evidence remains valid for its unchanged source. This increment needs
its own independent functional/quality review before any separately authorized
promotion; the live deployment was not changed.
