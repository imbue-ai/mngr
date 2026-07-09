# Cover the new backup disable route with the backups-manage verb

- The `minds-workspaces-backups-manage` target-scoped verb's path pattern now also covers `POST /api/v1/workspaces/<id>/backup-service/disable` (the minds app's new "turn backups off" action), and its description mentions disabling.
