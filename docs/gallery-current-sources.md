# Current gallery source evidence

This increment uses bounded public metadata from two primary hosts, checked on 31 October 2026. The art discovery brief is background evidence, not proof of current dates, prices or availability.

The evidence receipts live in `../../gallery-evidence/current-source-metadata.json`, with robots receipts in `current-source-robots.json` and access/retention decisions in `current-source-policy.json`. Three `current-source-*.html` files retain tiny factual fragments of the actual responses for parser verification. They contain no artwork images, descriptive article prose, scripts, forms or checkout tokens. The APW homepage fragment has a closing wrapper added solely to balance the excerpt.

- Gertrude’s *Et cetera* page confirms the 6 November 2026 opening, 6–8pm Melbourne time, and exhibition run of 7 November–12 December at 21–31 High Street, Preston South.
- Australian Print Workshop’s homepage confirms the 6pm opening on 6 November for *IMPRESSIONS: 45 Years in Print*. A closing date and opening end time remain unknown.
- APW’s *Goompi* listing confirms A$1,100 including GST and an Australian shipping rate of A$33 per tube. Destination, stock, framing, installation, other required fees and the final all-in quote remain unresolved. The offer is not purchase-qualified under the strictly-below-A$2,500 ceiling.

Gertrude’s robots URL returned HTTP 404; this does not grant reuse rights. APW’s robots response returned HTTP 200 and specifies a ten-second crawl delay; the selected paths are not disallowed. Its terms permit informational retrieval and expressly prohibit commercial exploitation. Facts are retained only for private informational use. No broad republication or artwork reproduction license is established.

Content fetches used a single coordinating sourceworker. Raw response sizes and SHA256 hashes are recorded; check times use the raw file completion timestamps because exact request starts were not captured. Content HTTP status codes were not captured and remain unknown; curl exit 0 alone is not an HTTP 200 receipt. Robots HTTP status codes and response headers were captured directly. No 403, 429 or login challenge was observed in the shared responses; any such future response must stop collection for that source.

Validation: all JSON receipts parse; retained HTML fragments were compared with the actual source responses and checked for image, form and script tags. No purchases, reservations, messages, image downloads or production changes were made.
