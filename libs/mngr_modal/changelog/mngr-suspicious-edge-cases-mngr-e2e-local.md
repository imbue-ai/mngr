Extracted the Modal-credential parsing shared by the mngr_modal and mngr e2e
test harnesses into a single `load_active_modal_credentials` helper in
`imbue/mngr_modal/testing.py`. The helper now indexes the active profile's
tokens directly, so a malformed `~/.modal.toml` fails loudly instead of
silently substituting empty `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`. The
mngr_modal test fixture now uses this helper and no longer relies on the
undeclared transitive `toml` dependency; it uses `tomlkit`, which is now
declared as a direct dependency of `mngr_modal`.
