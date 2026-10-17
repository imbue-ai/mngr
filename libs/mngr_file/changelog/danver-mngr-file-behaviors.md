Added a behavior corpus for `mngr file` at `libs/mngr_file/behaviors/`, specifying the plugin's
externally observable behavior as 48 units across three areas.

- `addressing/` covers turning a target and a path into one absolute path on one machine: which
  base directory each kind of target fixes, how an absolute path bypasses that choice, and what a
  host that is not running changes about all of it.

- `transfer/` covers moving one file's bytes in either direction, including where the local end of
  a transfer sits and what each direction reports.

- `listing/` covers reporting a directory's contents: which attributes an entry carries, which are
  displayed, and why a directory holding nothing and a path holding no directory are different
  answers.

Four corpus-wide invariants bind every unit: refusals are stated errors rather than internal
failures, reports name resolved absolute paths, a write replaces a whole file while a read changes
nothing, and the output formats are a fixed set with no caller-supplied template.

The corpus states the intended behavior, not the current behavior. One known divergence is
recorded in `listing/listing.md`: listing a path where no directory exists is not yet refused
cleanly when the host is stopped and its persisted storage reports a missing path as a
storage-service error rather than an ordinary filesystem error. That note is temporary and is to be
deleted once the code is corrected; the specification itself is not provisional.

Three units were revised after review of the code the corpus specifies. `named-output-formats`
(formerly `fixed-output-formats`) now states that a subcommand whose outcome is a sequence of
records accepts a caller-supplied template, and that one whose outcome is a file's bytes does not;
the corpus previously said no subcommand accepted a template at all. `listing/` gained two
scenarios for template rendering, and `transfer.path-is-a-directory` now requires the refusal to
point at the means of transferring a directory rather than only at the means of listing it, plus a
scenario requiring that refusal to read the same however the machine is reached.

`named-output-formats` was revised again when `mngr file put` gained template support: the Rule
now turns on whether a subcommand describes its outcome with records at all, rather than on whether
there is a sequence of them, and requires a field name shared by more than one subcommand to render
the same way in each. `transfer/` gained a scenario for a write reported through a template.

No production code changed.

Two units were revised after the first witness run exposed problems in their wording.
`clean-refusals` was named "says so in one line", which a witness agent read literally and obeyed by
stripping the worked examples out of `mngr file put`'s no-input refusal. The intent was to outlaw
stack traces, not to cap a refusal's length, so the Rule is renamed and now says explicitly that a
refusal may carry whatever guidance helps the user act on it. `addressing.absolute-path-wins` named
`/etc/hostname`, a file macOS does not have, so its witness could only run on Linux; the scenario now
describes the property that matters -- a file outside every base directory the target admits -- and
leaves the choice of path to whoever witnesses it. Both keep their identity tags, so no coordinate
moves and no witness link breaks.
