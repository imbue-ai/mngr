import m from "mithril";

interface IframePanelAttrs {
  url: string;
  title: string;
  serverName?: string;
}

export const IFRAME_PANEL_SERVER_NAME_ATTR = "data-server-name";

export const IframePanel: m.Component<IframePanelAttrs> = {
  view(vnode) {
    const { url, title, serverName } = vnode.attrs;
    const attrs: Record<string, string> = {
      src: url,
      title,
      style: "width: 100%; height: 100%; border: none;",
      sandbox: "allow-scripts allow-same-origin allow-forms allow-popups",
    };
    if (serverName) {
      attrs[IFRAME_PANEL_SERVER_NAME_ATTR] = serverName;
    }
    return m("iframe", attrs);
  },
};

/** Reload every iframe tagged with data-server-name===serverName.
 *
 *  Prefers contentWindow.location.reload() when same-origin (common case:
 *  proxied under /forwarding/... so the iframe and host share origin).
 *  Falls back to reassigning the src attribute for cross-origin iframes,
 *  where reading contentWindow.location throws a SecurityError. Used by
 *  both the per-tab refresh button and the WS-driven agent-triggered
 *  refresh. */
export function reloadIframesForServer(serverName: string): number {
  const iframes = document.querySelectorAll<HTMLIFrameElement>(
    `iframe[${IFRAME_PANEL_SERVER_NAME_ATTR}="${CSS.escape(serverName)}"]`,
  );
  iframes.forEach((iframe) => {
    try {
      const win = iframe.contentWindow;
      if (win !== null) {
        win.location.reload();
        return;
      }
    } catch {
      // Cross-origin iframe: fall through to src reassignment.
    }
    const currentSrc = iframe.getAttribute("src");
    if (currentSrc !== null) {
      iframe.setAttribute("src", currentSrc);
    }
  });
  return iframes.length;
}
