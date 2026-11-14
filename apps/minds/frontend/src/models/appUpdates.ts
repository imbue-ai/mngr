// What the app's own updater failures tell the user: the Settings > Updates
// panel and the floating "ready to install" card. One sentence of ours per
// failure, and the main process's text kept for "See details".

import { InstallUpdateError } from "../electron-bridge";
import type { UpdateStatus } from "../electron-bridge";
import { errorFromException, fallbackErrorSentence } from "./errors";
import type { UserFacingError } from "./errors";

/** An updater failure, and whether its line asks for a bug report. */
export interface AppUpdateFailure extends UserFacingError {
  /** False for a failure whose sentence already says what to do next. */
  isBugReportOffered: boolean;
}

function reportable(error: UserFacingError): AppUpdateFailure {
  return { ...error, isBugReportOffered: true };
}

function thrownText(thrown: unknown): string {
  return thrown instanceof Error ? thrown.message : String(thrown);
}

/** The standing line for an `error` status, and what the updater said. */
export function updateCheckFailure(status: UpdateStatus): UserFacingError {
  const details = status.message ?? "";
  if (status.kind === "download" && status.version !== undefined) {
    return {
      message:
        `Imbue Studio ${status.version} is available but couldn't be downloaded. ` +
        "It will try again at the next check.",
      details,
    };
  }
  if (status.kind === "network") {
    return {
      message:
        "Couldn't check for updates — *check your internet connection, then try again*.",
      details,
    };
  }
  return {
    message: fallbackErrorSentence("Couldn't check for updates"),
    details,
  };
}

/** Whether a failed check's line asks for a bug report: a connection the user
 * can fix, or a download the next check retries, does not. */
export function isCheckFailureReportable(status: UpdateStatus): boolean {
  return status.kind !== "network" && status.kind !== "download";
}

/** Why a staged update did not install, for the button labelled `action`
 * that tries it again. */
export function installFailure(
  thrown: unknown,
  action: string,
): AppUpdateFailure {
  const details = thrownText(thrown);
  if (thrown instanceof InstallUpdateError && thrown.kind === "no-signal") {
    return {
      message:
        "Imbue Studio couldn't confirm the update started installing. " +
        `If it hasn't restarted in a minute, press ${action} to try again.`,
      details,
      isBugReportOffered: false,
    };
  }
  return reportable({
    message: fallbackErrorSentence(
      "The update didn't install. It's still downloaded",
    ),
    details,
  });
}

/** The Updates panel could not read the app's update state at all. */
export function updateStateLoadFailure(thrown: unknown): AppUpdateFailure {
  return reportable(
    errorFromException("Couldn't load your update settings", thrown),
  );
}

/** A refused channel switch; `currentLabel` is the channel still in effect. */
export function channelSwitchFailure(
  thrown: unknown,
  currentLabel: string,
): AppUpdateFailure {
  return reportable({
    message: fallbackErrorSentence(
      `Imbue Studio couldn't switch which updates you get. You're still on ${currentLabel} updates`,
    ),
    details: thrownText(thrown),
  });
}

/** A "Check now" whose call to the main process was refused. */
export function checkRequestFailure(thrown: unknown): AppUpdateFailure {
  return reportable(errorFromException("Couldn't check for updates", thrown));
}

/** A failed re-read of what each channel serves. */
export function channelReadFailure(thrown: unknown): AppUpdateFailure {
  return reportable(
    errorFromException("Couldn't check which updates are available", thrown),
  );
}
