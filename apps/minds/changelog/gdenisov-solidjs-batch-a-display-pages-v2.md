# Batch A: Solid migration of the four display-only pages

Continues the Solid.js UI migration. Routes ``/``, ``/accounts``,
``/agents/<id>/recovery``, and ``/destroying/<id>`` are now backed by
Solid components under ``apps/minds/frontend/src/routes/``. The Python
``render_*`` shims build a JSON-serializable props dict and call the
shared ``render_ssr_or_fallback`` helper, falling back to the client
shell when the SSR sidecar is unhealthy. Behavior is unchanged from the
user's perspective; the migration is structural.

New JSX components introduced for this batch:

- ``components/ui/Badge.jsx`` -- status pill with
  ``success``/``warn``/``error``/``info``/``neutral`` variants.
- ``components/ui/AccentStripe.jsx`` -- per-workspace OKLCH accent
  wrapper. Renders any tag (``component`` prop) with the legacy
  ``.accent-spine`` class and the ``--workspace-accent`` CSS variable.
- ``components/ui/IconButton.jsx`` -- ghost-styled icon-only button
  with built-in ``settings``/``restart`` SVGs.
- ``components/ui/Dialog.jsx`` -- generalized modal (click-outside +
  Escape to close, body-scroll lock).
- ``components/cards/WorkspaceRow.jsx`` -- one component for the
  three landing-row variants (running / destroying / destroy_failed).
- ``components/cards/WorkspaceCardEmpty.jsx`` -- empty / discovering
  variants for the landing page.

The legacy ``templates/landing.html``, ``templates/accounts.html``,
``templates/destroying.html``, and ``static/destroying.js`` files are
intentionally left in place for the Phase 8 cleanup pass. The recovery
page's hand-rolled ``_RECOVERY_STYLE`` and ``_RECOVERY_SCRIPT`` constants
in ``templates.py`` had ``render_recovery_page`` as their only reader and
were removed alongside the migration.

Tests:

- 18 new component tests under ``frontend/src/components/`` and 15
  new route tests under ``frontend/src/routes/`` (Vitest).
- 6 new Python contract tests in
  ``apps/minds/imbue/minds/desktop_client/ssr_routes_test.py``.
- The corresponding Jinja-asserting tests in ``templates_test.py``
  and ``test_desktop_client.py`` were deleted or updated to assert on
  the SSR payload via ``extract_ssr_route_payload``, per the per-page
  parity gate in the migration plan.
