// Release-channel preference and feed resolution.
//
// Deliberately free of any `electron` import (paths.js pulls in `app`) so the
// channel rules are unit-testable under plain node -- the caller passes the
// data directory in. See test/unit/update-channel.test.js.

const fs = require('fs');
const path = require('path');
const semver = require('semver');

// Slowest to fastest. Index order is the ordering: a later entry always serves
// a version greater than or equal to an earlier one.
const CHANNELS = ['stable', 'beta', 'alpha'];
const DEFAULT_CHANNEL = 'stable';

const PREFERENCE_FILENAME = 'update-channel.json';

// The fallback feed, for a build naming no manifest host: ToDesktop's own
// generic feed, under the channel name electron-builder stamps into every build.
const TODESKTOP_FEED_BASE = 'https://download.todesktop.com';
const TODESKTOP_CHANNEL = 'latest';

function preferencePath(dataDir) {
  return path.join(dataDir, PREFERENCE_FILENAME);
}

/**
 * Resolve a stored value to a known channel.
 *
 * Returns null for anything unrecognized rather than passing the string
 * through: it reaches a feed URL, and an unknown channel silently resolves to
 * a 404 feed that reports as an update-check error forever.
 */
function normalizeChannel(raw) {
  return typeof raw === 'string' && CHANNELS.includes(raw) ? raw : null;
}

/**
 * Read the stored channel, plus why the default was used when it was.
 *
 * `reason` is null on a clean read. Callers log it -- a preference that silently
 * resets to stable is how an alpha tester quietly stops being one.
 */
function readChannel(dataDir) {
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(preferencePath(dataDir), 'utf8'));
  } catch (err) {
    const reason = err.code === 'ENOENT' ? null : `unreadable (${err.message})`;
    return { channel: DEFAULT_CHANNEL, reason };
  }
  const channel = normalizeChannel(raw && raw.channel);
  if (channel === null) {
    return { channel: DEFAULT_CHANNEL, reason: `unrecognized value ${JSON.stringify(raw && raw.channel)}` };
  }
  return { channel, reason: null };
}

function writeChannel(dataDir, channel) {
  if (normalizeChannel(channel) === null) {
    throw new Error(`Refusing to store unknown update channel ${JSON.stringify(channel)}`);
  }
  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(preferencePath(dataDir), JSON.stringify({ channel }, null, 2) + '\n');
}

/**
 * The electron-updater feed for a channel.
 *
 * Every channel is served from `feedBaseUrl` (ClientEnvConfig
 * `update_feed_base_url`), including stable, so one manifest format and one
 * promotion mechanism cover all of them.
 *
 * ToDesktop's own feed is the fallback for a build that configures no host.
 * Production always sets one, so this is a guard rather than a path anything
 * takes today. It is NOT how the installs already in the field keep working --
 * those predate this file entirely and update through `@todesktop/runtime`, so
 * reaching them at all takes one more Release in ToDesktop.
 */
function feedForChannel(channel, { appId, feedBaseUrl }) {
  if (normalizeChannel(channel) === null) {
    throw new Error(`Unknown update channel ${JSON.stringify(channel)}`);
  }
  if (feedBaseUrl) {
    return { url: feedBaseUrl, channel };
  }
  if (channel !== DEFAULT_CHANNEL) {
    throw new Error(
      `The ${channel} channel needs update_feed_base_url in the tier's client.toml; this build has none configured.`
    );
  }
  return { url: `${TODESKTOP_FEED_BASE}/${appId}`, channel: TODESKTOP_CHANNEL };
}

/**
 * Point an electron-updater instance at a feed.
 *
 * ORDER IS LOAD-BEARING. electron-updater's `channel` setter ends with
 * `this.allowDowngrade = true` (AppUpdater.js), so assigning the channel after
 * `allowDowngrade` silently re-enables downgrades. Verified against the real
 * updater: with the two assignments swapped, a feed serving an older version
 * than the running build hands back a cancellation token -- an offered
 * downgrade -- instead of parking. Nothing in ~/.minds has a down-migration,
 * so that is data loss rather than a degraded experience.
 */
function applyFeedToUpdater(updater, feed) {
  updater.channel = feed.channel;
  updater.allowDowngrade = false;
  updater.autoDownload = false;
  updater.autoInstallOnAppQuit = false;
  updater.setFeedURL({ provider: 'generic', url: feed.url });
}

/**
 * Forget the check electron-updater is holding, so the next one starts a request.
 *
 * `checkForUpdates()` memoizes its promise and clears it only from that promise's
 * own settle handlers (AppUpdater.js), so a check abandoned on a deadline -- which
 * never settles -- is handed back to every later caller. Unbounded, that makes one
 * stalled request the end of checking, peeking and channel switching until the app
 * restarts.
 */
function discardInFlightCheck(updater) {
  updater.checkForUpdatesPromise = null;
}

/** Every channel this build can serve, slowest first. */
function availableChannels(feedBaseUrl) {
  return feedBaseUrl ? [...CHANNELS] : [DEFAULT_CHANNEL];
}

/**
 * Refuse a channel this build cannot serve.
 *
 * Being a known channel name is not the same as being one this build can
 * reach: the preference resolves to a feed URL, so storing `alpha` where no
 * manifest host is configured makes every check from then on throw, and the
 * Settings panel lists only the channels the build offers -- so the stored one
 * has no radio to click back off. Checked before the preference is written.
 */
function assertChannelIsAvailable(channel, feedBaseUrl) {
  const available = availableChannels(feedBaseUrl);
  if (!available.includes(channel)) {
    throw new Error(
      `This build cannot serve the ${channel} channel, only ${available.join(', ')}: ` +
        "its tier's client.toml configures no update_feed_base_url."
    );
  }
}

/**
 * Classify a completed check.
 *
 * `isUpdateAvailable` must come from electron-updater's own verdict of the same
 * name, never from `updateInfo` being truthy: updateInfo is the parsed manifest
 * and is populated on every successful check, including when the feed is older
 * than the running build.
 */
function computeUpdateStatus({ currentVersion, feedVersion, isUpdateAvailable }) {
  if (isUpdateAvailable) {
    return 'update-available';
  }
  if (feedVersion && semver.valid(feedVersion) && semver.valid(currentVersion) && semver.lt(feedVersion, currentVersion)) {
    return 'parked';
  }
  return 'up-to-date';
}

/**
 * Whether a check found the very update already staged for the next restart.
 *
 * An update stays "available" until it is installed, so every check after a
 * download reports the staged version as available again. Without this, the
 * interval re-downloads what is already waiting, and two checks that queue
 * together each append a download of the same artifact.
 */
function isAlreadyStaged({ isUpdateAvailable, feedVersion }, downloadedVersion) {
  return Boolean(isUpdateAvailable) && downloadedVersion != null && feedVersion === downloadedVersion;
}

module.exports = {
  isAlreadyStaged,
  normalizeChannel,
  readChannel,
  writeChannel,
  feedForChannel,
  applyFeedToUpdater,
  discardInFlightCheck,
  availableChannels,
  assertChannelIsAvailable,
  computeUpdateStatus,
  preferencePath,
};
