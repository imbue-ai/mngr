Phase 2 of the user-controlled-keys plan: store-backed host-key pinning for leased hosts.

`_record_host_key` (authoritative pin at lease/rebuild) and `_ensure_host_key_pinned` (add-if-absent per endpoint+keytype) now write through mngr core's new host-key pin store as bootstrap-origin pins, so the per-host known_hosts file becomes a derived artifact. Bootstrap pins can never displace the user-origin pins that adoption introduces in a later phase, and the Phase 0 guarantees are preserved: a slow-path-rebuilt container's locally-recorded key is never clobbered by the connector's stale initial key, and a foreign-keytype entry never blocks a pin.

The stop/start relocation re-pin (`_start_workspace_and_wait`) is thereby store-routed too, and slice-provider pins (bake-time VM-root and container host keys) carry the host_id so they land on the host's own store record.
