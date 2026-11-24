An Electron end-to-end test that fails to attach to the app (`Electron CDP attach failed after 3 relaunch attempts`) now says in the failure itself what each launch wrote: whether `main.js` printed its `[startup] Backend ready at` line, its `[startup]` lines for every attempt, and the last lines of the final attempt's stdout and stderr. One-time login codes in that output are masked.

The app logs two more `[startup]` lines on a first launch: one when the first window's route is resolved and it starts waiting for the loading document's intro, and one when the loading document reports the intro finished.

`test_create_workspace_and_sign_in_via_modal_then_chat_via_electron` carries the flaky mark: it fails intermittently in CI on that attach failure, as `test_legacy_association_files_migrate_into_synced_records` already does.
