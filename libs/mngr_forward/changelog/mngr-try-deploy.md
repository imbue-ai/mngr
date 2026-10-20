Fixed: terminals (and other service panels) in workspaces created before the
default-workspace-template dropped its `/service/` path-prefix proxy no longer
reload forever under the new desktop client. Those workspaces' system_interface
serves service panels at `/service/<name>/...` on its own origin behind a
service-worker bootstrap whose `document.cookie` write the desktop's
partitioned content embedding rejects, so the bootstrap page looped
indefinitely. The forward proxy now intercepts such navigations when they would
route to the shell and 307-redirects them to the service's own origin
(`<name>.host-<hex>.localhost`), where the service is proxied directly with no
bootstrap involved. Label-less legacy registrations resolve via the existing
label-as-name fallback. Scoped to navigations naming a service that actually
resolves; everything else passes through unchanged. CLEANUP-marked for removal
once every pre-update workspace has run `update-self`.
