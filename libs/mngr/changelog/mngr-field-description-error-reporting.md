# Corrected `is_error_reporting_enabled` config field description

The description for the `is_error_reporting_enabled` config field was out of
date: it claimed the option controls prompting users to report unexpected
errors as GitHub issues. The option actually controls whether, on an unexpected
error while running interactively, mngr suggests launching a diagnostic agent
via a copy-paste-ready `mngr create` command. The description now matches that
behavior.
