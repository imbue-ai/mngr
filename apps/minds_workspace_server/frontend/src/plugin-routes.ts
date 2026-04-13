/**
 * Plugin route registration. Plugins call $llm.registerRoute() to add
 * custom frontend routes. Registrations are collected before bootstrap
 * and merged into the Mithril route map.
 */

import m from "mithril";

type RouteRenderCallback = (container: HTMLElement, params: Record<string, string>) => void;
type RouteDestroyCallback = () => void;

interface PluginRouteHandler {
  render: RouteRenderCallback;
  destroy?: RouteDestroyCallback;
}

const registeredPluginRoutes: Map<string, PluginRouteHandler> = new Map();

export function registerPluginRoute(path: string, handler: RouteRenderCallback | PluginRouteHandler): boolean {
  if (registeredPluginRoutes.has(path)) {
    return false;
  }

  const normalizedHandler: PluginRouteHandler = typeof handler === "function" ? { render: handler } : handler;

  registeredPluginRoutes.set(path, normalizedHandler);
  return true;
}

export function getPluginRouteMithrilComponents(): Record<string, m.RouteDefs[string]> {
  const routes: Record<string, m.RouteDefs[string]> = {};

  for (const [path, handler] of registeredPluginRoutes) {
    routes[path] = createPluginRouteComponent(handler);
  }

  return routes;
}

function createPluginRouteComponent(handler: PluginRouteHandler): m.Component {
  return {
    oncreate(vnode: m.VnodeDOM) {
      handler.render(vnode.dom as HTMLElement, m.route.param() ?? {});
    },

    onupdate(vnode: m.VnodeDOM) {
      handler.render(vnode.dom as HTMLElement, m.route.param() ?? {});
    },

    onremove() {
      if (handler.destroy) {
        handler.destroy();
      }
    },

    view() {
      return m("div", { class: "plugin-route" });
    },
  };
}

export type { RouteRenderCallback, RouteDestroyCallback, PluginRouteHandler };
