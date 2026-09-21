// Unit tests for the executable-format classifier that build.js runs over
// every staged binary set.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// ELF_X86_64 and MACHO_ARM64 below are the leading bytes of real payloads (uv's
// linux-x64 and darwin-arm64 builds), so the classifier is held to what the
// downloaders actually produce; the other headers vary one field of those two.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {
  BINARIES,
  assertStagedExecutablesMatchTarget,
  classifyExecutable,
  getProvisionedBinaries,
  isExecutableForTarget,
} = require('../../scripts/download-binaries');

const LINUX_X64 = { platform: 'linux', arch: 'x86_64' };
const DARWIN_ARM64 = { platform: 'darwin', arch: 'aarch64' };

// ELF64, little-endian, e_machine 0x3e (x86-64) at offset 18.
const ELF_X86_64 = Buffer.from(
  '7f454c4602010100000000000000000003003e0001000000',
  'hex',
);
// The same header with e_machine 0xb7 (aarch64).
const ELF_AARCH64 = Buffer.from(
  '7f454c460201010000000000000000000300b70001000000',
  'hex',
);
// Mach-O 64 little-endian magic, cputype 0x0100000c (arm64).
const MACHO_ARM64 = Buffer.from('cffaedfe0c000001', 'hex');
// Mach-O 64 little-endian magic, cputype 0x01000007 (x86_64).
const MACHO_X86_64 = Buffer.from('cffaedfe07000001', 'hex');
// A universal (fat) Mach-O header.
const MACHO_FAT = Buffer.from('cafebabe00000002', 'hex');
const SHELL_SCRIPT = Buffer.from('#!/bin/sh\nexec true\n');

test('classifies the four real header shapes and scripts', () => {
  assert.deepEqual(classifyExecutable(ELF_X86_64), { format: 'elf', arch: 'x86_64' });
  assert.deepEqual(classifyExecutable(ELF_AARCH64), { format: 'elf', arch: 'aarch64' });
  assert.deepEqual(classifyExecutable(MACHO_ARM64), { format: 'macho', arch: 'aarch64' });
  assert.deepEqual(classifyExecutable(MACHO_X86_64), { format: 'macho', arch: 'x86_64' });
  assert.deepEqual(classifyExecutable(MACHO_FAT), { format: 'macho-fat', arch: null });
  assert.deepEqual(classifyExecutable(SHELL_SCRIPT), { format: 'script', arch: null });
});

test('anything else is unknown rather than a guess', () => {
  assert.deepEqual(classifyExecutable(Buffer.alloc(0)), { format: 'unknown', arch: null });
  assert.deepEqual(classifyExecutable(Buffer.from('PK\x03\x04junkjunk')), { format: 'unknown', arch: null });
  // A truncated ELF header carries no machine field to read.
  assert.deepEqual(classifyExecutable(ELF_X86_64.subarray(0, 8)), { format: 'unknown', arch: null });
});

test('an arm64 Mach-O in the Linux set is exactly the shipped bug', () => {
  assert.equal(isExecutableForTarget(classifyExecutable(MACHO_ARM64), LINUX_X64), false);
  assert.equal(isExecutableForTarget(classifyExecutable(ELF_X86_64), LINUX_X64), true);
  assert.equal(isExecutableForTarget(classifyExecutable(ELF_AARCH64), LINUX_X64), false);
});

test('macOS accepts its own arch and fat binaries, never ELF', () => {
  assert.equal(isExecutableForTarget(classifyExecutable(MACHO_ARM64), DARWIN_ARM64), true);
  assert.equal(isExecutableForTarget(classifyExecutable(MACHO_X86_64), DARWIN_ARM64), false);
  assert.equal(isExecutableForTarget(classifyExecutable(MACHO_FAT), DARWIN_ARM64), true);
  assert.equal(isExecutableForTarget(classifyExecutable(ELF_X86_64), DARWIN_ARM64), false);
});

test('scripts pass on every target, since the interpreter is the platform', () => {
  assert.equal(isExecutableForTarget(classifyExecutable(SHELL_SCRIPT), LINUX_X64), true);
  assert.equal(isExecutableForTarget(classifyExecutable(SHELL_SCRIPT), DARWIN_ARM64), true);
});

// The git payload's remote helper, the one executable beyond the table's
// requiredPaths that the guard reads.
const GIT_REMOTE_HELPER = path.join('git', 'libexec', 'git-core', 'git-remote-http');

/** Lay out every executable the guard reads for `target`, each holding `header`. */
function stageResources(target, header) {
  const resourcesDir = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-staged-executables-'));
  const relativePaths = getProvisionedBinaries(target).map((name) => path.join(name, BINARIES[name].requiredPath));
  for (const relative of [...relativePaths, GIT_REMOTE_HELPER]) {
    const absolute = path.join(resourcesDir, relative);
    fs.mkdirSync(path.dirname(absolute), { recursive: true });
    fs.writeFileSync(absolute, header);
  }
  return resourcesDir;
}

test('a staged tree whose executables all match the target passes', (t) => {
  const resourcesDir = stageResources(LINUX_X64, ELF_X86_64);
  t.after(() => fs.rmSync(resourcesDir, { recursive: true, force: true }));
  assert.doesNotThrow(() => assertStagedExecutablesMatchTarget(resourcesDir, LINUX_X64));
});

test('one wrong-platform executable fails the whole target and is named', (t) => {
  const resourcesDir = stageResources(LINUX_X64, ELF_X86_64);
  t.after(() => fs.rmSync(resourcesDir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(resourcesDir, GIT_REMOTE_HELPER), MACHO_ARM64);
  assert.throws(
    () => assertStagedExecutablesMatchTarget(resourcesDir, LINUX_X64),
    (error) =>
      error.message.includes('do not match linux/x86_64') &&
      error.message.includes(`${GIT_REMOTE_HELPER}: macho/aarch64`),
  );
});
