Bump the bundled Latchkey version to 2.18.0.

When a mind needs a Google API and its credentials are not valid, Minds now tries the Minds-provided Google OAuth consent screen first (registering its client via `latchkey auth prepare`), and only falls back to the old "create your own Google project" self-setup flow if that fails. Most users no longer see the self-setup step. Non-Google services are unchanged.
