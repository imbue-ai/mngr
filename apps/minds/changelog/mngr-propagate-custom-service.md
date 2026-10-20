Custom services (the ones minds ships itself, currently `claude.ai`) now work in remote workspaces: their latchkey registration is written into the config of every gateway that serves minds agents, including the VPS-resident one, so it travels with the credentials that are synchronized there. Previously a remote workspace received the credentials but its gateway could not match a request to the service and never injected them.

Claude can now be connected from the Connectors tab and the permission dialog like any other service: `claude.ai` signs in through the browser (capturing its session cookie) instead of requiring hand-written credentials.

The additional-services section of `docs/latchkey-permissions.md` is updated for both.
