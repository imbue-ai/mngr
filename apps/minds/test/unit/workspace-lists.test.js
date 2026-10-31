'use strict';

// Unit tests for the per-account workspace lists main keeps from the windows'
// relayed ``workspaces`` frames (electron/workspace-lists.js): one frame
// replaces one account's rows, a workspace is found under any account, and
// which workspaces vanished is read against the frame's own account.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  allWorkspaceRows,
  findWorkspaceRow,
  withAccountRows,
  workspaceRowFromFrameEntry,
} = require('../../electron/workspace-lists');

const ALICE = 'user-alice';
const BOB = 'user-bob';

function row(id, accountUserId = '') {
  return { id, name: id, account: accountUserId ? `${accountUserId}@example.com` : '', accountUserId };
}

test('workspaceRowFromFrameEntry keeps the fields main reads and blanks the ones a row lacks', () => {
  assert.deepEqual(
    workspaceRowFromFrameEntry({
      id: 'agent-a1',
      name: 'alices',
      account: 'alice@example.com',
      account_user_id: ALICE,
      accent: '#112233',
    }),
    { id: 'agent-a1', name: 'alices', account: 'alice@example.com', accountUserId: ALICE },
  );
  assert.deepEqual(workspaceRowFromFrameEntry({ id: 'agent-d1' }), {
    id: 'agent-d1',
    name: '',
    account: '',
    accountUserId: '',
  });
});

test("one account's frame replaces that account's rows alone, and the union lists every account's", () => {
  let lists = new Map();
  ({ rowsByAccount: lists } = withAccountRows(lists, ALICE, [row('agent-a1', ALICE)]));
  ({ rowsByAccount: lists } = withAccountRows(lists, BOB, [row('agent-b1', BOB)]));

  const { rowsByAccount: after, vanishedIds } = withAccountRows(lists, ALICE, [row('agent-a2', ALICE)]);

  assert.deepEqual(vanishedIds, ['agent-a1']);
  assert.deepEqual(
    allWorkspaceRows(after).map((r) => r.id),
    ['agent-a2', 'agent-b1'],
  );
  // The lists main held before are left as they were.
  assert.deepEqual(
    allWorkspaceRows(lists).map((r) => r.id),
    ['agent-a1', 'agent-b1'],
  );
});

test('a workspace listed only under another account is not vanished from a frame that lacks it', () => {
  let lists = new Map();
  ({ rowsByAccount: lists } = withAccountRows(lists, BOB, [row('agent-b1', BOB)]));

  // Alice's first frame, then one that still does not list Bob's workspace.
  let result = withAccountRows(lists, ALICE, [row('agent-a1', ALICE)]);
  assert.deepEqual(result.vanishedIds, []);
  result = withAccountRows(result.rowsByAccount, ALICE, []);
  assert.deepEqual(result.vanishedIds, ['agent-a1']);
});

test('findWorkspaceRow finds a workspace under any account through the id comparator', () => {
  const { rowsByAccount: lists } = withAccountRows(
    new Map([[ALICE, [row('agent-a1', ALICE)]]]),
    'local',
    [row('agent-d1')],
  );
  const aliases = new Map([['host-a1', 'agent-a1']]);
  const isSameId = (a, b) => a === b || aliases.get(b) === a;

  assert.equal(findWorkspaceRow(lists, 'agent-d1', isSameId).accountUserId, '');
  assert.equal(findWorkspaceRow(lists, 'host-a1', isSameId).accountUserId, ALICE);
  assert.equal(findWorkspaceRow(lists, 'agent-zz', isSameId), null);
  assert.equal(findWorkspaceRow(lists, '', isSameId), null);
});
