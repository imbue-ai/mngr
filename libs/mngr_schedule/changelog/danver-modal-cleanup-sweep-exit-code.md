mngr_schedule: follow the Modal CLI JSON parser to its new home.

`parse_modal_app_listings` moved from `imbue.mngr.utils.modal_cli` to `imbue.mngr_modal.modal_cli`, and `ModalCliOutputError` from `imbue.mngr.errors` to `imbue.mngr_modal.errors`. `remove_modal_schedule` and the `cleanup_modal_app` test helper import from the new locations. No behaviour change.
