// Legacy-URL shim: /workspace/<id>/settings?group= was the standalone
// machine-settings page, a full-page duplicate of the options panel's
// settings tab. External deep links and restored pre-SPA sessions may still
// carry it, so it redirects (replace, no history entry) into the options
// overlay's settings tab rather than maintaining a second rendering of the
// same groups.

import m from "mithril";

export const WorkspaceSettingsRedirect: m.ClosureComponent = () => ({
  oninit() {
    const agentId = m.route.param("agentId");
    const group = m.route.param("group");
    const params = new URLSearchParams({ tab: "settings" });
    if (group) params.set("group", group);
    m.route.set(`/workspace/${agentId}/options?${params.toString()}`, undefined, { replace: true });
  },
  view() {
    return null;
  },
});
