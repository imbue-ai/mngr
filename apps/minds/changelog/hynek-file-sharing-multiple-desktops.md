- Each desktop serves shared files under its own device id: the WebDAV file server now answers at `/api/v1/files/<device id>/<path>`, and with a 404 for any other device id. The device-less `/api/v1/files/<path>` is still served, for workspaces built before this change.

- File-sharing grants name the desktop whose file they share. The grant and deny messages an agent receives name the device, and a desktop's Local files pane lists only its own shared paths, not those another of the user's desktops shared with the same remote workspace, and refuses to switch another desktop's shared path on or off.

- Grants made before this change keep working at the device-less URL, and each gains a twin for the desktop that first reads the workspace's policy afterwards, so the same share is reachable at the new URL too. New grants always name a desktop. In the Local files pane an old grant is part of its path's row, and removing the row (or narrowing it to read) revokes the old grant as well.
