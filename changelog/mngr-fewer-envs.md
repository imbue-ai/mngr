Collapse Modal environments across an offload-acceptance / offload-release
run to a single shared env (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`).
Each fanned-out sandbox in `just test-offload-acceptance` and
`just test-offload-release` used to mint its own Modal environment and
delete it on teardown -- dozens to hundreds per run, driving the
1500-env-per-workspace cap into transient failures. The justfile recipes
now pre-create a single `mngr_test-YYYY-MM-DD-HH-MM-SS-shared-<uuid>` env
once, forward its name into every sandbox via `--env`, and `trap`-delete
it at recipe exit. Inside each sandbox, the modal test fixtures
(`real_modal_provider`, `persistent_modal_provider`,
`initial_snapshot_provider`, plus the session-scoped subprocess-env
fixtures) honor the env var: they thread its name through
`MngrConfig.prefix` + `ModalProviderConfig.user_id` so every test lands
in the shared env, and they skip env creation / deletion / leak-tracking
at the fixture layer (apps and volumes are still created and deleted
per-test as before). Local pytest behavior (no env var set) is unchanged.
