import { For, Show } from 'solid-js';
import { PageContainer } from '../components/ui/PageContainer.jsx';
import { CardRow } from '../components/ui/Card.jsx';
import { ButtonLink } from '../components/ui/ButtonLink.jsx';
import { Button } from '../components/ui/Button.jsx';
import { Badge } from '../components/ui/Badge.jsx';

// Manage accounts page.
//
// Props (from the Python SSR shim):
//   * ``accounts``: [{ user_id, email, workspace_ids: [...] }]
//   * ``default_account_id``: string user_id of the default, or ""
//   * ``enabled_by_user_id``: { [user_id]: bool } -- whether the matching
//     ``[providers.imbue_cloud_<slug>]`` block is currently enabled
//
// The set-default and logout buttons still POST via regular HTML forms
// for now -- batch A is display-only and we don't want to introduce a
// JSON-conversion that crosses into batch B's form-post conversion work.

function AccountRow(props) {
  const isDefault = () => String(props.account.user_id) === String(props.defaultAccountId || '');
  const enabled = () => {
    const value = props.enabledByUserId?.[String(props.account.user_id)];
    return value === undefined ? true : Boolean(value);
  };
  const workspaceCount = () => (props.account.workspace_ids || []).length;
  return (
    <CardRow extra="mt-2">
      <div>
        <div class="font-medium">
          {props.account.email}
          <Show when={!enabled()}>
            {' '}
            <Badge
              variant="warn"
              extra="text-xs ml-2 border border-amber-200"
              title="Session was rejected by the server. Sign in again to re-enable."
            >
              Signed out
            </Badge>
          </Show>
        </div>
        <div class="text-xs text-zinc-400">
          {workspaceCount()} workspace(s)
          <Show when={isDefault()}>
            {' '}
            &middot; Default
          </Show>
        </div>
      </div>
      <div class="flex gap-2">
        <Show when={!enabled()}>
          <ButtonLink href="/auth/login" variant="primary">
            Sign in again
          </ButtonLink>
        </Show>
        <Show
          when={!isDefault()}
          fallback={
            <span class="inline-flex items-center justify-center px-3.5 py-2 rounded-md font-medium text-sm bg-zinc-100 text-zinc-900 border border-zinc-200 opacity-60 cursor-default">
              Default
            </span>
          }
        >
          <form method="POST" action="/accounts/set-default">
            <input type="hidden" name="user_id" value={props.account.user_id} />
            <Button type="submit" variant="secondary">
              Set default
            </Button>
          </form>
        </Show>
        <form method="POST" action={`/accounts/${props.account.user_id}/logout`}>
          <Button type="submit" variant="danger">
            Log out
          </Button>
        </form>
      </div>
    </CardRow>
  );
}

export function AccountsRoute(props) {
  const accounts = () => props.accounts || [];
  const enabledByUserId = () => props.enabled_by_user_id || {};
  return (
    <PageContainer>
      <h1 class="text-xl font-semibold text-zinc-900 leading-tight mb-5">Manage Accounts</h1>

      <Show
        when={accounts().length > 0}
        fallback={<p class="text-zinc-500">No accounts logged in.</p>}
      >
        <For each={accounts()}>
          {(account) => (
            <AccountRow
              account={account}
              defaultAccountId={props.default_account_id}
              enabledByUserId={enabledByUserId()}
            />
          )}
        </For>
      </Show>

      <div class="mt-4">
        <ButtonLink href="/auth/login" variant="primary">
          Add account
        </ButtonLink>
      </div>
      <div class="mt-4">
        <a href="/" class="text-blue-600 hover:underline">
          &larr; Back to projects
        </a>
      </div>
    </PageContainer>
  );
}
