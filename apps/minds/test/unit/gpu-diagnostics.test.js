// Unit tests for the GPU-process-death log line main.js emits.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { formatGpuFeatureStatus, formatGpuProcessGone, formatMegabytes } = require('../../electron/gpu-diagnostics');

test('feature status renders sorted feature=status pairs', () => {
  assert.equal(
    formatGpuFeatureStatus({ webgl: 'unavailable_off', gpu_compositing: 'disabled_software' }),
    'gpu_compositing=disabled_software,webgl=unavailable_off',
  );
});

test('feature status is "unavailable" when absent or empty', () => {
  assert.equal(formatGpuFeatureStatus(null), 'unavailable');
  assert.equal(formatGpuFeatureStatus(undefined), 'unavailable');
  assert.equal(formatGpuFeatureStatus({}), 'unavailable');
});

test('a GPU death after a termination signal records the signal and shutdown state', () => {
  assert.equal(
    formatGpuProcessGone({
      reason: 'killed',
      exitCode: 15,
      gpuDeathCount: 1,
      isShuttingDown: true,
      terminationSignal: 'SIGTERM',
      uptimeSeconds: 120.04,
      featureStatus: { gpu_compositing: 'disabled_software' },
      freeMemoryBytes: 1024 * 1024 * 1024,
      appMemoryBytes: 374.6 * 1024 * 1024,
    }),
    '[gpu] GPU process gone (reason=killed, exitCode=15, gpuDeathCount=1, isShuttingDown=true, ' +
      'terminationSignal=SIGTERM, uptime=120.0s, freeMemory=1024MB, appMemory=375MB, ' +
      'features=gpu_compositing=disabled_software)',
  );
});

test('a GPU death with no signal, feature status or app metrics says "none" and "unavailable"', () => {
  const line = formatGpuProcessGone({
    reason: 'crashed',
    exitCode: 139,
    gpuDeathCount: 3,
    isShuttingDown: false,
    terminationSignal: null,
    uptimeSeconds: 42,
    featureStatus: null,
    freeMemoryBytes: 0,
    appMemoryBytes: null,
  });
  assert.match(line, /reason=crashed, exitCode=139, gpuDeathCount=3, isShuttingDown=false, terminationSignal=none, /);
  assert.match(line, /, freeMemory=0MB, appMemory=unavailable, /);
  assert.match(line, /features=unavailable\)$/);
});

test('megabytes round to whole numbers and missing values are "unavailable"', () => {
  assert.equal(formatMegabytes(1.4 * 1024 * 1024), '1MB');
  assert.equal(formatMegabytes(1.6 * 1024 * 1024), '2MB');
  assert.equal(formatMegabytes(null), 'unavailable');
  assert.equal(formatMegabytes(undefined), 'unavailable');
});
