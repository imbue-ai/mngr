Added a "Proxy through this desktop" toggle to the Permissions tab of a remote workspace. When it is on for a service, the requests that workspace makes to that service leave from this computer instead of from the workspace's machine. Some services refuse requests from the datacenter IP ranges a workspace's machine sits in, and accept them from the user's own computer.

- The toggle is per service, not per account. It is shown in every connection panel of the service, with the same value in each.

- The toggle is offered only when both of these hold: Minds knows this computer's device id, and the workspace runs on a machine of its own. Every catalog service can be routed.

- Turning it on writes one grant per scope of the service into the workspace's permissions file. Each grant is limited to this computer's device id, so the user's other computers do not start forwarding. The machine's routing rules file, which is keyed by latchkey service name, is then rebuilt from every such grant in the file, and the permissions file and the rules file are pushed to the machine in one round trip.

- Turning it off deletes only this computer's grants. The service stays routed while another of the user's computers still has a grant for it, and the entries of other services are left in place.

- The machine routes a request by the service whose credentials latchkey used for it. Some addresses are used by several services: the Google Drive files API is used by Google Drive, Google Docs and Google Sheets. Turning the toggle on for Google Docs does not route a request that latchkey made with the Google Drive account's credentials.

- Routing needs latchkey 3.15.0 on the workspace's machine, which is the version the app now installs there. A machine still on an older version routes nothing until its next provisioning pass upgrades it.

- This computer has to be running and connected to the workspace for the forwarded requests to succeed.

- Revoking or disconnecting an account does not remove these grants. The machine's gateway checks the account's own permissions before it routes a request, so a grant left behind allows nothing by itself.

New route: `POST /ui/api/workspaces/<agent_id>/permissions/desktop-egress-toggle` with body `{ "service_name": str, "enabled": bool }`. Each connection in the permissions payload now carries `desktop_egress: { is_supported, is_enabled }`.

The desktop app bundles `latchkey-curl-shims` v0.4.0, the release whose router reads rules keyed by service name.
