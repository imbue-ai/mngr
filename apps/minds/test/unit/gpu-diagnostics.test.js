// Unit tests for the GPU-process-death log line main.js emits.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { formatGpuFeatureStatus, formatGpuProcessGone } = require('../../electron/gpu-diagnostics');

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
    }),
    '[gpu] GPU process gone (reason=killed, exitCode=15, gpuDeathCount=1, isShuttingDown=true, ' +
      'terminationSignal=SIGTERM, uptime=120.0s, features=gpu_compositing=disabled_software)',
  );
});

test('a GPU death with no termination signal says "none"', () => {
  const line = formatGpuProcessGone({
    reason: 'crashed',
    exitCode: 139,
    gpuDeathCount: 3,
    isShuttingDown: false,
    terminationSignal: null,
    uptimeSeconds: 42,
    featureStatus: null,
  });
  assert.match(line, /reason=crashed, exitCode=139, gpuDeathCount=3, isShuttingDown=false, terminationSignal=none, /);
  assert.match(line, /features=unavailable\)$/);
});
