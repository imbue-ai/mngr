import { llmApi } from "./llm-api";
import type { LlmApi } from "./llm-api";
import { runHook } from "./hooks";
import { getPluginRouteMithrilComponents } from "./plugin-routes";
import { getBasePath } from "./base-path";
import m from "mithril";
import "./style.css";
import { App } from "./views/App";
import { SubagentView } from "./views/SubagentView";

declare global {
  interface Window {
    $llm: LlmApi;
  }
  var $llm: LlmApi;
}

window.$llm = llmApi;

async function bootstrap(): Promise<void> {
  m.route.prefix = getBasePath();
  const rootElement = document.getElementById("app");
  if (rootElement) {
    const pluginRoutes = getPluginRouteMithrilComponents();
    const appResolver: m.RouteResolver = { render: () => m(App) };
    m.route(rootElement, "/", {
      "/": appResolver,
      "/agents/:agentId": appResolver,
      "/agents/:agentId/subagents/:subagentSessionId": {
        render() {
          const agentId = m.route.param("agentId") ?? "";
          const subagentSessionId = m.route.param("subagentSessionId") ?? "";
          return m("div", { class: "app-layout flex h-screen" }, [
            m("div", { class: "app-main flex flex-1 flex-col min-w-80" }, [
              m(SubagentView, { agentId, subagentSessionId }),
            ]),
          ]);
        },
      },
      ...pluginRoutes,
    });
    await runHook("ready");
  }
}

window.addEventListener("load", bootstrap);
