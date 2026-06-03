Migrated the agent-creation form (`/create`) to Solid.js. The page is
now rendered server-side via the SSR sidecar (with the inline-shell
fallback when the sidecar is unhealthy) and the form submission posts
JSON and reads `{ ok, errors, data }` instead of round-tripping a
re-rendered HTML page on validation errors.

Reusable form primitives landed under
`apps/minds/frontend/src/components/forms/`: `FormField`,
`FormSelect`, and `FormSection`. The Solid route composes them so the
create form's "label + select + helper-error" rows now read as a list
of `FormSelect` calls instead of five hand-spelled `<div flex>` blocks.

The Jinja `templates/create.html` file is still present (a Phase 8
cleanup pass removes the dead Jinja code wholesale); it is no longer
referenced from Python and `render_create_form` is a thin shim around
the SSR sidecar.
