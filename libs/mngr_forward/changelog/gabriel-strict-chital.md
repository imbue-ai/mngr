Stop following the per-agent `refresh` event source in `ForwardStreamManager`: the default event sources are now just `services` and `requests`.

This is part of tearing out the unused refresh-event plumbing across `mngr_forward` and `minds.desktop_client`. The refresh-via-desktop-client mechanism has been superseded by an `open_tab` WebSocket broadcast from the workspace server.
