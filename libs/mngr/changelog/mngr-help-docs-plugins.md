Plugins can now contribute standalone help topic pages via the new `register_help_topics` hook. Topics from an installed plugin show up in `mngr help` and are viewable via `mngr help <topic>`, just like mngr's built-in topics. Each topic is a `TopicHelpPage` with explicit metadata (key, description, aliases, see-also) whose body is either inline (`content`) or a markdown file (`body_path`). Plugin topics that collide with a built-in topic key or alias are skipped so built-in topics always win.

`mngr help <topic>` now renders markdown nicely in an interactive terminal (headings, bold, code, links, and tables) via `rich`, with paragraphs wrapped to the terminal width; the same rendering is applied to command `--help` description and sections. Non-interactive output (pipes, scripts) stays plain. `rich` is imported lazily so it does not affect CLI startup time.

Built-in topic docs are now shipped inside the wheel (`force-include` of the topic doc dirs), fixing a bug where `mngr help <topic>` showed no doc-based topics in a PyPI/wheel install (only the top-level `docs/` tree, which is not packaged, was previously read at runtime).

Tab completion suggests every command and help topic as an argument to `mngr help` (e.g. `mngr help <TAB>` lists `create`, `address`, and any plugin-contributed topics).

Internally, the plugin-facing `TopicHelpPage` model lives in `imbue.mngr.interfaces.help_topic` (so the plugin hookspec can reference it without the plugins layer importing the CLI), while the runtime topic registry lives in `imbue.mngr.cli.help_topics`. mngr's own built-in topics are registered through this same hook (as a built-in plugin) from an explicit registry -- no directory scanning or heading parsing.
