# Rate across the Cabinet

Open **Rate & Synergize** for the selected owner profile. Gathering a round makes
an explicit bounded read using the already permitted TMDB and Hardcover
catalogues (including the graphic-novel category), Triple R, PBS Events, Beat, Tribe announcements, Sound Systems of Melbourne,
and ListenBrainz radio for two existing artist identities. Per-source failures
remain visible. Rendering, rating and moving rounds do not fetch providers.
No provider is complete coverage; source freshness is independent of preference.

Give 1–5 anticipated-interest or experienced-enjoyment ratings. Enjoyment requires
an explicit experience acknowledgement, including attendance for events. Clearing
one basis preserves the other; an unknown or skipped item is never a negative
rating. Catalogue favourites stay in the existing SavedItem store, with its
existing Obtain links; event/music favourites stay in discovery history. They do
not become star ratings. Older qualitative responses keep their original labels.

The selection balances the five media, reserves exploration, and retains distinct
explicit positive roots when candidates exist. Exact source lanes provide only
mild affinity. Low ratings never reject a whole genre. This increment reorders the
available source pool; it does not claim an unlimited personalized catalogue or
use Astra output to overwrite ratings. Next round leaves its displayed choices
unrated and avoids automatic recycling. Source refresh remains explicit. Ratings
survive sign-out; provider candidate caches keep their existing 15-minute lifetime.
Use the paged response history to edit earlier numeric judgments.

A form binds the selected profile, current durable revision, nonce, displayed
source identities and canonical favourites. Replays of the same committed action
are harmless; changes from another tab or stale source identities require reload.
History is bounded to 500 actions and 1,000 receipts. No hidden reset removes
responses. The existing shared discovery undo applies to the latest effective
history action; catalogue favourites have their own explicit remove operation.

## Synergize

An explicit request snapshots only rated/saved/qualitative item identities,
titles, media, source labels and signal values. It contains no address, ticket,
private social content or messages. Material signal hashes prevent navigation,
source refresh and round changes from consuming another inference. Changed
ratings produce a new snapshot, and old results are labelled historical.

The website queues a request for supervised **gpt-6-astra, high reasoning** in
Codex. It has no backend API credential or autonomous worker. The fixed operator
CLI reserves one request before exporting its minimal prompt; uncertain outcomes
must be reconciled, never blindly retried. Only an actual model result recorded
through the trusted operator path can produce the completed state. Clusters and
cross-media links cite exact submitted keys and are visibly inferred, not sourced
relationships. See [the operator contract](taste-synthesis.md).

A review demonstration uses synthetic preferences and a real Codex Astra call:
rating POSTs → queued request → single reservation → actual high-reasoning output
→ completed display → historical after a rating edit. It does not seed the live
owner's preferences or prove autonomous inference. Synthetic source names and
subjects are labelled in the demonstration.

## Source breadth

Triple R, Beat, PBS Events, Tribe RSS and Sound Systems of Melbourne have explicit public readers. Existing catalogue,
MusicBrainz/ListenBrainz and source-link discovery remain available. The source
registry exposes unconnected venues and guides rather than suggesting that these
three outlets replace them. PBS Presents RSS was empty during the audit;
Darebin's blocked pages were not bypassed.

Requested coverage also includes festivals, indie/community day parties,
psytrance park gatherings and doofs, independently of major venue gigs. Sound Systems and Tribe now contribute bounded public leads. Area 3000 and
Triniq remain researched source candidates. OzTrance is not verified active. No forum/group account
access is assumed. Future normalized facts need source-evidenced genre,
organiser, outdoor/daytime/community/free status, announced-later location,
cancellation and freshness. Unknown attributes remain unknown; private or
embargoed locations are not published. Tribe RSS publication dates are not event starts. Its iCalendar route is
disallowed by the publisher; Cabinet does not request it. Sound Systems has
explicit source dates, but timezone and ending day remain unverified. Halftix
is an official link only because its terms restrict automatic ingestion. The
remaining specialist sources are backlog entries, not working connectors.

## Verification and deployment

Run the normal guarded backend/manage.py test on the pinned Omarchy runtime.
Focused suites: test_taste_selection, test_taste_funnel, test_synthesis,
test_cross_media_feedback, test_pbs_events, plus the existing discovery UI,
source transport and saved-intent regressions. Network access is denied in these
synthetic tests. Migration 0016 only adds the owned synthesis receipt table and
its metadata; existing preferences and grants are preserved. Source rollback
must retain that additive table and any recorded outcomes. Runtime renewal is
independent and is not extended by deployment.
