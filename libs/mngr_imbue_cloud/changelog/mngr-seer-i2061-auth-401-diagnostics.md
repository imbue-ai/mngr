Added diagnostics for connector requests refused with HTTP 401, to tell an access token that expired during a transport retry apart from a session revoked elsewhere. Behavior is unchanged.

- A connector call made through the client's retrying sender (which includes the workspace and host listings discovery uses) that draws a 401 now logs, at info: the method and route path, which attempt drew it (a transport retry re-sends the token captured before the first attempt), how many seconds of validity the token had left at the first attempt and at the refused one (from the token's `exp`; the token itself is never logged), and whether the computer was suspended between those two attempts.

- A token refresh now logs at info the lifetime the new token was granted. A refresh the connector refuses logs its status and message at warning.

- `decode_jwt_exp` in `connector/session_store.py` (previously `_decode_jwt_exp`) is now public, since the client imports it too. It returns None instead of raising for a payload or `exp` it cannot read, so the 401 log never replaces the auth error.
