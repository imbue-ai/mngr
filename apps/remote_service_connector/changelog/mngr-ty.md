# supertokens floor bump + ratchet count tightening

- Raised the `supertokens-python` floor from `>=0.27.0` to `>=0.31.3`. During the repo-wide `uv lock --upgrade`, the resolver would otherwise backtrack `supertokens-python` to 0.30.3 (an auth-library downgrade, which also caps `aiosmtplib<4`) in order to keep `packaging` at 26; the floor keeps it at the latest 0.31.3, leaving `aiosmtplib` at 5.x and `packaging` at 25 (immaterial).
- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.
