// The auto-updater.
//
// Drives `electron-updater` directly rather than `@todesktop/runtime`'s
// wrapper, because the wrapper cannot serve a release channel:
//
//   * it asks ToDesktop whether the RUNNING build is released and, when it is
//     not, never constructs its updater agent -- and every build a fast channel
//     serves is unreleased, so those users would have a dead updater;
//   * its agent's constructor sets `allowDowngrade = true` on the shared
//     electron-updater singleton, from an async init that can land after ours.
//
// ToDesktop still builds, signs, notarizes, and hosts every artifact. Only the
// feed each channel reads is ours.

const { app } = require('electron');
const fs = require('fs');
const { autoUpdater } = require('electron-updater');
const { parse: parseToml } = require('smol-toml');

const paths = require('./paths');
const channels = require('./update-channel');

const CHECK_INTERVAL_MS = 10 * 60 * 1000;
const CHECK_TIMEOUT_MS = 60 * 1000;

let statusListener = null;
let lastStatus = { type: 'idle' };
let inFlight = null;
/**
 * When a check last settled, ISO-8601, or null before the first one.
 *
 * Most checks are ones the user did not ask for -- at startup and every
 * interval after -- so a panel opened between them would have nothing to show
 * for any of them.
 */
let lastCheckedAt = null;
/** The version already downloaded and offered, so a later check does not re-offer it. */
let downloadedVersion = null;

/**
 * The channel-manifest host for this tier, or null when unconfigured.
 *
 * A build with no host falls back to ToDesktop's feed and can offer stable
 * only.
 */
function readFeedBaseUrl() {
  const configPath = paths.getBundledClientConfigPath();
  if (!configPath) {
    return null;
  }
  try {
    const parsed = parseToml(fs.readFileSync(configPath, 'utf8'));
    return parsed.update_feed_base_url || null;
  } catch (err) {
    console.error(`[update] Could not read ${configPath}: ${err.message}`);
    return null;
  }
}

function getAppId() {
  return require('../todesktop.js').id;
}

/**
 * Whether electron-updater can run at all.
 *
 * Only a packaged build has Contents/Resources/app-update.yml, without which
 * every check rejects with ENOENT -- which would read as a broken feed rather
 * than as updates being switched off in dev. A dev run has nothing to update
 * either way: there is no signed bundle for Squirrel to swap.
 */
function isUpdaterUsable() {
  return app.isPackaged;
}

function resolveFeed(channel) {
  return channels.feedForChannel(channel, { appId: getAppId(), feedBaseUrl: readFeedBaseUrl() });
}

/** The stored channel, falling back to stable and saying so in the log. */
function currentChannel() {
  const { channel, reason } = channels.readChannel(paths.getDataDir());
  if (reason) {
    console.warn(`[update] Falling back to ${channel}: stored channel ${reason}`);
  }
  return channel;
}

function setStatus(status) {
  lastStatus = status;
  if (statusListener) {
    statusListener(status);
  }
}

function getStatus() {
  return lastStatus;
}

/** Record that a check settled, and hand back the time for the status it publishes. */
function stampCheck() {
  lastCheckedAt = new Date().toISOString();
  return lastCheckedAt;
}

/** Report a settled check. */
function publishOutcome(channel, outcome) {
  stampCheck();
  setStatus({
    type: outcome.status,
    channel,
    currentVersion: app.getVersion(),
    feedVersion: outcome.feedVersion,
    lastCheckedAt,
  });
}

/**
 * Reject if `promise` has not settled within `timeoutMs`.
 *
 * electron-updater has no working request timeout under Electron: its handler
 * arms inside `request.on('socket')`, and `net.ClientRequest` never emits that
 * event, so a connection that stalls without an RST leaves `checkForUpdates()`
 * pending for good. That promise is `inFlight`, so unbounded it takes the whole
 * chain with it -- every later check, peek and channel switch queues behind it,
 * the status stays `checking` because a hang is not a rejection, and the Settings
 * panel's controls stay disabled with nothing said.
 */
function withTimeout(promise, timeoutMs, description) {
  let timer = null;
  const deadline = new Promise((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`${description} timed out after ${timeoutMs / 1000}s.`)), timeoutMs);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

/**
 * Run one check against `channel`.
 *
 * Checks are serialized: electron-updater dedupes concurrent `checkForUpdates`
 * calls by returning the in-flight promise, which would hand a caller a result
 * produced under the PREVIOUS feed configuration. Awaiting our own chain keeps
 * "configure, then check" atomic.
 *
 * Only the check is bounded, never the download, which legitimately runs for
 * minutes.
 */
async function checkChannel(channel) {
  const feed = resolveFeed(channel);
  channels.applyFeedToUpdater(autoUpdater, feed);
  let result;
  try {
    result = await withTimeout(autoUpdater.checkForUpdates(), CHECK_TIMEOUT_MS, `The ${channel} update check`);
  } catch (err) {
    // On a deadline the check is still pending, and electron-updater hands a
    // pending check to whoever asks next -- so without this one stall ends
    // checking, peeking and switching for the life of the process. A rejection
    // it produced itself has already cleared the same field, so this is a no-op
    // there rather than a case to tell apart.
    channels.discardInFlightCheck(autoUpdater);
    throw err;
  }
  // Never `updateInfo`: that is the parsed manifest and is present on every
  // successful check, including when the feed is behind us.
  const isUpdateAvailable = Boolean(result && result.isUpdateAvailable);
  const feedVersion = result && result.updateInfo ? result.updateInfo.version : null;
  return {
    channel,
    feedVersion,
    isUpdateAvailable,
    status: channels.computeUpdateStatus({
      currentVersion: app.getVersion(),
      feedVersion,
      isUpdateAvailable,
    }),
  };
}

/**
 * Run `task` after every previously queued one, never concurrently.
 *
 * Two tasks interleaving would let one caller's feed configuration be checked
 * under another's, and electron-updater compounds that by returning an
 * already-in-flight `checkForUpdates` promise to whoever asks second.
 */
function serialize(task) {
  inFlight = inFlight ? inFlight.then(task, task) : task();
  return inFlight;
}

/**
 * Run one check, reporting rather than rejecting.
 *
 * Callers read the pushed status, never a returned promise, so a rejection has
 * nowhere to surface. The failures `runCheck` cannot see -- it resolves the
 * channel out of the data directory before entering its own try, and
 * `getMindsRootName()` throws on a bundle whose `root_name` does not match the
 * runtime regex -- become an error status here rather than a line in
 * electron.log and a panel that says nothing.
 */
async function check() {
  try {
    return await runCheck();
  } catch (err) {
    return { channel: null, status: 'error', message: reportRejectedCheck(err) };
  }
}

async function runCheck() {
  if (!isUpdaterUsable()) {
    const channel = currentChannel();
    setStatus({ type: 'disabled', reason: 'not-packaged' });
    return { channel, status: 'disabled' };
  }
  const run = async () => {
    const channel = currentChannel();
    try {
      setStatus({ type: 'checking', channel });
      const outcome = await checkChannel(channel);
      // An update stays "available" until it is installed, so without this the
      // interval check would re-download what is already staged. Stamped like
      // any other settled check: from here on every check takes this path, so
      // skipping it would freeze "Checked ... ago" for as long as the update
      // waits to be installed.
      if (channels.isAlreadyStaged(outcome, downloadedVersion)) {
        setStatus({ type: 'update-downloaded', version: downloadedVersion, lastCheckedAt: stampCheck() });
        return outcome;
      }
      publishOutcome(channel, outcome);
      if (outcome.isUpdateAvailable) {
        startDownload(channel);
      }
      return outcome;
    } catch (err) {
      const message = String((err && err.message) || err);
      // A failed check is still a check that ran, and saying so is what
      // separates a feed that is down from an updater that stopped running.
      // A feed we cannot reach is an error, never "up to date": reporting it as
      // success is how a channel that has quietly 404'd looks healthy.
      setStatus({ type: 'error', channel, message, lastCheckedAt: stampCheck() });
      return { channel, status: 'error', message };
    }
  };
  return serialize(run);
}

/**
 * Queue the download behind the check that found it, rather than awaiting it.
 *
 * The caller asked what the check found, which is already known. Awaiting the
 * download would make every caller wait out the whole transfer with the
 * serialized chain held for the whole of it. It still runs as a serialized
 * task, so nothing else touches the updater singleton while it is in flight.
 *
 * The task re-checks before downloading because `downloadUpdate()` takes no
 * update: it serves whatever the last `checkForUpdates()` left on the shared
 * updater. `serialize()` keeps tasks from overlapping, but the singleton carries
 * that state ACROSS tasks, and `peekChannels()` -- which checks every channel --
 * can be queued between the check that found this update and this download.
 * Re-checking here is what makes the bytes belong to `channel`.
 */
function startDownload(channel) {
  // The task must not reject: `inFlight` would be a rejected promise with no
  // handler until the next serialize() call.
  void serialize(async () => {
    try {
      if (currentChannel() !== channel) {
        console.log(`[update] Dropping the ${channel} download: the channel changed`);
        return;
      }
      const outcome = await checkChannel(channel);
      // Applied here as well as in the check, because this is where the bytes
      // are actually fetched.
      if (channels.isAlreadyStaged(outcome, downloadedVersion)) {
        setStatus({ type: 'update-downloaded', version: downloadedVersion });
        return;
      }
      publishOutcome(channel, outcome);
      if (!outcome.isUpdateAvailable) {
        return;
      }
      await downloadAndOffer(outcome.feedVersion);
    } catch (err) {
      const message = String((err && err.message) || err);
      setStatus({ type: 'error', channel, message });
    }
  });
}

async function downloadAndOffer(version) {
  // MacUpdater reads `autoInstallOnAppQuit` exactly once, as the download
  // completes, to decide whether to hand the zip to Squirrel. It extends
  // AppUpdater rather than BaseUpdater, so there is no quit handler either:
  // arming this afterwards installs nothing.
  autoUpdater.autoInstallOnAppQuit = true;
  await autoUpdater.downloadUpdate();
  downloadedVersion = version;
  // The whole announcement: a status the window renders itself. Nothing
  // interrupts -- no dialog, and no OS notification either. The update installs
  // on the next restart whether or not it is ever acknowledged, so it does not
  // deserve to pull the user out of whatever they are doing.
  setStatus({ type: 'update-downloaded', version });
}

/** Restart into the downloaded update, from the renderer's "Restart" control. */
function installNow() {
  autoUpdater.quitAndInstall();
}

/**
 * Switch channels.
 *
 * Drops whatever the updater has cached first: every channel shares one
 * `updaterCacheDirName`, so the previous channel's artifact would otherwise sit
 * there for a later download to reuse.
 *
 * This does not un-stage a completed download. `downloadAndOffer` arms
 * `autoInstallOnAppQuit` before downloading, so Squirrel is handed the zip the
 * moment it lands and installs it on the next launch regardless -- switching
 * changes what the app asks for next, not what is already on its way in.
 */
async function setChannel(channel) {
  if (channels.normalizeChannel(channel) === null) {
    throw new Error(`Unknown update channel ${JSON.stringify(channel)}`);
  }
  channels.assertChannelIsAvailable(channel, readFeedBaseUrl());
  // Serialized with checks: a download from the previous channel may be in
  // flight, and clearing the cache out from under it would fail the install
  // rather than the switch.
  await serialize(async () => {
    autoUpdater.autoInstallOnAppQuit = false;
    await discardStagedUpdate();
    downloadedVersion = null;
    channels.writeChannel(paths.getDataDir(), channel);
  });
  return check();
}

/**
 * Empty the shared updater cache.
 *
 * Goes through `getOrCreateDownloadHelper()` rather than reading
 * `downloadedUpdateHelper` directly: that field stays null until this process
 * downloads something, so reading it would no-op on the case that matters --
 * an artifact cached by a previous run of the app, before the channel changed.
 *
 * There is nothing to discard where the updater cannot run: the helper reads
 * `updaterCacheDirName` out of the app-update.yml a dev build does not have, so
 * without this guard every switch in dev logs an ENOENT that means nothing is
 * wrong -- which is what would make the same message, for a real reason, easy
 * to miss.
 */
async function discardStagedUpdate() {
  if (!isUpdaterUsable()) {
    return;
  }
  try {
    const helper = await autoUpdater.getOrCreateDownloadHelper();
    await helper.clear();
  } catch (err) {
    console.error(`[update] Could not discard the staged update: ${err.message}`);
  }
}

/**
 * What each channel currently serves, and whether moving there would park.
 *
 * `wouldPark` is computed here rather than in the renderer so semver comparison
 * lives in exactly one place -- the renderer only renders the answer.
 */
async function peekChannels() {
  if (!isUpdaterUsable()) {
    return {};
  }
  return serialize(async () => {
    const feedBaseUrl = readFeedBaseUrl();
    const currentVersion = app.getVersion();
    const peeked = {};
    for (const channel of channels.availableChannels(feedBaseUrl)) {
      try {
        const { feedVersion, isUpdateAvailable } = await checkChannel(channel);
        peeked[channel] = {
          version: feedVersion,
          wouldPark:
            channels.computeUpdateStatus({ currentVersion, feedVersion, isUpdateAvailable }) === 'parked',
        };
      } catch (err) {
        // The panel renders every one of these the same way ("Unavailable right
        // now"), so without this the difference between a channel nobody has
        // promoted to and a manifest host that is down is lost here.
        const message = String((err && err.message) || err);
        console.error(`[update] Could not read what the ${channel} channel serves: ${message}`);
        peeked[channel] = { version: null, wouldPark: false, error: message };
      }
    }
    // Leave the updater pointed back at the channel the user is actually on.
    channels.applyFeedToUpdater(autoUpdater, resolveFeed(currentChannel()));
    return peeked;
  });
}

function describe() {
  const feedBaseUrl = readFeedBaseUrl();
  return {
    channel: currentChannel(),
    currentVersion: app.getVersion(),
    available: channels.availableChannels(feedBaseUrl),
    status: getStatus(),
    lastCheckedAt,
    // Whether an artifact is staged, separately from the transient status: a
    // failed check replaces `update-downloaded` with an error, and the zip is
    // with Squirrel by then, so a panel reading the status alone stops saying
    // the update still installs at exactly the moment it is being asked.
    downloadedVersion,
  };
}

function init({ onStatus } = {}) {
  statusListener = onStatus || null;
  if (!isUpdaterUsable()) {
    console.log('[update] Skipping auto-update (dev build -- not packaged)');
    setStatus({ type: 'disabled', reason: 'not-packaged' });
    return;
  }
  // Debug included: on macOS the handover to Squirrel is reported only at that
  // level, and it is the slowest step of an install by a wide margin. Without
  // it the log jumps from the download finishing to the app being asked to
  // quit, with the minutes in between unaccounted for.
  autoUpdater.logger = { info: console.log, warn: console.warn, error: console.error, debug: console.log };
  void check();
  const timer = setInterval(() => void check(), CHECK_INTERVAL_MS);
  timer.unref?.();
}

/** Report a check that rejected outright, which `runCheck`'s own catch cannot see. */
function reportRejectedCheck(err) {
  const message = String((err && err.message) || err);
  console.error(`[update] Check failed: ${message}`);
  setStatus({ type: 'error', message });
  return message;
}

module.exports = {
  check,
  describe,
  init,
  installNow,
  peekChannels,
  setChannel,
};
