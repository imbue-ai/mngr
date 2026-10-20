The default GCE boot image is now the global `debian-13` (trixie) family instead of `debian-12`, matching the other mngr_vps clouds, the workspace container, and the desktop lima VM. Override with `default_source_image` or `--gcp-image=` as before.

- The GCE startup script now installs mngr's SSH host key and sshd drop-in only when they differ from what is on disk, and restarts sshd only then. The guest agent re-runs the script on every boot, so the old unconditional restart happened on every `mngr start` too, right after mngr had seen the expected host key and just as it opened its first connection, which sshd then reset (`Connection reset by peer`). Bare-isolation hosts, whose agent endpoint is the VM's port 22, hit this most.

