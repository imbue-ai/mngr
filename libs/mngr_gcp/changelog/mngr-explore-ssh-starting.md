- GCP hosts inherit the shared VPS host-setup fix that registers the gVisor
  (runsc) runtime with `--overlay2=none`, so an agent container's writable layer
  persists across a `docker stop`/`start` or host reboot instead of being lost to
  the default per-sandbox overlay.
