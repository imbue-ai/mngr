# imbue-mngr-pi-coding-usage

pi data provider for `mngr usage`. It records each pi agent's per-message usage
(via mngr_pi_coding's lifecycle extension) and aggregates it for `mngr usage`
(see `imbue-mngr-usage`).

## What gets captured

pi computes per-message cost client-side, so cost is **REPORTED** (no
estimation). Each assistant message records the reported cost, tokens
(`input`/`output`/`cacheRead`/`cacheWrite`), the provider-qualified model
(`<provider>/<model>`), and the session id. The reader sums cost per session.
`cost_mode` is `API_KEY` (pi bills a real provider key).
