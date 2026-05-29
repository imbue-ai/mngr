# Fix OAuth CLI hang after successful browser sign-in

- Fixed a bug in `mngr imbue_cloud auth oauth` where the local callback listener would hang until the 300s timeout after the browser had already returned the OAuth code. The handler now only records query params when the request is for `/oauth/callback` and carries non-empty params, so secondary browser GETs (favicon, prefetches, etc.) can no longer overwrite the captured callback with `{}`.
