import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@solidjs/testing-library';
import { FormSection } from './FormSection.jsx';

describe('FormSection', () => {
  it('hides children until the toggle is clicked', () => {
    const { queryByText, getByRole } = render(() => (
      <FormSection showLabel="Configure..." hideLabel="Hide">
        <p>section body</p>
      </FormSection>
    ));
    expect(queryByText('section body')).toBeNull();
    fireEvent.click(getByRole('button', { name: 'Configure...' }));
    expect(queryByText('section body')).toBeInTheDocument();
  });

  it('updates the toggle label between show and hide states', () => {
    const { getByRole } = render(() => (
      <FormSection showLabel="Configure..." hideLabel="Hide">
        <p>body</p>
      </FormSection>
    ));
    const button = getByRole('button', { name: 'Configure...' });
    fireEvent.click(button);
    expect(getByRole('button', { name: 'Hide' })).toBeInTheDocument();
  });

  it('starts open when initiallyOpen is set', () => {
    const { getByText } = render(() => (
      <FormSection showLabel="Show" hideLabel="Hide" initiallyOpen>
        <p>visible body</p>
      </FormSection>
    ));
    expect(getByText('visible body')).toBeInTheDocument();
  });

  it('renders the summary text in the toggle row', () => {
    const { getByText } = render(() => (
      <FormSection showLabel="Configure..." summary="compute via Lima.">
        <p>body</p>
      </FormSection>
    ));
    expect(getByText('compute via Lima.')).toBeInTheDocument();
  });
});
