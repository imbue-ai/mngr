import { llmApi } from "./llm-api";
import type { LlmApi } from "./llm-api";
import { runHook } from "./hooks";
import { getPluginRouteMithrilComponents } from "./plugin-routes";
import { getBasePath } from "./base-path";
import m from "mithril";
import "./style.css";
import { App } from "./views/App";

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
      ...pluginRoutes,
    });
    await runHook("ready");
  }
}

window.addEventListener("load", bootstrap);
