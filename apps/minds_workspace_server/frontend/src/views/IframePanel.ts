import m from "mithril";

interface IframePanelAttrs {
  url: string;
  title: string;
}

export const IframePanel: m.Component<IframePanelAttrs> = {
  view(vnode) {
    const { url, title } = vnode.attrs;
    return m("iframe", {
      src: url,
      title,
      style: "width: 100%; height: 100%; border: none;",
      sandbox: "allow-scripts allow-same-origin allow-forms allow-popups",
    });
  },
};
