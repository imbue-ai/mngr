# Spec uncertainties

Conflicts or potentially-outdated information found while writing specs, to be
resolved later. Each entry: the conflict, where it is, and the assumption made.

## VPS Docker "single mode of operation" vs the bare/docker realizer axis

- **Where:** `specs/vps-docker-provider/spec.md` ("Single mode of operation"
  section) asserts the VPS providers have one mode: the VPS always runs and the
  Docker container is the host, with `docker stop`/`docker commit` as the
  stop/snapshot primitives.
- **Conflict:** `specs/bare-providers/spec.md` introduces a *bare* realization
  (agent directly on the VM, no container) selected by `config.mode`, and the
  instance-stop lifecycle (`specs/aws-ec2-stop-start-lifecycle/spec.md`) already
  added a machine-stop path that the "single mode" framing predates.
- **Assumption made:** the "single mode" statement describes the original Docker
  shape, not an invariant. The bare spec treats realization as an explicit axis and
  the Docker shape as one (default) point on it. When the bare work lands, update
  `specs/vps-docker-provider/spec.md` to reference the realizer axis.

## minds-webcontentsview-refactor spec vs current chrome architecture

- **Where:** `specs/minds-webcontentsview-refactor/spec.md` describes a
  left-slide-in sidebar view fed by `/_chrome/events`, `/forwarding/{agent_id}/`
  content URLs, and a three-view layout with a dedicated sidebar view.
- **Conflict:** the shipped code uses a floating workspace-switcher menu
  anchored to the titlebar breadcrumb (shared modal/overlay WebContentsView,
  `Sidebar.jinja` + `sidebar.js`), `/goto/<agent-id>/` URLs served by the
  `mngr forward` plugin, and an always-warm overlay surface (`/_chrome/overlay`)
  hosting modal iframes.
- **Assumption made (while writing `specs/minds-chrome-mithril-migration/`):**
  the code is authoritative; the old spec is a historical design document for a
  refactor that has since evolved. The new spec describes the current
  architecture from the code, not from that spec.
