Reworked the desktop client's overlay layer into a single always-warm surface. The workspace menu, inbox, help, and sign-in dialogs now share one persistent overlay (hosted as kept-warm iframes that load once and stay mounted) instead of loading a fresh page into the overlay view every time one opens. Their appearance and behavior are unchanged.

This is the foundation for custom hover tooltips on the titlebar buttons (which render above both the chrome and the workspace content); the tooltips themselves land in a follow-up change on this branch.
