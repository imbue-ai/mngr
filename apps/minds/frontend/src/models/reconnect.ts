// A 401 from Imbue Studio on this computer: this window's own login with the
// app has lapsed. Trying again cannot help; restarting the app reconnects it
// (offer `restartAppActions` beside the error).

import { WINDOW_LOGIN_LAPSED_SENTENCE, responseDetails } from "./errors";
import type { RequestOutcome, UserFacingError } from "./errors";

export function isReconnectNeeded(outcome: RequestOutcome): boolean {
  return outcome.status === 401;
}

export function reconnectError(outcome: RequestOutcome): UserFacingError {
  return {
    message: WINDOW_LOGIN_LAPSED_SENTENCE,
    details: responseDetails(outcome),
  };
}
