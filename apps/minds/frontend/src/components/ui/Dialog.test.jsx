import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@solidjs/testing-library';
import { Dialog } from './Dialog.jsx';

describe('Dialog', () => {
  it('does not render content when closed', () => {
    const { queryByText } = render(() => (
      <Dialog open={false} onClose={() => {}} title="Hidden">
        body
      </Dialog>
    ));
    expect(queryByText('Hidden')).toBeNull();
    expect(queryByText('body')).toBeNull();
  });

  it('renders title and children when open', () => {
    const { getByText, getByRole } = render(() => (
      <Dialog open onClose={() => {}} title="Confirm" labelledBy="dlg-title">
        <p>Are you sure?</p>
      </Dialog>
    ));
    expect(getByText('Confirm')).toBeInTheDocument();
    expect(getByText('Are you sure?')).toBeInTheDocument();
    const dialog = getByRole('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.getAttribute('aria-labelledby')).toBe('dlg-title');
  });

  it('invokes onClose when the backdrop is clicked', () => {
    const onClose = vi.fn();
    const { getByRole } = render(() => (
      <Dialog open onClose={onClose} title="X">
        <p>body</p>
      </Dialog>
    ));
    const backdrop = getByRole('dialog');
    fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not invoke onClose when clicking inside the dialog body', () => {
    const onClose = vi.fn();
    const { getByText } = render(() => (
      <Dialog open onClose={onClose} title="X">
        <p>inside</p>
      </Dialog>
    ));
    fireEvent.click(getByText('inside'));
    expect(onClose).not.toHaveBeenCalled();
  });

  it('invokes onClose when Escape is pressed', () => {
    const onClose = vi.fn();
    render(() => (
      <Dialog open onClose={onClose} title="X">
        <p>body</p>
      </Dialog>
    ));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
