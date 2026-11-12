# Interface preview

The screenshot in the project README is a Chromium capture of a static render
of the application's actual cabinet template and styles.

It uses invented titles, preferences and explanations. It does not show a real
person's cabinet, provider results, an API key, a live inference result or a
verified publication from the application.

## Reproduce

Use a Python environment with the project's pinned Django dependency installed
(see [requirements.lock](../../requirements.lock) and
[DEPENDENCIES.md](../../DEPENDENCIES.md)). From the repository root:

```sh
python -B docs/showcase/render_preview.py .work/showcase.html
```

Open the generated file locally. The script reads the committed template and
CSS without importing application settings, starting a database or launching a
server. It inlines the styles, adds a visible synthetic-preview banner, removes
scripts, makes navigation inert and disables form actions. Application source
files are not modified.

A screenshot can be captured with an installed Chromium:

```sh
chromium --headless --disable-gpu --disable-background-networking \
  --no-first-run --no-default-browser-check \
  --user-data-dir=/tmp/cabinet-preview-browser \
  --window-size=1440,1320 \
  --screenshot=docs/showcase/cabinet-preview.png \
  "file://$(pwd)/.work/showcase.html"
```

The browser profile should be a new disposable directory, not a signed-in
personal browser profile.

## Provenance and limitations

- Application source: commit `6bfcf6108732f6c5a3c153cfd4601580ddfa073d`.
- Template: `templates/cabinet/portable_cabinet.html`.
- Base layout: `_PORTABLE_ENHANCE_BASE` in `backend/shelf/enhancement_views.py`,
  read as a literal through Python's AST, never executed as application code.
- Styles: `static/cabinet.css` and `static/enhancement.css`.
- Data: invented text fixtures defined in `render_preview.py`.
- Capture date: 11 November 2026.
- Viewport: 1440 × 1320.

This validates the visual presentation of one synthetic template state.
It does not test navigation, permissions, persistence, a provider connection,
inference or any live end-to-end workflow.
