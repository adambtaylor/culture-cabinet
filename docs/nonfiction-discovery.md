# Podcasts and nonfiction discovery

`/profiles/<id>/nonfiction/` is an authenticated, selected-profile surface. Its GET loads the checked-in five-cluster source catalog and owned local state; it never calls a provider or creates state. Existing taste ratings remain separate. The five supplied topics and personality references are authored exploration requests, not endorsements or inferred enjoyment.

`nonfiction_views.py` owns forms, presentation, per-session source-result caching and the portable export. `authored_discovery.py` owns seed/shortlist persistence, action receipts and compare-and-swap. `podcast_sources.py` owns bounded Apple episode search. `data/nonfiction_catalog.json` records source evidence and verified exact episode and bibliographic links. Episodes are leads; all research claims default to not independently appraised. Host or guest credentials do not verify a claim.

An explicit household-mode POST searches one topic once. State reserves its nonce before the search; exact replay does not repeat the call. Results are accepted only if the selected profile, grant and reserved state are still current. At most ten source leads are cached for 15 minutes, scoped to actor, profile, mode and session. Shortlisting copies a bounded server-owned episode snapshot into durable local JSON so it survives cache expiry and restart. POST never accepts episode metadata or arbitrary network destinations. Unknown/expired result identities are rejected. Sample mode does not expose a live search operation.

The authenticated export GET produces standalone UTF-8 HTML with inline CSS, escaped source text, safe HTTPS links, no scripts, forms, tokens or external assets. It is a portable file, not phone delivery or app sync. A phone can open it after the user transfers it. A Mac localhost:8766 URL is Mac-only. Apple links open exact episodes; queue changes remain manual in Apple Podcasts. No documented direct write path to Playing Next has been established for this project. The surface does not follow shows, download audio, play media or write podcast queues.

Run the complete suite using the isolated Django runtime described in the repository README:

```
python backend/manage.py test --verbosity=1 --noinput
```

The fixed entry point rejects positional test labels. P3.23 integrates the authored discovery state and additive migration `0018_authored_discovery_state`, bounded episode adapter, UI and separate synthesis signal. Deployment requires actual migration preservation proof and independent functional and quality acceptance. The Mac editing environment has no Django runtime; source compilation is only a preliminary check, never a substitute for the supported suite.
