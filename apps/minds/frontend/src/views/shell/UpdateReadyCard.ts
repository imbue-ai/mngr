// The downloaded-update offer.
//
// Deliberately quiet. Under the on-quit policy the update installs on the
// next restart whether or not this is ever touched, so it states a fact and
// offers a shortcut; under on-request it states that the install is waiting
// for a click. Either way it does not demand an answer, and nothing behind it
// is blocked while it is up.
//
// Carries no position of its own: the shell floats it in the corner, and the
// styleguide renders it in the flow of the page. Keeping placement out of it is
// what lets it be looked at without an update actually existing.

import m from "mithril";
import type { UpdateInstallPolicy } from "../../electron-bridge";
import { Button } from "../components/Button";
import { Icon16 } from "../components/Icon";

export interface UpdateReadyCardAttrs {
  version: string;
  installPolicy: UpdateInstallPolicy;
  needsPassword: boolean;
  /** Why the last install from this card did not go through, or null. */
  error: string | null;
  /** An install from this card is running and the app has not quit yet. */
  isInstalling: boolean;
  onRestart: () => void;
  onDismiss: () => void;
}

/** The second line and the button, which together say what a click costs. */
export function updateReadyCopy(installPolicy: UpdateInstallPolicy, needsPassword: boolean): {
  detail: string;
  action: string;
} {
  if (installPolicy === "on-quit") {
    return { detail: "Installs when you restart", action: "Restart now" };
  }
  return {
    detail: needsPassword ? "Installs when you ask; you'll be asked for your password" : "Installs when you ask",
    action: "Install and restart",
  };
}

/**
 * The two lines and the held button's label while the install runs: what is
 * happening, and what the user still has to do (on a .deb, answer the password
 * prompt) before the restart. Varies by policy like `updateReadyCopy`, so the
 * held label continues the sentence the live one started.
 */
export function updateInstallingCopy(
  version: string,
  installPolicy: UpdateInstallPolicy,
  needsPassword: boolean,
): { title: string; detail: string; action: string } {
  if (installPolicy === "on-quit") {
    return {
      title: `Restarting into Mind ${version}`,
      detail: "Mind will be back in a moment",
      action: "Restarting...",
    };
  }
  return {
    title: `Installing Mind ${version}`,
    detail: needsPassword
      ? "Enter your password when asked; Mind restarts when it's done"
      : "Mind restarts when it's done",
    action: "Installing...",
  };
}

export function UpdateReadyCard(): m.Component<UpdateReadyCardAttrs> {
  return {
    view(vnode) {
      const { version, installPolicy, needsPassword, error, isInstalling, onRestart, onDismiss } = vnode.attrs;
      // While the install runs the card is the only account of it: the button
      // is held, the copy says what is happening, and there is nothing to
      // dismiss, since dismissing would hide the one surface saying so.
      const copy = isInstalling
        ? updateInstallingCopy(version, installPolicy, needsPassword)
        : { title: `Mind ${version} is ready`, ...updateReadyCopy(installPolicy, needsPassword) };
      return m(
        "div",
        {
          class:
            "flex items-center gap-4 rounded-lg pl-4 pr-3 py-3 " +
            "bg-surface-primary border border-subtle shadow-raised",
          role: "status",
          "aria-busy": isInstalling ? "true" : undefined,
        },
        [
          // Two lines, so the version does not have to share weight with the
          // instruction: what happened, then what it costs.
          m("div", { class: "flex flex-col gap-0.5 min-w-0" }, [
            m("span", { class: "type-label text-primary truncate" }, copy.title),
            m("span", { class: "type-helper text-tertiary" }, copy.detail),
            // The install failed and the app is still here, so the button is
            // live again; without this line the click looks ignored.
            !isInstalling && error !== null
              ? m("span", { class: "type-helper text-important", role: "alert" }, error)
              : null,
          ]),
          isInstalling
            ? m(Button, { variant: "primary", disabled: true, extra: "shrink-0" }, copy.action)
            : m(Button, { variant: "primary", onclick: onRestart, extra: "shrink-0" }, copy.action),
          // The same shape as a dialog's close: a glyph in its own hit area,
          // rather than a bare character with no target to speak of.
          isInstalling
            ? null
            : m(
                "button",
                {
                  type: "button",
                  "aria-label": "Dismiss",
                  onclick: onDismiss,
                  class:
                    "shrink-0 inline-flex items-center justify-center w-7 h-7 rounded-md " +
                    "text-tertiary hover:text-primary hover:bg-fill-hover cursor-pointer",
                },
                m(Icon16, { name: "close" }),
              ),
        ],
      );
    },
  };
}
