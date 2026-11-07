# Public social event intake

The social discovery page accepts one manually supplied browser observation or public-event handoff at a time. It uses the existing authenticated, CSRF-protected POST action with profile authority, mode isolation, revision and nonce checks. GET only renders stored observations. No new browser adapter, social API, crawl, recurring ingestion or migration is introduced.

`social_views.ActionForm` bounds and permits fields per action; `social_event_intake` validates public identity, evidence and provenance and projects history; `social_discovery` persists the result in the existing state document. The prior source, task, manual lead and import forms remain available.

Use `unknown` for unresolved raw date/time or timezone; the intake does not guess a normalized session. Keep short public facts and evidence/action references. Public cultural scope must be explicitly confirmed before storage; private parties, private venue details, full pages and credentials must not be submitted. Platform identity must match the canonical supported public link.

Observation history retains source differences and correction links. Repeated capture does not create independent interest. A browser capture is not proof of who acted. Owner-confirmed signals need evidence of owner action; agent-marked and unknown signals are excluded. Agent lineage cannot become owner preference merely through recapture. Attending expresses stronger intent than Interested but establishes no enjoyment, completed attendance, booking, ticket ownership or payment. The UI shows current eligibility and exclusions; intake itself does not record a taste response.

Withdrawal removes stored event metadata from the visible history. The core retains bounded hash/action audit and lineage exclusions so deletion cannot launder agent-origin evidence. Corrections preserve history until withdrawal. This is local state handling, not a social-platform unlike or cancellation.

Run the complete regression suite using the isolated Django test runtime described in the repository README. The fixed entry point accepts the whole suite, not positional test labels:

```
python backend/manage.py test --verbosity=1 --noinput
```

Fixtures use synthetic events and in-memory test storage. No real social account is accessed. Production activation, remote writes, deployment and a live migration are outside this change. Repository license and unresolved source provenance remain governed by existing notices.
