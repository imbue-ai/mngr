Feature: Addressing invariants
  These properties hold for every resolution of a target and a path, whichever subcommand is running.

  @same-resolution-everywhere
  Rule: Resolution does not depend on which subcommand is running
    One target and one path name the same absolute file on the same machine whether they are being read, written, or listed.
    A refusal arising from resolution is likewise the same refusal for every subcommand.
    Rationale: a user who has located a file with one subcommand can hand the identical target and path to another and be certain it means the same file.

  @never-changes-lifecycle
  Rule: Addressing a machine never changes whether it is running
    A command addressing a stopped host leaves it stopped, and reports what it can or cannot do in that state rather than altering it.
    Rationale: reaching a file is an operation users expect to be cheap and invisible; starting a host to serve one would be neither, and would silently spend the resources of a machine the user had deliberately stopped.
