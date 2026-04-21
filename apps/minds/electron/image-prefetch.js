/**
 * Background prefetch of the Lima VM base image.
 *
 * Writes the downloaded qcow2 directly into Lima's content-addressable cache
 * (`~/Library/Caches/lima/download/by-url-sha256/<sha256(url)>/data`) so
 * `limactl start` later finds it and skips its own download.
 *
 * When no digest is configured for the current architecture, the prefetch is
 * skipped -- stock Ubuntu cloud images move too often to pin, and Lima's own
 * download already handles them fine. The prefetch pays off only for the
 * published mngr-lima qcow2 (fat, digest-pinned).
 *
 * Keep the URL / digest constants in sync with
 * libs/mngr_lima/imbue/mngr_lima/constants.py.
 */

const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const https = require('https');

const DEFAULT_IMAGE_URL_ARM64 =
  'https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-arm64.img';
const DEFAULT_IMAGE_URL_X86_64 =
  'https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img';
// Flip these to the hex sha256 of the published qcow2 when available. While
// null, prefetch is a no-op (Lima handles the stock Ubuntu image itself).
const DEFAULT_IMAGE_SHA256_ARM64 = null;
const DEFAULT_IMAGE_SHA256_X86_64 = null;

const STATE_IDLE = 'idle';
const STATE_DOWNLOADING = 'downloading';
const STATE_READY = 'ready';
const STATE_ERROR = 'error';
const STATE_SKIPPED = 'skipped';

const MAX_REDIRECTS = 5;
const PROGRESS_EMIT_INTERVAL_MS = 500;

let currentState = {
  status: STATE_IDLE,
  percent: 0,
  bytesDownloaded: 0,
  totalBytes: 0,
  error: null,
};
const subscribers = new Set();

function getState() {
  return { ...currentState };
}

function setState(patch) {
  currentState = { ...currentState, ...patch };
  for (const cb of subscribers) {
    try {
      cb(getState());
    } catch (err) {
      console.error('[image-prefetch] subscriber error:', err);
    }
  }
}

function subscribe(cb) {
  subscribers.add(cb);
  return () => subscribers.delete(cb);
}

function getImageForArch() {
  if (process.arch === 'arm64') {
    return { url: DEFAULT_IMAGE_URL_ARM64, sha256: DEFAULT_IMAGE_SHA256_ARM64 };
  }
  return { url: DEFAULT_IMAGE_URL_X86_64, sha256: DEFAULT_IMAGE_SHA256_X86_64 };
}

function getLimaCacheDataPath(imageUrl) {
  const urlHash = crypto.createHash('sha256').update(imageUrl, 'utf-8').digest('hex');
  let cacheRoot;
  if (process.platform === 'darwin') {
    cacheRoot = path.join(os.homedir(), 'Library', 'Caches', 'lima');
  } else if (process.env.XDG_CACHE_HOME) {
    cacheRoot = path.join(process.env.XDG_CACHE_HOME, 'lima');
  } else {
    cacheRoot = path.join(os.homedir(), '.cache', 'lima');
  }
  return path.join(cacheRoot, 'download', 'by-url-sha256', urlHash, 'data');
}

function verifyFileSha256(filepath, expected) {
  return new Promise((resolve, reject) => {
    const hasher = crypto.createHash('sha256');
    const stream = fs.createReadStream(filepath);
    stream.on('data', (chunk) => hasher.update(chunk));
    stream.on('end', () => resolve(hasher.digest('hex') === expected));
    stream.on('error', reject);
  });
}

function streamDownloadWithProgress(url, destPath, expectedSha256, redirectsRemaining = MAX_REDIRECTS) {
  return new Promise((resolve, reject) => {
    https
      .get(url, { headers: { 'User-Agent': 'minds-image-prefetch' } }, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          res.resume();
          if (redirectsRemaining <= 0) {
            reject(new Error(`Too many redirects while fetching ${url}`));
            return;
          }
          streamDownloadWithProgress(res.headers.location, destPath, expectedSha256, redirectsRemaining - 1)
            .then(resolve)
            .catch(reject);
          return;
        }
        if (res.statusCode !== 200) {
          res.resume();
          reject(new Error(`HTTP ${res.statusCode} for ${url}`));
          return;
        }

        const totalBytes = parseInt(res.headers['content-length'] || '0', 10) || 0;
        let bytesDownloaded = 0;
        let lastEmit = 0;
        const hasher = crypto.createHash('sha256');
        const partPath = `${destPath}.part`;
        const out = fs.createWriteStream(partPath);

        res.on('data', (chunk) => {
          bytesDownloaded += chunk.length;
          hasher.update(chunk);
          const now = Date.now();
          if (now - lastEmit >= PROGRESS_EMIT_INTERVAL_MS) {
            lastEmit = now;
            const percent = totalBytes ? Math.floor((bytesDownloaded / totalBytes) * 100) : 0;
            setState({ status: STATE_DOWNLOADING, percent, bytesDownloaded, totalBytes, error: null });
          }
        });
        res.pipe(out);
        out.on('finish', () => {
          const actual = hasher.digest('hex');
          if (actual !== expectedSha256) {
            fs.rmSync(partPath, { force: true });
            reject(
              new Error(
                `SHA256 mismatch for ${url}: expected ${expectedSha256}, got ${actual}. Refusing to install.`,
              ),
            );
            return;
          }
          try {
            fs.renameSync(partPath, destPath);
            resolve();
          } catch (err) {
            reject(err);
          }
        });
        out.on('error', (err) => {
          fs.rmSync(partPath, { force: true });
          reject(err);
        });
      })
      .on('error', reject);
  });
}

async function start() {
  const { url, sha256: expectedSha256 } = getImageForArch();
  if (!expectedSha256) {
    setState({ status: STATE_SKIPPED, percent: 0, bytesDownloaded: 0, totalBytes: 0, error: null });
    return;
  }

  const cachePath = getLimaCacheDataPath(url);

  try {
    if (fs.existsSync(cachePath)) {
      const ok = await verifyFileSha256(cachePath, expectedSha256);
      if (ok) {
        setState({ status: STATE_READY, percent: 100, bytesDownloaded: 0, totalBytes: 0, error: null });
        return;
      }
      // Digest mismatch -- stale cache, force re-download.
      fs.rmSync(cachePath, { force: true });
    }

    fs.mkdirSync(path.dirname(cachePath), { recursive: true });
    setState({ status: STATE_DOWNLOADING, percent: 0, bytesDownloaded: 0, totalBytes: 0, error: null });
    await streamDownloadWithProgress(url, cachePath, expectedSha256);
    setState({ status: STATE_READY, percent: 100, bytesDownloaded: 0, totalBytes: 0, error: null });
  } catch (err) {
    console.error('[image-prefetch] failed:', err);
    setState({
      status: STATE_ERROR,
      percent: 0,
      bytesDownloaded: 0,
      totalBytes: 0,
      error: err.message || String(err),
    });
  }
}

module.exports = {
  start,
  getState,
  subscribe,
  // Exposed for tests.
  _getLimaCacheDataPath: getLimaCacheDataPath,
  _getImageForArch: getImageForArch,
};
