# Culture Cabinet — private development baseline

Culture Cabinet is a work in progress for collecting cultural discoveries,
recording reactions and preparing recommendations across books, film, television,
music, events and outings.

This is a source-only development snapshot, intended for a private repository.
It is not a finished home demonstration or a public/open-source release.
Working end-to-end Enhance, live providers and ordinary-user acceptance have not
been established for this snapshot. See [DEVELOPMENT-STATUS.md](DEVELOPMENT-STATUS.md)
for validation and known limitations.

## What is included

- Python/Django source, migrations, templates, local static assets and tests.
- Source dependency pins and third-party notices; no dependency installations.
- Labeled synthetic test cases and bounded public-reference metadata.
- Historical feature documentation, with private attribution and local evidence
  paths replaced by explicit examples/placeholders.

No user database, ratings/history export, live credentials, sessions, acquisition
controls, media downloads, development evidence or previous Git history is
included. Bundled sample/reference data is not a real user's taste or attendance
history and is not evidence of current API availability.

## Developer setup boundary

Read [the portable preview notes](docs/portable-preview.md) and
[the dependency inventory](DEPENDENCIES.md) before attempting an isolated setup.
Some legacy entry points deliberately enforce fixed host/runtime boundaries.
Those guards have been preserved; this export does not make them portable or
authorize changing them. Documentation placeholders such as
`${PRIVATE_LOCAL_PATH}`, `${PRIVATE_RUNTIME_PATH}` and `${PRIVATE_EVIDENCE_PATH}`
are examples, not paths or credentials supplied by this repository.

Preparation did not install dependencies, create application databases, launch a
server/browser, authenticate a provider or make live requests. Use disposable
synthetic test state for future admitted validation; never substitute a personal
database to make a test pass. See the status document before running old tooling
or following historical operational instructions.

## Rights and collaboration

No application license has been selected. Retain
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) and the existing dependency
notices. Private development access does not grant public redistribution of
application code, source data, third-party excerpts, artwork or media. The small
gallery fixtures are factual parser examples, with no image/media payloads; their
inclusion here is not a broad third-party license grant.

Future coherent changes should use draft pull requests with exact revision
evidence and independent technical review. A private WIP baseline or a successful
Git commit does not establish product, security-runtime, provider or release
acceptance.
