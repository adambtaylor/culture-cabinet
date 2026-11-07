# Gallery discovery presentation

Gallery reads use the existing authenticated music/event discovery page and its
POST action. The explicit `gallery` button shares the existing mode, revision,
nonce and CSRF fields. Loading discovery or recommendations does not request a
source. Existing artist, music and event response forms retain their behavior.

Gallery event cards and recommendation cards reuse `gallery_detail.html`.
Opening date/start/end and source time wording are distinct from exhibition run
dates. Missing end times, dates, admission and currentness stay visible as
unknown. Date-only runs are not daily hours or confirmed sessions. Source check
times and official links support a later verification; a current source read is
not a booking or a guarantee that a programme remains unchanged.

`gallery_offers.html` renders sale observations separately from event cards.
Each offer shows the listed object price and required cost components with their
source wording. The curation cost gate, rather than template arithmetic, decides
whether a current, confirmed-stock total is strictly below AUD 2,500 all-in.
Exactly AUD 2,500 fails; any unknown required cost, conversion or stock leaves
the lead unqualified. A passing screen does not authorise a purchase. Offers
have no event response, save, rating or purchase controls.

The shared views supply owner-only `gallery_brief_note` and curatorial reasons;
these remain hypotheses and qualitative seeds, never invented ratings or likes.
Other profiles may see public source metadata from their own reads, without
receiving the owner's personal brief. The bounded named sources do not establish
complete coverage of Melbourne galleries, openings, fairs or sales. Templates
use Django autoescaping and source links retain external-link protections. No
source art images or new image assets are included.

## Verification

`backend/shelf/tests/test_gallery_ui.py` exercises authenticated shared GET/POST,
replay, no I/O on GET, separate opening/run wording, escaped source text,
unqualified sale costs, recommendations and profile isolation with synthetic
outcomes. Source parser and strict-cost edge cases belong to the source and
curation tests. The supported verification entry point accepts the whole suite;
positional test labels are rejected by the fixed boundary:

```sh
python -B backend/manage.py test --verbosity=1 --noinput
```

Run from the candidate root using the configured isolated runtime and private
TMPDIR described in the repository handoff. The local Mac Python has no Django,
so static edits alone are not a test pass. Integrated whole-suite and independent
functional/maintainability results remain the lead's promotion gates.
