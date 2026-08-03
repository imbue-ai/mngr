// In-app error surface (the SPA twin of RequestError.jinja): a friendly
// fallback with a title, a message, and a way back to the machine list, for
// routing-level dead ends or a page whose data load failed. Auth-failure
// copy (the used-once login code) now lives on the static login page, so this
// covers only the in-app navigation errors.

import m from "mithril";
import { ButtonLink } from "../components/Button";
import { PageNarrowContainer } from "../components/Layout";
import { routeLinkAttrs } from "../components/route-link";

export interface RouteErrorAttrs {
  title?: string;
  message?: string;
}

export function RouteError(): m.Component<RouteErrorAttrs> {
  return {
    view(vnode) {
      const title = vnode.attrs.title ?? "Page not found";
      const message = vnode.attrs.message ?? "That link doesn't lead anywhere in the app.";
      return m(PageNarrowContainer, { maxWidth: "max-w-[460px]" }, [
        m("h1", { class: "type-heading text-primary" }, title),
        m("p", { class: "mt-2 text-primary" }, message),
        m("div", { class: "mt-4" }, m(ButtonLink, { variant: "primary", ...routeLinkAttrs("/") }, "Back to machines")),
      ]);
    },
  };
}
