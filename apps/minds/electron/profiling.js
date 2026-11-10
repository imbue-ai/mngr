'use strict';

// The shell's half of the opt-in profiling mode (`minds run --profile` on the
// backend side): the launch switch that turns it on, the categories Chromium
// records under it, and the flush that hands Chromium's trace to the backend
// before the backend is stopped at quit, so the one trace file the backend
// writes also carries the shell's and the renderer's timeline.
//
// Free of any `electron` import so it is unit-testable under plain node (see
// ../test/unit/profiling.test.js); main.js hands the flush its live
// collaborators (contentTracing, fs, net.request).

const PROFILE_SWITCH = '--profile';
const PROFILE_FILE_ENV_VAR = 'MINDS_PROFILE_FILE';

// `blink.user_timing` is excluded because the SPA posts its own marks and
// measures to the backend, with their arguments and flow links; recording them
// here too would double every renderer slice.
const CHROMIUM_TRACE_CATEGORIES = Object.freeze({
  included: Object.freeze(['toplevel', 'devtools.timeline', 'v8', 'navigation', 'loading']),
  excluded: Object.freeze(['blink.user_timing']),
});
// A fixed buffer, so a long session stops recording rather than growing
// without bound.
const CHROMIUM_TRACE_BUFFER_KB = 128 * 1024;
// How long the quit waits for the backend to take the trace before writing it
// beside the profile file instead.
const FLUSH_TIMEOUT_MS = 30000;

/**
 * The trace file a launch asks for, or null when profiling is off: the switch
 * in the launch arguments (either form) first, then the exported variable. A
 * switch with no value, or an empty value in either place, asks for nothing.
 */
function profilePathFromLaunch({ args, env }) {
  const argv = Array.isArray(args) ? args : [];
  let fromArgs = null;
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (typeof argument !== 'string') continue;
    if (argument === PROFILE_SWITCH) {
      const next = argv[index + 1];
      if (typeof next === 'string' && !next.startsWith('--')) fromArgs = next;
      break;
    }
    if (argument.startsWith(`${PROFILE_SWITCH}=`)) {
      fromArgs = argument.slice(PROFILE_SWITCH.length + 1);
      break;
    }
  }
  if (fromArgs !== null && fromArgs !== '') return fromArgs;
  const fromEnv = env ? env[PROFILE_FILE_ENV_VAR] : undefined;
  if (typeof fromEnv === 'string' && fromEnv !== '') return fromEnv;
  return null;
}

/**
 * The environment entry backend.js adds for the backend: the trace file's
 * path, or nothing when profiling is off.
 */
function profileFileEnv(profilePath) {
  if (profilePath === null) return {};
  return { [PROFILE_FILE_ENV_VAR]: profilePath };
}

/** The options `contentTracing.startRecording` records Chromium's trace with. */
function chromiumTracingOptions() {
  return {
    included_categories: [...CHROMIUM_TRACE_CATEGORIES.included],
    excluded_categories: [...CHROMIUM_TRACE_CATEGORIES.excluded],
    recording_mode: 'record-until-full',
    trace_buffer_size_in_kb: CHROMIUM_TRACE_BUFFER_KB,
  };
}

/** Where Chromium's trace lands when the backend could not take it. */
function sidecarPathFor(profilePath) {
  return `${profilePath}.chromium.json`;
}

/**
 * Hand Chromium's trace to the backend at quit: stop the recording (which
 * writes the trace to a temporary file), read it, and post it. When the post
 * fails or times out, the raw document is written to `sidecarPath` instead so
 * nothing is lost; the temporary file is deleted either way. Never throws, so
 * the quit is never blocked past the timeout: every failure is logged and
 * reported in the result.
 *
 * @param {object} collaborators
 * @param {() => Promise<string>} collaborators.stopRecording  Stops Chromium's recording;
 *                                                             resolves with the temporary
 *                                                             file's path.
 * @param {(path: string) => Promise<string|Buffer>} collaborators.readFile
 * @param {(document: string|Buffer, signal: AbortSignal) => Promise<void>} collaborators.postTrace
 *        Posts the document to the backend; resolves once the backend accepted it, rejects
 *        otherwise, and gives up when the signal aborts.
 * @param {(path: string, document: string|Buffer) => Promise<void>} collaborators.writeSidecar
 * @param {(path: string) => Promise<void>} collaborators.deleteFile
 * @param {(line: string) => void} collaborators.log
 * @param {number} [collaborators.timeoutMs]
 * @param {string} collaborators.sidecarPath
 * @returns {Promise<{posted: boolean, sidecarPath: string|null}>}
 */
async function flushChromiumTrace({
  stopRecording,
  readFile,
  postTrace,
  writeSidecar,
  deleteFile,
  log,
  timeoutMs = FLUSH_TIMEOUT_MS,
  sidecarPath,
}) {
  const nothing = { posted: false, sidecarPath: null };

  let tracePath = null;
  try {
    tracePath = await stopRecording();
  } catch (err) {
    log(`[profiling] could not stop Chromium's trace recording: ${err.message || err}`);
    return nothing;
  }
  let document = null;
  try {
    document = await readFile(tracePath);
  } catch (err) {
    log(`[profiling] could not read Chromium's trace at ${tracePath}: ${err.message || err}`);
    await deleteQuietly(deleteFile, tracePath, log);
    return nothing;
  }

  let isPosted = false;
  try {
    await postWithTimeout(postTrace, document, timeoutMs);
    isPosted = true;
    log('[profiling] posted Chromium\'s trace to the backend');
  } catch (err) {
    log(`[profiling] could not post Chromium's trace to the backend: ${err.message || err}`);
  }

  let writtenSidecarPath = null;
  if (!isPosted) {
    try {
      await writeSidecar(sidecarPath, document);
      writtenSidecarPath = sidecarPath;
      log(`[profiling] wrote Chromium's trace to ${sidecarPath} instead`);
    } catch (err) {
      log(`[profiling] could not write Chromium's trace to ${sidecarPath}: ${err.message || err}`);
    }
  }

  await deleteQuietly(deleteFile, tracePath, log);
  return { posted: isPosted, sidecarPath: writtenSidecarPath };
}

function postWithTimeout(postTrace, document, timeoutMs) {
  const controller = new AbortController();
  let timer = null;
  const timeout = new Promise((_resolve, reject) => {
    timer = setTimeout(() => {
      // Rejected before the abort so the race reports the timeout, not the
      // abandoned post's own rejection.
      reject(new Error(`timed out after ${timeoutMs}ms`));
      controller.abort();
    }, timeoutMs);
  });
  return Promise.race([postTrace(document, controller.signal), timeout]).finally(() => clearTimeout(timer));
}

async function deleteQuietly(deleteFile, tracePath, log) {
  try {
    await deleteFile(tracePath);
  } catch (err) {
    log(`[profiling] could not delete Chromium's temporary trace at ${tracePath}: ${err.message || err}`);
  }
}

module.exports = {
  PROFILE_SWITCH,
  PROFILE_FILE_ENV_VAR,
  CHROMIUM_TRACE_CATEGORIES,
  CHROMIUM_TRACE_BUFFER_KB,
  FLUSH_TIMEOUT_MS,
  profilePathFromLaunch,
  profileFileEnv,
  chromiumTracingOptions,
  sidecarPathFor,
  flushChromiumTrace,
};
