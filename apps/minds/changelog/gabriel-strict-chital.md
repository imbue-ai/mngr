Tear out the unused refresh-event plumbing from `minds.desktop_client`:

- Drop `REFRESH_EVENT_SOURCE_NAME`, `_on_refresh_callbacks`, and the `add_on_refresh_callback` / `remove_on_refresh_callback` / `fire_on_refresh` APIs from `MngrCliBackendResolver`.
- Remove the `_handle_refresh_event_callback`, `_dispatch_refresh_broadcast`, `_parse_refresh_service_name`, and `_log_refresh_dispatch_result` helpers from `desktop_client.app`, along with the `_refresh_event_apps` registry and its callback registration.
- Stop dispatching the per-agent `refresh` event source in the `forward_cli` envelope consumer.
- Remove the now-dead refresh integration tests in `desktop_client.test_desktop_client` and the `forward_cli_test` envelope dispatch test for refresh.

The refresh-via-desktop-client mechanism has been superseded by an `open_tab` WebSocket broadcast from the workspace server, so the desktop-client-mediated refresh path is no longer wired up.
