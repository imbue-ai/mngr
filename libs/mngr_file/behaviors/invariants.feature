Feature: mngr file invariants
  These properties hold for every subcommand, every target, and every state the addressed machine can be in -- including flows no scenario in this corpus describes.

  @clean-refusals
  Rule: A command that cannot do what was asked states why, and never crashes
    Whenever a command does not carry out the requested operation, it reports the reason as a stated error and exits nonzero.
    No condition arising from the target, the path, or the state of the addressed machine surfaces as an uncaught internal failure or a stack trace, and no such condition is reported through the mechanism used to reach the machine rather than in the plugin's own terms.
    A refusal may carry whatever guidance helps the user act on it; what it may not do is crash, or speak in the terms of the mechanism that reached the machine.
    Rationale: which machine is addressed, whether it is running, and how it is reached all vary independently of the user's request, so a user whose request cannot be served must get the same clear answer in every combination.
    The conditions that constitute a refusal differ by subcommand and are stated where each subcommand is specified; this Rule governs the form every refusal takes, not which conditions provoke one.

  @resolved-path-reported
  Rule: A report names the absolute path it concerns, not the path as typed
    A command acting on one file reports that file's absolute path, and a listing reports an absolute path for every entry it yields.
    A refusal likewise names the absolute path it could not use.
    Rationale: the base directory is chosen by the target and by the requested base, so the resolved path is the only unambiguous record of which file was touched, and the only way a user can confirm that a relative path meant what they intended.

  @whole-files-only
  Rule: A write replaces a whole file and a read changes nothing
    The unit of transfer is an entire file: a write supplies the file's complete new content, and no operation appends to, patches, or otherwise edits part of a file in place.
    Reading a file and listing a directory leave the addressed machine's contents exactly as they were.

  @named-output-formats
  Rule: Every command reports in one of the named output formats
    Each subcommand renders its outcome either for a human reader or as one machine-readable record per event, and the user chooses which by name.
    A subcommand that describes its outcome with records additionally accepts a caller-supplied template naming the fields to render, and emits one line per record, however many records the outcome has.
    A subcommand whose outcome is a file's own bytes does not, because a template there would displace the content the user asked for rather than describe it.
    A field name that more than one subcommand offers renders the same way in each, so that reading a template does not require knowing which subcommand produced it.

    @template-refused-where-the-outcome-is-content
    Example: A template is refused by the subcommand whose outcome is a file's bytes
      When a subcommand whose outcome is a file's own bytes is invoked with an output template in place of a format name
      Then the command refuses as a usage error
      And nothing on the addressed machine is read or written
