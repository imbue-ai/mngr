The create-a-mind form now validates the advanced "Name" field live as you type, so a bad or already-used name is caught immediately instead of failing partway through creation.

As you type, the field shows a specific message for any format problem (for example, dots aren't allowed, or a name can't start or end with a dash or underscore).

It also checks availability: if a name is already taken by an active mind on the selected compute provider (and account, for Imbue Cloud), the field says so right away. Names freed by destroyed minds are treated as available, and the check is case-insensitive. Pressing Create with a known-invalid or taken name surfaces the inline error instead of starting creation; once you change the name (or switch provider/account/region) a previous "taken" result no longer blocks Create for the new name.

Auto-generated workspace names now use the ``workspace-N`` pattern (e.g. ``workspace-1``) instead of ``mind-N``.

The landing page now says "Workspaces" instead of "Projects" (heading, window title, and empty-state text).
