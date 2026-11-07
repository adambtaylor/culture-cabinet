# Apple TV completion destination

Apple TV is an independent completion destination. Kodi remains enabled through
the existing bounded dispatcher and retains its original receipts, caches and
scan reservations. Apple TV has separate job journals, derivative verification
and watchfolder handoff states. A Kodi import does not imply an Apple TV import.

## Source and episode identity

Only an acknowledged, currently authorized Cabinet TV job with final SAB history
and verified bytes can supply an automatic contract. SAB `Completed` is provider
history; this does not invent independent repair, archive or CRC checks. Failed,
partial, unknown and unverified jobs cannot remux or import. Source originals and
the exact registered Complete job tree stay unchanged. Derivatives use a separate
fixed private `.apple-tv-work` root, bound to the contract hash.

The contract binds the existing completion evidence, exact selected source file,
request/result/NZB/SAB identity and explicit show, season, episode and title. The legacy `series_s01e01` scope authorizes only S01E01. P3.21 adds explicit
owner-selected `episode_s02e02` scopes, bound through preview, approval and
completion evidence. A filename never turns a pilot selection into S02E01. See
[selected episodes](selected-episodes.md) for scope, migration and provenance
boundaries. No season-pack or autonomous series acquisition is enabled.

## Compatibility and preservation

The planner selects explicitly tagged English, non-commentary audio and preserves
selected video and audio with stream copy. It prefers supported Atmos audio when
available and refuses ambiguous or unsupported choices. It never silently
re-encodes or downgrades audio/video. English text subtitles may become mov_text;
other languages remain in the preserved original. Unsupported image subtitles,
codecs, language ambiguity and HDR combinations produce actionable blocked states.

Validation checks bounded container/stream metadata, duration, packet payload
hashes, copied HDR/Dolby Vision metadata and Atmos signaling when present. Native
AVFoundation must report playable with matching tracks/duration before handoff.
These are file compatibility observations, not playback or display-output tests.
HDR display output remains unverified. No HDMI, passthrough, display or Apple TV
preferences are changed.

Disk checks include preserved originals, remux/faststart staging and Mac transfer
space. Verified output is transferred into private staging on the existing TV
watchfolder filesystem, then handed off by atomic move. No second full-size Mac
copy or manual import command follows a pending watchfolder ingestion.

## Durable handoff and recovery

A durable reservation precedes the watchfolder move. Handoff pending means only
that a verified file was offered to Apple TV. A disappearing watchfolder file is
not proof of import. The worker reconciles against an exact organized library
file, hash, selected metadata and native compatibility before reporting observed
library import. Unknown moves and interrupted handoffs are reconciled before any
retry; they never cause a second import. Terminal observed imports remain terminal.

Receipts are destination-specific. Backend status preserves Kodi observations and
shows Apple TV handoff pending/unknown separately from observed library import.
The operator transport uses existing SSH and fixed owner/profile/session bounds;
no new credential grant, HTTP upload or library control permission is introduced.

## Existing-file acceptance limits

The already imported Dark Matter (2024) S02E01 derivative is separate manual-media
evidence: its source/derivative video and English E-AC-3 Atmos packet payloads were
verified, AVFoundation reported playable, and the owner confirmed Atmos on the physical
HDMI soundbar. HDR display output was not verified. Reconciliation checks its
existing file identity/hash and native compatibility without another 10 GB copy,
remux, watchfolder move or import. It must never acquire an invented Cabinet job
or completion receipt. Synthetic regression jobs remain labeled synthetic.

The current actual Cabinet database has no successfully completed TV job available
for live automatic-import acceptance. Functional tests exercise isolated synthetic
contracts and injected transports. Deployment does not initiate downloads or
playback, and does not fabricate a successful Cabinet acquisition.

## Development and rollback

Use the established venv and isolated source-copy boundary described in README.md.
Run the full Django suite plus these offline adapter suites:

```sh
python3 -B -m unittest discover -s tools -p 'test_mac_*.py' -v
python3 -B -m unittest discover -s tools -p 'test_apple_tv_*.py' -v
```

`tools/apple_tv_media.py` owns pure selection/validation; `apple_tv_remux.py` owns
remote derivative work at `<project>/.apple-tv-work/<contract hash>/receipt.json`.
`mac_apple_tv_consumer.py` owns Mac delivery at
`appletv-evidence/runtime/consumer/<request UUID>.json`, locked by `consumer.lock`.
`mac_apple_tv_dispatcher.py` owns `runtime/jobs/<job ID>.json` and `dispatcher.lock`.
The backend independently records exact-bound destination receipts at
`<project>/.acquisition-control/apple-tv-receipts/<request UUID>.json`.
Kodi retains `dispatcher-evidence/runtime` and its original consumer state.
No new dependency installation is required. P3.21 requires migration 0017 for
the explicit episode scope columns and constraint; follow the preservation and
rollback limits in [selected episodes](selected-episodes.md). The Mac worker
and remote remux subprocesses are bounded by the same approved 6 November
04:17:29 UTC expiry, with no automatic renewal.

Stop only the exact owned Apple TV dispatcher for rollback. Preserve its remux
journals, source-bound derivative evidence, pending handoffs and library IDs; keep
Kodi receipts untouched. Do not delete uncertain state to force another remux or
import, remove library items, restore an older database or change playback settings.
