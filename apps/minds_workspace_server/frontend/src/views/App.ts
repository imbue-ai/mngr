import m from "mithril";
import { DockviewWorkspace } from "./DockviewWorkspace";

export function App(): m.Component {
  return {
    view() {
      return m("div", { class: "app-layout flex", style: "height: calc(100vh - var(--minds-titlebar-height, 0px))" }, [
        m("div", { class: "minds-titlebar-spacer" }),
        m("div", { class: "app-main flex flex-1 min-w-80" }, [m(DockviewWorkspace)]),
      ]);
    },
  };
}
