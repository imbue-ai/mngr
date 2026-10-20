Desktop egress is redesigned. A remote machine's gateway now injects the credentials itself and routes a chosen service's requests out through the user's computer, which only forwards them. The old `/via-desktop` route, which injected credentials on the desktop, is removed.

- The VPS gateway run script creates `~/.latchkey/proxyRules.json` as `{}` when the machine has none, and exports its path as `LATCHKEY_DESKTOP_PROXY_CONFIG`. The curl router reads the file on every request: each key is a latchkey service name, and a request that latchkey matched to a service whose value is truthy is sent through the desktop gateway. Every catalog service can be routed, including the ones latchkey matches by regular expression.

- The router does not match URLs itself. Latchkey names the service it matched a request to in the request header `X-Latchkey-Matched-Service`, and the router looks that name up in the rules file. Latchkey sets the header only with its diagnostic headers turned on, so the VPS gateway run script also exports `LATCHKEY_DIAGNOSTIC_HEADERS=1`. This needs latchkey 3.15.0, which added `LATCHKEY_DIAGNOSTIC_HEADERS` and is the version the VPS install pins.

- The desktop gateway runs with `LATCHKEY_PASSTHROUGH_UNKNOWN=1`, without which latchkey refuses a request that asks for no credential injection. The permission check still runs on such a request.

- Added `imbue.mngr_latchkey.desktop_egress`. `build_desktop_egress_grant` builds the permissions rule that lets one computer forward one scope: the scope's schema combined with a `customMetadata.deviceId` gate, with the `any` permission. The gate is on the device id because a host's permissions file is shared between the user's computers. `list_desktop_egress_grants` reads such rules back from the schema structure. `build_desktop_egress_rules`, `serialize_desktop_egress_rules`, `parse_desktop_egress_rules` and `is_service_routed` write and read the rules file.

- The rules file travels in the existing machine round trips, with no new remote command. `MachineCredentials.refresh` reads it together with the credentials and the policy (`FetchedMachineState.desktop_egress_rules_json`). `MachineCredentials.set_permissions_and_desktop_egress_rules` pushes a permissions snapshot and a rules snapshot in one remote command, after validating both.

- This computer keeps a copy of each remote host's rules file at `<latchkey_directory>/mngr_latchkey/hosts/<host_id>/proxyRules.json` (`store.desktop_egress_rules_path_for_host`), handled like its copy of the permissions file. A refresh adopts the machine's file, and removes the copy when the machine has no file. The machine is never seeded from the copy. `read_host_desktop_egress_rules` returns the copy's text.

- `FetchedMachineState` has a new required field `desktop_egress_rules_json`.

- Removed the `/via-desktop` route of the remote desktop-gateway proxy extension, the `MINDS_VIA_DESKTOP_URL_PREFIX` workspace environment variable (`ENV_MINDS_VIA_DESKTOP_URL_PREFIX`, `VIA_DESKTOP_URL_PREFIX`), and the `via-desktop-egress` baseline permission (`VIA_DESKTOP_PATH_PATTERN`). Permissions files of existing hosts keep a stale copy of that permission and its schema. It has no effect, because the route no longer exists.

- `DESKTOP_EGRESS_RULES_FILENAME` moved from `remote._machine` to `store`. In `account_scopes`, `SCHEMA_REFERENCE_PREFIX`, `ACCOUNT_METADATA_KEY`, `referenced_schema_name` and `custom_metadata_const_gate` are now public, so `desktop_egress` reuses them.

- The rules file format above is the one `latchkey-curl-shims` v0.4.0 reads, which is the release the VPS install pins. Its router also authenticates to the desktop gateway with the connected computer's gateway password and permissions-override JWT, read from the files provisioning already writes for the forwarding extension.
