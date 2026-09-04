# Frontend assets (fetched on first run, not vendored)

foreman's web UI uses a handful of third-party JS/CSS/font libraries. They used
to be committed to the repo under `static/vendor/` (~4MB of minified blobs).
They are now **pinned and fetched on first run** instead, so the repo stays
small and the exact bytes are reproducible + tamper-evident.

## How it works

- Every asset is pinned in [`imbue/mngr_foreman/assets.py`](imbue/mngr_foreman/assets.py)
  as `(path, url, sha256, tier)` in `_RAW` → `MANIFEST`.
- On server startup, `ensure_assets()` fetches any asset that is missing (or
  whose cached bytes don't match the pinned sha256) into a local cache dir:
  `<mngr host_dir>/plugin/foreman/assets` (e.g. `~/.mngr/plugin/foreman/assets`).
  A present, hash-verified file is served straight from the cache.
- The server serves `/static/vendor/<path>` from that cache
  (`server.py::_read_static`); `/static/vendor/atkinson.css` and all non-vendor
  pages/scripts still ship in the package.

## Tiers & offline behavior

`ensure_assets` never crashes the server — an offline box still runs:

- **required** — `marked` (markdown) and `xterm` + `xterm-addon-fit` + `xterm.min.css`
  (web terminal). If these are neither cached nor fetchable, a loud error names
  them, but the server still serves: the agent list and chat work (markdown
  degrades to escaped plain text via `app.js::renderMarkdown`) and the terminal
  page surfaces its own load error.
- **optional** — highlight.js, KaTeX (+ fonts), mermaid, Atkinson Hyperlegible
  fonts. A fetch failure is logged quietly and the feature stays off; the
  frontend loaders already `.catch` a missing asset (syntax highlighting, math,
  diagrams, and custom typography simply don't render).

## Pinned versions

| Library | Version | Source |
| --- | --- | --- |
| marked | 12.0.2 | jsdelivr npm `marked@12.0.2` |
| xterm.js (+ fit addon, css) | 5.3.0 / 0.8.0 | jsdelivr npm `xterm@5.3.0`, `xterm-addon-fit@0.8.0` |
| highlight.js (+ atom-one-dark) | 11.9.0 | jsdelivr gh `highlightjs/cdn-release@11.9.0` |
| KaTeX (js, css, auto-render, 20 fonts) | 0.16.9 | jsdelivr npm `katex@0.16.9` |
| mermaid | 10.9.1 | jsdelivr npm `mermaid@10.9.1` |
| Atkinson Hyperlegible + Mono (12 woff2) | fontsource 5.3.0 | jsdelivr npm `@fontsource/atkinson-hyperlegible[-mono]@5.3.0` |

Licenses: marked / xterm / KaTeX / mermaid — MIT; highlight.js — BSD-3-Clause;
Atkinson Hyperlegible — SIL OFL 1.1.

## Updating a version

1. Change the URL(s) for that library in `assets.py` `_RAW` to the new pinned
   version, and update its sha256 to the new file's digest
   (`curl -sL <url> | sha256sum`). Update the table above.
2. If the library ships extra files (e.g. KaTeX/fontsource fonts), update those
   entries too — the CSS references them by relative path under the same dir.
3. Delete the local cache dir (or bump nothing and let the hash-mismatch path
   re-fetch) and restart; `ensure_assets` fetches the new bytes.
