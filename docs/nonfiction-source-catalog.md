# Nonfiction source catalog

Verified at 2026-10-31T00:44:36Z. This is a static discovery catalog: ten actual episodes across five interest clusters, each with an exact Apple episode ID and URL and a book bridge. Personalities are discovery seeds, not endorsements. Follows coverage is not relevant here.

## Contract and evidence

`backend/shelf/data/nonfiction_catalog.json` uses version 1 with top-level `clusters`. Episode keys share the adapter identity `apple:podcast_episode:<id>`. Apple IDs are strings. All records retain `not_independently_appraised`; content kinds distinguish research discussion, personal/site anecdote and historical discussion. No clinical, investment or causal conclusion is implied by inclusion. Unknown exact duration is null; rounded minute labels are not converted into seconds. Dates are retained only when observed; the Johnson date remains null.

Exact source URLs, bibliographic IDs, retrieval time and verification limitations are recorded in the workspace evidence file `nonfiction-evidence/research-catalog-verification.json` (outside the shipped source candidate). The research used official show/distributor pages, official Apple metadata, publishers and Stanford's bibliographic catalog. No media was fetched or played, and no queue, remote state, phone sync or follow state was changed.

## Episode coverage

### AI, history & geopolitics

Long conversations connecting models of intelligence with history and state power.

- [#392 – Joscha Bach: Life, Intelligence, Consciousness, AI & the Future of Humans](https://podcasts.apple.com/us/podcast/392-joscha-bach-life-intelligence-consciousness-ai/id1434243584?i=1000623089443) — Apple episode 1000623089443, collection 1434243584. [Producer/distributor source](https://lexfridman.com/joscha-bach-3/). Explores cognitive architecture, artificial consciousness and open-source AI; speculative positions remain the guest's views.
- [#289 – Stephen Kotkin: Putin, Zelenskyy, and War in Ukraine](https://podcasts.apple.com/au/podcast/289-stephen-kotkin-putin-zelenskyy-and-war-in-ukraine/id1434243584?i=1000563819491) — Apple episode 1000563819491, collection 1434243584. [Producer/distributor source](https://lexfridman.com/stephen-kotkin-2/). Connects Russian and Soviet history with the 2022 invasion; analysis is dated to the recording rather than a current forecast.

### Money markets & financial systems

Understand market plumbing and compare competing monetary-system explanations.

- [Zoltan Pozsar and Perry Mehrling On The Historic Crisis Of Financial Market Plumbing](https://podcasts.apple.com/us/podcast/zoltan-pozsar-and-perry-mehrling-on-the-historic/id1056200096?i=1000469941965) — Apple episode 1000469941965, collection 1056200096. [Producer/distributor source](https://omny.fm/shows/odd-lots/zoltan-pozsar-and-perry-mehrling-on-the-historic-c). A crisis-era explanation of funding stress and Federal Reserve intervention, useful before tackling rate transmission.
- [Zoltan Pozsar and Perry Mehrling Debate Bretton Woods 3.0](https://podcasts.apple.com/us/podcast/zoltan-pozsar-and-perry-mehrling-debate-bretton-woods-3-0/id1056200096?i=1000579120792) — Apple episode 1000579120792, collection 1056200096. [Producer/distributor source](https://omny.fm/shows/odd-lots/zoltan-pozsar-and-perry-mehrling-debate-bretton-wo). Contrasting interpretations of dollar dominance, commodities and monetary change; the thesis is debated rather than established.

### Sleep & health optimisation

Discovery seeds include Andrew Huberman, Matthew Walker and Bryan Johnson (the dictated Brian/Optimize interest); inclusion is not endorsement.

- [GUEST SERIES | Dr. Matt Walker: The Biology of Sleep & Your Unique Sleep Needs](https://podcasts.apple.com/gb/podcast/huberman-lab/id1545953110?i=1000651280950) — Apple episode 1000651280950, collection 1545953110. [Producer/distributor source](https://www.hubermanlab.com/episode/guest-series-dr-matthew-walker-the-biology-of-sleep-your-unique-sleep-needs). Introduces sleep stages, chronotype and individual needs. Discussion and linked studies require claim-by-claim appraisal; this is not proven personalised advice.
- [The $2M Longevity Protocol: Bryan Johnson’s Biohacking Blueprint](https://podcasts.apple.com/au/podcast/the-%242m-longevity-protocol-bryan-johnsons-biohacking/id582272991?i=1000643355534) — Apple episode 1000643355534, collection 582272991. [Producer/distributor source](https://www.youtube.com/watch?v=roHeUk7ApUo). Personal self-experiment and longevity ambitions described by Johnson. The official description is promotional; reversal-of-aging claims are not independently supported by this catalog. The sleep book is a separate topic bridge, not validation of Blueprint.

### Urban orchards, soil & fungi

Pair community-orchard observation with measured soil testing and a focused mycorrhizae book.

- [How to Read a Soil Test for Fruit Trees (and What to Try Next) with Ariel Agenbroad](https://podcasts.apple.com/us/podcast/how-to-read-a-soil-test-for-fruit-trees-and-what-to/id1078657833?i=1000758421498) — Apple episode 1000758421498, collection 1078657833. [Producer/distributor source](https://podcasts.apple.com/us/podcast/how-to-read-a-soil-test-for-fruit-trees-and-what-to/id1078657833?i=1000758421498). An extension educator discusses pH, organic matter and test interpretation, encouraging context before amendments.
- [Why do some fruit trees thrive while others struggle? with Derek Barbe and Chrisina Balint](https://podcasts.apple.com/us/podcast/why-do-some-fruit-trees-thrive-while-others-struggle/id1078657833?i=1000792193607) — Apple episode 1000792193607, collection 1078657833. [Producer/distributor source](https://share.transistor.fm/s/02acd8c6). Visits two Peterborough community orchards to compare tree growth and soil biology. Site observations generate questions; they do not establish causal benefits of a product or practice.

### Espionage & unusual history

Narrative history spanning intelligence operations and wartime amphetamine use.

- [Inside the Iranian Embassy Siege with Ben Macintyre](https://podcasts.apple.com/us/podcast/inside-the-iranian-embassy-siege-with-ben-macintyre/id1229409577?i=1000669016871) — Apple episode 1000669016871, collection 1229409577. [Producer/distributor source](https://spybrary.com/iranian-embassy-siege-ben-macintyre/). Author interview on the 1980 siege, hostage negotiations and intelligence context, with a direct bridge to the source book.
- [#481 – Norman Ohler: Hitler, Nazis, Drugs, WW2, Blitzkrieg, LSD, MKUltra & CIA](https://podcasts.apple.com/in/podcast/481-norman-ohler-hitler-nazis-drugs-ww2-blitzkrieg/id1434243584?i=1000727548166) — Apple episode 1000727548166, collection 1434243584. [Producer/distributor source](https://lexfridman.com/norman-ohler/). Includes Pervitin, military stimulant use and the author's response to historians' criticism. Drug use does not by itself establish an explanation for military outcomes.

## Book provenance

- [Principles of Synthetic Intelligence: Psi: An Architecture of Motivated Cognition](https://global.oup.com/academic/product/principles-of-synthetic-intelligence-9780195370676?title=Guide), Joscha Bach; ISBN 9780195370676. [Publisher source](https://global.oup.com/academic/product/principles-of-synthetic-intelligence-9780195370676?title=Guide). Hardcover format explicitly verified at publisher. Technical bridge from the conversation about minds to a specified cognitive architecture.
- [Stalin: Paradoxes of Power, 1878–1928](https://www.penguinrandomhouse.com/books/299269/stalin-by-stephen-kotkin/), Stephen Kotkin; ISBN 9780143127864. [Publisher source](https://www.penguinrandomhouse.com/books/299269/stalin-by-stephen-kotkin/). No hardcover link asserted. Historical context for the discussion of Russian state power; this volume ends in 1928.
- [The New Lombard Street: How the Fed Became the Dealer of Last Resort](https://searchworks.stanford.edu/view/8838838), Perry Mehrling; ISBN 9780691143989. [Publisher source](https://pup-assets.imgix.net/onix/images/9781400836260/9780691242200.pdf?fm=pdf). No hardcover link asserted. Develops the money-market and central-bank framework behind the episode.
- [Why We Sleep: Unlocking the Power of Sleep and Dreams](https://www.simonandschuster.com/books/Why-We-Sleep/Matthew-Walker/9781501144325), Matthew Walker; ISBN 9781501144325. [Publisher source](https://www.simonandschuster.com/books/Why-We-Sleep/Matthew-Walker/9781501144325). No hardcover link asserted. Longer treatment of sleep and circadian topics; publisher description verifies the book, not clinical claims.
- [Teaming with Fungi: The Organic Grower's Guide to Mycorrhizae](https://www.hachettebookgroup.com/titles/jeff-lowenfels/teaming-with-fungi/9781604697292/), Jeff Lowenfels; ISBN 9781604697292. [Publisher source](https://www.hachettebookgroup.com/titles/jeff-lowenfels/teaming-with-fungi/9781604697292/). Hardcover format explicitly verified at publisher. Topic bridge from orchard soil life to plant–fungus relationships; applicability depends on species and site.
- [The Siege: A Six-Day Hostage Crisis and the Daring Special-Forces Operation That Shocked the World](https://www.penguinrandomhouse.com/books/739963/the-siege-by-ben-macintyre/), Ben Macintyre; ISBN 9780593728116. [Publisher source](https://www.penguinrandomhouse.com/books/739963/the-siege-by-ben-macintyre/). No hardcover link asserted. The book discussed in the interview, expanding the 1980 embassy-crisis reconstruction.
- [Blitzed: Drugs in the Third Reich](https://www.harpercollins.com/products/blitzed-norman-ohler), Norman Ohler; edition ISBN not verified. [Publisher source](https://www.harpercollins.com/products/blitzed-norman-ohler). No hardcover link asserted. Book behind the wartime-stimulant discussion; compare its interpretation with the criticism discussed in the episode.

The Mehrling bridge uses a Princeton University Press excerpt plus the Stanford Libraries bibliographic record (2011, ISBN 9780691143989); the publisher product page was not verified. Its `publisher_url` therefore points directly to the verified publisher excerpt. Publisher descriptions establish identity, subject and format, not independent validation of the books' claims.

## Verification limits

Eight exact Apple episode pages were opened successfully. The Johnson and Ohler exact IDs and titles were present in official Apple search results, corroborated by their official producer pages; direct opens returned tool Internal Error. Their link verification is consequently weaker than the other eight and is explicitly recorded in the evidence ledger. A bounded Apple lookup request failed local DNS resolution; web primary sources completed discovery. The catalog stores no guessed Apple IDs, exact runtimes, dates, prices or availability promises.

