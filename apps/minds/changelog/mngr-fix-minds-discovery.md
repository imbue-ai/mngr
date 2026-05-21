`minds env activate` no longer exports `MODAL_PROFILE` by default.
Activation now has two modes:

- **Use-only (default)**: `minds env activate <name>` exports the four
  use-side env vars (`MINDS_ROOT_NAME`, `MNGR_HOST_DIR`, `MNGR_PREFIX`,
  `MINDS_CLIENT_CONFIG_PATH`) and emits `unset MODAL_PROFILE`. This is
  what every non-deploying user wants -- the desktop client, mngr, and
  Latchkey no longer try to authenticate against a Modal workspace the
  operator may not have tokens for. Fixes the spurious "Modal is not
  authorized" warnings + Latchkey breakage that hit anyone running
  `minds run` after `eval "$(uv run minds env activate staging)"`
  without a `minds-staging` profile in `~/.modal.toml`.
- **Deploy-mode (`--deploy`)**: `minds env activate --deploy <name>`
  additionally exports `MODAL_PROFILE=<tier's modal_workspace>` and
  pre-validates that `~/.modal.toml` has a matching profile (fails up
  front with a `modal token set --profile <workspace>` hint when it
  doesn't, instead of letting downstream `modal …` shellouts surface
  the auth error).

`minds env deploy`, `minds env destroy`, and `minds env recover` now
refuse to run unless the shell is deploy-activated (`MODAL_PROFILE`
must equal the tier's `modal_workspace`). The refusal message tells
the operator the exact `eval "$(uv run minds env activate --deploy
<name>)"` to run.

The packaged Electron app and `deployment_tests/helpers.py` are
unchanged -- both set their Modal credentials independently of shell
activation.
