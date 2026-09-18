# Listing

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

This area covers reporting what a directory on the addressed machine contains.

A listing addresses a directory rather than a file, and the path is optional: with no path, the base directory the target fixed is itself the directory listed.
Everything about how that base is chosen belongs to `addressing/`.

## An entry and its attributes

A listing yields one entry per member of the directory, and each entry carries a fixed set of attributes: its name, its absolute path, what kind of filesystem object it is, its size, when it was last modified, and its permissions.
The user chooses which of those attributes are displayed; the set that exists is not the set shown by default.
A listing describes its outcome with records, so it accepts a template naming the fields to render, per the corpus-wide `named-output-formats` Rule.
A template reaches the whole attribute set rather than the displayed subset, which keeps the two choices independent of each other.

Two of those attributes are not always knowable.
A size is meaningful only for something that holds content, so a directory reports none.
The kind and the permissions are reported to whatever precision the machine's state allows, which `addressing/stopped-hosts.feature` states for a stopped host.
An attribute that cannot be determined is reported as absent, and absent is distinguishable from zero or empty.

## Empty and missing are different answers

A directory that exists and holds nothing, and a path where no directory exists, are different facts about the machine, and a listing reports them differently.
Conflating them would let a mistyped path masquerade as a true statement about the machine, which is the most costly error a listing can make: it is silent, and it is indistinguishable from success.
The first is a successful listing of nothing; the second is a refusal, as the corpus-wide `clean-refusals` Rule requires of anything a command cannot do.
