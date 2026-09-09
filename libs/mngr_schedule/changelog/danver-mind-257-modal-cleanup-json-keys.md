mngr_schedule: fix two Modal app lookups that could never find an app.

`remove_modal_schedule` (in `implementations/modal/deploy.py`, production code) and the `cleanup_modal_app` test helper both matched `modal app list --json` rows on `"Description"` and read the id from `"App ID"`. The Modal CLI emits `description` and `app_id`, so the match never succeeded: `mngr schedule remove` logged "Modal app not found in environment" and left the app running, and the test helper leaked its app.

Both now parse through mngr's `utils/modal_cli.py`, which raises `ModalCliOutputError` if the payload stops carrying the keys we read.
