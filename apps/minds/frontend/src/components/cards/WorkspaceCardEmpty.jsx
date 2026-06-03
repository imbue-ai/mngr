import { Show, splitProps, mergeProps } from 'solid-js';
import { ButtonLink } from '../ui/ButtonLink.jsx';

// Empty / waiting card for the landing page.
//
// Two visually distinct states (controlled by ``variant``):
//   * ``empty`` (default)  -- "No projects yet" + a "Create" call-to-action
//   * ``discovering``      -- "Discovering agents..." (no button)
//
// Mirrors the two else-branches of templates/landing.html.

export function WorkspaceCardEmpty(rawProps) {
  const props = mergeProps({ variant: 'empty' }, rawProps);
  const [local] = splitProps(props, ['variant', 'createHref']);
  const createHref = () => local.createHref || '/create';
  return (
    <Show
      when={local.variant === 'discovering'}
      fallback={
        <div class="text-center py-12">
          <p class="text-zinc-400 mb-6">No projects yet</p>
          <ButtonLink href={createHref()} variant="primary">
            Create
          </ButtonLink>
        </div>
      }
    >
      <div class="flex items-center justify-center min-h-[80vh]">
        <p class="text-zinc-400 text-center">Discovering agents...</p>
      </div>
    </Show>
  );
}
