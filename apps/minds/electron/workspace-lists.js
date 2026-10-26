'use strict';

// The workspace rows the windows relay to main, one list per account. Every
// window's ``workspaces`` frame is the projection for its own account, so one
// frame replaces that account's rows and never the install's: main reads the
// union for window titles and a workspace's owning account, and the
// per-account difference for which workspaces vanished. Kept free of any
// `electron` import so it runs under plain node; the lists themselves are a
// Map (account id -> rows) that main.js holds.

/** The row main keeps for one entry of a relayed ``workspaces`` frame. */
function workspaceRowFromFrameEntry(entry) {
  return {
    id: String(entry.id),
    name: entry.name ? String(entry.name) : '',
    account: entry.account ? String(entry.account) : '',
    accountUserId: entry.account_user_id ? String(entry.account_user_id) : '',
  };
}

/**
 * The lists with `accountId`'s rows replaced by `rows`, and which of its
 * workspaces went away: the ids the account listed before and lists no longer.
 * Another account's rows are not consulted, so a workspace listed only under
 * another account never reads as vanished from this one.
 *
 * @param {Map<string, object[]>} rowsByAccount  Each account's rows, as their last frames said.
 * @param {string} accountId  The account whose frame arrived.
 * @param {object[]} rows  That frame's rows.
 * @returns {{ rowsByAccount: Map<string, object[]>, vanishedIds: string[] }}
 */
function withAccountRows(rowsByAccount, accountId, rows) {
  const newIds = new Set(rows.map((row) => row.id));
  const vanishedIds = (rowsByAccount.get(accountId) || [])
    .map((row) => row.id)
    .filter((id) => !newIds.has(id));
  const updated = new Map(rowsByAccount);
  updated.set(accountId, rows);
  return { rowsByAccount: updated, vanishedIds };
}

/** Every account's rows, as their last frames said. */
function allWorkspaceRows(rowsByAccount) {
  return [...rowsByAccount.values()].flat();
}

/**
 * The row for `workspaceId` under any account, or null for a workspace no list
 * knows. `isSameId` compares a row's id with the asked one (main's reconciles
 * a workspace's host and agent coordinates).
 */
function findWorkspaceRow(rowsByAccount, workspaceId, isSameId) {
  if (!workspaceId) return null;
  return allWorkspaceRows(rowsByAccount).find((row) => isSameId(row.id, workspaceId)) || null;
}

module.exports = {
  allWorkspaceRows,
  findWorkspaceRow,
  withAccountRows,
  workspaceRowFromFrameEntry,
};
