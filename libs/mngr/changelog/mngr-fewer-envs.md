Support a shared Modal env across an offload-acceptance / offload-release
run (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`). `imbue.mngr.utils.testing`
gains a `read_shared_modal_env_name` helper that returns the shared env
name when the env var is set (and a non-empty dash-suffixed value), or
`None` otherwise. Used by the modal test fixtures to skip per-sandbox env
creation/deletion and route all tests into a single pre-created env, so
fanned-out offload runs stay well under Modal's per-workspace env cap.
Local pytest behavior (no env var set) is unchanged.
