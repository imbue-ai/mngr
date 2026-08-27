# Globally unique codex common-transcript event ids

- Common-transcript event ids are now derived by hashing the rollout line's timestamp and the first 1024 characters of its content (plus the item kind), instead of the line index alone. Line-index ids (`line-3-user`) repeated identically for every codex agent on every host, which collides under fleet-wide dedup by event id (e.g. in analytics). Re-processing the same input stays idempotent, and outputs written by the old converter are still recognized so upgrading never re-appends already-converted history.
