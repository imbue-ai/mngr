import { For, Show, createMemo, createSignal } from 'solid-js';
import { createStore } from 'solid-js/store';
import { Button } from '../components/ui/Button.jsx';
import { Notice } from '../components/ui/Notice.jsx';
import { FormField } from '../components/forms/FormField.jsx';
import { FormSection } from '../components/forms/FormSection.jsx';
import { FormSelect } from '../components/forms/FormSelect.jsx';
import { api } from '../lib/api.js';

// Default contents of the restic.env textarea when the user picks the
// "manual" (API_KEY) backup provider. Mirrors the
// `default_restic_env` value in templates/create.html.
const DEFAULT_RESTIC_ENV =
  '# see docs linked above for available options\n' +
  '# for example:\n' +
  'RESTIC_REPOSITORY=s3:s3.amazonaws.com/<bucket_name>\n' +
  'AWS_ACCESS_KEY_ID=\n' +
  'AWS_SECRET_ACCESS_KEY=\n';

// Human-readable labels for the generated config summary line.
const COMPUTE_LABELS = {
  IMBUE_CLOUD: 'Imbue Cloud',
  DOCKER: 'Docker',
  LIMA: 'Lima',
  CLOUD: 'a cloud VM',
};
const AI_LABELS = {
  IMBUE_CLOUD: 'Imbue Cloud',
  API_KEY: 'your Anthropic API key',
  SUBSCRIPTION: 'your Claude subscription',
};
const BACKUP_LABELS = {
  IMBUE_CLOUD: 'Imbue Cloud',
  API_KEY: 'your own storage',
  CONFIGURE_LATER: 'off for now',
};

// Display labels for the backup-provider options: "manual" reads
// better than "api_key" for this particular field, matching the
// Jinja template's lowercased-with-an-override behavior.
const BACKUP_DISPLAY = { API_KEY: 'manual' };

function joinNames(names) {
  if (names.length === 1) return names[0];
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

function capitalize(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function buildSummary({ launchMode, aiProvider, backupProvider }) {
  const facets = [
    { name: 'compute', label: COMPUTE_LABELS[launchMode] || launchMode },
    { name: 'tokens', label: AI_LABELS[aiProvider] || aiProvider },
  ];
  if (backupProvider !== 'CONFIGURE_LATER') {
    facets.push({ name: 'backups', label: BACKUP_LABELS[backupProvider] || backupProvider });
  }
  const labelOrder = [];
  const namesByLabel = {};
  for (const facet of facets) {
    if (!namesByLabel[facet.label]) {
      namesByLabel[facet.label] = [];
      labelOrder.push(facet.label);
    }
    namesByLabel[facet.label].push(facet.name);
  }
  const sentences = labelOrder.map((label) => capitalize(`${joinNames(namesByLabel[label])} via ${label}`));
  return `${sentences.join('. ')}.`;
}

// Defaults applied when the account selection changes. Mirrors the
// `onAccountChange` block in templates/create.html.
function defaultsForAccount(hasAccount) {
  if (hasAccount) {
    return {
      launch_mode: 'IMBUE_CLOUD',
      ai_provider: 'IMBUE_CLOUD',
      backup_provider: 'IMBUE_CLOUD',
    };
  }
  return {
    launch_mode: 'LIMA',
    ai_provider: 'SUBSCRIPTION',
    backup_provider: 'CONFIGURE_LATER',
  };
}

// Build a select-option list from a list of enum values, lowercasing
// the value for display and applying any per-field display overrides
// (e.g. "API_KEY" -> "manual" for backups).
function optionsFromEnum(values, displayOverrides = {}) {
  return values.map((value) => ({
    value,
    label: displayOverrides[value] ?? value.toLowerCase(),
    requiresAccount: value === 'IMBUE_CLOUD',
  }));
}

export function CreateRoute(props) {
  const accounts = props.accounts ?? [];

  // The form values are tracked in a single store so individual
  // field updates don't trigger whole-component re-renders.
  const [form, setForm] = createStore({
    git_url: props.git_url ?? '',
    host_name: props.host_name ?? '',
    branch: props.branch ?? '',
    account_id: props.default_account_id ?? '',
    launch_mode: props.selected_launch_mode ?? 'LIMA',
    ai_provider: props.selected_ai_provider ?? 'SUBSCRIPTION',
    backup_provider: props.selected_backup_provider ?? 'CONFIGURE_LATER',
    backup_encryption_method: props.selected_backup_encryption_method ?? 'NO_PASSWORD',
    backup_api_key_env: props.backup_api_key_env || DEFAULT_RESTIC_ENV,
    anthropic_api_key: props.anthropic_api_key ?? '',
    backup_master_password: '',
    backup_save_password: false,
  });

  // Top-level submission error from the server (re-rendered banner).
  // Field-keyed errors live in `errors` when the server adds them in
  // a future iteration; for now the validation matches today's API:
  // a single message under the form heading.
  const [submitError, setSubmitError] = createSignal(props.error_message ?? '');
  const [isSubmitting, setIsSubmitting] = createSignal(false);

  const hasAccount = createMemo(() => form.account_id !== '');

  // Account-required validation. Mirrors the `launchInvalid` /
  // `aiInvalid` / `backupInvalid` block in templates/create.html.
  const launchInvalid = createMemo(() => !hasAccount() && form.launch_mode === 'IMBUE_CLOUD');
  const aiInvalid = createMemo(() => !hasAccount() && form.ai_provider === 'IMBUE_CLOUD');
  const backupInvalid = createMemo(() => !hasAccount() && form.backup_provider === 'IMBUE_CLOUD');
  const hasValidationError = createMemo(() => launchInvalid() || aiInvalid() || backupInvalid());

  const disabledValues = createMemo(() => (hasAccount() ? new Set() : new Set(['IMBUE_CLOUD'])));

  const summaryText = createMemo(() =>
    buildSummary({
      launchMode: form.launch_mode,
      aiProvider: form.ai_provider,
      backupProvider: form.backup_provider,
    }),
  );

  const launchModeOptions = createMemo(() => optionsFromEnum(props.launch_modes ?? []));
  const aiProviderOptions = createMemo(() => optionsFromEnum(props.ai_providers ?? []));
  const backupProviderOptions = createMemo(() => optionsFromEnum(props.backup_providers ?? [], BACKUP_DISPLAY));
  const backupEncryptionOptions = createMemo(() => optionsFromEnum(props.backup_encryption_methods ?? []));

  const handleAccountChange = (event) => {
    const next = event.currentTarget.value;
    setForm({ account_id: next, ...defaultsForAccount(next !== '') });
  };

  const isApiKeyAi = () => form.ai_provider === 'API_KEY';
  const isBackupConfigured = () => form.backup_provider === 'IMBUE_CLOUD' || form.backup_provider === 'API_KEY';
  const isBackupApiKey = () => form.backup_provider === 'API_KEY';
  const isMasterPassword = () => isBackupConfigured() && form.backup_encryption_method === 'MASTER_PASSWORD';

  // Serialize the form into the JSON payload accepted by the new
  // `/create` JSON endpoint. Mirrors the field set the Jinja form
  // posted as form-encoded data so the Python handler stays the
  // same shape modulo content-type.
  function serializeForm() {
    return {
      git_url: form.git_url.trim(),
      host_name: form.host_name.trim(),
      branch: form.branch.trim(),
      launch_mode: form.launch_mode,
      ai_provider: form.ai_provider,
      account_id: form.account_id,
      anthropic_api_key: form.anthropic_api_key.trim(),
      backup_provider: form.backup_provider,
      backup_encryption_method: form.backup_encryption_method,
      backup_api_key_env: form.backup_api_key_env,
      backup_master_password: form.backup_master_password,
      backup_save_password: form.backup_save_password,
    };
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (hasValidationError() || isSubmitting()) return;
    setIsSubmitting(true);
    setSubmitError('');
    const result = await api.postJson('/create', serializeForm());
    if (result.ok && result.data && result.data.redirect_url) {
      window.location.href = result.data.redirect_url;
      return;
    }
    setIsSubmitting(false);
    const errors = result.errors || {};
    const message = errors._ || Object.values(errors)[0] || 'Could not create workspace.';
    setSubmitError(String(message));
  }

  return (
    <main class="w-full max-w-[520px] bg-white border border-zinc-200 rounded-xl shadow-sm p-6 mx-auto my-6">
      <form id="create-form" onSubmit={handleSubmit}>
        <div class="flex items-center justify-between gap-4 mb-5">
          <h1 class="text-sm font-medium text-zinc-500">Create workspace</h1>
          <select
            id="account_id"
            name="account_id"
            value={form.account_id}
            onChange={handleAccountChange}
            class="max-w-[220px] text-xs text-right text-zinc-400 bg-transparent border-0 rounded px-1 py-1 outline-none cursor-pointer transition-colors hover:text-zinc-600 focus:outline-none focus:ring-0"
          >
            <For each={accounts}>
              {(account) => <option value={account.user_id}>{account.email}</option>}
            </For>
            <option value="">No account (private project)</option>
          </select>
        </div>

        <Show when={submitError()}>
          <Notice variant="error">{submitError()}</Notice>
        </Show>

        <div class="flex gap-2 items-stretch">
          <div class="flex-1">
            <FormField
              name="host_name"
              value={form.host_name}
              onInput={(event) => setForm('host_name', event.currentTarget.value)}
              placeholder="assistant"
              required
            />
          </div>
          <Button
            type="submit"
            variant="primary"
            id="create-submit"
            disabled={hasValidationError() || isSubmitting()}
          >
            {isSubmitting() ? 'Creating...' : 'Create'}
          </Button>
        </div>

        <FormSection
          summary={summaryText()}
          showLabel="Configure..."
          hideLabel="Hide"
        >
          <FormSelect
            label="Compute provider"
            name="launch_mode"
            value={form.launch_mode}
            onChange={(value) => setForm('launch_mode', value)}
            options={launchModeOptions()}
            disabledValues={disabledValues()}
            error={launchInvalid() ? 'imbue_cloud requires a selected account.' : ''}
          />

          <div>
            <FormSelect
              label="AI provider"
              name="ai_provider"
              value={form.ai_provider}
              onChange={(value) => setForm('ai_provider', value)}
              options={aiProviderOptions()}
              disabledValues={disabledValues()}
              error={aiInvalid() ? 'imbue_cloud requires a selected account.' : ''}
            />
            <Show when={isApiKeyAi()}>
              <div class="mt-2">
                <FormField
                  label="Anthropic API key"
                  name="anthropic_api_key"
                  type="password"
                  value={form.anthropic_api_key}
                  onInput={(event) => setForm('anthropic_api_key', event.currentTarget.value)}
                  placeholder="sk-ant-..."
                  required
                />
              </div>
            </Show>
          </div>

          <div>
            <FormSelect
              label="Backup provider"
              name="backup_provider"
              value={form.backup_provider}
              onChange={(value) => setForm('backup_provider', value)}
              options={backupProviderOptions()}
              disabledValues={disabledValues()}
              error={backupInvalid() ? 'imbue_cloud requires a selected account.' : ''}
            />

            <Show when={isBackupApiKey()}>
              <div class="mt-2">
                <FormField
                  label="restic environment"
                  name="backup_api_key_env"
                  rows={6}
                  value={form.backup_api_key_env}
                  onInput={(event) => setForm('backup_api_key_env', event.currentTarget.value)}
                  helper="Written verbatim to restic.env. Don't set RESTIC_PASSWORD -- minds assigns each workspace its own."
                />
              </div>
            </Show>

            <Show when={isBackupConfigured()}>
              <div class="mt-2">
                <FormSelect
                  label="Backup encryption method"
                  name="backup_encryption_method"
                  value={form.backup_encryption_method}
                  onChange={(value) => setForm('backup_encryption_method', value)}
                  options={backupEncryptionOptions()}
                />
              </div>
            </Show>

            <Show when={isMasterPassword()}>
              <div class="mt-2">
                <Show
                  when={props.has_saved_backup_password}
                  fallback={
                    <>
                      <FormField
                        label="Backup master password"
                        name="backup_master_password"
                        type="password"
                        value={form.backup_master_password}
                        onInput={(event) => setForm('backup_master_password', event.currentTarget.value)}
                        placeholder="remember this -- backups are unrecoverable without it"
                        required
                      />
                      <label class="mt-2 flex items-center gap-2 text-xs text-zinc-500">
                        <input
                          type="checkbox"
                          id="backup_save_password"
                          name="backup_save_password"
                          checked={form.backup_save_password}
                          onChange={(event) => setForm('backup_save_password', event.currentTarget.checked)}
                        />
                        Save this password for all my workspaces
                      </label>
                    </>
                  }
                >
                  <p class="text-xs text-zinc-500">
                    A saved backup password will be used for this workspace.
                  </p>
                </Show>
              </div>
            </Show>
          </div>

          <FormSection
            showLabel="Show advanced settings"
            hideLabel="Hide advanced settings"
          >
            <FormField
              label="Repository"
              name="git_url"
              value={form.git_url}
              onInput={(event) => setForm('git_url', event.currentTarget.value)}
              placeholder="https://github.com/user/repo.git"
              helper="Git URL or local path"
              required
            />
            <FormField
              label="Branch"
              name="branch"
              value={form.branch}
              onInput={(event) => setForm('branch', event.currentTarget.value)}
              placeholder="latest tag"
              helper="Leave empty for latest version"
            />
          </FormSection>
        </FormSection>
      </form>
    </main>
  );
}
