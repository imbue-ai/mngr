The following are things that you often think are issues, but in fact are not:

- default arguments in api/*.py top level command functions (e.g., list.py::list()). These are fine as they are the main entrypoints and having defaults makes sense for usability.
- missing "is_" prefix for boolean options in CLI functions and data classes. The style guide explicitly says that this is ok.
- missing Field and description for CLI options data classes. The style guide explicitly says that this is ok, since the descriptions are on the click decorators instead (and we don't want to duplicated them)
- 
