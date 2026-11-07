# Final download and Kodi import status

The final SAB observer now stores its validated v2 completion export in
`ObtainCompletionStatus`, keyed to the existing `ObtainTransfer`. The existing
submission state and postprocessing receipt keep their original meanings. The
job page reports final download history separately from Mac Kodi import, and
states that current availability, playback and full-work identity are unverified.

The stored export binds the owner/profile's current acknowledged job, request,
SAB nzo, selected result and digest, NZB hash, exact file paths/hashes/sizes,
selected catalogue identity and final SAB completion timestamp. Repeated exports
preserve the original observation. Changed evidence refuses to overwrite it.

## Operator feedback boundary

`tools/record_kodi_completion.py` is an existing-SSH operator command, not an HTTP
endpoint. It must run from the fixed deployment path as the existing operator,
under the current server renewal and bounded expiry. Actor/profile are fixed to
the existing owner; the command accepts neither arbitrary user identities nor
credential paths. It reads at most 256 KiB from stdin and rejects duplicate JSON
keys, non-finite values and synthetic or unsupported-media input before setting up Django.

The operator must retrieve the actual private receipt emitted by the existing
`kodi-loop/consume_video.py` or the selected TV/album consumer after exact-file transfer and Kodi readback.
JSON is not independently authenticated proof. Browser uploads, manually authored
success claims and receipts supplied by source content are not accepted routes.
No additional endpoint, credential, listener or trusted browser assertion exists.

Invocation shape after deployment, using that actual receipt on stdin:

```text
ssh omarchy ${PRIVATE_LOCAL_PATH} -B \
  ${PRIVATE_LOCAL_PATH} \
  --job EXACT_JOB --request EXACT_REQUEST_UUID \
  --expires CURRENT_BOUNDED_EXPIRY --renewal CURRENT_RENEWAL < ACTUAL_CONSUMER_RECEIPT
```

The command rechecks authority, cancellation and session validity under the
transaction lock. The receipt must match the stored completion version, all
selection provenance, file bytes, hook hash and movie identity. `imported` also
requires the consumer's exact local file and Kodi movie ID/TMDB readback. Receipt
metadata is reduced before storage; resume/history, raw probe data and arbitrary
adapter errors are omitted. The full bounded input's digest detects replay.

Identical receipt replay is a no-op. `blocked` or `unknown` may reconcile forward
to `imported`; unknown never becomes blocked, and imported is terminal. A changed
receipt for the same state is rejected rather than silently replacing evidence.
Conflicts require an explicit operator investigation. Status retention does not
start a new import, rescan, playback, provider read or download.

## Automatic dispatch

P3.18 supplies a bounded Mac dispatcher and owner-SSH eligible-job listing.
See [Automatic completion dispatch](automatic-kodi-dispatcher.md) for the exact
state boundaries, retry rules, execution instructions and remaining limits.
P3.19 adds fixed adapters for the selected S01E01 pilot and complete FLAC albums.
Their receipts bind the selected show or canonical album identity and exact Kodi
readback; album files retain bounded artwork and opaque CUE evidence. Existing historical imports do not prove a Cabinet Obtain job.

New movie receipts can include separately validated Mac file, video-stream and
library observation timestamps. Old receipts remain valid without that evidence
and do not acquire a new readiness claim merely because the code was upgraded.
Playback is never started or claimed by either type of receipt.
