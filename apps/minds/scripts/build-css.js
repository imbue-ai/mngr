#!/usr/bin/env node
/**
 * postinstall hook: compile static/app.css -> static/app.min.css so a fresh
 * dev checkout has styled chrome right after `pnpm install`.
 *
 * No-op when the Tailwind CLI is absent. ToDesktop's cloud builder installs
 * with `pnpm recursive install --prod`, which omits devDependencies
 * (tailwindcss / @tailwindcss/cli) but still runs lifecycle scripts; the
 * packaged app ships app.min.css inside the minds wheel (built by build.js),
 * so the cloud install never compiles CSS.
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const tailwindBin = path.join(
  ROOT,
  'node_modules',
  '.bin',
  process.platform === 'win32' ? 'tailwindcss.cmd' : 'tailwindcss'
);

if (!fs.existsSync(tailwindBin)) {
  console.log('[build-css] Tailwind CLI not installed (prod-only install); skipping CSS build.');
  process.exit(0);
}

console.log('[build-css] Compiling static/app.css -> static/app.min.css...');
execSync('pnpm run build:css', { cwd: ROOT, stdio: 'inherit' });
