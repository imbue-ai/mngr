import { describe, it, expect } from 'vitest';
import { render } from '@solidjs/testing-library';
import { AccountsRoute } from './accounts.jsx';

const SAMPLE = [
  { user_id: 'u-alpha', email: 'alpha@example.com', workspace_ids: ['w1', 'w2'] },
  { user_id: 'u-beta', email: 'beta@example.com', workspace_ids: [] },
];

describe('AccountsRoute', () => {
  it('renders the empty state when no accounts are logged in', () => {
    const { getByText } = render(() => (
      <AccountsRoute accounts={[]} default_account_id="" enabled_by_user_id={{}} />
    ));
    expect(getByText('No accounts logged in.')).toBeInTheDocument();
  });

  it('lists each account with its workspace count', () => {
    const { getByText } = render(() => (
      <AccountsRoute
        accounts={SAMPLE}
        default_account_id="u-alpha"
        enabled_by_user_id={{ 'u-alpha': true, 'u-beta': true }}
      />
    ));
    expect(getByText('alpha@example.com')).toBeInTheDocument();
    expect(getByText('beta@example.com')).toBeInTheDocument();
    expect(getByText(/2 workspace\(s\)/)).toBeInTheDocument();
    expect(getByText(/0 workspace\(s\)/)).toBeInTheDocument();
  });

  it('marks the default account and renders the static Default pill instead of Set default', () => {
    const { getByText, queryByText } = render(() => (
      <AccountsRoute
        accounts={SAMPLE}
        default_account_id="u-alpha"
        enabled_by_user_id={{ 'u-alpha': true, 'u-beta': true }}
      />
    ));
    expect(getByText('Default')).toBeInTheDocument();
    // The non-default row should show a Set default submit button.
    const setDefault = queryByText('Set default');
    expect(setDefault).not.toBeNull();
    expect(setDefault.tagName.toLowerCase()).toBe('button');
  });

  it('shows the Signed out badge and a Sign in again link when the provider is disabled', () => {
    const { getByText, getByRole } = render(() => (
      <AccountsRoute
        accounts={SAMPLE}
        default_account_id="u-alpha"
        enabled_by_user_id={{ 'u-alpha': false, 'u-beta': true }}
      />
    ));
    expect(getByText('Signed out')).toBeInTheDocument();
    expect(getByRole('link', { name: 'Sign in again' }).getAttribute('href')).toBe('/auth/login');
  });

  it('renders the logout form pointing at /accounts/<user_id>/logout', () => {
    const { container } = render(() => (
      <AccountsRoute
        accounts={SAMPLE}
        default_account_id="u-alpha"
        enabled_by_user_id={{}}
      />
    ));
    const logoutForms = container.querySelectorAll('form[action$="/logout"]');
    expect(logoutForms.length).toBe(2);
    expect(logoutForms[0].getAttribute('action')).toBe('/accounts/u-alpha/logout');
    expect(logoutForms[1].getAttribute('action')).toBe('/accounts/u-beta/logout');
  });
});
