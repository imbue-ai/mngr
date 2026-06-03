import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render } from '@solidjs/testing-library';
import { Titlebar } from './Titlebar.jsx';

describe('Titlebar', () => {
  it('renders the basic toolbar buttons and page title', () => {
    const { getByTitle, getByText } = render(() => (
      <Titlebar pageTitle="My Workspace" />
    ));
    expect(getByTitle('Projects')).toBeInTheDocument();
    expect(getByTitle('Home')).toBeInTheDocument();
    expect(getByTitle('Back')).toBeInTheDocument();
    expect(getByTitle('Forward')).toBeInTheDocument();
    expect(getByTitle('Requests')).toBeInTheDocument();
    expect(getByText('My Workspace')).toBeInTheDocument();
  });

  it('hides the Windows-style window controls in macOS mode', () => {
    const { queryByTitle } = render(() => <Titlebar isMac />);
    expect(queryByTitle('Minimize')).toBeNull();
    expect(queryByTitle('Maximize')).toBeNull();
    expect(queryByTitle('Close')).toBeNull();
  });

  it('shows the Windows-style window controls on non-macOS', () => {
    const { getByTitle } = render(() => <Titlebar isMac={false} />);
    expect(getByTitle('Minimize')).toBeInTheDocument();
    expect(getByTitle('Maximize')).toBeInTheDocument();
    expect(getByTitle('Close')).toBeInTheDocument();
  });

  it('renders the user-button label based on isAuthenticated', () => {
    const { getByText, unmount } = render(() => <Titlebar isAuthenticated={false} />);
    expect(getByText('Log in')).toBeInTheDocument();
    unmount();
    const { getByText: getByText2 } = render(() => <Titlebar isAuthenticated />);
    expect(getByText2('Manage account(s)')).toBeInTheDocument();
  });

  it('shows the requests badge only when requestCount is positive', () => {
    const { container, unmount } = render(() => <Titlebar requestCount={0} />);
    expect(container.querySelector('#requests-badge')).toBeNull();
    unmount();
    const { container: c2 } = render(() => <Titlebar requestCount={3} />);
    expect(c2.querySelector('#requests-badge')).not.toBeNull();
  });

  it('fires the user-click callback when the user button is pressed', () => {
    const handler = vi.fn();
    const { getByText } = render(() => (
      <Titlebar onUserClick={handler} isAuthenticated={false} />
    ));
    fireEvent.click(getByText('Log in'));
    expect(handler).toHaveBeenCalledTimes(1);
  });
});
