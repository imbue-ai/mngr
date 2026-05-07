/**
 * Informational modal for the per-service Share button.
 *
 * Sharing (Cloudflare tunnels + Access policies) is configured from the
 * Minds desktop app's workspace settings page. The workspace server no
 * longer routes share-button clicks back to minds via request events --
 * this modal just tells the user where to go.
 */

import m from "mithril";

interface ShareModalAttrs {
  serviceName: string;
  onClose: () => void;
}

export const ShareModal: m.Component<ShareModalAttrs> = {
  view(vnode) {
    const { serviceName, onClose } = vnode.attrs;
    return m(
      "div.share-modal-overlay",
      {
        onclick: (e: Event) => {
          if (e.target === e.currentTarget) onClose();
        },
      },
      [
        m("div.share-modal", [
          m("div.share-modal-header", [
            m("h3.share-modal-title", `Share "${serviceName}"`),
            m("button.share-modal-close-x", { onclick: onClose, title: "Close" }, "x"),
          ]),
          m("div", { style: "padding: 8px 0; color: #444; font-size: 14px; line-height: 1.5;" }, [
            m("p", { style: "margin: 0 0 12px 0;" }, [
              "To share this service externally, open the Minds desktop app, go to ",
              m("strong", "workspace settings"),
              ", and enable sharing for the ",
              m("strong", `"${serviceName}"`),
              " service.",
            ]),
            m(
              "p",
              { style: "margin: 0; color: #666;" },
              "You'll need to be signed in with an Imbue Cloud account so a Cloudflare tunnel can be created on your behalf.",
            ),
          ]),
          m("div.share-modal-footer", [
            m("button.share-modal-btn.share-modal-btn-secondary", { onclick: onClose }, "Close"),
          ]),
        ]),
      ],
    );
  },
};
