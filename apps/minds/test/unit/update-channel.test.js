// Unit tests for release-channel preference, feed resolution, and the
// updater-configuration ordering rule.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// electron/update-channel.js deliberately imports no `electron`, so all of this
// is verifiable without launching Electron. The facts it encodes about
// electron-updater 6.8.9 were established by driving the real updater against a
// local fixture feed; the mock below reproduces the one behavior that matters.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const channels = require('../../electron/update-channel');

const APP_ID = '26032588hqdzk';
const FEED_BASE = 'https://releases.example.com/';

function tempDataDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'minds-channel-'));
}

test('normalizeChannel accepts only the known channels', () => {
  assert.equal(channels.normalizeChannel('stable'), 'stable');
  assert.equal(channels.normalizeChannel('beta'), 'beta');
  assert.equal(channels.normalizeChannel('alpha'), 'alpha');
  assert.equal(channels.normalizeChannel('latest'), null);
  assert.equal(channels.normalizeChannel('Alpha'), null);
  assert.equal(channels.normalizeChannel(''), null);
  assert.equal(channels.normalizeChannel(undefined), null);
  assert.equal(channels.normalizeChannel({ channel: 'alpha' }), null);
});

// A tier that configures a feed serves every channel; one that does not serves
// stable alone, which is what readChannel resolves a stored preference against.
const FEED = 'https://updates.example.com';

test('an absent preference means stable, with nothing to report', () => {
  const { channel, reason } = channels.readChannel(tempDataDir());
  assert.equal(channel, 'stable');
  assert.equal(reason, null);
});

test('a stored channel round-trips', () => {
  const dir = tempDataDir();
  channels.writeChannel(dir, 'alpha');
  assert.deepEqual(channels.readChannel(dir, FEED), { channel: 'alpha', reason: null });
});

test('writeChannel creates the data dir when the app has not yet', () => {
  const dir = path.join(tempDataDir(), 'not-created-yet');
  channels.writeChannel(dir, 'beta');
  assert.equal(channels.readChannel(dir, FEED).channel, 'beta');
});

test('an unrecognized stored value falls back to stable AND says why', () => {
  const dir = tempDataDir();
  fs.writeFileSync(channels.preferencePath(dir), JSON.stringify({ channel: 'nightly' }));
  const { channel, reason } = channels.readChannel(dir);
  assert.equal(channel, 'stable');
  // The reason is what turns a silent re-cadencing into something a log shows.
  assert.match(reason, /nightly/);
});

test('a stored channel this build cannot serve falls back AND says why', () => {
  // A tier that configures no update_feed_base_url serves stable alone. Resolved
  // here rather than at feedForChannel, which raises by name and would take
  // every check, peek and switch down with it.
  const dir = tempDataDir();
  channels.writeChannel(dir, 'alpha');
  const { channel, reason } = channels.readChannel(dir, null);
  assert.equal(channel, 'stable');
  assert.match(reason, /alpha/);
});

test('a stored channel the build does serve reads back clean', () => {
  const dir = tempDataDir();
  channels.writeChannel(dir, 'beta');
  assert.deepEqual(channels.readChannel(dir, FEED), { channel: 'beta', reason: null });
});

test('a corrupt preference file falls back to stable AND says why', () => {
  const dir = tempDataDir();
  fs.writeFileSync(channels.preferencePath(dir), '{ not json');
  const { channel, reason } = channels.readChannel(dir);
  assert.equal(channel, 'stable');
  assert.match(reason, /unreadable/);
});

test('writeChannel refuses to store an unknown channel', () => {
  assert.throws(() => channels.writeChannel(tempDataDir(), 'nightly'), /Refusing to store/);
});

test('stable is served from our own feed, like every other channel', () => {
  const feed = channels.feedForChannel('stable', { appId: APP_ID, feedBaseUrl: FEED_BASE });
  assert.deepEqual(feed, { url: FEED_BASE, channel: 'stable' });
});

test("a build predating channels falls back to ToDesktop's feed for stable", () => {
  // Every install already in the field. It configures no host, so it keeps
  // updating from whatever is Released in ToDesktop until it takes a build that
  // names one -- which is the entire migration onto our feed.
  const feed = channels.feedForChannel('stable', { appId: APP_ID, feedBaseUrl: null });
  assert.deepEqual(feed, { url: `https://download.todesktop.com/${APP_ID}`, channel: 'latest' });
});

test('alpha and beta resolve to the self-hosted feed under their own names', () => {
  for (const name of ['alpha', 'beta']) {
    assert.deepEqual(channels.feedForChannel(name, { appId: APP_ID, feedBaseUrl: FEED_BASE }), {
      url: FEED_BASE,
      channel: name,
    });
  }
});

test('a fast channel without a configured feed fails loudly rather than falling back', () => {
  assert.throws(
    () => channels.feedForChannel('alpha', { appId: APP_ID, feedBaseUrl: null }),
    /update_feed_base_url/
  );
});

test('a build with no feed configured offers stable only', () => {
  assert.deepEqual(channels.availableChannels(null), ['stable']);
  assert.deepEqual(channels.availableChannels(FEED_BASE), ['stable', 'beta', 'alpha']);
});

test('a channel this build cannot serve is refused, and stable never is', () => {
  // Storing it would be the durable failure: every later check throws on the
  // missing feed, and the panel lists only what the build offers, so the
  // stored channel has no radio to click back off.
  for (const name of ['alpha', 'beta']) {
    assert.throws(() => channels.assertChannelIsAvailable(name, null), /cannot serve the /);
    channels.assertChannelIsAvailable(name, FEED_BASE);
  }
  channels.assertChannelIsAvailable('stable', null);
});

// The claim under test: electron-updater's `channel` setter ends with
// `this.allowDowngrade = true`, so the assignments must happen in that order.
// This mock reproduces exactly that, so a reordering of applyFeedToUpdater
// fails here instead of shipping a silent downgrade.
function fakeUpdater() {
  return {
    _channel: null,
    allowDowngrade: null,
    autoDownload: null,
    autoInstallOnAppQuit: null,
    feedUrl: null,
    get channel() {
      return this._channel;
    },
    // The body of electron-updater's own setter, in AppUpdater's `set channel`.
    set channel(value) {
      this._channel = value;
      this.allowDowngrade = true;
    },
    setFeedURL(options) {
      this.feedUrl = options.url;
    },
  };
}

test('applyFeedToUpdater leaves allowDowngrade false despite the channel setter forcing it true', () => {
  const updater = fakeUpdater();
  channels.applyFeedToUpdater(updater, { url: FEED_BASE, channel: 'alpha' });
  assert.equal(updater.channel, 'alpha');
  assert.equal(updater.feedUrl, FEED_BASE);
  // If someone assigns allowDowngrade before channel, this is true and users
  // switching to a slower channel get downgraded into a data-loss migration.
  assert.equal(updater.allowDowngrade, false);
  assert.equal(updater.autoDownload, false);
  assert.equal(updater.autoInstallOnAppQuit, false);
});

test('the mock itself proves the setter hazard is real', () => {
  const updater = fakeUpdater();
  updater.allowDowngrade = false;
  updater.channel = 'latest';
  assert.equal(updater.allowDowngrade, true);
});

// The claim under test: electron-updater memoizes the in-flight check and clears
// it only when that check settles, so a check abandoned on a deadline is handed
// to every later caller. This mock reproduces exactly that.
function fakeDedupingUpdater() {
  return {
    checkForUpdatesPromise: null,
    startedChecks: 0,
    // The shape of electron-updater's own AppUpdater.checkForUpdates.
    checkForUpdates() {
      if (this.checkForUpdatesPromise !== null) {
        return this.checkForUpdatesPromise;
      }
      this.startedChecks += 1;
      // A stalled request under Electron: pending forever, so the settle
      // handlers that would clear this field never run.
      this.checkForUpdatesPromise = new Promise(() => {});
      return this.checkForUpdatesPromise;
    },
  };
}

test('discardInFlightCheck lets the next check start a fresh request', () => {
  const updater = fakeDedupingUpdater();
  updater.checkForUpdates();

  channels.discardInFlightCheck(updater);
  updater.checkForUpdates();

  assert.equal(updater.startedChecks, 2);
});

test('the mock itself proves a stalled check is otherwise handed to every later one', () => {
  const updater = fakeDedupingUpdater();
  updater.checkForUpdates();
  updater.checkForUpdates();
  // Without the discard this is what every check after a timeout gets: the same
  // promise that already failed to settle, racing a fresh deadline.
  assert.equal(updater.startedChecks, 1);
});

test('the field discardInFlightCheck clears still exists in electron-updater', () => {
  // The mocks above prove our half of the contract and would keep passing if
  // electron-updater renamed the field, silently restoring the wedge a stalled
  // check causes. There is no public way to forget a check, so this reads the
  // installed library and fails on a rename instead. `require.resolve` does not
  // execute the module, so this needs no Electron.
  const entry = require.resolve('electron-updater');
  const appUpdater = path.join(path.dirname(entry), 'AppUpdater.js');
  assert.match(fs.readFileSync(appUpdater, 'utf8'), /checkForUpdatesPromise/);
});

test('an update is only "available" when electron-updater says so, not when updateInfo exists', () => {
  // Every successful check returns a parsed manifest, including this one.
  assert.equal(
    channels.computeUpdateStatus({ currentVersion: '0.4.30', feedVersion: '0.4.12', isUpdateAvailable: false }),
    'parked'
  );
  assert.equal(
    channels.computeUpdateStatus({ currentVersion: '0.4.12', feedVersion: '0.4.30', isUpdateAvailable: true }),
    'update-available'
  );
  assert.equal(
    channels.computeUpdateStatus({ currentVersion: '0.4.30', feedVersion: '0.4.30', isUpdateAvailable: false }),
    'up-to-date'
  );
});

test('a feed version that is not valid semver never reads as parked', () => {
  assert.equal(
    channels.computeUpdateStatus({ currentVersion: '0.4.30', feedVersion: 'not-a-version', isUpdateAvailable: false }),
    'up-to-date'
  );
  assert.equal(
    channels.computeUpdateStatus({ currentVersion: '0.4.30', feedVersion: null, isUpdateAvailable: false }),
    'up-to-date'
  );
});

// The switch confirmation asks for a channel only when moving there would park
// the user, from the per-channel `wouldPark` flags main computes with
// computeUpdateStatus. These two cases are that decision, at its source.
test('a faster channel that has caught up does not park, so it can be offered', () => {
  const running = '0.4.30';
  const parkedOn = (feedVersion) =>
    channels.computeUpdateStatus({ currentVersion: running, feedVersion, isUpdateAvailable: false }) === 'parked';
  assert.equal(parkedOn('0.4.12'), true, 'stable is behind');
  assert.equal(parkedOn('0.4.30'), false, 'level with us');
  // A channel ahead of us reports an update rather than parking; electron-updater
  // supplies isUpdateAvailable in that case.
  assert.equal(
    channels.computeUpdateStatus({ currentVersion: running, feedVersion: '0.4.31', isUpdateAvailable: true }),
    'update-available'
  );
});

test('after a rollback EVERY channel parks, so no switch can rescue the user', () => {
  // The user took a build that was then withdrawn from every channel. Nothing
  // in the panel offers a way out, because there is none to offer: the version
  // printed beside each channel is the whole of what can honestly be said.
  const running = '0.4.30';
  for (const feedVersion of ['0.4.12', '0.4.20', '0.4.29']) {
    assert.equal(
      channels.computeUpdateStatus({ currentVersion: running, feedVersion, isUpdateAvailable: false }),
      'parked'
    );
  }
});

test('a check that rediscovers the staged update is not a second download', () => {
  // An update stays "available" until it is installed, so every check after a
  // download reports the staged version again. Two checks that queue together
  // each append a download, and the second still sees it available once the
  // first has finished -- which fetched the same artifact twice.
  assert.equal(channels.isAlreadyStaged({ isUpdateAvailable: true, feedVersion: '0.5.0' }, '0.5.0'), true);
});

test('a newer version than the staged one is still a download', () => {
  assert.equal(channels.isAlreadyStaged({ isUpdateAvailable: true, feedVersion: '0.5.1' }, '0.5.0'), false);
});

test('nothing staged is never already staged, even when the feed version is unknown', () => {
  // `checkChannel` reports feedVersion as null when the manifest carried no
  // version. With nothing downloaded, comparing the two directly makes
  // null === null true and suppresses the first download of the session --
  // an update that is available and never fetched, with the panel reporting it
  // as already downloaded.
  assert.equal(channels.isAlreadyStaged({ isUpdateAvailable: true, feedVersion: null }, null), false);
  assert.equal(channels.isAlreadyStaged({ isUpdateAvailable: true, feedVersion: '0.5.0' }, null), false);
});

test('a newer feed version the updater will not offer reads as outside the rollout', () => {
  // `stagingPercentage` keeps this install out of the bucket, so
  // electron-updater answers isUpdateAvailable false while the manifest plainly
  // carries a newer build.
  assert.equal(
    channels.isOutsideRollout({ currentVersion: '0.4.2', feedVersion: '0.5.0', isUpdateAvailable: false }),
    true,
  );
});

test('being offered the update, or level with it, is not outside the rollout', () => {
  assert.equal(
    channels.isOutsideRollout({ currentVersion: '0.4.2', feedVersion: '0.5.0', isUpdateAvailable: true }),
    false,
  );
  assert.equal(
    channels.isOutsideRollout({ currentVersion: '0.5.0', feedVersion: '0.5.0', isUpdateAvailable: false }),
    false,
  );
});

test('running ahead of the channel is parked, never outside the rollout', () => {
  // Both states answer isUpdateAvailable false. Only the version comparison
  // separates "there is something newer you are not being offered" from
  // "you are past what this channel serves".
  assert.equal(
    channels.isOutsideRollout({ currentVersion: '0.5.0', feedVersion: '0.4.2', isUpdateAvailable: false }),
    false,
  );
});

test('an unreadable or unparseable feed version is not outside the rollout', () => {
  for (const feedVersion of [null, undefined, '', 'latest']) {
    assert.equal(
      channels.isOutsideRollout({ currentVersion: '0.4.2', feedVersion, isUpdateAvailable: false }),
      false,
    );
  }
});
