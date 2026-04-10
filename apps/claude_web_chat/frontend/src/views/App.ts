import m from "mithril";
import { getSelectedAgentId } from "../navigation";
import { MessageList } from "./MessageList";
import { Sidebar } from "./Sidebar";

export function App(): m.Component {
  return {
    view() {
      const selectedAgentId = getSelectedAgentId();

      return m("div", { class: "app-layout flex h-full" }, [
        m("div", { class: "minds-titlebar-spacer" }),
        m(Sidebar),
        m("div", { class: "app-main flex flex-1 flex-col min-w-80" }, [
          m(MessageList, { agentId: selectedAgentId }),
        ]),
      ]);
    },
  };
}
