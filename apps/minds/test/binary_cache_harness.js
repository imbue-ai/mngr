/**
 * Exercise download-binaries.js's shared cache under contention, with the
 * download stubbed out so no network is involved. Driven by build_test.py's
 * cache tests; prints one JSON object of observations on stdout.
 *
 * Usage: node test/binary_cache_harness.js <cacheDir> <concurrency>
 */

const fs = require('fs');
const path = require('path');

const db = require('../scripts/download-binaries.js');

const cacheDir = process.argv[2];
const concurrency = Number(process.argv[3]);
process.env.MINDS_BINARY_CACHE_DIR = cacheDir;

const platformArch = { platform: 'darwin', arch: 'aarch64' };
let version = 'v1';
let downloadCount = 0;

db.BINARIES.restic.version = () => version;
db.BINARIES.restic.download = (resourcesDir) => {
  downloadCount += 1;
  // Widen the window between the "is it cached?" check and the rename so
  // concurrent callers genuinely overlap rather than serializing by luck.
  return new Promise((resolve) => setTimeout(resolve, 100)).then(() => {
    const dir = path.join(resourcesDir, 'restic');
    // A payload with a subdirectory beside the completion marker, like the real
    // ones. Pruning just the marker then leaves the entry non-empty, which is
    // what makes renaming onto it fail.
    fs.mkdirSync(path.join(dir, 'share'), { recursive: true });
    fs.writeFileSync(path.join(dir, 'share', 'notice.txt'), version);
    fs.writeFileSync(path.join(dir, 'restic'), version);
  });
};

const racers = Array.from({ length: concurrency }, () => db.ensureCachedBinary('restic', platformArch));

Promise.all(racers)
  .then((entries) => {
    const raceDownloadCount = downloadCount;
    const distinctEntries = [...new Set(entries)];

    // A dependency bump: same binary, new pinned version, nothing has it yet.
    version = 'v2';
    downloadCount = 0;
    return db.ensureCachedBinary('restic', platformArch).then((bumped) => {
      const bumpDownloadCount = downloadCount;

      // What a cache cleaner leaves behind: the files inside an entry are gone
      // but its directories remain, so the entry exists with no completion
      // marker.
      fs.rmSync(path.join(bumped, 'restic'));
      downloadCount = 0;
      return db.ensureCachedBinary('restic', platformArch).then((repaired) => {
        console.log(
          JSON.stringify({
            raceDownloadCount,
            distinctEntriesFromRace: distinctEntries.length,
            racedContent: fs.readFileSync(path.join(distinctEntries[0], 'restic'), 'utf-8'),
            bumpDownloadCount,
            bumpedContent: fs.readFileSync(path.join(bumped, 'restic'), 'utf-8'),
            oldVersionStillIntact: fs.existsSync(path.join(distinctEntries[0], 'restic')),
            repairDownloadCount: downloadCount,
            repairedContent: fs.readFileSync(path.join(repaired, 'restic'), 'utf-8'),
            stagingLeftBehind: fs
              .readdirSync(path.join(cacheDir, 'restic'))
              .filter((name) => name.startsWith('.staging-')).length,
          }),
        );
      });
    });
  })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
