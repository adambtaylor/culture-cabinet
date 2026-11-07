# Portable setup preview

This separate developer preview opens owner and model setup in a new private
installation. It does not yet produce personal recommendations or enable Run.
The existing application entrypoint and its data are separate.

The development target is Linux ARM64 (Omarchy), Python 3.14.8 and Django 5.2.18.
These are observed development prerequisites, not a clean-machine certification
or a release package. Use the project's existing environment; no model is
silently downloaded and no dependency installation happens on launch.

From the source bundle root, with Django available in the selected Python:

```sh
.venv/bin/python -B backend/portable.py --data-root /absolute/path/to/new-private-cabinet
```

Choose an absent directory whose parent exists, or an empty directory owned by
you with mode 0700. The path and its parents must not be symlinks. Do not select
an existing household store or another application's directory. The launcher
refuses foreign data and incompatible/partial schemas; it does not upgrade,
repair or adopt them. Keep the terminal open. Copy the printed loopback origin
into your browser and open `/setup`. The automatically chosen port belongs only
to this process; `--port 8765` requests that exact port and fails if occupied.
There is no externally reachable fallback or automatic browser opener.

For an unclaimed installation, a terminal-only code expires after five minutes
or five failed attempts. Enter it once and choose a password of 12–256 characters.
A restart of an unclaimed installation issues a fresh code; a claimed one asks
for its existing owner's password. Five failed passwords cause a 15-minute
cooldown. A code cannot reclaim an existing owner.

Your model saves only the supported OpenAI Responses / gpt-6-astra / high
selection. Add a key through the password field. Keys live only in this server
process and owner session; logout, removal or process restart revokes them.
Reopening setup preserves the nonsecret selection, not the key or working
validation. Session inactivity expires after 30 minutes, with an eight-hour
absolute maximum; sensitive actions require recent password confirmation.

Saving a key does not prove a working connection. Preview test shows the exact
neutral request, fixed destination and 2048 output-token cap. Its separate final
confirmation can incur provider charges for one send, with no automatic retry.
This preview does not need private taste information or grants. Never confirm a
paid test merely to reproduce synthetic QA. An ambiguous outcome blocks further
tests until explicit reconciliation exists; restart, replacing keys or selecting
another connection cannot clear that uncertainty. There is no hidden fallback.

Press Ctrl-C in the terminal (or send SIGTERM) to stop. The selected listener,
process-local secrets and root lock are released. Re-run the same command to
unlock the installation. Core dumps are disabled before secret handling; this
does not promise Python byte erasure or protection from an OS administrator.

Working real inference, consent and serializer integration, attributable live
sources, the grounded no-key catalogue baseline, coherent all-category Run,
full accessibility/browser validation, supported backup/update/recovery, a clean
machine and a nonbuilder first-use exercise remain open acceptance requirements.
Do not publish or promote this preview as ordinary-user-ready.
