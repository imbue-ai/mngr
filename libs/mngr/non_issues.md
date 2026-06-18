The following are things that you often think are issues, but in fact are not:

- default arguments in api/*.py top level command functions (e.g., list.py::list()). These are fine as they are the main entrypoints and having defaults makes sense for usability.
- missing "is_" prefix for boolean options in CLI command functions and CLI-options data classes (e.g. ListCliOptions). These mirror user-facing CLI args, which the style guide explicitly exempts from the is_ convention. (Internal boolean fields on non-CLI data classes should still use is_.)
- missing Field and description for CLI-options data classes (e.g. ListCliOptions). These classes intentionally mirror the click options, with the descriptions/defaults living on the click.option() decorators instead (and we don't want to duplicate them), as documented on the classes themselves.
- 
