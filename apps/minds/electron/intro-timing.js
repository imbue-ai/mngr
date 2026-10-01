// The loading document's first-launch intro, as numbers: how the loader comes
// and goes, and how the page hands over to the app once the mark is parked.
//
// The intro is in two halves, and only the second one is on a clock:
//
//   The ARRIVAL runs as soon as the page loads. The letters pop in (intro-pop.js,
//   springs rather than keyframes, so there is no duration to write down here)
//   and the loader may fade in under them once they have settled. The page then
//   holds there for as long as starting up takes -- which is the whole point of
//   the split: the wait happens under a finished mark, not under a film that
//   has to be stretched or cut to cover it.
//
//   The DEPARTURE runs when the main process says the app is ready. The loader
//   goes, the lockup travels to its parked place, and the page settles onto
//   white. Only then does the route land, so the travel always means the same
//   thing: the app is ready.

const INTRO_TIMING = {
  // The loader fading in under the settled mark, and back out when the app is
  // ready. One number for both: it is the same object arriving and leaving.
  loaderFadeMs: 400,
  // The loader is for a wait worth reporting, and a wait worth reporting is one
  // you notice. A launch that is ready within loaderGraceMs of the mark landing
  // never shows it at all -- better the film simply runs on than a status line
  // appears and is gone before it can be read. One that does show it holds it
  // for at least loaderMinMs, so what it says gets read rather than glimpsed.
  loaderGraceMs: 500,
  loaderMinMs: 1500,
  // The beat after the loader is gone before the lockup sets off, and how long
  // its travel to the parked place takes.
  travelGapMs: 150,
  travelMs: 500,
  // The settle, once the mark is parked: the page goes from the intro's brown
  // to white while the mark goes the other way, the two crossing together so
  // the drawing never loses its contrast against the page.
  settleMs: 600,
};

/**
 * Walk the departure forward from the moment the app reports itself ready: the
 * loader leaves, then the lockup travels and the page settles under it.
 *
 * A departure with no loader to clear passes `loaderFadeMs: 0`, so the mark
 * sets off straight away rather than waiting out a fade with nothing fading.
 *
 * @returns {{ travelAt: number, parkedAt: number, settledAt: number }}
 *   every instant in milliseconds from the ready signal.
 */
function computeDepartureSchedule(timing = INTRO_TIMING) {
  const travelAt = timing.loaderFadeMs + timing.travelGapMs;
  const parkedAt = travelAt + timing.travelMs;
  return { travelAt, parkedAt, settledAt: parkedAt + timing.settleMs };
}

const introTiming = { INTRO_TIMING, computeDepartureSchedule };

// Loaded by shell.html with a plain script tag (the renderer has no require),
// and required by the node unit tests.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = introTiming;
}
if (typeof window !== 'undefined') {
  window.mindsIntroTiming = introTiming;
}
