'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { askingShellHostname, createShellClientIds, legacyShellOrigins, shellHostnameOf } = require('../../electron/shell-client-ids');

const GEEBSPACE_SHELL = 'system_interface-7ey91715.agent-9a24f990fb7e46f281afe66088144c60.localhost';
const OTHER_SHELL = 'system-interface-29livw2l.agent-4acb3a0042814e759cc6ccaed2d332e2.localhost';

function tempDataDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'shell-client-ids-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function open(dataDir, minted = []) {
  const warnings = [];
  let next = 0;
  const store = createShellClientIds(dataDir, {
    mintClientId: () => minted[next++],
    warn: (message) => warnings.push(message),
  });
  return { store, warnings };
}

test('the first frame to ask fixes the id, and every later frame of that shell runs as it, across launches', (t) => {
  const dataDir = tempDataDir(t);
  const { store } = open(dataDir);
  // A restored window's frame in the device partition, then a popout's in the account partition.
  assert.equal(store.clientIdFor(GEEBSPACE_SHELL, 'id-in-device-partition'), 'id-in-device-partition');
  assert.equal(store.clientIdFor(GEEBSPACE_SHELL, 'id-in-account-partition'), 'id-in-device-partition');
  assert.equal(store.clientIdFor(GEEBSPACE_SHELL, null), 'id-in-device-partition');

  const relaunched = open(dataDir).store;
  assert.equal(relaunched.clientIdFor(GEEBSPACE_SHELL, 'id-in-account-partition'), 'id-in-device-partition');
});

test('a shell that never had an id, or reports a malformed one, gets a fresh one', (t) => {
  const { store } = open(tempDataDir(t), ['minted-1', 'minted-2']);
  assert.equal(store.clientIdFor(GEEBSPACE_SHELL, null), 'minted-1');
  assert.equal(store.clientIdFor(OTHER_SHELL, '<script>'), 'minted-2');
  assert.equal(store.clientIdFor(OTHER_SHELL, null), 'minted-2');
});

test('any id the client id contract allows is kept, not replaced', (t) => {
  const { store } = open(tempDataDir(t), ['minted']);
  const contractId = `client.${'a_b-'.repeat(30)}`;
  assert.equal(store.clientIdFor(GEEBSPACE_SHELL, contractId), contractId);
});

test('the legacy import replaces ids minted since, keeps the rest, and is recorded once done', (t) => {
  const dataDir = tempDataDir(t);
  const { store } = open(dataDir);
  store.clientIdFor(GEEBSPACE_SHELL, '3e0a6136-c0dd-4bc9-a247-38995b8ece3b');
  store.clientIdFor(OTHER_SHELL, 'kept-id');
  assert.equal(store.isLegacyImportDone(), false);

  store.completeLegacyImport(new Map([
    [GEEBSPACE_SHELL, '6a8bc152-a4af-4c08-a240-7d80ba93757e'],
    ['chat-jpj97vyj.agent-9a24f990fb7e46f281afe66088144c60.localhost', 'not-a-shell'],
  ]));

  const relaunched = open(dataDir).store;
  assert.equal(relaunched.isLegacyImportDone(), true);
  assert.equal(relaunched.clientIdFor(GEEBSPACE_SHELL, '3e0a6136-c0dd-4bc9-a247-38995b8ece3b'), '6a8bc152-a4af-4c08-a240-7d80ba93757e');
  assert.equal(relaunched.clientIdFor(OTHER_SHELL, null), 'kept-id');
});

test('an unreadable store is reported and starts empty, so each shell keeps the id its frame has', (t) => {
  const dataDir = tempDataDir(t);
  fs.writeFileSync(path.join(dataDir, 'shell-client-ids.json'), '{ not json');
  const { store, warnings } = open(dataDir);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /ignoring the stored ids: unreadable/);
  assert.equal(store.clientIdFor(GEEBSPACE_SHELL, 'frame-id'), 'frame-id');
  assert.equal(open(dataDir).store.clientIdFor(GEEBSPACE_SHELL, null), 'frame-id');
});

test('only an https shell origin has a shell hostname, on the forward or a share domain', () => {
  assert.equal(shellHostnameOf(`https://${GEEBSPACE_SHELL}:8421/`), GEEBSPACE_SHELL);
  assert.equal(shellHostnameOf(`https://${GEEBSPACE_SHELL}:51234/desktop?x=1`), GEEBSPACE_SHELL);
  assert.equal(
    shellHostnameOf('https://System-Interface-QZV9E2AP.355c2ee547cea3f4.bd3f2f7dc17587a6.us1.personal-imbue.com/'),
    'system-interface-qzv9e2ap.355c2ee547cea3f4.bd3f2f7dc17587a6.us1.personal-imbue.com',
  );
  assert.equal(shellHostnameOf('https://chat-jpj97vyj.agent-9a24f990fb7e46f281afe66088144c60.localhost:8421/'), null);
  assert.equal(shellHostnameOf(`http://${GEEBSPACE_SHELL}:8421/`), null);
  assert.equal(shellHostnameOf('https://system-interface-29livw2l/'), null);
  assert.equal(shellHostnameOf('http://localhost:57122/workspace/agent-9a24'), null);
  assert.equal(shellHostnameOf('not a url'), null);
});

test('the legacy origins are each workspace\'s shell label under its agent coordinate, from the forward\'s label map', () => {
  const serviceMap = JSON.stringify({
    format_version: 2,
    services_by_instance: {},
    label_to_name_by_instance: {
      'agent-9a24f990fb7e46f281afe66088144c60@host-bcf8c1b2824a4126a966d793db48ca05': {
        'system_interface-7ey91715': 'system_interface',
        'chat-jpj97vyj': 'chat',
      },
      'agent-4acb3a0042814e759cc6ccaed2d332e2@host-6a32ae13ad9f406a92dfe958ca8ea859': {
        'system-interface-29livw2l': 'system_interface',
      },
      'agent-0011': { 'system-interface-aaaaaaaa': 'system_interface' },
    },
  });
  assert.deepEqual(legacyShellOrigins(serviceMap, 8421).sort(), [
    'https://system-interface-29livw2l.agent-4acb3a0042814e759cc6ccaed2d332e2.localhost:8421',
    'https://system_interface-7ey91715.agent-9a24f990fb7e46f281afe66088144c60.localhost:8421',
  ]);
  // A forward that fell back off its default port this launch: the default port's origin is still read, and first.
  const onFallbackPort = legacyShellOrigins(serviceMap, 51234).filter((origin) => origin.includes('29livw2l'));
  assert.deepEqual(onFallbackPort, [
    'https://system-interface-29livw2l.agent-4acb3a0042814e759cc6ccaed2d332e2.localhost:8421',
    'https://system-interface-29livw2l.agent-4acb3a0042814e759cc6ccaed2d332e2.localhost:51234',
  ]);
});

test('a service map the import cannot read fails it rather than finding no origins', () => {
  assert.throws(() => legacyShellOrigins(JSON.stringify({ format_version: 3, label_to_name_by_instance: {} }), 8421), /unknown format version 3/);
  assert.throws(() => legacyShellOrigins(JSON.stringify({ format_version: 2 }), 8421), /no label map/);
  assert.throws(() => legacyShellOrigins('{ not json', 8421), SyntaxError);
});

test('only a shell frame the trusted page frames directly asks as a shell', () => {
  const mainFrame = { frameTreeNodeId: 1, url: 'http://localhost:57122/workspace/agent-9a24' };
  const untrustedPage = { frameTreeNodeId: 1, url: 'https://evil.example.com/' };
  const isTrustedPage = (page) => page.url.startsWith('http://localhost:57122/');
  const shellFrame = { url: `https://${GEEBSPACE_SHELL}:8421/`, parent: mainFrame };
  assert.equal(askingShellHostname(shellFrame, mainFrame, isTrustedPage), GEEBSPACE_SHELL);
  assert.equal(askingShellHostname({ url: shellFrame.url, parent: shellFrame }, mainFrame, isTrustedPage), null);
  assert.equal(askingShellHostname({ url: shellFrame.url, parent: untrustedPage }, untrustedPage, isTrustedPage), null);
  assert.equal(askingShellHostname({ url: 'https://chat-jpj97vyj.agent-9a24f990fb7e46f281afe66088144c60.localhost:8421/', parent: mainFrame }, mainFrame, isTrustedPage), null);
  assert.equal(askingShellHostname({ url: shellFrame.url, parent: null }, mainFrame, isTrustedPage), null);
  assert.equal(askingShellHostname(null, mainFrame, isTrustedPage), null);
});
