import { describe, it, expect } from 'vitest';
import { render } from '@solidjs/testing-library';
import { WorkspaceCardEmpty } from './WorkspaceCardEmpty.jsx';

describe('WorkspaceCardEmpty', () => {
  it('renders the empty state with a Create link by default', () => {
    const { getByText, getByRole } = render(() => <WorkspaceCardEmpty />);
    expect(getByText('No projects yet')).toBeInTheDocument();
    const link = getByRole('link', { name: 'Create' });
    expect(link.getAttribute('href')).toBe('/create');
  });

  it('honours a custom createHref', () => {
    const { getByRole } = render(() => <WorkspaceCardEmpty createHref="/create?from=x" />);
    expect(getByRole('link', { name: 'Create' }).getAttribute('href')).toBe('/create?from=x');
  });

  it('renders the discovering variant without the Create link', () => {
    const { getByText, queryByRole } = render(() => (
      <WorkspaceCardEmpty variant="discovering" />
    ));
    expect(getByText('Discovering agents...')).toBeInTheDocument();
    expect(queryByRole('link', { name: 'Create' })).toBeNull();
  });
});
