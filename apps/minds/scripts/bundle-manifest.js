/**
 * Integrity manifest for a staged bundle directory (the latchkey bundle).
 *
 * Maps each file's POSIX path relative to the bundle root to its SHA-256.
 * Mach-O files get a null hash, meaning presence only: macOS packaging
 * re-signs them after the build, which rewrites their bytes.
 *
 * ``imbue.minds.cli.latchkey_bundle_diagnostics`` reads it back; change the
 * format on both sides together.
 */

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { classifyExecutable } = require('./download-binaries.js');

const BUNDLE_MANIFEST_FILENAME = 'manifest.json';

function listFilesRelative(root, relativeDir, out) {
  for (const entry of fs.readdirSync(path.join(root, relativeDir), { withFileTypes: true })) {
    const relativePath = relativeDir ? `${relativeDir}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      listFilesRelative(root, relativePath, out);
    } else if (relativePath !== BUNDLE_MANIFEST_FILENAME) {
      out.push(relativePath);
    }
  }
  return out;
}

function isMachO(contents) {
  const { format } = classifyExecutable(contents);
  return format === 'macho' || format === 'macho-fat';
}

/**
 * Hash every file under ``bundleDir`` and write ``<bundleDir>/manifest.json``.
 *
 * Returns the number of files recorded.
 */
function writeBundleManifest(bundleDir) {
  const files = {};
  for (const relativePath of listFilesRelative(bundleDir, '', []).sort()) {
    const contents = fs.readFileSync(path.join(bundleDir, relativePath));
    files[relativePath] = isMachO(contents)
      ? null
      : crypto.createHash('sha256').update(contents).digest('hex');
  }
  fs.writeFileSync(
    path.join(bundleDir, BUNDLE_MANIFEST_FILENAME),
    JSON.stringify({ files }) + '\n'
  );
  return Object.keys(files).length;
}

module.exports = {
  BUNDLE_MANIFEST_FILENAME,
  writeBundleManifest,
};
