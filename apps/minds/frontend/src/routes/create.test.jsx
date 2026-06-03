import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor } from '@solidjs/testing-library';
import { CreateRoute } from './create.jsx';

// Standard prop fixture mirroring what the Python `render_create_form`
// shim now passes through to the Solid component. Tests override
// specific keys as needed.
const BASE_PROPS = {
  git_url: 'https://github.com/imbue-ai/forever-claude-template.git',
  host_name: 'assistant',
  branch: '',
  selected_launch_mode: 'LIMA',
  selected_ai_provider: 'SUBSCRIPTION',
  selected_backup_provider: 'CONFIGURE_LATER',
  selected_backup_encryption_method: 'NO_PASSWORD',
  backup_api_key_env: '',
  has_saved_backup_password: false,
  accounts: [],
  default_account_id: '',
  anthropic_api_key: '',
  error_message: '',
  launch_modes: ['DOCKER', 'CLOUD', 'LIMA', 'IMBUE_CLOUD'],
  ai_providers: ['IMBUE_CLOUD', 'API_KEY', 'SUBSCRIPTION'],
  backup_providers: ['IMBUE_CLOUD', 'API_KEY', 'CONFIGURE_LATER'],
  backup_encryption_methods: ['MASTER_PASSWORD', 'NO_PASSWORD'],
};

describe('CreateRoute', () => {
  let originalFetch;
  let originalLocation;
  beforeEach(() => {
    originalFetch = globalThis.fetch;
    originalLocation = window.location;
    // jsdom guards window.location; replace with a writable shim so
    // the route's `window.location.href = ...` redirect is observable.
    delete window.location;
    window.location = { href: '' };
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    window.location = originalLocation;
  });

  it('renders the form with the heading, host_name field, and submit button', () => {
    const { getByRole, getByText } = render(() => <CreateRoute {...BASE_PROPS} />);
    expect(getByText('Create workspace')).toBeInTheDocument();
    expect(getByRole('button', { name: 'Create' })).toBeInTheDocument();
    expect(getByText('No account (private project)')).toBeInTheDocument();
  });

  it('submits the form as JSON and follows the redirect on success', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ redirect_url: '/creating/abc123' }),
    });
    globalThis.fetch = fetchMock;

    const { getByRole } = render(() => <CreateRoute {...BASE_PROPS} />);
    fireEvent.submit(getByRole('button', { name: 'Create' }).closest('form'));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/create');
    expect(init.method).toBe('POST');
    expect(init.headers['Content-Type']).toBe('application/json');
    const sent = JSON.parse(init.body);
    expect(sent.host_name).toBe('assistant');
    expect(sent.git_url).toBe('https://github.com/imbue-ai/forever-claude-template.git');
    expect(sent.launch_mode).toBe('LIMA');
    await waitFor(() => expect(window.location.href).toBe('/creating/abc123'));
  });

  it('renders the server error message and does not redirect on a validation failure', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      text: async () => JSON.stringify({ errors: { _: 'Repository URL is required.' } }),
    });
    globalThis.fetch = fetchMock;

    const { getByRole, findByText } = render(() => <CreateRoute {...BASE_PROPS} />);
    fireEvent.submit(getByRole('button', { name: 'Create' }).closest('form'));

    expect(await findByText('Repository URL is required.')).toBeInTheDocument();
    expect(window.location.href).toBe('');
  });

  it('shows the initial error_message banner without a network round-trip', () => {
    const { getByText } = render(() => (
      <CreateRoute {...BASE_PROPS} error_message="imbue_cloud requires an account." />
    ));
    expect(getByText('imbue_cloud requires an account.')).toBeInTheDocument();
  });

  it('disables the submit button when an IMBUE_CLOUD option is picked without an account', async () => {
    const { getByLabelText, getByRole, findByText, container } = render(() => (
      <CreateRoute
        {...BASE_PROPS}
        selected_launch_mode="IMBUE_CLOUD"
      />
    ));
    // Open the configure panel so the inline error becomes part of
    // the DOM and the user-visible state is realistic.
    fireEvent.click(getByRole('button', { name: 'Configure...' }));
    expect(await findByText('imbue_cloud requires a selected account.')).toBeInTheDocument();
    const submit = getByRole('button', { name: 'Create' });
    expect(submit.disabled).toBe(true);
    // Sanity: the launch_mode select still exists and is set to IMBUE_CLOUD.
    expect(getByLabelText('Compute provider').value).toBe('IMBUE_CLOUD');
    // The container check makes sure rendering happened (silences
    // unused-binding lint -- the destructure is what we mean).
    expect(container).not.toBeNull();
  });
});
