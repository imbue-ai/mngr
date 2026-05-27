The `permission_requests` gateway extension now validates the `scope` and
`permissions` of incoming `predefined` POST `/permission-requests` bodies
against the bundled `services.json` catalog. A request whose `scope` is not a
known Detent scope, or whose `permissions` list contains entries that the
catalog does not list under that scope, is rejected with HTTP 400 at creation
time rather than persisted as a pending request that approval would happily
splice into `permissions.json`. File-sharing requests are unaffected.
