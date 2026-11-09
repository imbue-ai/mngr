// A supported platform/arch needs a pinned SHA256 for every release asset it
// downloads; otherwise verifyChecksum only throws when someone runs
// `pnpm start` on that platform. These tests enumerate the supported tuples
// against EXPECTED_SHA256 so a version bump that forgets one arch fails here.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const db = require('../../scripts/download-binaries.js');

function assetFilename(url) {
  return path.basename(new URL(url).pathname);
}

// BINARIES entries verified against EXPECTED_SHA256, each with the asset
// filename it looks up. git's dugite-native payloads are pinned by
// git-manifest.json (guarded by scripts/build_test.py) and uv-shims is
// generated, so neither is checked here; the one git asset that does live in
// EXPECTED_SHA256 (MinGit, win32 only) is not shipped and is left to
// verifyChecksum.
const PINNED_ASSET_FILENAME_BY_BINARY = {
  uv: (platformArch) => assetFilename(db.getUvDownloadUrl(platformArch)),
  restic: (platformArch) => assetFilename(db.getResticDownloadUrl(platformArch)),
  desync: (platformArch) => assetFilename(db.getDesyncDownloadUrl(platformArch)),
  lima: (platformArch) => assetFilename(db.getLimaDownloadUrl(platformArch)),
  curl: (platformArch) => db.getLatchkeyCurlDownloadInfo(platformArch).filename,
  unison: (platformArch) => assetFilename(db.getUnisonDownloadUrl(platformArch)),
  'unison-fsmonitor': (platformArch) => assetFilename(db.getUnisonFsmonitorDownloadUrl(platformArch)),
};
const BINARIES_PINNED_ELSEWHERE = new Set(['git', 'uv-shims']);

test('every BINARIES entry is either covered by the pin check or pinned elsewhere', () => {
  for (const name of Object.keys(db.BINARIES)) {
    assert.ok(
      name in PINNED_ASSET_FILENAME_BY_BINARY || BINARIES_PINNED_ELSEWHERE.has(name),
      `BINARIES.${name} is not covered by PINNED_ASSET_FILENAME_BY_BINARY; add it (or, if its ` +
        'pin lives outside EXPECTED_SHA256, list it in BINARIES_PINNED_ELSEWHERE)',
    );
  }
});

test('every supported platform/arch has a pinned SHA256 for each asset it downloads', () => {
  for (const [nodeTarget, platformArch] of Object.entries(db.PLATFORM_ARCH_BY_NODE_TARGET)) {
    for (const name of db.getProvisionedBinaries(platformArch)) {
      const filenameFor = PINNED_ASSET_FILENAME_BY_BINARY[name];
      if (filenameFor === undefined) continue;
      const filename = filenameFor(platformArch);
      assert.match(
        db.EXPECTED_SHA256[filename] ?? '',
        /^[0-9a-f]{64}$/,
        `${name} on ${nodeTarget} downloads ${filename}, which has no SHA256 pinned in EXPECTED_SHA256`,
      );
    }
  }
});

// The upload estimator must price exactly the files the ToDesktop CLI's
// fast-glob selection uploads, so it refuses any appFiles shape it has not
// been taught; these tests keep it in step with the real todesktop.js.
const fs = require('node:fs');
const os = require('node:os');

test('the estimator understands every appFiles pattern todesktop.js declares', () => {
  const { appFiles } = require('../../todesktop.js');
  assert.doesNotThrow(() => db.parseAppFilesExclusions(appFiles));
});

test('root-level file exclusions drop only the top-level entry, as fast-glob does', () => {
  const exclusions = db.parseAppFilesExclusions(['**', '!resources/**', '!.env', '!.env.*']);
  assert.equal(exclusions.isExcludedFile('.env'), true);
  assert.equal(exclusions.isExcludedFile('.env.production'), true);
  assert.equal(exclusions.isExcludedFile('electron/.env'), false);
  assert.equal(exclusions.isExcludedFile('.envrc'), false);
  assert.equal(exclusions.isExcludedFile('resources/darwin-arm64/payload/uv/uv'), true);
  assert.equal(exclusions.isExcludedDirectory('resources'), true);
  assert.equal(exclusions.isExcludedDirectory('electron'), false);
});

test('an appFiles pattern the estimator cannot price is refused', () => {
  assert.throws(() => db.parseAppFilesExclusions(['**', '!*.log']), /only understands/);
  assert.throws(() => db.parseAppFilesExclusions(['**', '!electron/*.js']), /only understands/);
});

test('the estimate leaves out the dotenv files at the app root and counts a nested one', () => {
  const appRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-estimate-'));
  try {
    fs.writeFileSync(path.join(appRoot, 'main.js'), 'a'.repeat(10));
    fs.writeFileSync(path.join(appRoot, '.env'), 'b'.repeat(100));
    fs.writeFileSync(path.join(appRoot, '.env.local'), 'c'.repeat(50));
    fs.mkdirSync(path.join(appRoot, 'electron'));
    fs.writeFileSync(path.join(appRoot, 'electron', '.env'), 'd'.repeat(7));
    const estimate = db.estimateToDesktopUploadBytes(appRoot, { appFiles: ['**', '!.env', '!.env.*'] });
    assert.equal(estimate.appFilesBytes, 17);
    assert.equal(estimate.extraBytes, 0);
  } finally {
    fs.rmSync(appRoot, { recursive: true, force: true });
  }
});
