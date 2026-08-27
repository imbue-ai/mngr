// The hub-page counterpart of the shell's band: same conditions, same copy,
// rendered in the page's own flow.
//
// A band over a hub page would reserve height with nothing behind it to
// preserve, and would make the page read as broken rather than out of date.
// Both surfaces take their payload from notice-band.ts, so neither can drift
// into describing the condition differently from the other.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { Button } from "../components/Button";
import { Notice } from "../components/Notice";
import { electronBridge } from "../../electron-bridge";
import { localPageNoticeFor } from "./notice-band";

export function LocalPageNotice(): m.Component {
  return {
    view() {
      const health = getAppContext().stores.health;
      const payload = localPageNoticeFor(health.discoveryHealth, electronBridge.isDesktop, health.appEnvironmentCondition());
      if (payload === null) return null;
      // Aligned to the page container rather than the full scroll width, so
      // it reads as part of the page's own column alongside the notices the
      // pages render themselves.
      return m(
        "div",
        { class: "max-w-[720px] mx-auto px-6 pt-6" },
        m(
          Notice,
          { variant: payload.variant, id: "local-page-notice" },
          m("div", { class: "flex items-center justify-between gap-3" }, [
            m("span", payload.message),
            payload.action !== null
              ? m(
                  Button,
                  {
                    variant: "secondary",
                    // The only action that fixes this condition, and it is
                    // recoverable: every machine keeps running across it.
                    onclick: () => electronBridge.restartApp(),
                  },
                  payload.action.label,
                )
              : null,
          ]),
        ),
      );
    },
  };
}
