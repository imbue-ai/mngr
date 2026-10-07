- `mngr latchkey forward --device-id`'s help text now says an agent, rather than a workspace, tells the user's desktops apart by it.

- File-sharing grants are scoped to one desktop. The gateway's `permission-requests` extension mints a `file-sharing` grant for the desktop it runs on (`LATCHKEY_EXTENSION_LOCAL_DEVICE_ID`): the permission is named `minds-file-server-<access>-<device id>:<path>` and matches `/minds-api-proxy/api/v1/files/<device id><path>`. A desktop therefore cannot grant access to another desktop's files, and a gateway without a device id refuses file-sharing requests with HTTP 503. The request payload is unchanged; a workspace picks the desktop by sending the request there with `X-Latchkey-Desktop`.

- The first permissions migration (format version 1) gives each existing file-sharing grant a twin naming the desktop that migrates the policy. The existing grant is left in place, so a workspace built against the device-less URL keeps its access. `PermissionsMigration.apply` now also takes a `PermissionsMigrationContext` carrying that desktop's device id, and `migrate_permissions`, `migrate_permissions_and_push`, `MachineCredentials.refresh` and `provision_remote_gateway` take it too.

- Approving a `file-sharing` request computes its grant at approval rather than applying the effect stored when it was filed, so a request filed by a build from before this change and approved after it also yields a grant naming the desktop.

- New `imbue.mngr_latchkey.file_sharing` module reads and writes file-sharing permission names, those from before grants named a desktop included, and now owns `FileSharingAccess`.
