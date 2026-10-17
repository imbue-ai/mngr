Plan selection updates alongside the new "free" plan:

- Every tier's deploy.toml now defines a "free" plan (1 remote workspace, 5 total workspaces, 25 GB backup storage, no Imbue-Cloud LLM budget) next to explorer and ally.

- The desktop Accounts page's plan switcher now shows a short description of the plan being switched to (with a "Learn more" privacy-policy link), and switching to Explorer requires checking an explicit agreement to the Explorer edition's product-data sharing before the Switch plan button activates.

- The Switch plan button is disabled while the selected plan equals the current one, and shows a disabled busy spinner while the switch request is in flight (double-clicks are swallowed).

- The plan payload from the desktop client's UI API now carries the tier's privacy-policy URL (accounts origin, falling back to the connector host).
