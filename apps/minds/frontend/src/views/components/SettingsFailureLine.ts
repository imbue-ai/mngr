// A settings request's failure, drawn beside the control it belongs to.

import m from "mithril";
import type { SettingsRequestFailure } from "../../models/settings";
import { ErrorNotice, restartAppActions } from "./ErrorDisplay";
import { InlineError } from "./errorDesign";

/** A settings request that failed, beside the control it belongs to: a
 * refusal that explains itself as plain text, anything else as an error with
 * its details and Try again (or, for a lapsed login, the restart that
 * reconnects). */
export function settingsFailureLine(
  id: string,
  failure: SettingsRequestFailure | null,
): m.Children {
  if (failure === null) return null;
  if (failure.isExplained) {
    return m(
      "div",
      { id, class: "mt-3", role: "alert" },
      m(InlineError, { message: failure.error.message }),
    );
  }
  return m(
    "div",
    { class: "mt-3" },
    m(ErrorNotice, {
      id,
      error: failure.error,
      actions: failure.isReconnectNeeded
        ? restartAppActions()
        : failure.retry !== undefined
          ? [{ label: "Try again", onclick: failure.retry }]
          : [],
      isBugReportOffered: failure.isBugReportOffered,
    }),
  );
}
