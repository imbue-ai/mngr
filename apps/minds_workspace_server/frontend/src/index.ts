import { llmApi } from "./llm-api";
import type { LlmApi } from "./llm-api";
import { runHook } from "./hooks";
import { getPluginRouteMithrilComponents } from "./plugin-routes";
import { getBasePath } from "./base-path";
import { initAgentManager } from "./models/AgentManager";
import m from "mithril";
import "./style.css";
import { App } from "./views/App";

interface MindsDesktopBridge {
  // The preload awaits the handler's return value, so sync or async
  // implementations are both fine.
  setCloseActiveTabHandler?: (handler: () => boolean | Promise<boolean>) => void;
}

declare global {
  interface Window {
    $llm: LlmApi;
    // Provided by the Electron preload script in the minds desktop shell;
    // undefined when the workspace frontend is served in a plain browser.
    minds?: MindsDesktopBridge;
  }
  var $llm: LlmApi;
}

window.$llm = llmApi;

function getEffectiveRoutePrefix(): string {
  // When served through the desktop client proxy, the <base> tag contains
  // the forwarding prefix (e.g., /forwarding/{agentId}/web/). Use this as
  // the Mithril route prefix so pushState preserves the correct URL in the
  // browser history stack (enabling back/forward navigation).
  const baseEl = document.querySelector("base[href]");
  if (baseEl) {
    const href = baseEl.getAttribute("href") ?? "";
    if (href.includes("/forwarding/")) {
      return href.replace(/\/+$/, "");
    }
  }
  return getBasePath();
}

async function bootstrap(): Promise<void> {
  m.route.prefix = getEffectiveRoutePrefix();
  initAgentManager();
  const rootElement = document.getElementById("app");
  if (rootElement) {
    const pluginRoutes = getPluginRouteMithrilComponents();
    const appResolver: m.RouteResolver = { render: () => m(App) };
    m.route(rootElement, "/", {
      "/": appResolver,
      ...pluginRoutes,
    });
    await runHook("ready");
  }
}

window.addEventListener("load", bootstrap);
